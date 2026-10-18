"""離線測試：用 tests/fixtures 的假資料驗證解析、分類、合併與寫入。執行：python -m unittest -v"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
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
            self.assertEqual(a["units_change"], 85000000)  # 當日申購買回增減單位數
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
            self.assertEqual(a["listed_date"], "2016-09-06")  # 上市日取自投信官網（交易所測試資料沒有這檔）
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


class CapitalTest(unittest.TestCase):
    def test_parse(self):
        from issuers import capital as c
        self.assertEqual(c.futures_code("TE202610", "電子期202610"), "TE-202610")
        self.assertEqual(c.futures_code("FTSE China A50 Index Futures202610", None), "FTSEChinaA50IndexFutures-202610")
        bb = c.parse_buyback(c.unwrap(json.loads((FIX / "capital" / "buyback_195.json").read_text("utf-8"))))
        self.assertEqual((bb["nav_official"], bb["nav_date"], bb["aum"], bb["units_change"]), (32.95, "2026-10-16", 6384.48, -500000.0))
        self.assertEqual([h["asset_code"] for h in bb["holdings"]], ["2881", "2882", "TX-202610"])
        d = c.parse_detail(c.unwrap(json.loads((FIX / "capital" / "detail_366.json").read_text("utf-8"))))
        self.assertIsNone(d["mgmt_fee"])  # 分段級距不換算
        self.assertEqual(d["manager"], "甲、乙")
        divs = c.parse_dividends(c.unwrap(json.loads((FIX / "capital" / "dividends.json").read_text("utf-8"))))
        self.assertEqual(divs[0], {"code": "00919", "ex_date": "2026-09-16", "pay_date": "2026-10-15", "cash": 1.1})
        self.assertIsNone(c.unwrap({"code": 500, "data": None}))

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔群益 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00919", "b": "群益台灣精選高息", "e": "32.74", "f": "32.90", "g": "", "h": "32.42", "i": "20261016", "j": "13:30:00"},
            {"a": "00927", "b": "群益半導體收益", "e": "38.22", "f": "37.95", "g": "", "h": "37.72", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Code": "00919", "Name": "群益台灣精選高息", "TradeVolume": "50,000,000", "ClosingPrice": "32.74", "Change": "-0.1000"},
                 {"Code": "00927", "Name": "群益半導體收益", "TradeVolume": "9,000,000", "ClosingPrice": "38.22", "Change": "0.3000"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00919"]
            self.assertEqual((a["nav"], a["nav_source"]), (32.95, "群益投信官網（正式淨值）"))
            self.assertAlmostEqual(a["premium"], (32.74 - 32.95) / 32.95 * 100, places=3)
            self.assertEqual((a["aum"], a["units_change"], a["mgmt_fee"]), (6384.48, -500000.0, 0.3))
            self.assertEqual((a["listed_date"], a["index_name"], a["manager"]), ("2022-10-20", "臺灣精選高息指數", "測試經理人"))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-09-16", "cash": 1.1, "pay_date": "2026-10-15", "source": "群益投信官網"})
            b = items["00927"]
            # 正式淨值是 10/15 的，當天改用群益公布的預估淨值，不用證交所轉載的 37.95
            self.assertEqual((b["nav"], b["nav_source"]), (37.98, "群益投信官網（預估淨值）"))
            self.assertIsNone(b["units_change"])
            self.assertIsNone(b["mgmt_fee"])
            self.assertIn("0.4%", b["mgmt_fee_text"])
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT units_change FROM daily WHERE code='00919'").fetchone()[0], -500000.0)
            self.assertEqual(con.execute("SELECT pay_date FROM dividend WHERE code='00765B'").fetchone()[0], "2023-04-12")  # 已下市也保留
            con.close()


class FuhHwaTest(unittest.TestCase):
    def test_parse(self):
        from datetime import date, timedelta
        from issuers import fuhhwa as fh
        info = fh.parse_fund(fh.result(json.loads((FIX / "fuhhwa" / "fund_ETF21.json").read_text("utf-8"))))
        self.assertEqual((info["nav_official"], info["nav_date"], info["risk_class"]), (29.75, "2026-10-16", "RR5"))
        self.assertIsNone(info["mgmt_fee"])  # 「0.30%~0.35%」是區間，不換算
        a = fh.parse_assets(fh.result(json.loads((FIX / "fuhhwa" / "assets_ETF21.json").read_text("utf-8"))))
        self.assertEqual((a["assets_date"], a["aum"], a["units"]), ("2026-10-16", 1626.77, 5468639000.0))
        self.assertEqual(a["holdings"][0], {"asset_code": "2357", "asset_name": "華碩電腦", "kind": "股票",
                                            "weight": 4.019, "qty": 6577000.0})
        # 申購買回清單回傳資料日期，主流程只採用日期等於正式淨值日期的那份
        self.assertEqual(fh.parse_pcf([{"postDate": "2026/10/19", "actBuyValDate": "2026/10/16", "qDiff": "23000000.0"}]),
                         ("2026-10-16", 23000000.0))
        self.assertEqual(fh.parse_pcf([]), (None, None))
        self.assertFalse(fh.parse_fund({"ec040": "0"})["distributes"])  # 不配息
        self.assertFalse(fh.parse_fund({"ec040": None})["distributes"])
        self.assertTrue(fh.parse_fund({"ec040": "每月底"})["distributes"])
        # 配息查詢一次最多兩年：從上市日回補要切段
        w = fh.dividend_windows(date(2023, 6, 9), date(2026, 10, 18))
        self.assertEqual(w[0][0], date(2023, 6, 9))
        self.assertEqual(w[-1][1], date(2026, 10, 18))
        self.assertTrue(all(e - s <= timedelta(days=fh.DIVIDEND_WINDOW_DAYS) for s, e in w))
        self.assertTrue(all(w[i + 1][0] == w[i][1] + timedelta(days=1) for i in range(len(w) - 1)))  # 不漏日子

    def test_fetch_backfill_only_once(self):
        from datetime import date
        from issuers import fuhhwa as fh
        calls = []
        # 用測試資料跑兩次：沒有紀錄時從上市日回補（多段），已有紀錄時只查最近一年（一段）
        orig = fh.dividend_windows
        fh.dividend_windows = lambda s, e: calls.append((s, e)) or orig(s, e)
        try:
            fh.fetch(fixtures=FIX / "fuhhwa", today=date(2026, 10, 18))
            full = list(calls); calls.clear()
            r = fh.fetch(fixtures=FIX / "fuhhwa", today=date(2026, 10, 18), dividend_codes={"00929", "00768B"})
        finally:
            fh.dividend_windows = orig
        self.assertEqual(full, [(date(2023, 6, 9), date(2026, 10, 18))])  # 00929 從上市日起；00768B 不配息不查
        self.assertEqual([c[0] for c in calls], [date(2025, 10, 18)])  # 已有紀錄：只補最近一年
        self.assertEqual(len(r["00929"]["dividends"]), 2)
        # 債券型：10/16 的清單是 10/14 的資料，要往後找到 10/19 那份（10/15）
        self.assertEqual(r["00768B"]["units_change"], -1000000.0)

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔復華 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00929", "b": "復華台灣科技優息", "e": "29.90", "f": "29.80", "g": "", "h": "29.55", "i": "20261016", "j": "13:30:00"},
            {"a": "00768B", "b": "復華20年美債", "e": "47.95", "f": "47.87", "g": "", "h": "47.80", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Code": "00929", "Name": "復華台灣科技優息", "TradeVolume": "80,000,000", "ClosingPrice": "29.90", "Change": "0.1500"},
                 {"Code": "00768B", "Name": "復華20年美債", "TradeVolume": "3,000,000", "ClosingPrice": "47.95", "Change": "0.0500"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00929"]
            self.assertEqual((a["nav"], a["nav_source"]), (29.75, "復華投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["risk_class"]), (1626.77, 23000000.0, "RR5"))
            self.assertEqual((a["mgmt_fee"], a["mgmt_fee_text"], a["listed_date"]), (None, "0.30%~0.35%", "2023-06-09"))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-09-17", "cash": 0.38, "pay_date": "2026-10-16", "source": "復華投信官網"})
            self.assertEqual(a["holdings_top"][0]["asset_name"], "華碩電腦")
            b = items["00768B"]
            # 正式淨值是 10/15 的，當天用復華公布的預估淨值
            self.assertEqual((b["nav"], b["nav_source"]), (47.87, "復華投信官網（預估淨值）"))
            self.assertEqual((b["aum"], b["mgmt_fee"]), (378.09, 0.15))
            self.assertIsNone(b["units_change"])  # 單位數增減屬於 10/15，不放在 10/16 那一列
            self.assertEqual(b["next_ex"], None)
            con = sqlite3.connect(db)
            # 債券型：10/16 的清單裝 10/14 的資料，要往後找到 10/19 那份（10/15）才對
            self.assertEqual(con.execute("SELECT COUNT(*) FROM dividend WHERE code='00768B'").fetchone()[0], 0)  # 測試資料設為不配息
            con.close()


class KgiTest(unittest.TestCase):
    def test_parse(self):
        from issuers import kgi as k
        page = lambda n: (FIX / "kgi" / f"{n}.html").read_text("utf-8")
        self.assertEqual(k.parse_nav_list(page("nav")),
                         {"J023": {"nav_official": 16.15, "nav_date": "2026-10-16"},
                          "J018": {"nav_official": 14.4736, "nav_date": "2026-10-15"}})
        d = k.parse_detail(page("detail_J018"))
        self.assertEqual((d["code"], d["list_date"], d["risk_class"], d["manager"]), ("00945B", "2023-11-02", "RR3", "債券經理人"))
        self.assertIsNone(d["mgmt_fee"])  # 分段級距不換算
        self.assertEqual(k.parse_detail(page("detail_J023"))["mgmt_fee"], 0.15)
        p = k.parse_pcf(page("pcf_J023"))
        self.assertEqual((p["code"], p["pcf_date"], p["aum"], p["units_change"]), ("009816", "2026-10-16", 2092.5, 116000000.0))
        self.assertEqual([h["asset_code"] for h in p["holdings"]], ["2330", "2454", "TX-202611"])
        b = k.parse_pcf(page("pcf_J018"))
        self.assertEqual((b["pcf_date"], b["units_change"], b["holdings"][0]["kind"]), ("2026-10-15", -2000000.0, "債券"))
        self.assertEqual(k.parse_pcf("<html>查無資料</html>"), {})
        divs = k.parse_dividends(page("dividends"))
        self.assertEqual(divs[0], {"code": "00945B", "ex_date": "2026-10-16", "pay_date": "2026-10-22", "cash": 0.115})
        self.assertEqual({x["code"] for x in divs}, {"00945B", "00952"})

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔凱基 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "009816", "b": "凱基台灣TOP50", "e": "16.20", "f": "16.14", "g": "", "h": "16.11", "i": "20261016", "j": "13:30:00"},
            {"a": "00945B", "b": "凱基美國非投等債", "e": "14.50", "f": "14.46", "g": "", "h": "14.32", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Code": "009816", "Name": "凱基台灣TOP50", "TradeVolume": "60,000,000", "ClosingPrice": "16.20", "Change": "0.0500"},
                 {"Code": "00945B", "Name": "凱基美國非投等債", "TradeVolume": "4,000,000", "ClosingPrice": "14.50", "Change": "0.0800"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["009816"]
            self.assertEqual((a["nav"], a["nav_source"]), (16.15, "凱基投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["mgmt_fee"], a["risk_class"]), (2092.5, 116000000.0, 0.15, "RR4"))
            self.assertEqual(a["holdings_top"][0]["asset_name"], "台積電")
            b = items["00945B"]
            self.assertTrue(b["nav_source"].startswith("證交所"))  # 正式淨值是 10/15 的，凱基沒有公布預估淨值
            self.assertIsNone(b["units_change"])  # 清單資料是 10/15 的，不放在 10/16
            self.assertEqual(b["last_ex"], {"ex_date": "2026-10-16", "cash": 0.115, "pay_date": "2026-10-22", "source": "凱基投信官網"})
            con = sqlite3.connect(db)
            # 持債依清單日期（10/15）保存，不是最新淨值日期
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00945B'").fetchall(), [("2026-10-15",)])
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='009816'").fetchall(), [("2026-10-16",)])
            con.close()


class FubonTest(unittest.TestCase):
    def test_parse(self):
        from issuers import fubon as fb
        page = lambda n: (FIX / "fubon" / f"{n}.html").read_text("utf-8")
        self.assertEqual(fb.parse_codes(page("list")), ["006208", "00696B"])  # 語言切換連結不算
        self.assertEqual(fb.parse_estimates(page("estimate")),
                         {"006208": {"nav_estimate": 150.3, "nav_estimate_date": "2026-10-16"},
                          "00696B": {"nav_estimate": 30.6, "nav_estimate_date": "2026-10-16"}})
        p = fb.parse_profile(page("profile_006208"))
        self.assertEqual((p["list_date"], p["risk_class"], p["index_name"], p["currency"]), ("2012-07-17", "RR4", "臺灣50指數", "新台幣"))
        self.assertIsNone(p["mgmt_fee"])  # 分段級距不換算
        self.assertEqual(p["mgmt_fee_text"], "0億(含)以上 : 0.15%；1000億(不含)以上 : 0.1%")
        self.assertEqual(fb.parse_profile(page("profile_00696B"))["mgmt_fee"], 0.12)  # 只有一段就是單一費率
        a = fb.parse_assets(page("assets_006208"))
        self.assertEqual((a["nav_date"], a["nav_official"], a["aum"], a["units"]), ("2026-10-16", 150.25, 3005.0, 2000000000.0))
        self.assertEqual([(h["asset_code"], h["kind"]) for h in a["holdings"]], [("WTXV6F", "期貨"), ("2317", "股票"), ("2330", "股票")])
        b = fb.parse_assets(page("assets_00696B"))
        self.assertEqual((b["nav_date"], b["holdings"][0]["kind"], b["holdings"][0]["qty"]), ("2026-10-15", "債券", 20000000.0))
        self.assertEqual(fb.parse_assets("<html>查無資料</html>"), {})
        # 淨值日期取自總價金差異額的欄位名稱，不是「預估發行受益權單位數」的日期
        self.assertEqual(fb.parse_pcf(page("pcf_00696B")),
                         {"date": "2026-10-15", "nav": 30.5, "units": 500000000.0, "units_change": -1000000.0,
                          "beneficiaries": 12000.0, "beneficiaries_month": "2026-09"})
        divs = fb.parse_dividends(page("dividends_all"))
        self.assertEqual(divs[0], {"code": "00696B", "ex_date": "2026-09-16", "pay_date": "2026-10-14", "cash": 0.35})
        self.assertEqual(len(divs), 4)
        q = fb.dividend_query(page("dividends"), date(2006, 1, 1), date(2027, 12, 31))
        pre = "ctl00$ctl00$mainContent$subMainContent$"
        self.assertEqual((q["__VIEWSTATE"], q[pre + "contact"], q[pre + "sDate"], q[pre + "ddlRange"]),
                         ("dGVzdC12aWV3c3RhdGU=", "2", "2006/01/01", "2026"))  # 年度只能送頁面上有的選項
        self.assertIsNone(fb.dividend_query("<html></html>", date(2006, 1, 1), date(2026, 12, 31)))

    def test_fetch_dividends_and_pcf_date(self):
        from issuers import fubon as fb
        recs = fb.fetch(fixtures=FIX / "fubon", dividend_codes=set())
        self.assertEqual(sorted((d["code"], d["ex_date"]) for r in recs.values() for d in r["dividends"]),
                         [("006208", "2025-11-18"), ("006208", "2026-07-16"), ("00696B", "2017-12-18"), ("00696B", "2026-09-16")])
        # 資料庫已有富邦紀錄時只看預設頁（今年）
        recs = fb.fetch(fixtures=FIX / "fubon", dividend_codes={"006208"})
        self.assertEqual(sorted(d["ex_date"] for d in recs["006208"]["dividends"]), ["2026-07-16"])
        self.assertEqual(recs["00696B"]["units_change"], -1000000.0)
        with tempfile.TemporaryDirectory() as tmp:
            fix = Path(tmp) / "fubon"
            shutil.copytree(FIX / "fubon", fix)
            # 完整歷史查詢失敗（回到預設頁）就整批不用，下次再查
            shutil.copy(fix / "dividends.html", fix / "dividends_all.html")
            # 申購買回清單和基金資產不是同一天，單位增減不採用
            pcf = fix / "pcf_00696B.html"
            pcf.write_text(pcf.read_text("utf-8").replace("2026/10/15 每", "2026/10/14 每"), "utf-8")
            # 網站改顯示別檔基金（標題代號不同）時不採用該頁
            assets = fix / "assets_006208.html"
            assets.write_text(assets.read_text("utf-8").replace("006208 / 富邦台50", "00692 / 富邦公司治理"), "utf-8")
            recs = fb.fetch(fixtures=fix, dividend_codes=set())
            self.assertEqual([d for r in recs.values() for d in r["dividends"]], [])
            self.assertIsNone(recs["00696B"]["units_change"])
            self.assertEqual((recs["006208"]["nav_official"], recs["006208"]["holdings"], recs["006208"]["risk_class"]), (None, [], "RR4"))

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔富邦 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "006208", "b": "富邦台50", "e": "150.60", "f": "150.28", "g": "", "h": "149.80", "i": "20261016", "j": "13:30:00"},
            {"a": "00696B", "b": "富邦美債20年", "e": "30.70", "f": "30.58", "g": "", "h": "30.50", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Code": "006208", "Name": "富邦台50", "TradeVolume": "5,000,000", "ClosingPrice": "150.60", "Change": "0.6000"},
                 {"Code": "00696B", "Name": "富邦美債20年", "TradeVolume": "3,000,000", "ClosingPrice": "30.70", "Change": "0.1500"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["006208"]
            self.assertEqual((a["issuer"], a["nav"], a["nav_source"]), ("富邦", 150.25, "富邦投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["risk_class"], a["listed_date"]), (3005.0, 4000000.0, "RR4", "2012-07-17"))
            self.assertEqual((a["beneficiaries"], a["beneficiaries_month"]), (500000.0, "2026-09"))
            self.assertAlmostEqual(a["premium"], (150.60 - 150.25) / 150.25 * 100, places=3)
            self.assertEqual(a["holdings_top"][0]["asset_name"], "台積電")
            self.assertEqual(a["last_ex"], {"ex_date": "2026-07-16", "cash": 1.0, "pay_date": "2026-08-12", "source": "富邦投信官網"})
            b = items["00696B"]
            # 正式淨值是 10/15 的，當天用富邦公布的預估淨值；單位增減屬於 10/15，不放在 10/16
            self.assertEqual((b["nav"], b["nav_source"], b["units_change"], b["mgmt_fee"]), (30.6, "富邦投信官網（預估淨值）", None, 0.12))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00696B'").fetchall(), [("2026-10-15",)])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM dividend WHERE source='富邦投信官網'").fetchone()[0], 4)
            con.close()


class CathayTest(unittest.TestCase):
    def test_parse(self):
        from issuers import cathay as c
        load = lambda n: c.unwrap(json.loads((FIX / "cathay" / f"{n}.json").read_text("utf-8")), n)
        # 外幣計價的預估淨值不收
        self.assertEqual(c.parse_estimates(load("estimate")),
                         {"00878": {"nav_estimate": 25.02, "nav_estimate_date": "2026-10-16"},
                          "00687B": {"nav_estimate": 30.2, "nav_estimate_date": "2026-10-16"}})
        self.assertEqual(c.distributing([{"fundCode": "CN", "isAllot": True, "allotPeriod": "季配", "allotYearMonth": "2026/07"},
                                         {"fundCode": "87", "isAllot": False, "allotPeriod": "累積型"}]), {"CN": "2026/07"})
        # 最近一次配息超過兩個月就不必每天重查
        self.assertEqual([c.recent(m, date(2026, 10, 18)) for m in ("2026/08", "2026/07", None)], [True, False, True])
        self.assertIsNone(c.unwrap({"returnCode": "4005", "returnMessage": "查無資料", "result": None}))
        p = c.parse_pcf(load("pcf_CN"))
        self.assertEqual((p["nav_date"], p["nav_official"], p["aum"], p["units_change"], p["beneficiaries_month"]),
                         ("2026-10-16", 25.0, 5000.0, 2000000.0, "2026-09"))
        usd = c.parse_pcf(load("pcf_DY"))
        self.assertEqual((usd["nav_official"], usd["aum"], usd["units"]), (None, None, 2000000.0))  # 美元計價不換算
        # 「期貨（名目本金）」查期貨明細，「附買回債券」不是債券成分
        self.assertEqual(c.detail_kinds(load("bal_CN")), ["股票", "期貨"])
        self.assertEqual(c.detail_kinds([{"item": "附買回債券"}, {"item": "現金"}]), [])
        self.assertEqual(c.parse_holdings("期貨", load("GetETFDetailFutureList_CN")),
                         [{"asset_code": "TX-202610", "asset_name": "台股期貨", "kind": "期貨", "weight": 3.0, "qty": 1000.0}])
        fund = c.parse_holdings("基金", [{"fnNo": "80907786", "fnName": "貨幣市場基金", "ntMkval": "9,031,629"}])
        self.assertEqual((fund[0]["kind"], fund[0]["weight"], fund[0]["qty"]), ("基金", None, 9031629.0))  # 網站只列持有單位數
        self.assertEqual(c.parse_dividends("00878", load("dividends_CN"))[0],
                         {"code": "00878", "ex_date": "2026-08-18", "pay_date": "2026-09-11", "cash": 0.8})
        with self.assertRaises(RuntimeError):
            c.unwrap({"returnCode": "5000", "returnMessage": "系統錯誤", "result": None}, "api/ETF/GetIndexInfo")

    def test_fetch(self):
        from issuers import cathay as c
        recs = c.fetch(fixtures=FIX / "cathay", dividend_codes=set())
        a = recs["00878"]
        self.assertEqual((a["manager"], a["list_date"], a["index_name"], a["custodian_fee_text"]),
                         ("測試經理人甲", "2020-07-20", "MSCI臺灣ESG永續高股息精選30指數", "0.03%"))
        self.assertIsNone(a["mgmt_fee"])  # 分段費率不換算
        self.assertEqual([h["asset_code"] for h in a["holdings"]], ["2330", "2891", "TX-202610"])
        self.assertEqual(recs["00687B"]["mgmt_fee"], 0.15)
        usd = recs["00687C"]
        self.assertEqual((usd["nav_official"], usd["nav_estimate"], usd["aum"], usd["dividends"]), (None, None, None, []))

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔國泰 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00878", "b": "國泰永續高股息", "e": "25.10", "f": "25.02", "g": "", "h": "24.90", "i": "20261016", "j": "13:30:00"},
            {"a": "00687B", "b": "國泰20年美債", "e": "30.30", "f": "30.21", "g": "", "h": "30.00", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Code": "00878", "Name": "國泰永續高股息", "TradeVolume": "50,000,000", "ClosingPrice": "25.10", "Change": "0.2000"},
                 {"Code": "00687B", "Name": "國泰20年美債", "TradeVolume": "8,000,000", "ClosingPrice": "30.30", "Change": "0.1000"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00878"]
            self.assertEqual((a["issuer"], a["nav"], a["nav_source"]), ("國泰", 25.0, "國泰投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["aum_source"], a["units_change"], a["beneficiaries"]), (5000.0, "國泰投信官網", 2000000.0, 1500000.0))
            self.assertAlmostEqual(a["premium"], 0.4, places=4)
            self.assertEqual(a["holdings_top"][0]["asset_name"], "中信金")
            self.assertEqual(a["last_ex"], {"ex_date": "2026-08-18", "cash": 0.8, "pay_date": "2026-09-11", "source": "國泰投信官網"})
            b = items["00687B"]
            # 正式淨值是 10/15 的，當天用國泰公布的預估淨值；單位增減屬於 10/15
            self.assertEqual((b["nav"], b["nav_source"], b["units_change"]), (30.2, "國泰投信官網（預估淨值）", None))
            self.assertEqual(b["last_ex"]["ex_date"], "2026-10-16")
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00687B'").fetchall(), [("2026-10-15",)])
            con.close()


class CtbcTest(unittest.TestCase):
    def test_parse(self):
        from issuers import ctbc as c
        load = lambda n: c.unwrap(json.loads((FIX / "ctbc" / f"{n}.json").read_text("utf-8")), n)
        funds = c.parse_list(load("list"))
        self.assertEqual([(x["code"], x["fid"], x["nav_date"]) for x in funds], [("00891", "E0017", "2026-10-16"), ("00772B", "E0003", "2026-10-15")])
        self.assertEqual(c.parse_estimates(load("inav"))["00772B"], {"nav_estimate": 32.6, "nav_estimate_date": "2026-10-16"})
        d = c.parse_detail(load("detail_74707446"), "00772B")
        self.assertEqual((d["index_name"], d["list_date"], d["mgmt_fee"]), ("彭博測試公司債指數", "2019-01-29", None))  # 英文指數名稱不收、分段費率不換算
        self.assertEqual(d["dividends"], [{"code": "00772B", "ex_date": "2026-09-16", "pay_date": "2026-10-15", "cash": 0.14}])
        p = c.parse_pcf(load("pcf_E0017"))
        self.assertEqual((p["pcf_date"], p["aum"], p["units_change"], p["beneficiaries_month"]), ("2026-10-16", 402.0, 5000000.0, "2026-09"))
        # 現金、保證金不是成分；期貨代號加上契約年月
        self.assertEqual([(h["asset_code"], h["kind"]) for h in p["holdings"]], [("2330", "股票"), ("2454", "股票"), ("TE-202610", "期貨")])
        self.assertEqual(c.parse_pcf({"Data": [{}], "Detail": []}), {})  # 查非交易日時清單是空的
        with self.assertRaises(RuntimeError):
            c.unwrap({"ResultCode": 1, "ResultMsg": "E404-查無指定的基金"}, "etf/ETFDetail")

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔中信 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00891", "b": "中信關鍵半導體", "e": "20.20", "f": "20.12", "g": "", "h": "20.00", "i": "20261016", "j": "13:30:00"},
            {"a": "00772B", "b": "中信高評級公司債", "e": "32.70", "f": "32.60", "g": "", "h": "32.50", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Code": "00891", "Name": "中信關鍵半導體", "TradeVolume": "30,000,000", "ClosingPrice": "20.20", "Change": "0.1000"},
                 {"Code": "00772B", "Name": "中信高評級公司債", "TradeVolume": "2,000,000", "ClosingPrice": "32.70", "Change": "0.1000"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00891"]
            self.assertEqual((a["issuer"], a["nav"], a["nav_source"]), ("中信", 20.1, "中信投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["beneficiaries"], a["fee_source"]), (402.0, 5000000.0, 150000.0, "中信投信官網"))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-08-18", "cash": 1.0, "pay_date": "2026-09-11", "source": "中信投信官網"})
            b = items["00772B"]
            # 正式淨值與清單都是 10/15 的，當天用中信公布的預估淨值；單位增減屬於 10/15
            self.assertEqual((b["nav"], b["nav_source"], b["units_change"]), (32.6, "中信投信官網（預估淨值）", None))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00772B'").fetchall(), [("2026-10-15",)])
            con.close()


class ReturnsTest(unittest.TestCase):
    def history(self):
        import returns
        return returns.parse_history(json.loads((FIX / "yahoo_history.json").read_text("utf-8")))

    def test_start_dates(self):
        import returns
        self.assertEqual(returns.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))  # 該月沒有 31 日
        self.assertEqual(returns.add_months(date(2024, 3, 31), -1), date(2024, 2, 29))
        self.assertEqual(returns.start_dates("2026-10-16"),
                         {"1w": "2026-10-09", "1m": "2026-09-16", "3m": "2026-07-16", "6m": "2026-04-16",
                          "ytd": "2025-12-31", "1y": "2025-10-16", "3y": "2023-10-16", "5y": "2021-10-16"})

    def test_compute(self):
        import returns
        h = self.history()
        r = returns.compute(h["0050"], "2026-10-16", 52.35, [("2026-07-21", 1.0)])
        # 期初取起始日當天或之前最近的交易日：近一週的起始日 10/09 沒有資料，用 10/08
        self.assertEqual(r["price"]["1w"], round((52.35 / 51.5 - 1) * 100, 2))
        self.assertEqual((r["price"]["1y"], r["total"]["1y"]), (round((52.35 / 45 - 1) * 100, 2), round((52.35 / 42 - 1) * 100, 2)))
        self.assertEqual(r["total"]["5y"], round((52.35 / 25 - 1) * 100, 2))
        # 期末用查核後的收盤價
        self.assertEqual(returns.compute(h["0050"], "2026-10-16", 52.0)["price"]["1m"], round((52.0 / 50.5 - 1) * 100, 2))
        # 資料庫有、Yahoo 沒列到的配息：涵蓋那天的區間不算含息報酬，市價報酬照算
        miss = returns.compute(h["0050"], "2026-10-16", 52.35, [("2026-08-20", 0.5)])
        self.assertEqual((miss["total"]["1m"] is None, miss["total"]["3m"], miss["price"]["3m"] is None), (False, None, False))
        # 配息金額不符也一樣
        self.assertIsNone(returns.compute(h["0050"], "2026-10-16", 52.35, [("2026-07-21", 1.2)])["total"]["6m"])
        # 分割前的配息：Yahoo 依分割比例調整過金額（1 拆 7），整數倍視為同一筆
        self.assertTrue(returns.listed_in_yahoo([("2022-04-20", 1.455714)], "2022-04-20", 10.19))
        self.assertFalse(returns.listed_in_yahoo([("2022-07-18", 0.22)], "2022-07-18", 0.3))
        # 上市不到 3 年就不算 3 年、5 年報酬
        b = returns.compute(h["00679B"], "2026-10-16", 28.0)
        self.assertEqual((b["price"]["3y"], b["total"]["5y"]), (None, None))
        self.assertIsNone(returns.compute(h["00679B"], "2026-10-19", 28.0))  # Yahoo 沒有期末那天

    def test_previous(self):
        import returns
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "latest.json"
            out.write_text(json.dumps({"trade_date": "2026-10-16", "source": "live",
                                       "items": [{"code": "0050", "returns": {"price": {"1m": 1.0}}}, {"code": "0056", "returns": None}]}), "utf-8")
            self.assertEqual(returns.previous(out, "2026-10-16", "live"), {"0050": {"price": {"1m": 1.0}}})
            self.assertEqual(returns.previous(out, "2026-10-19", "live"), {})  # 換了交易日要重算
            self.assertEqual(returns.previous(out, "2026-10-16", "fixture"), {})
            self.assertEqual(returns.previous(Path(tmp) / "none.json", "2026-10-16", "live"), {})

    def test_pipeline_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            fix = fixtures_without(tmp)
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            args = ["--fixtures", str(fix), "--db", str(db), "--out", str(out)]
            self.assertEqual(f.main(args), 0)
            payload = json.loads(out.read_text("utf-8"))
            self.assertEqual(payload["returns_info"]["starts"]["1y"], "2025-10-16")
            items = {i["code"]: i for i in payload["items"]}
            self.assertEqual(items["0050"]["returns"]["price"]["1y"], round((52.35 / 45 - 1) * 100, 2))
            self.assertIsNone(items["0056"]["returns"])  # Yahoo 沒有這檔的日線
            # 同一交易日再跑：不再需要五年日線，沿用上次的報酬
            (fix / "yahoo_history.json").unlink()
            self.assertEqual(f.main(args), 0)
            again = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertEqual(again["0050"]["returns"], items["0050"]["returns"])


class SitcaTest(unittest.TestCase):
    def test_parse(self):
        import sitca
        page = lambda n: (FIX / "sitca" / f"{n}.html").read_text("utf-8")
        # 多列表頭展開：跨兩欄的「封閉式基金」下面是上市日期、下市日期
        self.assertIn("封閉式基金/上市日期", sitca.table(page("basic_std"))[0])
        funds = sitca.parse_basic(page("basic_std"), page("basic_fee"))
        self.assertEqual(sorted(funds), ["0050", "0056", "00757"])  # 一般共同基金（受益憑證代號 T0102Y）不收
        self.assertEqual((funds["0056"]["mgmt_fee_text"], funds["0056"]["custodian_fee_text"], funds["0056"]["inception"]),
                         ("0.40%", "0.035%", "2007-12-13"))
        ratios = sitca.parse_ratios(page("ratio_2025"))
        self.assertEqual(ratios["48966213"], {"mgmt": 0.3, "custodian": 0.04, "other": 0.05, "total": 0.58})
        self.assertEqual(sitca.selected(page("basic_std"), "ddlQ_YYYYMM"), "202609")
        info = sitca.fetch(FIX / "sitca", today=date(2026, 10, 18))
        self.assertEqual((info["month"], info["year"]), ("2026-09", 2025))
        self.assertIsNone(info["funds"]["00757"]["expense"])  # 2025 年才成立，全年比率只涵蓋幾個月
        self.assertEqual(info["funds"]["0050"]["expense"]["total"], 0.22)

    def test_apply(self):
        info = {"month": "2026-09", "funds": {
            "00636": {"fund_id": "1", "name": "國泰中國A50", "inception": "2015-05-01", "currency": "新台幣", "manager": "甲",
                      "risk_class": "RR5", "mgmt_fee_text": "0.40%", "custodian_fee_text": "0.10%",
                      "expense": {"year": 2025, "mgmt": 0.4, "custodian": 0.1, "other": 0.05, "total": 0.6}}}}
        base = {"mgmt_fee": None, "mgmt_fee_text": None, "custodian_fee_text": None, "manager": None, "risk_class": None,
                "fee": None, "fee_source": None}
        own = {**base, "code": "00636", "mgmt_fee_text": "官網原文", "fee_source": "國泰投信官網", "fee": 0.5}
        k_line = {**base, "code": "00636K"}
        f.apply_fund_info([own, k_line], info)
        # 投信官網已有的費率不覆蓋，人工填的總費用率也不覆蓋；年度費用比率照樣附上
        self.assertEqual((own["mgmt_fee_text"], own["fee_source"], own["fee"], own["expense"]["total"]), ("官網原文", "國泰投信官網", 0.5, 0.6))
        # 外幣交易線沿用同一檔基金的公會資料
        self.assertEqual((k_line["mgmt_fee"], k_line["custodian_fee_text"], k_line["fee"], k_line["manager"]), (0.4, "0.10%", 0.6, "甲"))
        self.assertEqual(k_line["fee_source"], "投信投顧公會基金基本資料（2026-09）")

    def test_pipeline_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fix = fixtures_without(tmp)
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            args = ["--fixtures", str(fix), "--db", str(db), "--out", str(out)]
            self.assertEqual(f.main(args), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["0056"]  # 沒有接入投信官網資料的 ETF
            self.assertEqual((a["mgmt_fee"], a["mgmt_fee_text"], a["custodian_fee_text"], a["manager"]), (0.4, "0.40%", "0.035%", "公會經理人乙"))
            self.assertEqual(a["fee_source"], "投信投顧公會基金基本資料（2026-09）")
            self.assertEqual((a["fee"], a["expense"]["mgmt"], a["expense"]["source"]), (0.58, 0.3, "投信投顧公會各項費用比率（2025 年度）"))
            self.assertIsNone(items["00757"]["expense"])
            # 一週內再跑：不重抓公會資料，沿用資料庫裡的
            shutil.rmtree(fix / "sitca")
            self.assertEqual(f.main(args), 0)
            again = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertEqual((again["0056"]["mgmt_fee_text"], again["0056"]["fee"]), ("0.40%", 0.58))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*), MAX(data_month) FROM fund_info").fetchone(), (3, "2026-09"))
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
