"""資料完整度盤點報告的離線測試：自建小資料庫與 latest.json，核對統計與缺日計算。"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import coverage_report as cr  # noqa: E402
import market_calendar as mc  # noqa: E402
import store  # noqa: E402


class CoverageReportTest(unittest.TestCase):
    def test_report(self):
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        mc.save(con, mc.parse(json.loads((ROOT / "tests" / "fixtures" / "calendar.json").read_text("utf-8"))), "2026-10-20T08:50:00+08:00")
        rows = [("A", d, 10.0, 1e8, 10.0) for d in ("2026-10-08", "2026-10-12", "2026-10-14")]  # 10/9 補假；10/13 真的缺
        con.executemany("INSERT INTO fund_daily(code, date, nav, units, aum, source, source_url) VALUES(?, ?, ?, ?, ?, '測試投信官網', 'https://x')", rows)
        con.execute("INSERT INTO fund_monthly(code, month, source, source_url) VALUES('A', '2026-09', '公會', 'https://y')")
        items = [{"code": "A", "issuer": "測試", "price": 10, "nav": 10, "nav_source": "測試投信官網（正式淨值）", "aum": 10,
                  "aum_source": "測試投信官網", "units": 1e8, "units_change": 0, "holdings_top": [{"asset_code": "2330"}],
                  "holdings_source": "測試投信官網", "flows": {"available_weeks": 1}},
                 {"code": "B", "issuer": "未接入", "price": 20, "nav": 19.9, "nav_source": "證交所（投信預估淨值）", "aum": None,
                  "units_change": None, "holdings_top": [], "flows": {"available_weeks": 0}}]
        text = cr.report(con, {"trade_date": "2026-10-14", "generated_at": "x", "items": items})
        self.assertIn("官方開休市日期涵蓋年度：2026", text)
        # A：正式淨值、官網規模、官網持股、可算一週；B：只有預估淨值
        self.assertIn("| 測試 | 1 | 1 | 1 | 0 | 1 | 0 | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 1 |", text)
        self.assertIn("| 未接入 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |", text)
        # 10/8～10/14 應有 4 個交易日（10/9 補假），缺 10/13 一天
        self.assertIn("| 測試投信官網 | 1 | 2026-10-08 | 2026-10-14 | 3 | 3 | 3 | 3 | 4 | 1 |", text)
        self.assertIn("| 投信投顧公會 ETF 月統計（fund_monthly） | 2026-09 | 1 |", text)
        self.assertIn("| 未接入 | 1 | B |", text)
        con.close()


if __name__ == "__main__":
    unittest.main()
