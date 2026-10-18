"""離線測試：用 tests/fixtures 的假資料驗證解析、分類、合併與寫入。執行：python -m unittest -v"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_etf as f  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


class ParseTest(unittest.TestCase):
    def test_num(self):
        self.assertEqual(f.num("1,234.50"), 1234.5)
        self.assertEqual(f.num("+0.35"), 0.35)
        self.assertEqual(f.num("-0.2"), -0.2)
        self.assertIsNone(f.num("-"))
        self.assertIsNone(f.num(""))

    def test_nav_premium_fallback(self):
        nav = f.parse_nav(json.loads((FIX / "nav.json").read_text("utf-8")))
        self.assertEqual(len(nav), 5)
        self.assertAlmostEqual(nav["0050"]["premium"], 0.10)
        # g 空白時用市價與淨值自己算
        self.assertAlmostEqual(nav["00757"]["premium"], (112.5 - 110) / 110 * 100)
        self.assertIsNone(nav["00631L"]["price"])

    def test_quotes_both_markets(self):
        twse = f.parse_quotes(json.loads((FIX / "twse.json").read_text("utf-8")), "上市")
        tpex = f.parse_quotes(json.loads((FIX / "tpex.json").read_text("utf-8")), "上櫃")
        self.assertEqual(twse["0050"]["volume_lots"], 25300)
        self.assertAlmostEqual(twse["0050"]["change_pct"], 0.45 / 51.9 * 100)
        self.assertEqual(tpex["00679B"]["volume_lots"], 9500)
        self.assertIsNone(twse["00631L"]["close"])

    def test_classify(self):
        cases = {("0050", "元大台灣50"): "市值型", ("0056", "元大高股息"): "高股息",
                 ("00679B", "元大美債20年"): "債券", ("00631L", "元大台灣50正2"): "槓桿反向",
                 ("00632R", "元大台灣50反1"): "槓桿反向", ("00635U", "元大S&P黃金"): "商品",
                 ("00757", "統一FANG+"): "海外股票", ("00891", "中信關鍵半導體"): "主題型",
                 ("00981A", "主動統一台股增長"): "主動式"}
        for (code, name), want in cases.items():
            self.assertEqual(f.classify(code, name), want, code)

    def test_etf_filter_and_roc_date(self):
        self.assertTrue(f.is_etf("0050"))
        self.assertTrue(f.is_etf("00679B"))
        self.assertFalse(f.is_etf("2330"))
        self.assertFalse(f.is_etf("020000"))
        self.assertEqual(f.roc_date("1151016"), "2026-10-16")
        self.assertIsNone(f.roc_date(""))

    def test_merge_without_nav(self):
        quotes = f.parse_quotes(json.loads((FIX / "twse.json").read_text("utf-8")), "上市")
        items = f.merge({}, quotes, {}, "2026-10-16")
        self.assertEqual(len(items), 4)
        it = next(i for i in items if i["code"] == "0050")
        self.assertEqual(it["name"], "元大台灣50")
        self.assertIsNone(it["premium"])

    def test_clean_name(self):
        self.assertEqual(f.clean_name("國泰標普低波高息(本基金並無保證收益及配息)"), "國泰標普低波高息")
        self.assertEqual(f.clean_name("聯邦台灣精彩50ETF證券投資信託基金"), "聯邦台灣精彩50")
        self.assertEqual(f.issuer_from_name("FT臺灣Smart"), "第一金")

    def test_fund_and_dividends(self):
        fund = f.parse_fund(json.loads((FIX / "fund.json").read_text("utf-8")))
        self.assertEqual(fund["0050"]["units"], 1e9)
        self.assertEqual(fund["0050"]["listed_date"], "2003-06-30")
        self.assertIsNone(fund["00981A"]["index_name"])  # 「不適用」視為空值
        divs = f.parse_dividends(json.loads((FIX / "div_twse.json").read_text("utf-8")),
                                 json.loads((FIX / "div_tpex.json").read_text("utf-8")))
        codes = {d["code"]: d for d in divs}
        self.assertNotIn("2330", codes)  # 個股不收
        self.assertEqual(codes["00679B"], {"code": "00679B", "ex_date": "2026-10-16", "cash": 0.34})
        self.assertIsNone(codes["00757"]["cash"])  # 尚未公告金額

    def test_yahoo_reconcile(self):
        yahoo = f.parse_yahoo(json.loads((FIX / "yahoo.json").read_text("utf-8")))
        self.assertEqual(yahoo["0050"]["2026-10-16"]["volume_lots"], 25300)
        self.assertAlmostEqual(yahoo["0050"]["2026-10-16"]["change_pct"], 0.45 / 51.9 * 100)
        exchange = {**f.parse_quotes(json.loads((FIX / "tpex.json").read_text("utf-8")), "上櫃"),
                    **f.parse_quotes(json.loads((FIX / "twse.json").read_text("utf-8")), "上市")}
        quotes, diffs = f.reconcile(yahoo, exchange, "2026-10-16")
        self.assertEqual(quotes["0050"]["check"], "一致")
        self.assertEqual(quotes["00679B"]["check"], "一致")
        self.assertEqual(quotes["00757"]["check"], "已修正")
        self.assertEqual(quotes["00757"]["close"], 112.50)  # 不一致時採用交易所
        self.assertEqual(diffs, [{"code": "00757", "field": "close", "yahoo": 112.8, "exchange": 112.5}])
        self.assertEqual(quotes["00631L"]["check"], "未查核")
        self.assertEqual(quotes["00631L"]["close"], 24.05)
        self.assertEqual(quotes["0056"]["check"], "交易所")  # Yahoo 沒有這檔

    def test_issuer(self):
        self.assertEqual(f.issuer_from_name("元大台灣50"), "元大")
        self.assertEqual(f.issuer_from_name("大華優利高填息30"), "大華銀")
        self.assertEqual(f.issuer_from_name("大華銀優利高填息30"), "大華銀")
        self.assertEqual(f.issuer_from_name("主動統一台股增長"), "統一")
        self.assertEqual(f.issuer_from_name("期元大S&P黃金"), "元大")


class PipelineTest(unittest.TestCase):
    def test_end_to_end_and_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            args = ["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]
            self.assertEqual(f.main(args), 0)
            self.assertEqual(f.main(args), 0)  # 同日重跑不應產生重複資料

            payload = json.loads(out.read_text("utf-8"))
            self.assertEqual(payload["trade_date"], "2026-10-16")
            self.assertEqual(payload["source"], "fixture")
            items = {i["code"]: i for i in payload["items"]}
            self.assertEqual(items["00679B"]["market"], "上櫃")
            self.assertEqual(items["0050"]["price"], 52.35)  # 有收盤價時優先用收盤價
            self.assertEqual(items["0050"]["issuer"], "元大")

            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM daily").fetchone()[0], 5)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM etf").fetchone()[0], 5)
            con.close()

    def test_price_check_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            payload = json.loads(out.read_text("utf-8"))
            self.assertEqual(payload["price_check"], {"一致": 2, "已修正": 1, "未查核": 1, "交易所": 1})
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT code, field, yahoo, exchange FROM price_check").fetchall(),
                             [("00757", "close", 112.8, 112.5)])
            con.close()

    def test_size_and_dividends_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertAlmostEqual(items["0050"]["aum"], 1e9 * 52.25 / 1e8)    # 上市：基金基本資料 × 預估淨值
            self.assertAlmostEqual(items["00679B"]["aum"], 6e9 * 28.40 / 1e8)  # 上櫃：Capitals × 預估淨值
            self.assertEqual(items["0056"]["next_ex"], {"ex_date": "2026-10-20", "cash": 0.866, "pay_date": None})
            self.assertEqual(items["0050"]["last_ex"]["ex_date"], "2026-10-15")
            self.assertIsNone(items["0050"]["next_ex"])
            self.assertEqual(items["00679B"]["last_ex"]["ex_date"], "2026-10-16")  # 交易日當天除息算「最近一次」

    def test_rerun_without_nav_keeps_nav(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            nonav = Path(tmp) / "nonav"
            nonav.mkdir()
            for name in ("twse.json", "tpex.json"):
                (nonav / name).write_bytes((FIX / name).read_bytes())
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            self.assertEqual(f.main(["--fixtures", str(nonav), "--db", str(db), "--out", str(out)]), 0)
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT nav FROM daily WHERE code='0050'").fetchone()[0], 52.25)
            con.close()
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertEqual(items["0050"]["nav"], 52.25)

    def test_morning_run_records_official_nav(self):
        """早上開盤前：淨值資料已換成下個交易日，收盤行情還是前一天。"""
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)

            morning = Path(tmp) / "morning"
            morning.mkdir()
            nav = json.loads((FIX / "nav.json").read_text("utf-8"))
            for block in nav["a1"]:
                for row in block["msgArray"]:
                    row.update({"i": "20261019", "j": "08:50:00", "e": "-", "g": "-", "f": "99.99"})
            nav["a1"][0]["msgArray"][0]["h"] = "52.20"  # 0050 在 10/16 的正式淨值
            (morning / "nav.json").write_text(json.dumps(nav), "utf-8")
            twse = json.loads((FIX / "twse.json").read_text("utf-8"))
            for row in twse:
                row["Date"] = "1151016"
            (morning / "twse.json").write_text(json.dumps(twse), "utf-8")
            (morning / "tpex.json").write_bytes((FIX / "tpex.json").read_bytes())
            self.assertEqual(f.main(["--fixtures", str(morning), "--db", str(db), "--out", str(out)]), 0)

            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(DISTINCT date) FROM daily").fetchone()[0], 1)
            nav_est, nav_off, prem_off = con.execute(
                "SELECT nav, nav_official, premium_official FROM daily WHERE code='0050'").fetchone()
            con.close()
            self.assertEqual(nav_est, 52.25)  # 前一天的預估淨值沒被今天的資料蓋掉
            self.assertEqual(nav_off, 52.20)
            self.assertAlmostEqual(prem_off, (52.35 - 52.20) / 52.20 * 100)


if __name__ == "__main__":
    unittest.main()
