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
        items = f.merge({}, quotes, {})
        self.assertEqual(len(items), 4)
        it = next(i for i in items if i["code"] == "0050")
        self.assertEqual(it["name"], "元大台灣50")
        self.assertIsNone(it["premium"])

    def test_clean_name(self):
        self.assertEqual(f.clean_name("國泰標普低波高息(本基金並無保證收益及配息)"), "國泰標普低波高息")
        self.assertEqual(f.clean_name("聯邦台灣精彩50ETF證券投資信託基金"), "聯邦台灣精彩50")
        self.assertEqual(f.issuer_from_name("FT臺灣Smart"), "第一金")

    def test_issuer(self):
        self.assertEqual(f.issuer_from_name("元大台灣50"), "元大")
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


if __name__ == "__main__":
    unittest.main()
