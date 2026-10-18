"""離線測試：用 tests/fixtures 的假資料驗證解析、分類、合併與寫入。執行：python -m unittest -v"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_etf as f  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def fixtures_without(tmp: str, *names: str) -> Path:
    """複製一份測試資料並拿掉指定來源，用來測試只有交易所資料的情況。"""
    dst = Path(tmp) / "fix"
    shutil.copytree(FIX, dst, ignore=shutil.ignore_patterns(*names))
    return dst


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
        self.assertEqual(yahoo["0050"]["2026-10-16"]["volume_lots"], 23500)
        self.assertAlmostEqual(yahoo["0050"]["2026-10-16"]["change_pct"], 0.45 / 51.9 * 100)
        exchange = {**f.parse_quotes(json.loads((FIX / "tpex.json").read_text("utf-8")), "上櫃"),
                    **f.parse_quotes(json.loads((FIX / "twse.json").read_text("utf-8")), "上市")}
        quotes, diffs = f.reconcile(yahoo, exchange, "2026-10-16")
        self.assertEqual(quotes["0050"]["check"], "一致")  # 成交量不同不算不一致
        self.assertEqual(quotes["0050"]["volume_lots"], 25300)  # 成交量採用交易所官方總量
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


class YuantaTest(unittest.TestCase):
    def test_fee_parsing(self):
        from issuers import yuanta as y
        self.assertEqual(y.official_fee("約0.0682%  (資料日期：2026/10/16) "), 0.0682)
        self.assertEqual(y.flat_fee("0.99%"), 0.99)
        self.assertIsNone(y.flat_fee("100億(含)以下：0.40%\n逾100億(不含)：0.30%"))  # 分段級距不換算
        self.assertIsNone(y.official_fee(""))

    def test_parse(self):
        from issuers import yuanta as y
        info = y.parse_info(y.unwrap(json.loads((FIX / "yuanta" / "info.json").read_text("utf-8"))))
        self.assertNotIn("0099X", info)  # 已下市
        self.assertEqual(info["0050"]["mgmt_fee"], 0.07)
        self.assertEqual(info["00679B"]["mgmt_fee"], 0.15)
        self.assertIsNone(info["00631L"]["mgmt_fee"])
        pcf = y.parse_pcf(json.loads((FIX / "yuanta" / "pcf_0050.json").read_text("utf-8")))
        self.assertEqual((pcf["nav_official"], pcf["nav_date"], pcf["aum"]), (52.30, "2026-10-16", 5230.0))
        self.assertEqual(pcf["beneficiaries_month"], "2026-09")
        self.assertEqual([h["asset_code"] for h in pcf["holdings"]], ["2330", "2317", "TX-202610"])
        empty = y.parse_pcf(json.loads((FIX / "yuanta" / "pcf_00631L.json").read_text("utf-8")))
        self.assertIsNone(empty["nav_official"])
        self.assertEqual(empty["holdings"], [])
        self.assertIsNone(y.parse_factset(None))

    def test_pipeline_prefers_issuer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["0050"]
            self.assertEqual(a["nav"], 52.30)  # 投信正式淨值取代證交所預估淨值 52.25
            self.assertIn("元大投信官網", a["nav_source"])
            self.assertAlmostEqual(a["premium"], (52.35 - 52.30) / 52.30 * 100, places=3)
            self.assertEqual((a["aum"], a["aum_source"]), (5230.0, "元大投信官網"))
            self.assertEqual((a["mgmt_fee"], a["beneficiaries"], a["stats"]["div_yield"]), (0.07, 1000000, 3.1))
            self.assertEqual(a["holdings_top"][0]["asset_name"], "台積電")
            b = items["00679B"]
            self.assertEqual(b["nav"], 28.40)  # 債券型正式淨值晚一天（10/15），當天仍用預估淨值
            self.assertTrue(b["nav_source"].startswith("證交所"))
            self.assertEqual(b["aum"], 1700.0)
            self.assertIsNone(b["stats"])
            # 尚未接入投信的 ETF（0056 不在元大測試資料裡）維持交易所資料
            self.assertTrue(items["0056"]["aum_source"] is None or items["0056"]["aum_source"].startswith("交易所"))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM holding WHERE code='0050'").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT nav_official FROM daily WHERE code='0050'").fetchone()[0], 52.30)
            con.close()


class UniTest(unittest.TestCase):
    def test_parse(self):
        from issuers import uni as u
        funds, token = u.parse_fund_list((FIX / "uni" / "div.html").read_text("utf-8"))
        self.assertEqual(funds, {"36YTW": "00757", "46YTW": "00939"})
        self.assertEqual(token, "TESTTOKEN")
        a = u.parse_info((FIX / "uni" / "info_36YTW.html").read_text("utf-8"))  # DataFund 被跳脫兩次
        self.assertEqual((a["code"], a["nav_official"], a["nav_date"], a["aum"]), ("00757", 150.0, "2026-10-15", 300.0))
        self.assertEqual((a["mgmt_fee"], a["custodian_fee_text"], a["manager"], a["risk_class"]), (0.85, "0.12%", "測試經理人", "RR5"))
        self.assertEqual((a["list_date"], a["index_name"]), ("2016-09-06", "NYSE FANG+指數"))
        self.assertEqual([h["asset_code"] for h in a["holdings"]], ["META", "NVDA", "NQ-202612"])
        b = u.parse_info((FIX / "uni" / "info_46YTW.html").read_text("utf-8"))
        self.assertIsNone(b["mgmt_fee"])  # 分段級距不換算
        self.assertIsNone(b["aum"])  # 外幣計價不自行換算規模
        divs = u.parse_dividends((FIX / "uni" / "div_46YTW.html").read_text("utf-8"))
        self.assertEqual(divs[0], {"code": "00939", "ex_date": "2026-10-05", "pay_date": "2026-10-29", "cash": 0.125})
        self.assertIsNone(u.parse_dividends("<html>被擋下的頁面</html>"))  # 查詢失敗與「沒有配息」要分開

    def test_pipeline_dividends(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00757"]
            self.assertEqual((a["aum"], a["aum_source"]), (300.0, "統一投信官網"))
            self.assertEqual(a["nav"], 110.00)  # 正式淨值是前一天的（10/15），當天仍用預估淨值
            # 交易所預告沒有金額，投信官網有，且多了發放日
            self.assertEqual(a["next_ex"], {"ex_date": "2026-11-03", "cash": 0.5, "pay_date": "2026-11-25", "source": "統一投信官網"})

            # 再跑一次但統一官網沒資料：交易所預告不可覆蓋投信公告
            fix = fixtures_without(tmp, "uni")
            self.assertEqual(f.main(["--fixtures", str(fix), "--db", str(db), "--out", str(out)]), 0)
            con = sqlite3.connect(db)
            row = con.execute("SELECT cash, pay_date, source FROM dividend WHERE code='00757' AND ex_date='2026-11-03'").fetchone()
            self.assertEqual(row, (0.5, "2026-11-25", "統一投信官網"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM dividend WHERE code='00939'").fetchone()[0], 2)
            con.close()


class StoreTest(unittest.TestCase):
    def snapshot(self, history: Path) -> dict:
        return {str(p.relative_to(history)): p.read_text("utf-8") for p in sorted(history.rglob("*.csv"))}

    def test_rebuild_from_history(self):
        """刪掉資料庫後從 CSV 重建，內容不變；再跑一次，昨天以前的資料仍在。"""
        import store
        with tempfile.TemporaryDirectory() as tmp:
            db, out, history = Path(tmp) / "etf.db", Path(tmp) / "latest.json", Path(tmp) / "history"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            self.assertTrue((history / "daily" / "2026-10-16.csv").exists())
            self.assertTrue((history / "holdings" / "2026-10-16.csv").exists())
            first = self.snapshot(history)

            def table(path):
                con = sqlite3.connect(path)
                try:
                    return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
                            for t in ("etf", "daily", "dividend", "price_check", "holding")}
                finally:
                    con.close()
            before = table(db)
            db.unlink()
            con = store.open_db(db, history)
            con.close()
            self.assertEqual(table(db), before)  # 數字、空值、前導零代號都還原

            con = sqlite3.connect(db)
            store.dump(con, history)
            con.close()
            self.assertEqual(self.snapshot(history), first)  # 內容沒變，檔案也完全相同

    def test_holdings_keep_history(self):
        import store
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        rows = [("0050", d, "2330", "台積電", "股票", w, 1.0, "測試") for d, w in (("2026-10-15", 50.0), ("2026-10-16", 51.0))]
        con.executemany("INSERT INTO holding VALUES(?,?,?,?,?,?,?,?)", rows)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM holding").fetchone()[0], 2)

    def test_old_holding_table_upgraded(self):
        import store
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE holding (code TEXT NOT NULL, date TEXT, asset_code TEXT NOT NULL, asset_name TEXT,"
                    " kind TEXT NOT NULL, weight REAL, qty REAL, source TEXT, PRIMARY KEY (code, asset_code, kind))")
        store.ensure_schema(con)
        pk = [r[1] for r in con.execute("PRAGMA table_info(holding)") if r[5]]
        self.assertIn("date", pk)


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
            fix = fixtures_without(tmp, "yuanta")
            self.assertEqual(f.main(["--fixtures", str(fix), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertAlmostEqual(items["0050"]["aum"], 1e9 * 52.25 / 1e8)    # 上市：基金基本資料 × 預估淨值
            self.assertAlmostEqual(items["00679B"]["aum"], 6e9 * 28.40 / 1e8)  # 上櫃：Capitals × 預估淨值
            self.assertEqual(items["0056"]["next_ex"],
                             {"ex_date": "2026-10-20", "cash": 0.866, "pay_date": None, "source": "交易所除權息預告"})
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
            fix = fixtures_without(tmp, "yuanta")
            self.assertEqual(f.main(["--fixtures", str(fix), "--db", str(db), "--out", str(out)]), 0)
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
            fix = fixtures_without(tmp, "yuanta")
            self.assertEqual(f.main(["--fixtures", str(fix), "--db", str(db), "--out", str(out)]), 0)

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
