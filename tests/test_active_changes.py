"""主動式 ETF 持股異動的離線測試：用自建的資料庫模擬兩天的申購買回清單。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import active_changes as ac  # noqa: E402
import store  # noqa: E402

DAY, PREV = "2026-10-19", "2026-10-16"


class ActiveChangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.open_db(Path(self.tmp.name) / "etf.db", None)
        rows = {
            # 單位數 1,000 → 1,010（申購 1%）：2330 同比例增加；2454 加碼；2317 刪除；2303 新增；1101 只多 50 股（一張以內）
            "00981A": {PREV: [("2330", 100000), ("2454", 50000), ("2317", 30000), ("1101", 10000)],
                       DAY: [("2330", 101000), ("2454", 60000), ("2303", 20000), ("1101", 10050)]},
            # 沒有逐日單位數：用投信紀錄的單位數與「與前日已發行單位差異數」（0）推回前一天
            "00985A": {PREV: [("2454", 10000), ("2344", 156000), ("1216", 14442000)],
                       DAY: [("2454", 12000), ("2344", 2294000), ("1216", 11728000)]},
            # 完全沒有單位數：明細照列，但不列入彙總
            "00980A": {PREV: [("2454", 5000)], DAY: [("2454", 9000)]},
            # 不是主動式 ETF
            "0050": {PREV: [("2330", 1000)], DAY: [("2330", 2000)]},
        }
        for code, days in rows.items():
            for day, holdings in days.items():
                self.con.executemany(
                    "INSERT INTO holding(code, date, asset_code, asset_name, kind, weight, qty, source) VALUES(?, ?, ?, ?, '股票', ?, ?, ?)",
                    [(code, day, a, f"股票{a}", 1.0, q, "測試投信官網") for a, q in holdings])
        self.con.executemany("INSERT INTO fund_daily(code, date, units, source, source_url) VALUES(?, ?, ?, '測試', 'https://example.test')",
                             [("00981A", PREV, 1000.0), ("00981A", DAY, 1010.0)])
        self.con.executemany("INSERT INTO daily(date, code) VALUES(?, ?)", [(PREV, "0050"), (DAY, "0050")])
        self.con.commit()
        self.items = [{"code": c} for c in ("0050", "00980A", "00981A", "00985A")]
        self.issuer = {"00985A": {"units": 1385789000.0, "units_change": 0.0, "pcf_date": DAY}}
        self.closes = {"2454": (DAY, 1500.0), "2303": (DAY, 50.0), "2317": (DAY, 200.0), "2344": (DAY, 80.0),
                       "1216": (PREV, 70.0)}  # 1216 的收盤價是別天的，不能用

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_per_etf(self):
        summary = ac.attach(self.con, self.items, DAY, self.issuer, self.closes)
        x = self.items[2]["holdings_changes"]
        self.assertEqual((x["date"], x["prev_date"], x["units"], x["units_prev"], x["flow_checked"]), (DAY, PREV, 1010.0, 1000.0, True))
        rows = {r["asset_code"]: r for r in x["changes"]}
        self.assertEqual({k: (r["action"], r["qty_change"], r["with_flow"]) for k, r in rows.items()},
                         {"2303": ("新增", 20000, None), "2330": ("加碼", 1000, True), "2454": ("加碼", 10000, False),
                          "1101": ("加碼", 50, True), "2317": ("刪除", -30000, None)})
        self.assertEqual(x["counts"], {"新增": 1, "加碼": 3, "減碼": 0, "刪除": 1})
        # 超出申購比例的百分點：2454 增加 20%、單位數增加 1%，超出 19 個百分點
        self.assertEqual((rows["2454"]["excess"], rows["2330"]["excess"], rows["1101"]["excess"], rows["2303"]["excess"]), (19.0, 0.0, -0.5, None))
        self.assertEqual((rows["2454"]["amount"], rows["2317"]["amount"]), (0.15, -0.06))
        # 單位數由「與前日已發行單位差異數」推回
        y = self.items[3]["holdings_changes"]
        self.assertEqual((y["units"], y["units_prev"], y["flow_checked"]), (1385789000.0, 1385789000.0, True))
        z = self.items[1]["holdings_changes"]
        self.assertEqual((z["flow_checked"], [r["with_flow"] for r in z["changes"]]), (False, [None]))
        self.assertIsNone(self.items[0]["holdings_changes"])  # 0050 不是主動式
        self.assertEqual(summary["etfs"], ["00981A", "00985A"])
        self.assertEqual(summary["not_included"], ["00980A"])

    def test_summary(self):
        s = ac.attach(self.con, self.items, DAY, self.issuer, self.closes)
        buys = {r["asset_code"]: r for r in s["buys"]}
        sells = {r["asset_code"]: r for r in s["sells"]}
        # 2454 有兩檔加碼（00980A 沒有單位數，不算）；2330 與 1101 是同比例增減，不算
        self.assertEqual((buys["2454"]["buy_etfs"], buys["2454"]["buyers"], buys["2454"]["buy_shares"], buys["2454"]["buy_amount"]),
                         (2, ["00981A", "00985A"], 12000, 0.18))
        self.assertNotIn("2330", buys)
        self.assertNotIn("1101", buys)
        self.assertEqual(s["buys"][0]["asset_code"], "2454")  # 依加碼檔數排序
        self.assertEqual((sells["2317"]["sell_etfs"], sells["2317"]["sell_amount"]), (1, 0.06))
        # 1216 的收盤價日期不符：金額留空
        self.assertEqual((sells["1216"]["sell_shares"], sells["1216"]["sell_amount"]), (2714000, None))
        self.assertEqual(s["date"], DAY)

    def test_side(self):
        row = lambda action, change, excess: {"action": action, "qty_change": change, "excess": excess,
                                              "with_flow": abs(excess) < ac.EXCESS if excess is not None else None}
        # 申購讓單位數增加 7.2%、股票增加 6.9%：大致維持比重，不算加碼
        self.assertIsNone(ac.side(row("加碼", 69000, -0.3)))
        # 股數增加但遠少於申購比例：沒有跟著買不等於賣出，也不算減碼
        self.assertIsNone(ac.side(row("加碼", 20000, -5.2)))
        self.assertEqual(ac.side(row("加碼", 50000, 6.0)), "buy")
        self.assertEqual(ac.side(row("減碼", -30000, -8.0)), "sell")
        # 買回讓單位數減少 5%、股票只減 2%：比重反而提高，也不算減碼
        self.assertIsNone(ac.side(row("減碼", -20000, 3.0)))
        self.assertEqual((ac.side(row("新增", 1000, None)), ac.side(row("刪除", -1000, None))), ("buy", "sell"))
        self.assertIsNone(ac.side(row("加碼", 5000, None)))  # 沒有單位數可判斷

    def test_single_list(self):
        self.con.execute("DELETE FROM holding WHERE date = ?", (PREV,))
        self.assertIsNone(ac.attach(self.con, self.items, DAY, self.issuer, self.closes))
        self.assertTrue(all(it["holdings_changes"] is None for it in self.items))


if __name__ == "__main__":
    unittest.main()
