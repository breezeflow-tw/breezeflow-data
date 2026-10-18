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

    def test_issuer(self):
        self.assertEqual(f.issuer_from_name("元大台灣50"), "元大")
        self.assertEqual(f.issuer_from_name("大華銀優利高填息30"), "大華銀")
        self.assertIsNone(f.issuer_from_name("主動統一台股增長"))


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


if __name__ == "__main__":
    unittest.main()
