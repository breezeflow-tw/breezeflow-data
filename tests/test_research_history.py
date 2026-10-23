"""十年匯入與完整度檢查；全部使用標示的離線測試資料。"""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import research_history as history
import store

FIXTURE = Path(__file__).parent / "fixtures/research_package.json"


class ResearchHistoryTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        store.ensure_schema(self.con)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.obj = json.loads(FIXTURE.read_text("utf-8"))
        self.item = {"code": "0050", "name": "測試", "market": "上市", "listed_date": "2003-06-30", "product": {"trading_currency": "TWD"}}

    def ingest(self, obj=None):
        path = self.path / "package.json"
        path.write_text(json.dumps(obj or self.obj), "utf-8")
        return history.import_package(self.con, path, "2026-10-22", allow_test=True)

    def build(self):
        return history.build(self.con, self.item, "2026-10-22")

    def test_import_is_idempotent_and_csv_round_trip(self):
        self.assertTrue(self.ingest())
        self.assertFalse(self.ingest())
        store.dump(self.con, self.path / "history")
        with sqlite3.connect(":memory:") as rebuilt:
            store.ensure_schema(rebuilt)
            store.load(rebuilt, self.path / "history")
            self.assertEqual(rebuilt.execute("SELECT count(*) FROM research_price").fetchone()[0], 4)
            self.assertEqual(history.build(rebuilt, self.item, "2026-10-22"), self.build())

    def test_ten_year_target_is_not_actual_coverage(self):
        self.ingest()
        result = self.build()
        self.assertEqual(result["target_start"], "2016-10-22")
        self.assertEqual(result["available_start"], "2026-10-19")
        self.assertFalse(result["ten_year_complete"])
        self.assertEqual(result["ready_ranges"], [{"start": "2026-10-19", "end": "2026-10-22"}])

    def test_listing_and_leap_boundaries(self):
        self.assertEqual(history.years_before("2024-02-29"), "2014-02-28")
        self.assertEqual(history.years_before("2024-02-29", 20), "2004-02-29")
        self.ingest()
        self.item["listed_date"] = "2026-10-21"
        result = self.build()
        self.assertEqual(result["required_start"], "2026-10-21")
        self.assertEqual(len(result["bars"]), 2)
        self.assertFalse(result["ten_year_complete"])

    def test_missing_actions_or_calendar_does_not_enable_simulation(self):
        for kind in ("actions", "calendar"):
            with self.subTest(kind=kind):
                self.con.execute("DELETE FROM research_coverage")
                self.con.execute("DELETE FROM research_import")
                obj = copy.deepcopy(self.obj)
                obj["coverage"] = [r for r in obj["coverage"] if r["kind"] != kind]
                self.ingest(obj)
                self.assertEqual(self.build()["ready_ranges"], [])

    def test_missing_price_splits_ready_ranges(self):
        self.obj["prices"] = [r for r in self.obj["prices"] if r["date"] != "2026-10-20"]
        self.ingest()
        result = self.build()
        self.assertIn("2026-10-20", result["missing_price_dates"])
        self.assertFalse(any(r["start"] <= "2026-10-20" <= r["end"] for r in result["ready_ranges"]))

    def test_unknown_or_mismatched_currency_blocks(self):
        self.ingest()
        self.item["product"]["trading_currency"] = "USD"
        result = self.build()
        self.assertEqual(result["bars"], [])
        self.assertTrue(result["issues"])
        self.assertEqual(result["ready_ranges"], [])

    def test_missing_evidence_bad_values_future_or_duplicates_rejected(self):
        mutations = [lambda o: o["source"].update(status="pending"),
                     lambda o: o["source"].pop("redistribution_evidence"),
                     lambda o: o["prices"][0].update(low=20),
                     lambda o: o["prices"][0].update(close=float("nan")),
                     lambda o: o["prices"][0].update(date="2026-10-23"),
                     lambda o: o["prices"].append(o["prices"][0]),
                     lambda o: o["prices"][0].update(code="00847B"),
                     lambda o: o["actions"][0].update(pay_date="2026-10-19")]
        for change in mutations:
            obj = copy.deepcopy(self.obj)
            change(obj)
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                self.ingest(obj)
            self.assertEqual(self.con.execute("SELECT count(*) FROM research_price").fetchone()[0], 0)

    def test_conflict_rolls_back_entire_package(self):
        self.ingest()
        obj = copy.deepcopy(self.obj)
        obj["source"]["id"] = "new-fixture"
        obj["prices"][0]["close"] = 11
        with self.assertRaisesRegex(ValueError, "衝突"):
            self.ingest(obj)
        self.assertIsNone(self.con.execute("SELECT * FROM research_source WHERE id='new-fixture'").fetchone())
        self.assertEqual(self.con.execute("SELECT close FROM research_price ORDER BY date LIMIT 1").fetchone()[0], 10)

    def test_review_date_can_follow_market_date_but_not_be_in_future(self):
        today = history.datetime.now(history.timezone(history.timedelta(hours=8))).date()
        self.obj["source"]["checked_at"] = today.isoformat()
        self.assertTrue(self.ingest())
        future = copy.deepcopy(self.obj)
        future["source"]["id"] = "future-review"
        future["source"]["checked_at"] = (today + history.timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "查核日期在未來"):
            self.ingest(future)

    def test_conflicting_calendar_and_suspicious_jump_block(self):
        self.ingest()
        self.con.execute("INSERT INTO research_coverage VALUES(?,?,?,?,?,?)", ("0050", "calendar", "2026-10-20", "2026-10-21", "[]", "fixture-only"))
        result = self.build()
        self.assertTrue(any(i["reason"] == "交易日曆證據矛盾" for i in result["issues"]))
        self.con.execute("UPDATE research_price SET open=20,high=21,low=19,close=20 WHERE date='2026-10-22'")
        self.assertTrue(any(i["reason"] == "大幅價差需核對公司行動" for i in self.build()["issues"]))

    def test_export_uses_content_address_and_excludes_blocked_code(self):
        self.ingest()
        index = history.export(self.con, [self.item, {"code": "00847B"}], "2026-10-22", self.path / "out", allow_test=True)
        filename = index["items"]["0050"]["file"]
        self.assertRegex(filename, r"^0050-[a-f0-9]{16}\.json$")
        self.assertNotIn("00847B", index["items"])
        self.assertEqual(json.loads((self.path / "out" / filename).read_text("utf-8"))["bars"][0]["close"], 10)

    def test_complete_ten_year_package_and_twenty_year_query(self):
        trading = [d for d in history.days("2016-10-22", "2026-10-22") if history.date.fromisoformat(d).weekday() < 5]
        self.obj["prices"] = [{**self.obj["prices"][0], "date": d} for d in trading]
        self.obj["actions"] = []
        self.obj["coverage"][0].update(start="2016-10-22", trading_days=trading)
        self.obj["coverage"][1]["start"] = "2016-10-22"
        self.ingest()
        result = self.build()
        self.assertTrue(result["ten_year_complete"])
        self.assertEqual(len(result["bars"]), len(trading))
        longer = history.build(self.con, self.item, "2026-10-22", years=20)
        self.assertEqual(longer["target_start"], "2006-10-22")
        self.assertFalse(longer["ten_year_complete"])

    def test_test_package_cannot_enter_production_or_be_published(self):
        with self.assertRaisesRegex(ValueError, "測試資料禁止"):
            history.import_package(self.con, FIXTURE, "2026-10-22")
        self.ingest()
        with self.assertRaisesRegex(ValueError, "測試來源不得發布"):
            history.export(self.con, [self.item], "2026-10-22", self.path / "public")


if __name__ == "__main__":
    unittest.main()
