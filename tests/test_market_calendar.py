"""官方開休市日期與週申贖跨假期的離線測試。日曆用證交所 2026 年的官方表，基金數值為合成樣本。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fund_flows as f  # noqa: E402
import market_calendar as mc  # noqa: E402
import store  # noqa: E402

ENTRIES = mc.parse(json.loads((ROOT / "tests" / "fixtures" / "calendar.json").read_text("utf-8")))
CAL = mc.Calendar(ENTRIES, {2026})


def rows(first="2026-09-21", last="2026-11-06", skip=(), change=1_000_000):
    """只在官方交易日有快照：每個交易日淨申購 change 單位、淨值 10 元。"""
    out, units = {}, 100_000_000
    for day in CAL.trading_days(first, last):
        if out:
            units += change
        if day in skip:
            continue
        out[day] = {"code": "TEST", "date": day, "nav": 10, "units": units, "units_change": change if out else None,
                    "aum": units * 10 / 1e8, "currency": "TWD", "source": "測試投信官網",
                    "source_url": "https://www.yuantaetfs.com/test"}
    return out


class CalendarTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(len(ENTRIES), 27)
        # 「開始交易日」「最後交易日」當天有交易；僅辦理結算交割、補假都是休市
        self.assertTrue(ENTRIES["2026-01-02"]["is_open"])
        self.assertTrue(ENTRIES["2026-02-11"]["is_open"])
        self.assertFalse(ENTRIES["2026-02-12"]["is_open"])
        self.assertFalse(ENTRIES["2026-10-09"]["is_open"])
        self.assertEqual(ENTRIES["2026-02-11"]["description"], "農曆春節前最後交易。")  # 去掉 <br>
        self.assertEqual(mc.parse([{"Date": "", "Name": "x"}, "壞資料"]), {})

    def test_status(self):
        self.assertTrue(CAL.is_open("2026-10-20"))  # 表上沒列的平日
        self.assertFalse(CAL.is_open("2026-10-24"))  # 週六
        self.assertFalse(CAL.is_open("2026-10-26"))  # 補假
        self.assertIsNone(CAL.is_open("2025-12-31"))  # 官方表沒涵蓋的年度
        self.assertEqual(CAL.trading_days("2026-10-26", "2026-10-30"),
                         ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"])
        self.assertIsNone(CAL.trading_days("2026-12-28", "2027-01-01"))
        self.assertEqual(CAL.previous_trading_day("2026-10-12"), "2026-10-08")  # 10/9 補假、10/10、10/11 週末
        self.assertEqual(CAL.previous_trading_day("2026-02-23"), "2026-02-11")  # 春節
        self.assertIsNone(CAL.previous_trading_day("2026-01-02"))  # 前一個交易日在 2025 年，沒涵蓋

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = store.open_db(Path(tmp) / "a.db", None)
            mc.save(con, ENTRIES, "2026-10-20T21:00:00+08:00")
            # 同一年重抓時整批取代：拿掉的日子不留下
            mc.save(con, {d: e for d, e in ENTRIES.items() if d != "2026-12-25"}, "2026-10-21T08:50:00+08:00")
            con.commit()
            store.dump(con, Path(tmp) / "history")
            con.close()
            con = store.open_db(Path(tmp) / "b.db", Path(tmp) / "history")
            cal = mc.load(con, include_announcements=False)
            self.assertEqual((cal.years, len(cal.entries)), ({2026}, 26))
            self.assertTrue(cal.is_open("2026-12-25"))
            self.assertEqual(con.execute("SELECT entries, fetched_at FROM market_calendar_year").fetchone(),
                             (26, "2026-10-21T08:50:00+08:00"))
            con.close()
        self.assertEqual(mc.load(sqlite3.connect(":memory:")).years, set())  # 舊資料庫沒有這張表


class CalendarFlowTest(unittest.TestCase):
    def test_announced_closure_is_persistent_and_changes_baseline(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        store.ensure_schema(con)
        mc.save(con, ENTRIES, "2026-10-26")
        mc.save_announcements(con)
        cal = mc.load(con)
        self.assertFalse(cal.is_open("2026-10-23"))
        self.assertEqual(cal.previous_trading_day("2026-10-27"), "2026-10-22")
        self.assertEqual(cal.entries["2026-10-23"]["reason_code"], "announced_closed")
        self.assertIn("staticFiles/news", con.execute("SELECT source_url FROM market_calendar WHERE date='2026-10-23'").fetchone()[0])
        # 只知道一則公告，不能當成已取得整年日曆。
        isolated = mc.Calendar(mc.announcements(), set())
        self.assertFalse(isolated.is_open("2026-10-23"))
        self.assertIsNone(isolated.is_open("2026-10-22"))
        snapshots = rows(skip=("2026-10-23",))
        # 合成樣本不保留休市日原先的單位增加。
        for day, item in snapshots.items():
            if day > "2026-10-23":
                item["units"] -= 1_000_000
                item["aum"] -= 0.1
        result = f.week(snapshots, date(2026,10,26), date(2026,10,30), (), cal)
        self.assertEqual((result["baseline"], result["net_amount"]), ("2026-10-22", 0.4))

    def test_week_across_holiday(self):
        # 10/26 補假：期初是 10/23，只比較 10/27～10/30 四個交易日
        r = f.week(rows(), date(2026, 10, 26), date(2026, 10, 30), (), CAL)
        self.assertEqual((r["baseline"], r["closed_days"], r["net_amount"], r["status"]), ("2026-10-23", ["2026-10-26"], 0.4, "可估算"))
        self.assertEqual((r["calendar"], r["reason_code"], len(r["sources"])), ("官方開休市日期", None, 5))
        # 沒有日曆時，10/26 會被當成缺資料
        old = f.week(rows(), date(2026, 10, 26), date(2026, 10, 30))
        self.assertIsNone(old["net_amount"])
        self.assertEqual((old["reason_code"], old["calendar"]), ("missing_official", "未使用"))
        self.assertIn("休市日亦須核對", old["reason"])

    def test_previous_friday_holiday(self):
        # 10/9（週五）補假：期初是 10/8
        r = f.week(rows(), date(2026, 10, 12), date(2026, 10, 16), (), CAL)
        self.assertEqual((r["baseline"], r["closed_days"], r["net_amount"]), ("2026-10-08", [], 0.5))

    def test_real_missing_day_is_not_holiday(self):
        r = f.week(rows(skip=("2026-10-14",)), date(2026, 10, 12), date(2026, 10, 16), (), CAL)
        self.assertIsNone(r["net_amount"])
        self.assertEqual(r["reason_code"], "missing_official")
        self.assertIn("2026-10-14", r["reason"])
        self.assertNotIn("休市日亦須核對", r["reason"])

    def test_calendar_not_covered(self):
        r = f.week(rows(), date(2026, 12, 28), date(2027, 1, 1), (), CAL)
        self.assertEqual((r["net_amount"], r["reason_code"], r["calendar"], r["baseline"]), (None, "calendar_unknown", "未涵蓋", None))

    def test_whole_week_closed(self):
        r = f.week(rows("2026-02-09", "2026-02-27"), date(2026, 2, 16), date(2026, 2, 20), (), CAL)
        self.assertEqual((r["status"], r["reason_code"], r["net_amount"], r["trading_days"]), ("休市", "market_closed", None, []))
        self.assertEqual(r["baseline"], "2026-02-11")

    def test_zero_flow_with_holiday_is_zero(self):
        r = f.week(rows(change=0), date(2026, 10, 26), date(2026, 10, 30), (), CAL)
        self.assertEqual((r["net_amount"], r["units_rate"], r["status"]), (0, 0, "可估算"))

    def test_split_before_and_after(self):
        events = [{"date": "2026-10-28", "kind": "分割"}]
        self.assertEqual(f.week(rows(), date(2026, 10, 26), date(2026, 10, 30), events, CAL)["reason_code"], "special_event")
        # 分割前一週與後一週照常計算（事件不在該週期初之後、期末之前）
        self.assertEqual(f.week(rows(), date(2026, 10, 19), date(2026, 10, 23), events, CAL)["net_amount"], 0.5)
        self.assertEqual(f.week(rows(), date(2026, 11, 2), date(2026, 11, 6), events, CAL)["net_amount"], 0.5)

    def test_four_weeks_with_only_three_available(self):
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        for r in rows(skip=("2026-10-07",)).values():
            f.upsert(con, "fund_daily", ("code", "date"), r)
        items = [{"code": "TEST"}]
        f.attach(con, items, "2026-10-30", CAL)
        flows = items[0]["flows"]
        self.assertEqual([w["net_amount"] for w in flows["weeks"]], [0.4, 0.5, 0.5, None])
        self.assertEqual(flows["weeks"][3]["reason_code"], "missing_official")
        self.assertEqual((flows["available_weeks"], flows["four_week_amount"], flows["positive_weeks"]), (3, None, None))
        con.close()


if __name__ == "__main__":
    unittest.main()
