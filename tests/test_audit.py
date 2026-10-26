"""資料查核腳本的離線測試：用假資料確認阻擋與提醒項目抓得到問題、正常資料全部通過。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import audit  # noqa: E402
import store  # noqa: E402

DAILY = ("date", "code", "price", "change_pct", "nav", "nav_prev", "premium", "volume_lots", "nav_official", "premium_official",
         "aum", "aum_source", "nav_source")
TAIWAN_STOCK = {"region": "臺灣", "asset_class": "股票"}


def build(tmp: str, edit=None, items_edit=None) -> list[str]:
    """兩個交易日（10/21、10/22）、兩檔 ETF 的一致資料；edit 可在寫入前改資料庫，items_edit 改 latest.json。"""
    db, latest = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
    con = sqlite3.connect(db)
    store.ensure_schema(con)
    rows = [
        ("2026-10-21", "0050", 51.0, 0.5, 51.1, 50.8, (51.0 - 51.1) / 51.1 * 100, 1200, 51.1, (51.0 - 51.1) / 51.1 * 100, 5000.0,
         "元大投信官網", "元大投信官網（正式淨值）"),
        ("2026-10-22", "0050", 52.35, 1.35 / 51.0 * 100, 52.3, 51.1, (52.35 - 52.3) / 52.3 * 100, 1500, None, None, 5100.0,
         "元大投信官網", "元大投信官網（正式淨值）"),
        ("2026-10-21", "0056", 58.75, 1.0, 58.69, 58.1, (58.75 - 58.69) / 58.69 * 100, 900, 58.69, (58.75 - 58.69) / 58.69 * 100,
         None, None, "投信投顧公會（正式淨值）"),
        ("2026-10-22", "0056", 56.3, None, 56.16, 58.69, (56.3 - 56.16) / 56.16 * 100, 800, None, None, None, None,
         "投信投顧公會（正式淨值）"),
    ]
    con.executemany(f"INSERT INTO daily({', '.join(DAILY)}) VALUES({', '.join('?' * len(DAILY))})", rows)
    con.executemany("""INSERT INTO exchange_daily(code, date, market, close, change, volume_shares, dataset, source_url)
                       VALUES(?, ?, '上市', ?, ?, ?, '盤後資訊 > 個股日成交資訊', 'https://example.com')""",
                    [("0050", "2026-10-21", 51.0, 0.25, 1200000), ("0050", "2026-10-22", 52.35, 1.35, 1500000),
                     ("0056", "2026-10-21", 58.75, 0.58, 900000), ("0056", "2026-10-22", 56.3, 0.0, 800000)])
    con.executemany("INSERT INTO fund_daily(code, date, nav, aum, units, currency, source, source_url) VALUES(?, ?, ?, ?, ?, 'TWD', ?, ?)",
                    [("0050", "2026-10-21", 51.1, 5000.0, 9784735812, "元大投信官網", "https://example.com"),
                     ("0050", "2026-10-22", 52.3, 5100.0, 9751434034, "元大投信官網", "https://example.com")])
    con.execute("INSERT INTO dividend(code, ex_date, cash, source) VALUES('0056', '2026-10-22', 1.72, '交易所除權息預告')")
    if edit:
        edit(con)
    con.commit()
    con.close()
    items = [{"code": "0050", "price": 52.35, "nav": 52.3, "nav_date": "2026-10-22", "aum_date": "2026-10-22", "product": TAIWAN_STOCK},
             {"code": "0056", "price": 56.3, "nav": 56.16, "nav_date": "2026-10-22", "product": TAIWAN_STOCK}]
    if items_edit:
        items_edit(items)
    text = json.dumps({"trade_date": "2026-10-22", "count": len(items), "items": items}, ensure_ascii=False)
    latest.write_text(text.replace('"nav": 999.0', '"nav": NaN'), "utf-8")  # 999.0 代表寫成 NaN：Python 讀得進來，瀏覽器不行
    return ["--db", str(db), "--latest", str(latest), "--json", str(Path(tmp) / "audit.json")]


def results(tmp: str) -> dict:
    return {c["key"]: c for c in json.loads((Path(tmp) / "audit.json").read_text("utf-8"))["checks"]}


class AuditTest(unittest.TestCase):
    def test_consistent_data_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(audit.main(build(tmp) + ["--all"]), 0)
            got = results(tmp)
            failed = {k for k, c in got.items() if c["failed"] or c["error"]}
            self.assertEqual(failed, set())
            self.assertGreater(got["P1"]["checked"], 0)
            self.assertEqual(got["C1"]["checked"], 3)  # 除息日漲跌幅留空，不列入
            self.assertEqual(got["N2"]["checked"], 2)

    def test_blocking_checks_fail_the_run(self):
        cases = {
            "P1": dict(edit=lambda con: con.execute("UPDATE daily SET price = 52.4 WHERE code = '0050' AND date = '2026-10-22'")),
            "J1": dict(items_edit=lambda items: items[0].update(nav=999.0)),  # 寫成 NaN
            "J2": dict(items_edit=lambda items: items[1].update(nav_date="2026-10-23")),
            # 2026-10-23 沒有交易的那種輸出：全部 ETF 成交量 0
            "J3": dict(items_edit=lambda items: items.extend({"code": f"00{900 + i}", "volume_lots": 0} for i in range(10))),
        }
        for key, kwargs in cases.items():
            with self.subTest(key), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(audit.main(build(tmp, **kwargs)), 1)
                got = results(tmp)
                self.assertEqual(got[key]["level"], audit.BLOCK)
                self.assertGreater(got[key]["failed"], 0)

    def test_warnings_flag_known_problems_without_blocking(self):
        def problems(con):
            # 2026-10-21 元大 0056：除息前一天官網的淨值已先扣掉 1.72 元配息
            con.execute("UPDATE daily SET nav_official = 56.97, premium_official = (58.75 - 56.97) / 56.97 * 100 "
                        "WHERE code = '0056' AND date = '2026-10-21'")
            con.execute("UPDATE daily SET nav_official = 51.1, nav_prev = 52.3, aum = 4900.0 WHERE code = '0050' AND date = '2026-10-22'")
            con.execute("UPDATE daily SET aum = 3.5, aum_source = '交易所資料推算（發行單位數×淨值）' WHERE code = '0056' AND date = '2026-10-22'")
            con.execute("DELETE FROM fund_daily WHERE code = '0050' AND date = '2026-10-22'")

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(audit.main(build(tmp, edit=problems) + ["--all"]), 0)
            got = results(tmp)
            self.assertEqual(got["N4"]["examples"], [["2026-10-21", "0056", "56.97", "配息 1.72", "證交所轉載 58.69"]])
            self.assertEqual(got["PO1"]["failed"], 1)  # 0056 在 10/21 的正式折溢價 +3.1%
            self.assertEqual(got["N3"]["failed"], 1)  # 0050 在 10/22 沿用 10/21 的正式淨值、收盤價漲 2.6%
            self.assertEqual(got["N2"]["examples"][0][:2], ["2026-10-22", "0050"])
            self.assertEqual(got["A5"]["failed"], 1)
            self.assertTrue(all(c["level"] == audit.WARN for c in got.values() if c["failed"]))

    def test_trade_date_scope_and_rebuild_from_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = build(tmp, edit=lambda con: con.execute("UPDATE daily SET volume_lots = 1 WHERE date = '2026-10-21'"))
            self.assertEqual(audit.main(args), 0)
            self.assertEqual(results(tmp)["V1"]["failed"], 0)  # 只查網站交易日（10/22）
            history = Path(tmp) / "history"
            con = sqlite3.connect(Path(tmp) / "etf.db")
            store.dump(con, history)
            con.close()
            args[1] = str(Path(tmp) / "missing.db")  # 資料庫檔不存在：從歷史 CSV 重建到暫存檔
            self.assertEqual(audit.main(args + ["--all", "--history", str(history)]), 0)
            self.assertEqual(results(tmp)["V1"]["failed"], 2)
            self.assertFalse((Path(tmp) / "missing.db").exists())


if __name__ == "__main__":
    unittest.main()
