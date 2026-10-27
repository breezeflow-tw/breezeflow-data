"""資料狀態與錯誤行情隔離的離線測試；數值皆為合成樣本。"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_etf as fetch
import history_corrections as repair
import market_calendar as mc
import market_status as status


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.cal = mc.Calendar({**mc.announcements(), "2026-10-26": {"is_open": False, "name": "補假"}}, {2026})
        self.items = [{"volume_lots": 1}] * 20

    def build(self, now="2026-10-26T18:00:00+08:00", day="2026-10-22", items=None):
        return status.build(self.items if items is None else items, day, datetime.fromisoformat(now), self.cal)

    def test_closed_day_keeps_last_real_session(self):
        result = self.build()
        self.assertEqual((result["session"], result["expected_trade_date"], result["data_status"]), ("closed", "2026-10-22", "current"))
        self.assertEqual(result["daily_returns_status"], "available")
        self.assertEqual(self.build("2026-10-23T18:00:00+08:00")["reason_code"], "announced_closed")

    def test_before_close_and_delayed_after_close(self):
        self.assertEqual(self.build("2026-10-27T09:30:00+08:00")["data_status"], "current")
        self.assertEqual(self.build("2026-10-27T18:00:00+08:00")["data_status"], "delayed")

    def test_market_invalid_but_individual_zero_valid(self):
        self.assertEqual(self.build(day="2026-10-23")["data_status"], "invalid")
        self.assertEqual(self.build(items=[{"volume_lots": None}] * 20)["daily_returns_status"], "unavailable")
        self.assertEqual(self.build(items=[{"volume_lots": 0}] * 19 + [{"volume_lots": 1}])["data_status"], "current")

    def test_unknown_year_is_not_called_closed(self):
        result = self.build("2027-01-04T18:00:00+08:00")
        self.assertEqual((result["session"], result["data_status"]), ("unknown", "unconfirmed"))

    def test_announced_closed_day_is_never_trade_date(self):
        """臨時休市公告日（2026-10-23）即使交易所或 MIS 出現那天的資料，也不能當成交易日。"""
        now = datetime.fromisoformat("2026-10-26T18:00:00+08:00")
        with self.assertRaises(ValueError):
            fetch.trade_date({}, {"0050": {"date": "2026-10-23"}}, now, self.cal)
        quotes = {"0050": {"date": "2026-10-23"}, "0056": {"date": "2026-10-23"}, "00679B": {"date": "2026-10-22"}}
        self.assertEqual(fetch.trade_date({"0050": {"date": "2026-10-23", "price": 20}}, quotes, now, self.cal), "2026-10-22")


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "test.db"
        self.history = self.root / "history"
        self.source = self.history / "daily/2026-10-23.csv"
        self.source.parent.mkdir(parents=True)
        self.raw = b"code,date,volume_lots,change_pct\n" + b"TEST,2026-10-23,0,0\n" * 360
        self.source.write_bytes(self.raw.replace(b"\n", b"\r\n"))
        checksum = patch.object(repair, "BAD_SHA256", hashlib.sha256(self.raw).hexdigest())
        checksum.start()
        self.addCleanup(checksum.stop)
        self.backup = self.history / "quarantine/daily/2026-10-23.csv"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE daily(date TEXT, volume_lots REAL, change_pct REAL)")
            con.executemany("INSERT INTO daily VALUES ('2026-10-23',0,0)", [()] * 360)
            con.execute("INSERT INTO daily VALUES ('2026-10-22',10,1)")
            con.execute("CREATE TABLE holding_lists(date TEXT)")
            con.execute("INSERT INTO holding_lists VALUES ('2026-10-23')")
        con.close()

    def test_quarantine_preserves_evidence_and_issuer_records(self):
        self.assertTrue(repair.apply(self.history, self.db))
        self.assertFalse(self.source.exists())
        self.assertEqual(self.backup.read_bytes(), self.raw)
        self.assertEqual(json.loads(self.backup.with_suffix(".json").read_text("utf-8"))["rows"], 360)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT date FROM daily").fetchall(), [("2026-10-22",)])
            self.assertEqual(con.execute("SELECT date FROM holding_lists").fetchone()[0], "2026-10-23")
        con.close()
        self.assertFalse(repair.apply(self.history, self.db))

    def test_unknown_contents_are_never_removed(self):
        self.source.write_bytes(self.raw + b"changed")
        with self.assertRaisesRegex(ValueError, "內容"):
            repair.apply(self.history, self.db)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.backup.exists())

    def test_changed_database_is_not_removed(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE daily SET volume_lots=100 WHERE date='2026-10-23'")
        con.close()
        with self.assertRaisesRegex(ValueError, "資料庫行情"):
            repair.apply(self.history, self.db)
        self.assertTrue(self.source.exists())

    def test_fresh_database_path_requires_no_existing_sqlite(self):
        self.db.unlink()
        self.assertTrue(repair.apply(self.history, self.db))
        self.assertFalse(self.db.exists())
