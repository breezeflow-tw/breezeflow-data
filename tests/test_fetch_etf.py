"""離線測試：用 tests/fixtures 的假資料驗證解析、分類、合併與寫入。執行：python -m unittest -v"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_etf as f  # noqa: E402
import store  # noqa: E402

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

    def test_trade_date(self):
        TW = timezone(timedelta(hours=8))
        evening, morning = datetime(2026, 10, 19, 18, 23, tzinfo=TW), datetime(2026, 10, 20, 9, 20, tzinfo=TW)
        calendar = f.market_calendar.Calendar({}, {2026})
        stale = {"0050": {"date": "2026-10-16"}, "0056": {"date": "2026-10-16"}, "00679B": {"date": "2026-10-19"}}
        # 傍晚：證交所開放資料還停在上週五（櫃買已是當天），MIS 已有當天市價：交易日換到當天
        self.assertEqual(f.trade_date({"0050": {"date": "2026-10-19", "price": 20}}, stale, evening, calendar), "2026-10-19")
        # MIS 抓不到時用交易所最多檔的日期，不因一檔較新的資料推進
        self.assertEqual(f.trade_date({}, stale, evening, calendar), "2026-10-16")
        # 隔天開盤後才跑：當天 MIS 的盤中資料不算，用前一個交易日
        self.assertEqual(f.trade_date({"0050": {"date": "2026-10-20", "price": 20}}, {"0050": {"date": "2026-10-19"}}, morning,
                                      calendar), "2026-10-19")
        # MIS 日期另需官方日曆與市價
        self.assertEqual(f.trade_date({"0050": {"date": "2026-10-16", "price": 20}}, {}, evening, calendar), "2026-10-16")

    def test_midnight_uses_newer_mis_and_same_day_issuer_nav(self):
        now = datetime(2026, 10, 22, 0, 30, tzinfo=f.TW)
        calendar = f.market_calendar.Calendar({}, {2026})
        nav = f.parse_nav({"a1": [{"msgArray": [{"a": "0050", "b": "測試基金", "e": "22", "f": "21.9",
                                                "h": "21.2", "i": "20261021", "j": "17:00:00"}]}]})
        exchange = {"0050": {"date": "2026-10-20", "close": 20, "name": "測試基金", "market": "上市"}}
        day = f.trade_date(nav, exchange, now, calendar)
        self.assertEqual(day, "2026-10-21")
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        con.execute("INSERT INTO exchange_daily(code, date, market, name, close, change, dataset, source_url) "
                    "VALUES('0050', '2026-10-20', '上市', '測試基金', 20, 0.1, 'd', 'u')")
        quotes = f.official_prices(con, exchange, day)
        con.close()
        items = f.merge(nav, quotes, {}, day)
        f.apply_issuer(items, {"0050": {"source": "測試投信官網", "nav_estimate": 21.8,
                                       "nav_estimate_date": "2026-10-21"}}, day)
        # 證交所當天的官方收盤行情還沒公布：不用 MIS 市價補，也不把前天收盤價貼上昨天日期，折溢價一併留空
        self.assertEqual((items[0]["price"], items[0]["premium"], items[0]["price_date"]), (None, None, None))
        self.assertEqual(items[0]["price_note"], "證交所的官方收盤行情還沒公布（通常隔天清晨更新）")
        self.assertEqual((items[0]["nav"], items[0]["nav_date"]), (21.8, "2026-10-21"))

    def test_mis_intraday_future_and_invalid_dates_are_not_candidates(self):
        now = datetime(2026, 10, 22, 13, 0, tzinfo=f.TW)
        calendar = f.market_calendar.Calendar({}, {2026})
        exchange = {"0050": {"date": "2026-10-21"}, "0056": {"date": "2026-10-22"}, "00679B": {"date": "invalid"}}
        for day in ("2026-10-22", "2026-10-23", "2026-02-30", "20261021", None):
            with self.subTest(day=day):
                nav = {"0050": {"date": day, "price": 20}}
                self.assertEqual(f.trade_date(nav, exchange, now, calendar), "2026-10-21")

    def test_mis_needs_open_calendar_day_and_price(self):
        now = datetime(2026, 10, 27, 0, 30, tzinfo=f.TW)
        calendar = f.market_calendar.Calendar({"2026-10-26": {"is_open": False}}, {2026})
        for day, cal, price in (("2026-10-26", calendar, 20), ("2026-10-25", calendar, 20),
                                ("2026-10-23", f.market_calendar.Calendar({}, set()), 20),
                                ("2026-10-23", None, 20), ("2026-10-23", calendar, None)):
            with self.subTest(day=day, calendar=cal, price=price):
                nav = {"0050": {"date": day, "price": price}}
                self.assertEqual(f.trade_date(nav, {"0050": {"date": "2026-10-22"}}, now, cal), "2026-10-22")

    def test_mis_uses_majority_date(self):
        now = datetime(2026, 10, 22, 18, 0, tzinfo=f.TW)
        nav = {"0050": {"date": "2026-10-21", "price": 20}, "0056": {"date": "2026-10-21", "price": 30},
               "00999": {"date": "2026-10-22", "price": 40}}
        self.assertEqual(f.trade_date(nav, {"0050": {"date": "2026-10-20"}}, now, f.market_calendar.Calendar({}, {2026})),
                         "2026-10-21")

    def test_missing_dates_do_not_become_today(self):
        with self.assertRaisesRegex(ValueError, "無法確認已收盤"):
            f.trade_date({}, {}, datetime(2026, 10, 22, 0, 30, tzinfo=f.TW))

    def test_zero_price_is_missing(self):
        # 沒有成交的 ETF，證交所即時資料的市價是 0；淨值尚未計算時也是 0
        n = f.parse_nav({"a1": [{"msgArray": [{"a": "00999B", "b": "測試債", "e": "0.00", "f": "0", "g": "-100.00", "h": "0", "i": "20261019", "j": "13:30:00"},
                                               {"a": "00998B", "b": "測試債二", "e": "0", "f": "23.29", "g": "-100.00", "h": "23.31", "i": "20261019", "j": "13:30:00"}]}]})
        self.assertEqual({k: n["00999B"][k] for k in ("price", "nav", "premium", "nav_prev")}, {"price": None, "nav": None, "premium": None, "nav_prev": None})
        self.assertEqual({k: n["00998B"][k] for k in ("price", "nav", "premium")}, {"price": None, "nav": 23.29, "premium": None})

    def test_excluded_code(self):
        """擁有者指示不收錄的代號：來源有資料也不進網站與資料庫，舊資料清掉。"""
        code = sorted(f.EXCLUDED)[0]
        with tempfile.TemporaryDirectory() as tmp:
            fix = fixtures_without(tmp)
            nav = json.loads((fix / "nav.json").read_text("utf-8"))
            nav["a1"].append({"msgArray": [{"a": code, "b": "不收錄", "e": "0.00", "f": "23.29", "g": "", "h": "23.31", "i": "20261016", "j": "13:30:00"}]})
            (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            con = store.open_db(db, Path(tmp) / "history")
            con.execute("INSERT INTO etf(code, name) VALUES(?, '舊資料')", (code,))
            con.execute("INSERT INTO dividend(code, ex_date, cash) VALUES(?, '2026-09-16', 0.1)", (code,))
            con.commit()
            con.close()
            self.assertEqual(f.main(["--fixtures", str(fix), "--db", str(db), "--out", str(out)]), 0)
            self.assertNotIn(code, {i["code"] for i in json.loads(out.read_text("utf-8"))["items"]})
            con = sqlite3.connect(db)
            for table in ("etf", "daily", "dividend"):
                self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table} WHERE code = ?", (code,)).fetchone()[0], 0, table)
            con.close()

    def test_official_prices(self):
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        con.executemany("INSERT INTO exchange_daily(code, date, market, name, close, change, volume_shares, dataset, source_url) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, 'd', 'u')",
                        [("0050", "2026-10-16", "上市", "元大台灣50", 52.35, 0.45, 25300000),
                         ("00631L", "2026-10-16", "上市", "元大台灣50正2", None, None, 0),
                         ("00679B", "2026-10-19", "上櫃", "元大美債20年", 28.0, -0.2, 1000000)])
        exchange = {"0050": {"market": "上市", "name": "元大台灣50", "date": "2026-10-16"},
                    "0056": {"market": "上市", "name": "元大高股息", "date": "2026-10-16"},
                    "00679B": {"market": "上櫃", "name": "元大美債20年", "date": "2026-10-19", "units": 5e8}}
        # 傍晚：櫃買已有 10/19 的官方收盤行情，證交所還停在 10/16：上市的留空並寫明原因，不混用別天的成交量
        q = f.official_prices(con, exchange, "2026-10-19")
        b = q["00679B"]
        self.assertEqual((b["close"], b["volume_lots"], b["check"], b["source"], b["units"]), (28.0, 1000, "交易所", "櫃買中心收盤行情", 5e8))
        self.assertAlmostEqual(b["change_pct"], -0.2 / 28.2 * 100)
        self.assertEqual((q["0050"]["close"], q["0050"]["volume_lots"], q["0050"]["note"]),
                         (None, None, "證交所的官方收盤行情還沒公布（通常隔天清晨更新）"))
        # 證交所公布那天：有成交的照用；沒有成交、交易所沒列出的都寫明原因
        q = f.official_prices(con, exchange, "2026-10-16")
        self.assertEqual((q["0050"]["close"], q["0050"]["volume_lots"], q["0050"]["check"]), (52.35, 25300, "交易所"))
        self.assertAlmostEqual(q["0050"]["change_pct"], 0.45 / 51.9 * 100)
        self.assertEqual((q["00631L"]["close"], q["00631L"]["volume_lots"], q["00631L"]["note"]), (None, 0, "當天沒有成交"))
        self.assertEqual((q["0056"]["close"], q["0056"]["note"]), (None, "交易所收盤行情沒有列出這檔"))
        # MIS 的市價不補收盤價，也不用來算折溢價
        mis = {"0056": {"name": "元大高股息", "price": 36.1, "nav": 36.0, "premium": 0.28, "nav_prev": 35.9, "date": "2026-10-16",
                        "source_date": "20261016", "source_time": "13:30:00"}}
        items = {it["code"]: it for it in f.merge(mis, q, {}, "2026-10-16")}
        self.assertEqual((items["0050"]["price"], items["0050"]["price_source"], items["0050"]["price_date"], items["0050"]["price_note"]),
                         (52.35, "證交所收盤行情", "2026-10-16", None))
        self.assertEqual((items["0056"]["price"], items["0056"]["premium"], items["0056"]["price_note"]),
                         (None, None, "交易所收盤行情沒有列出這檔"))
        self.assertEqual((items["00631L"]["price"], items["00631L"]["price_date"], items["00631L"]["price_note"]),
                         (None, None, "當天沒有成交"))
        con.close()

    def test_clean_name(self):
        self.assertEqual(f.clean_name("國泰標普低波高息(本基金並無保證收益及配息)"), "國泰標普低波高息")
        self.assertEqual(f.clean_name("聯邦台灣精彩50ETF證券投資信託基金"), "聯邦台灣精彩50")
        self.assertEqual(f.issuer_from_name("FT臺灣Smart"), "富蘭克林華美")  # FT 是富蘭克林華美投信（Franklin Templeton）的品牌

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
        twse += [{"Date": "1151016", "Code": "00919", "Name": "群益台灣精選高息", "TradeVolume": "50,000,000", "ClosingPrice": "32.74", "Change": "-0.1000"},
                 {"Date": "1151016", "Code": "00927", "Name": "群益半導體收益", "TradeVolume": "9,000,000", "ClosingPrice": "38.22", "Change": "0.3000"}]
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
        twse += [{"Date": "1151016", "Code": "00929", "Name": "復華台灣科技優息", "TradeVolume": "80,000,000", "ClosingPrice": "29.90", "Change": "0.1500"},
                 {"Date": "1151016", "Code": "00768B", "Name": "復華20年美債", "TradeVolume": "3,000,000", "ClosingPrice": "47.95", "Change": "0.0500"}]
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
        twse += [{"Date": "1151016", "Code": "009816", "Name": "凱基台灣TOP50", "TradeVolume": "60,000,000", "ClosingPrice": "16.20", "Change": "0.0500"},
                 {"Date": "1151016", "Code": "00945B", "Name": "凱基美國非投等債", "TradeVolume": "4,000,000", "ClosingPrice": "14.50", "Change": "0.0800"}]
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
        twse += [{"Date": "1151016", "Code": "006208", "Name": "富邦台50", "TradeVolume": "5,000,000", "ClosingPrice": "150.60", "Change": "0.6000"},
                 {"Date": "1151016", "Code": "00696B", "Name": "富邦美債20年", "TradeVolume": "3,000,000", "ClosingPrice": "30.70", "Change": "0.1500"}]
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
        twse += [{"Date": "1151016", "Code": "00878", "Name": "國泰永續高股息", "TradeVolume": "50,000,000", "ClosingPrice": "25.10", "Change": "0.2000"},
                 {"Date": "1151016", "Code": "00687B", "Name": "國泰20年美債", "TradeVolume": "8,000,000", "ClosingPrice": "30.30", "Change": "0.1000"}]
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
        twse += [{"Date": "1151016", "Code": "00891", "Name": "中信關鍵半導體", "TradeVolume": "30,000,000", "ClosingPrice": "20.20", "Change": "0.1000"},
                 {"Date": "1151016", "Code": "00772B", "Name": "中信高評級公司債", "TradeVolume": "2,000,000", "ClosingPrice": "32.70", "Change": "0.1000"}]
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


class NomuraTest(unittest.TestCase):
    def load(self, name: str):
        from issuers import nomura as n
        return n.unwrap(json.loads((FIX / "nomura" / f"{name}.json").read_text("utf-8")), name)

    def test_parse(self):
        from issuers import nomura as n
        funds = n.parse_list(self.load("list"))
        self.assertEqual([(x["code"], x["nav_official"], x["nav_date"]) for x in funds],
                         [("00935", 56.84, "2026-10-16"), ("009812", 13.2, "2026-10-16"), ("00987B", 15.5401, "2026-10-15"),
                          ("00420A", None, None)])
        self.assertEqual(n.parse_estimates(self.load("inav"))["00987B"], {"nav_estimate": 15.6529, "nav_estimate_date": "2026-10-16"})
        d = n.parse_detail(self.load("detail_00935"), "00935")
        # 追蹤指數去掉後面的指數代碼
        self.assertEqual((d["index_name"], d["list_date"], d["mgmt_fee"], d["manager"], d["risk_class"]),
                         ("臺灣指數公司特選臺灣上市上櫃FactSet創新科技50指數", "2023-11-01", 0.4, "林怡君", "RR5"))
        self.assertEqual(d["dividends"][0], {"code": "00935", "ex_date": "2026-09-16", "pay_date": "2026-10-15", "cash": 1.83})
        # 分段費率不換算；只寫代碼（AUDGQ9CW、1306 JP）的追蹤指數不收
        b = n.parse_detail(self.load("detail_00987B"), "00987B")
        self.assertEqual((b["mgmt_fee"], b["index_name"], b["custodian_fee_text"]),
                         (None, None, "100億(含)以下：0.08%；100億-300 億(含)：0.07%；300億以上：0.06%"))
        self.assertIsNone(n.parse_detail(self.load("detail_009812"), "009812")["index_name"])
        self.assertEqual(n.parse_detail(self.load("detail_00935"), "00944"), {})
        p = n.parse_pcf(self.load("trade_00935"))
        self.assertEqual((p["pcf_date"], p["aum"], p["units"], p["units_change"], p["beneficiaries"], p["beneficiaries_month"]),
                         ("2026-10-16", 521.53, 917504000.0, 14500000.0, 130102.0, "2026-09"))
        self.assertEqual(p["pcf_announced"], "2026-10-19")
        # 期貨代號加上契約年月
        self.assertEqual(p["holdings"][-1], {"asset_code": "TX-202610", "asset_name": "台灣證交所加權股價指數", "kind": "期貨",
                                             "weight": 2.92, "qty": 161.0})
        self.assertEqual([(h["asset_code"], h["kind"]) for h in n.parse_pcf(self.load("trade_009812"))["holdings"]],
                         [("TP-202612", "期貨"), ("1306 JP", "基金")])
        r = n.parse_pcf(self.load("trade_00987B"))
        self.assertEqual(r["holdings"][0], {"asset_code": "AU0000407256", "asset_name": "ACGB 4 1/4 10/21/36", "kind": "債券",
                                            "weight": 12.02, "qty": 6750000.0})
        self.assertEqual(n.parse_pcf({"CFundId": "00420A"}), {})
        with self.assertRaises(RuntimeError):
            n.unwrap({"StatusCode": 1, "Message": "Year, Month, and Day parameters describe an un-representable DateTime."})

    def test_fetch(self):
        from issuers import nomura as n
        recs = n.fetch(fixtures=FIX / "nomura")
        self.assertEqual(sorted(recs), ["00420A", "00935", "009812", "00987B"])
        # 債券 ETF 的基金資料頁只寫指數代碼，改用申購買回清單裡的指數名稱
        self.assertEqual(recs["00987B"]["index_name"],
                         "ICE TPEx 10+ Year Australia Government & Quasi-Government Coupon Weighted Index")
        # 還沒上市的 ETF 沒有申購買回清單，只有基金資料
        c = recs["00420A"]
        self.assertEqual((c["nav_official"], c["aum"], c["holdings"], c["mgmt_fee"], "pcf_date" in c), (None, None, [], 0.99, False))

    def test_units_change_date(self):
        from issuers import nomura as n
        pcf = {"pcf_date": "2026-10-14", "units_change": -1000000.0}
        # 淨值日期早於前一份清單（投資海外）：單位差異記在前一份清單的日期，資產與持股維持淨值日期
        self.assertEqual(n.date_units_change(pcf, "2026-10-15"),
                         {"pcf_date": "2026-10-15", "asset_date": "2026-10-14", "holdings_date": "2026-10-14", "units_change": -1000000.0})
        self.assertEqual(n.date_units_change({"pcf_date": "2026-10-15"}, "2026-10-15"), {"pcf_date": "2026-10-15"})  # 國內 ETF 不變
        self.assertEqual(n.date_units_change({}, "2026-10-15"), {})

    def test_history(self):
        from issuers import nomura as n
        recs = n.fetch(fixtures=FIX / "nomura", history={"attempted": set(), "budget": 3})
        # 照 AllDate 由新到舊：10/16 依序查 00987B、009812（查無資料）與 00935
        self.assertEqual([(c, [x["status"] for x in r.get("history_attempts", [])]) for c, r in sorted(recs.items())],
                         [("00420A", []), ("00935", ["ok"]), ("009812", ["empty"]), ("00987B", ["empty"])])
        h = recs["00935"]["history"][0]
        self.assertEqual((h["pcf_announced"], h["pcf_date"], h["units"], h["pcf_nav"]), ("2026-10-16", "2026-10-15", 903004000.0, 56.48))

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔野村 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00935", "b": "野村臺灣新科技50", "e": "57.00", "f": "56.80", "g": "", "h": "56.42", "i": "20261016", "j": "13:30:00"},
            {"a": "00987B", "b": "野村10+澳洲公債", "e": "15.70", "f": "15.60", "g": "", "h": "15.55", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Date": "1151016", "Code": "00935", "Name": "野村臺灣新科技50", "TradeVolume": "20,000,000", "ClosingPrice": "57.00", "Change": "0.1500"},
                 {"Date": "1151016", "Code": "00987B", "Name": "野村10+澳洲公債", "TradeVolume": "1,000,000", "ClosingPrice": "15.70", "Change": "0.0500"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00935"]
            self.assertEqual((a["issuer"], a["nav"], a["nav_source"]), ("野村", 56.84, "野村投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["beneficiaries"], a["mgmt_fee"], a["fee_source"]),
                             (521.53, 14500000.0, 130102.0, 0.4, "野村投信官網"))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-09-16", "cash": 1.83, "pay_date": "2026-10-15", "source": "野村投信官網"})
            self.assertEqual(a["holdings_top"][0]["asset_code"], "2330")
            b = items["00987B"]
            # 正式淨值與清單資產都是 10/15 的，當天用野村公布的預估淨值；
            # 投資海外的 ETF 單位差異是清單日期前一個交易日（10/16）的，記在當天
            self.assertEqual((b["nav"], b["nav_source"], b["units_change"]), (15.6529, "野村投信官網（預估淨值）", 0.0))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT date, units, units_change FROM fund_daily WHERE code='00987B' ORDER BY date").fetchall(),
                             [("2026-10-15", 76327000.0, None), ("2026-10-16", None, 0.0)])
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00987B'").fetchall(), [("2026-10-15",)])
            con.close()


class JpmorganTest(unittest.TestCase):
    def load(self, name: str):
        return json.loads((FIX / "jpmorgan" / f"{name}.json").read_text("utf-8"))

    def test_parse(self):
        from issuers import jpmorgan as j
        self.assertEqual(j.parse_list(self.load("list")), [{"code": "00401A", "isin": "TW00000401A1"}, {"code": "00989A", "isin": "TW00000989A5"}])
        self.assertEqual(j.parse_estimates(self.load("inav"))["00989A"], {"nav_estimate": 17.56, "nav_estimate_date": "2026-10-16"})
        a = j.parse_product(self.load("product_00401A"), "00401A")
        self.assertEqual((a["nav_official"], a["nav_date"], a["aum"], a["units"], a["units_change"], a["pcf_date"]),
                         (13.76, "2026-10-16", 38.11, 276895000.0, 0.0, "2026-10-16"))
        self.assertEqual((a["manager"], a["risk_class"], a["list_date"], a["mgmt_fee"], a["mgmt_fee_text"], a["custodian_fee_text"]),
                         ("沈馨、魏伯宇", "RR4", "2026-04-10", 0.6, "0.6%", "0.045%"))
        # 選擇權與現金不是成分；期貨用官網的代號（已含月份）
        self.assertEqual({h["kind"] for h in a["holdings"]}, {"股票", "期貨"})
        self.assertIn({"asset_code": "FTV6", "asset_name": "台股期貨10/26", "kind": "期貨", "weight": 0.1817, "qty": 28.0}, a["holdings"])
        # 海外 ETF：淨值、規模與持股是前一天的，單位差異已經是當天的
        b = j.parse_product(self.load("product_00989A"), "00989A")
        self.assertEqual((b["nav_date"], b["holdings_date"], b["pcf_date"]), ("2026-10-15", "2026-10-15", "2026-10-16"))
        self.assertEqual(j.parse_dividends(self.load("history_00401A"), "00401A")[0],
                         {"code": "00401A", "ex_date": "2026-10-15", "pay_date": "2026-11-05", "cash": 0.131})
        self.assertEqual(j.parse_product({"fundData": {}}, "00401A"), {})

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔摩根 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00401A", "b": "主動摩根台灣鑫收", "e": "13.80", "f": "13.70", "g": "", "h": "13.63", "i": "20261016", "j": "13:30:00"},
            {"a": "00989A", "b": "主動摩根美國科技", "e": "17.60", "f": "17.50", "g": "", "h": "17.45", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Date": "1151016", "Code": "00401A", "Name": "主動摩根台灣鑫收", "TradeVolume": "5,000,000", "ClosingPrice": "13.80", "Change": "0.1000"},
                 {"Date": "1151016", "Code": "00989A", "Name": "主動摩根美國科技", "TradeVolume": "1,000,000", "ClosingPrice": "17.60", "Change": "0.1500"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00401A"]
            self.assertEqual((a["issuer"], a["nav"], a["nav_source"]), ("摩根", 13.76, "摩根投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["mgmt_fee"], a["fee_source"], a["manager"]),
                             (38.11, 0.0, 0.6, "摩根投信官網", "沈馨、魏伯宇"))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-10-15", "cash": 0.131, "pay_date": "2026-11-05", "source": "摩根投信官網"})
            b = items["00989A"]
            # 正式淨值是 10/15 的，當天用摩根公布的預估淨值；單位差異標示為 10/16，記在當天
            self.assertEqual((b["nav"], b["nav_source"], b["units_change"]), (17.56, "摩根投信官網（預估淨值）", 0.0))
            con = sqlite3.connect(db)
            # 持股依持股表標示的日期保存
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00989A'").fetchall(), [("2026-10-15",)])
            con.close()

class BlackrockTest(unittest.TestCase):
    def read(self, name: str) -> str:
        return (FIX / "blackrock" / name).read_text("utf-8-sig")

    def test_parse(self):
        from issuers import blackrock as b
        funds = b.parse_screener(json.loads(self.read("screener.json")))
        self.assertEqual(sorted((x["code"], x["nav_date"]) for x in funds),
                         [("009813", "2026-10-15"), ("009826", "2026-10-15"), ("00985D", "2026-10-15"), ("00991B", "2026-10-15")])
        p = b.parse_product(self.read("product_00991B.html"))
        self.assertEqual((p["aum_twd"], p["units"], p["units_change"], p["pcf_date"], p["list_date"]),
                         (5950326637.0, 400328000.0, 34500000.0, "2026-10-15", "2026-08-03"))
        self.assertEqual(p["index_name"], "ICE TIP 10年期以上A級成熟市場美元公司債指數")
        self.assertTrue(p["holdings_url"].startswith("/tw/products/351827/fund/"))
        s = b.parse_product(self.read("product_009813.html"))["stats"]
        self.assertEqual((s["date"], s["pe"], s["pb"]), ("2026-10-15", 32.73, 8.33))
        day, rows = b.parse_holdings(self.read("holdings_00991B.csv"))
        # 債券代號用「Ticker 票面利率 到期日」；現金不是成分
        self.assertEqual((day, rows[0]["asset_code"], rows[0]["qty"], rows[0]["kind"]), ("2026-10-16", "TXEDCI 7.53 2048-11-30", 5400000.0, "債券"))
        self.assertNotIn("現金", {r["kind"] for r in rows})
        _, stock = b.parse_holdings(self.read("holdings_009813.csv"))
        self.assertIn({"asset_code": "HWAZ6", "asset_name": "MICRO EMINI S&P 500 DEC 26", "kind": "期貨", "weight": 0.0, "qty": 8.0}, stock)
        self.assertEqual(b.parse_dividends(self.read("dividends.html"))["00985D"][0],
                         {"code": "00985D", "ex_date": "2026-09-17", "pay_date": "2026-10-20", "cash": 0.061})
        self.assertTrue(b.blocked_page("<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>"))
        # 債券 ETF 持有的 ETF（資產類別也是 Fixed Income）依產業欄歸為基金；現金、保證金、其他衍生性商品不收
        _, bond = b.parse_holdings(self.read("holdings_00985D.csv"))
        self.assertEqual(bond[0], {"asset_code": "TLTW", "asset_name": "iShares TLT Prem Income+ ETF COM", "kind": "基金",
                                   "weight": 19.9, "qty": 354782.0})
        self.assertEqual([r["kind"] for r in bond], ["基金", "債券", "期貨", "債券", "債券"])
        # 重複的代號：債券加上名稱、股票加上市場，仍重複的再加序號
        dup = ('x,1\n\nTicker,Name,Asset Class,Weight (%),Shares,Par Value,Coupon (%),Maturity,Location\n'
               'PPL,PPL ELECTRIC,Fixed Income,1,10,10,5.85,2055年8月15日,美國\nPPL,PPL CAPITAL,Fixed Income,1,10,10,5.85,2055年8月15日,美國\n'
               'MRK,MERCK & CO INC,指數股票型,1,5,,,,美國\nMRK,MERCK KGAA,指數股票型,1,5,,,,德國\nX,X1,指數股票型,1,5,,,,\nX,X2,指數股票型,1,5,,,,\n')
        self.assertEqual([r["asset_code"] for r in b.parse_holdings(dup)[1]],
                         ["PPL 5.85 2055-08-15 PPL ELECTRIC", "PPL 5.85 2055-08-15 PPL CAPITAL", "MRK 美國", "MRK 德國", "X", "X #2"])

    def test_fetch(self):
        from issuers import blackrock as b
        recs = b.fetch(fixtures=FIX / "blackrock")
        # 產品頁抓不到的 ETF 仍保留篩選器的正式淨值與配息紀錄
        self.assertEqual(sorted(recs), ["009813", "009826", "00985D", "00991B"])
        d = recs["00985D"]
        self.assertEqual((d["nav_official"], d["aum"], d["holdings"]), (9.7947, None, []))
        self.assertTrue(d["dividends"])
        a = recs["00991B"]
        self.assertEqual((a["aum"], a["units_change"], a["pcf_date"], a["holdings_date"], len(a["holdings"])), (59.5, 34500000.0, "2026-10-15", "2026-10-16", 12))


class BackfillTest(unittest.TestCase):
    def test_order_budget_and_errors(self):
        from issuers import common
        calls = []

        def one(code, day):
            calls.append((code, day))
            if day == "2026-10-13":
                raise RuntimeError("連線失敗")
            return {"pcf_date": day, "units": 1.0, "holdings": [1]} if code == "A" else {}

        out = common.backfill_pcfs({"A": ["2026-10-16", "2026-10-15", "2026-10-14", "2026-10-13"], "B": ["2026-10-16", "2026-10-15"]},
                                   {("A", "2026-10-15")}, one, budget=10)
        # 新到舊、同一天各檔輪流；查過的跳過；連線失敗就停，那一筆不算查過
        self.assertEqual(calls, [("B", "2026-10-16"), ("A", "2026-10-16"), ("B", "2026-10-15"), ("A", "2026-10-14"), ("A", "2026-10-13")])
        self.assertEqual([a["status"] for a in out["B"]["attempts"]], ["empty", "empty"])
        self.assertEqual([x["pcf_date"] for x in out["A"]["history"]], ["2026-10-16", "2026-10-14"])
        self.assertNotIn("holdings", out["A"]["history"][0])
        calls.clear()
        common.backfill_pcfs({"A": ["2026-10-16", "2026-10-15"]}, set(), one, budget=1)
        self.assertEqual(calls, [("A", "2026-10-16")])


class AllianzTest(unittest.TestCase):
    def load(self, name: str):
        from issuers import allianz as a
        return a.unwrap(json.loads((FIX / "allianz" / f"{name}.json").read_text("utf-8")), name)

    def test_parse(self):
        from issuers import allianz as a
        funds = a.parse_overview(self.load("overview"))
        self.assertEqual([(x["fund_no"], x["code"], x["nav_official"], x["nav_date"]) for x in funds][:3],
                         [("E0001", "00984A", 15.26, "2026-10-16"), ("E0002", "00993A", 13.29, "2026-10-16"),
                          ("E0003", "00402A", 9.48, "2026-10-15")])
        self.assertEqual(a.parse_estimates(self.load("inav"))["00412A"], {"nav_estimate": 9.03, "nav_estimate_date": "2026-10-16"})
        d = a.parse_detail(self.load("detail_00984A"), "00984A")
        self.assertEqual((d["list_date"], d["manager"], d["risk_class"], d["mgmt_fee"], d["custodian_fee_text"]),
                         ("2025-07-14", "廖本隆", "RR4", 0.7, "0.04%"))
        # 分段費率不換算
        self.assertEqual(a.parse_detail(self.load("detail_00412A"), "00412A")["mgmt_fee"], None)
        self.assertEqual(a.parse_detail(self.load("detail_00984A"), "00993A"), {})
        self.assertEqual(a.parse_dividends(self.load("yield"))["00984A"][1],
                         {"code": "00984A", "ex_date": "2026-07-16", "pay_date": "2026-08-11", "cash": 0.415})
        p = a.parse_pcf(self.load("trade_00984A"))
        self.assertEqual((p["pcf_date"], p["aum"], p["units"], p["units_change"], p["beneficiaries"], p["beneficiaries_month"]),
                         ("2026-10-16", 104.14, 682458000.0, 9500000.0, 63210.0, "2026-09"))
        self.assertEqual(p["holdings"][0], {"asset_code": "3017", "asset_name": "奇鋐", "kind": "股票", "weight": 4.39, "qty": 138000.0})
        # 清單日期（公告日期）是下一個交易日，資料日期是淨值日期
        self.assertEqual((p["pcf_announced"], p["pcf_date"]), ("2026-10-19", "2026-10-16"))
        # 期貨代號加上契約年月
        self.assertEqual(a.parse_pcf(self.load("trade_00993A"))["holdings"][-1],
                         {"asset_code": "TX-202611", "asset_name": "台指期貨", "kind": "期貨", "weight": 1.9, "qty": 19.0})
        # 投資海外的 ETF：清單資料晚兩個交易日；附買回債券沒有代號與權重，不收
        r = a.parse_pcf(self.load("trade_00412A"))
        self.assertEqual((r["pcf_date"], [h["asset_code"] for h in r["holdings"]]), ("2026-10-15", ["2330", "3008", "6857 JP", "3443"]))
        # 舊清單沒有序號欄，代號與名稱有多餘空白
        self.assertEqual(a.parse_pcf(self.load("trade_old"))["holdings"][1],
                         {"asset_code": "1210", "asset_name": "大成", "kind": "股票", "weight": 0.21, "qty": 42000.0})
        # 查無資料（StatusCode 1）
        self.assertIsNone(self.load("trade_00402A"))
        self.assertEqual(a.parse_pcf(None), {})
        with self.assertRaises(RuntimeError):
            a.unwrap({"StatusCode": 2, "Message": "錯誤"})

    def test_fetch(self):
        from issuers import allianz as a
        recs = a.fetch(fixtures=FIX / "allianz")
        self.assertEqual(sorted(recs), ["00402A", "00412A", "00984A", "00993A"])
        # 查不到申購買回清單的 ETF 仍保留官網的淨值與基金資料
        b = recs["00402A"]
        self.assertEqual((b["nav_official"], b["aum"], b["holdings"], b["manager"], "pcf_date" in b), (9.48, None, [], "洪華珍", False))
        self.assertEqual(recs["00984A"]["source_url"], "https://etf.allianzgi.com.tw/etf-info/E0001")

    def test_history(self):
        from issuers import allianz as a
        import market_calendar as mc
        cal = mc.Calendar(mc.parse(json.loads((FIX / "calendar.json").read_text("utf-8"))), {2026})
        self.assertEqual(a.history_dates("2026-10-19", cal, "2026-10-13"), ["2026-10-16", "2026-10-15", "2026-10-14", "2026-10-13"])
        self.assertEqual(a.history_dates("2026-10-13", cal)[:2], ["2026-10-12", "2026-10-08"])  # 10/9 補假
        recs = a.fetch(fixtures=FIX / "allianz", history={"calendar": cal, "attempted": set(), "budget": 3})
        # 清單日期由新到舊、各檔輪流：10/16 先查 00993A（查無資料）、00984A、00412A
        self.assertEqual(recs["00993A"]["history_attempts"], [{"pcf_date": "2026-10-16", "data_date": None, "status": "empty"}])
        h = recs["00984A"]["history"]
        self.assertEqual([(x["pcf_announced"], x["pcf_date"], x["units"], x["units_change"]) for x in h],
                         [("2026-10-16", "2026-10-15", 672958000.0, 5000000.0)])
        self.assertNotIn("holdings", h[0])
        # 投資海外的 00412A：清單日期 10/16 的資料日期是 10/14
        self.assertEqual(recs["00412A"]["history"][0]["pcf_date"], "2026-10-14")
        # 查過的不再查；預算用完就停
        again = a.fetch(fixtures=FIX / "allianz", history={"calendar": cal, "budget": 1,
                                                          "attempted": {("00993A", "2026-10-16"), ("00984A", "2026-10-16")}})
        self.assertEqual([c for c, r in again.items() if r.get("history_attempts")], ["00412A"])
        self.assertNotIn("history", a.fetch(fixtures=FIX / "allianz")["00984A"])  # 沒給 history 就不回補

    def fixtures(self, tmp: str) -> Path:
        """在共用測試資料加上兩檔安聯 ETF 的交易所資料。"""
        fix = fixtures_without(tmp)
        nav = json.loads((fix / "nav.json").read_text("utf-8"))
        nav["a1"].append({"msgArray": [
            {"a": "00984A", "b": "主動安聯台灣高息", "e": "15.30", "f": "15.25", "g": "", "h": "15.92", "i": "20261016", "j": "13:30:00"},
            {"a": "00412A", "b": "主動安聯亞半導體", "e": "9.10", "f": "9.02", "g": "", "h": "9.05", "i": "20261016", "j": "13:30:00"}]})
        (fix / "nav.json").write_text(json.dumps(nav, ensure_ascii=False), "utf-8")
        twse = json.loads((fix / "twse.json").read_text("utf-8"))
        twse += [{"Date": "1151016", "Code": "00984A", "Name": "主動安聯台灣高息", "TradeVolume": "20,000,000", "ClosingPrice": "15.30", "Change": "-0.6200"},
                 {"Date": "1151016", "Code": "00412A", "Name": "主動安聯亞半導體", "TradeVolume": "5,000,000", "ClosingPrice": "9.10", "Change": "0.0500"}]
        (fix / "twse.json").write_text(json.dumps(twse, ensure_ascii=False), "utf-8")
        return fix

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(self.fixtures(tmp)), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            a = items["00984A"]
            self.assertEqual((a["issuer"], a["nav"], a["nav_source"]), ("安聯", 15.26, "安聯投信官網（正式淨值）"))
            self.assertEqual((a["aum"], a["units_change"], a["beneficiaries"], a["mgmt_fee"], a["fee_source"], a["manager"]),
                             (104.14, 9500000.0, 63210.0, 0.7, "安聯投信官網", "廖本隆"))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-10-16", "cash": 0.473, "pay_date": "2026-11-13", "source": "安聯投信官網"})
            self.assertEqual((a["holdings_top"][0]["asset_code"], a["holdings_date"]), ("3017", "2026-10-16"))
            b = items["00412A"]
            # 正式淨值與清單都是 10/15 的，當天用安聯公布的預估淨值；單位增減屬於 10/15
            self.assertEqual((b["nav"], b["nav_source"], b["units_change"]), (9.03, "安聯投信官網（預估淨值）", None))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT DISTINCT date FROM holding WHERE code='00412A'").fetchall(), [("2026-10-15",)])
            # 回補的歷史清單依資料日期寫入，公告日期是清單日期；查過的清單日期記在 fund_backfill
            self.assertEqual(con.execute("SELECT date, units, announced FROM fund_daily WHERE code='00984A' ORDER BY date").fetchall(),
                             [("2026-10-15", 672958000.0, "2026-10-16"), ("2026-10-16", 682458000.0, "2026-10-19")])
            self.assertEqual(con.execute("SELECT status FROM fund_backfill WHERE code='00993A' AND pcf_date='2026-10-16'").fetchone(),
                             ("empty",))
            con.close()


class ReturnsTest(unittest.TestCase):
    """區間報酬只用交易所開放資料逐日保存的官方收盤行情（合成數值，開休市日期用證交所 2026 年官方表）。"""
    CAL = f.market_calendar.Calendar(f.market_calendar.parse(json.loads((FIX / "calendar.json").read_text("utf-8"))), {2026})
    # 10/09 休市；10/13 除息 1 元：參考價 49.5 ＝ 前一個收盤價 50.5 − 1
    SERIES = {"2026-10-08": (50.0, 0.5), "2026-10-12": (50.5, 0.5), "2026-10-13": (49.0, -0.5), "2026-10-14": (49.5, 0.5),
              "2026-10-15": (50.0, 0.5), "2026-10-16": (51.0, 1.0)}

    def compute(self, series=None, captured=None, dividends=None, end="2026-10-16", cal=None):
        import returns
        series = dict(self.SERIES if series is None else series)
        return returns.compute(series, set(self.SERIES) if captured is None else captured, end, cal or self.CAL,
                               {"2026-10-13": 1.0} if dividends is None else dividends)

    def test_start_dates(self):
        import returns
        self.assertEqual(returns.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))  # 該月沒有 31 日
        self.assertEqual(returns.add_months(date(2024, 3, 31), -1), date(2024, 2, 29))
        self.assertEqual(returns.start_dates("2026-10-16"),
                         {"1w": "2026-10-09", "1m": "2026-09-16", "3m": "2026-07-16", "6m": "2026-04-16",
                          "ytd": "2025-12-31", "1y": "2025-10-16", "3y": "2023-10-16", "5y": "2021-10-16"})

    def test_compute(self):
        r = self.compute()
        # 近一週的起始日 10/09 休市，期初用之前最近的交易日 10/08；配息在除息日以收盤價再投入
        self.assertEqual(r["price"]["1w"], round((51 / 50 - 1) * 100, 2))
        self.assertEqual(r["total"]["1w"], round((51 / 50 * (1 + 1 / 49) - 1) * 100, 2))
        # 期初早於官方收盤行情的保存起點：留空，不回補
        self.assertEqual((r["price"]["1m"], r["total"]["1y"], r["price"]["5y"]), (None, None, None))
        # 期末沒有官方收盤價：整檔不算
        self.assertIsNone(self.compute({**self.SERIES, "2026-10-16": (None, None)}))

    def test_dividend_checks(self):
        # 資料庫漏列配息：參考價對不上前一個收盤價，含息報酬留空，市價報酬照算
        r = self.compute(dividends={})
        self.assertEqual((r["price"]["1w"], r["total"]["1w"]), (2.0, None))
        # 配息金額還沒公告也一樣
        r = self.compute(dividends={"2026-10-13": None})
        self.assertEqual((r["price"]["1w"], r["total"]["1w"]), (2.0, None))
        # 資料庫的配息日期不符（參考價那天沒有調整）：含息報酬留空，不當成分割
        r = self.compute(dividends={"2026-10-13": 1.0, "2026-10-15": 0.5})
        self.assertEqual((r["price"]["1w"], r["total"]["1w"]), (2.0, None))
        # 除息日沒有成交：沒有再投入的價格，含息報酬留空；之後的參考價從最後一個收盤價核對
        no_trade = {**self.SERIES, "2026-10-13": (None, None), "2026-10-14": (49.5, -0.0)}
        r = self.compute(no_trade)
        self.assertEqual((r["price"]["1w"], r["total"]["1w"]), (2.0, None))

    def test_split_and_gaps(self):
        # 疑似分割（參考價比前一個收盤價低超過 25%）：市價、含息報酬都留空
        split = {**self.SERIES, "2026-10-14": (12.4, 0.15), "2026-10-15": (12.5, 0.1), "2026-10-16": (12.75, 0.25)}
        r = self.compute(split)
        self.assertEqual((r["price"]["1w"], r["total"]["1w"]), (None, None))
        # 期間內有交易日沒有保存到這個市場的收盤行情，或交易所沒有列出這檔
        self.assertIsNone(self.compute(captured=set(self.SERIES) - {"2026-10-14"})["price"]["1w"])
        self.assertIsNone(self.compute({d: v for d, v in self.SERIES.items() if d != "2026-10-14"})["price"]["1w"])
        # 沒有漲跌價差、當天也沒有配息：無法核對參考價
        self.assertIsNone(self.compute({**self.SERIES, "2026-10-15": (50.0, None)})["price"]["1w"])
        # 開休市日期沒涵蓋：不判定期初
        self.assertIsNone(self.compute(cal=f.market_calendar.Calendar({}, set()))["price"]["1w"])

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            # 先前已保存 10/08～10/15 的官方收盤行情（10/15 除息 1 元，參考價 50.4）；這次的測試資料補上 10/16（收盤 52.35、漲 0.45）
            con = store.open_db(db, Path(tmp) / "history")
            con.executemany("INSERT INTO exchange_daily(code, date, market, name, close, change, dataset, source_url) "
                            "VALUES('0050', ?, '上市', '元大台灣50', ?, ?, 'd', 'u')",
                            [("2026-10-08", 50.0, 0.1), ("2026-10-12", 50.5, 0.5), ("2026-10-13", 51.0, 0.5),
                             ("2026-10-14", 51.4, 0.4), ("2026-10-15", 51.9, 1.5)])
            con.commit()
            con.close()
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            payload = json.loads(out.read_text("utf-8"))
            info = payload["returns_info"]
            self.assertEqual((info["starts"]["1y"], info["history_from"]), ("2025-10-16", {"上市": "2026-10-08", "上櫃": "2026-10-16"}))
            self.assertIn("11549", info["source"])
            items = {i["code"]: i for i in payload["items"]}
            # 交易所除權息預告的 0050 配息（10/15，1 元）在除息日以收盤價再投入
            self.assertEqual((items["0050"]["returns"]["price"]["1w"], items["0050"]["returns"]["total"]["1w"]),
                             (round((52.35 / 50 - 1) * 100, 2), round((52.35 / 50 * (1 + 1 / 51.9) - 1) * 100, 2)))
            self.assertIsNone(items["0050"]["returns"]["price"]["1m"])  # 官方歷史還不到一個月
            # 有官方收盤價、但還沒有歷史：各區間留空；沒有成交（沒有期末收盤價）：整檔不算
            self.assertEqual(set(items["0056"]["returns"]["price"].values()), {None})
            self.assertIsNone(items["00631L"]["returns"])


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
        # 基金資料彙總表：以基金統編對到 ETF 代號，公司小計與總計列不收
        self.assertEqual(len(sitca.parse_summary(page("summary"))), 3)
        self.assertEqual((info["summary"]["month"], sorted(info["summary"]["funds"])), ("2026-09", ["0050", "0056"]))
        self.assertEqual(info["summary"]["funds"]["0056"],
                         {"aum_twd": 800875038138.0, "nav": 56.75, "units": 14112534000.0, "beneficiaries": 1745584,
                          "beneficiaries_natural": 1742995, "beneficiaries_legal": 2589,
                          "subscribe_twd": 6578050000.0, "redeem_twd": 7496115000.0})
        # 月前十大：名稱去掉警語後對到代號，對不到的基金（兆豐藍籌30）不收；標的種類換成成分類型
        self.assertEqual(sitca.name_key("元大台灣高股息基金 (本基金之配息來源可能為收益平準金)"), "元大台灣高股息基金")
        self.assertEqual(sitca.name_key("中國信託亞太高股息ＥＴＦ基\uf90a（本子基金）"), "中國信託亞太高股息ETF基金")  # 全形與相容字
        self.assertEqual([sitca.asset_kind(k) for k in ("國內上市", "國外股票(含承銷)", "公司債 無擔保", "國外掛牌交易之ETF",
                                                       "同公司發行之境內基金", "其他")], ["股票", "股票", "債券", "基金", "基金", "其他"])
        self.assertEqual(len(sitca.parse_top10(page("top10_AH11"))), 3)
        self.assertEqual((info["top10"]["month"], sorted(info["top10"]["funds"])), ("2026-09", ["0050", "0056"]))
        self.assertEqual(info["top10"]["funds"]["0056"][0], {"rank": 1, "asset_code": "1303", "asset_name": "南亞", "kind": "股票", "weight": 8.37})
        # 配息資料：只收新台幣、對得到代號的
        self.assertEqual(len(sitca.parse_dividends(page("dividend_202607"))), 2)
        self.assertEqual(info["dividends"], [{"code": "0056", "ex_date": "2026-07-21", "pay_date": "2026-08-10", "cash": 1.35}])
        # 每日淨值：只收證券代號格式、新台幣計價的；回應的日期和要查的交易日不同就不用
        self.assertEqual(sitca.parse_nav(page("nav")), {"0056": 36.2})
        self.assertEqual((sitca.fetch_nav("2026-10-16", FIX / "sitca"), sitca.fetch_nav("2026-10-15", FIX / "sitca")), ({"0056": 36.2}, {}))

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
        # 主要交易線代號不是去掉 K 的例外：00625K 是 006205 的人民幣交易線
        self.assertEqual((f.main_line("00625K"), f.main_line("00636K"), f.main_line("0050")), ("006205", "00636", None))
        rmb = {**base, "code": "00625K"}
        f.apply_fund_info([rmb], {"month": "2026-09", "funds": {"006205": {**info["funds"]["00636"], "manager": "乙"}}})
        self.assertEqual((rmb["mgmt_fee_text"], rmb["manager"]), ("0.40%", "乙"))

    def test_main_line_holdings(self):
        hold = [{"asset_code": "600519", "asset_name": "貴州茅台", "kind": "股票", "weight": 5.0, "qty": 100.0}]
        main = {"code": "006205", "holdings_top": hold, "holdings_date": "2026-10-19", "holdings_source": "富邦投信官網"}
        k_line = {"code": "00625K", "holdings_top": [], "holdings_date": None, "holdings_source": None}
        own = {"code": "00636K", "holdings_top": [{"asset_code": "X"}], "holdings_source": "國泰投信官網"}
        f.apply_main_line_holdings([main, k_line, own, {"code": "00636", "holdings_top": []}])
        # 外幣交易線沿用主要交易線的成分股並註明；已經有自己成分股的不覆蓋
        self.assertEqual((k_line["holdings_top"], k_line["holdings_date"], k_line["holdings_source"]),
                         (hold, "2026-10-19", "富邦投信官網（與 006205 同一檔基金）"))
        self.assertEqual(own["holdings_source"], "國泰投信官網")

    def test_backfill(self):
        import sitca
        funds = {"0050": {"fund_id": "98764389"}, "0056": {"fund_id": "48966213"}}
        old = sitca.backfill({"summary": ["2026-08", "2026-07", "2026-06"], "dividend": ["2026-07"]}, funds, FIX / "sitca")
        # 8 月有資料；7 月公會回應的是別的月份（記為已處理、不收資料）；6 月沒有測試資料（下次再試）
        self.assertEqual(sorted(old["summary"]), ["2026-08"])
        self.assertEqual(old["summary"]["2026-08"]["0056"]["beneficiaries"], 1700000)
        self.assertEqual(old["done"], [("summary", "2026-08"), ("summary", "2026-07"), ("dividend", "2026-07")])
        self.assertEqual(old["dividends"], [{"code": "0056", "ex_date": "2026-07-21", "pay_date": "2026-08-10", "cash": 1.35}])
        self.assertEqual(f.months_between("2025-11", "2026-02"), ["2025-11", "2025-12", "2026-01", "2026-02"])

    def test_backfill_pipeline(self):
        import sitca
        with tempfile.TemporaryDirectory() as tmp:
            fix = fixtures_without(tmp)
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            args = ["--fixtures", str(fix), "--db", str(db), "--out", str(out)]
            self.assertEqual(f.main(args), 0)  # 第一次：抓最新資料，不回補
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT report, month FROM fund_fetch ORDER BY report, month").fetchall(),
                             [("dividend", "2026-07"), ("summary", "2026-09")])
            con.close()
            plan = f.backfill_plan(db)
            self.assertEqual(plan["summary"], ["2026-08", "2026-07", "2026-06"][:sitca.BACKFILL_MONTHS])
            self.assertEqual(f.main(args), 0)  # 一週內再跑：回補歷史月份
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT month, beneficiaries FROM fund_month WHERE code='0056' ORDER BY month").fetchall(),
                             [("2026-08", 1700000), ("2026-09", 1745584)])
            self.assertIn(("summary", "2026-07"), con.execute("SELECT report, month FROM fund_fetch").fetchall())
            con.close()
            # 網站顯示的仍是最新一個月
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertEqual(items["0056"]["monthly"]["month"], "2026-09")

    def test_apply_month(self):
        row = {"code": "0056", "month": "2026-09", "aum_twd": 800875038138.0, "nav": 56.75, "units": 14112534000.0,
               "beneficiaries": 1745584, "beneficiaries_natural": 1742995, "beneficiaries_legal": 2589,
               "subscribe_twd": 6578050000.0, "redeem_twd": 7496115000.0}
        same = {"code": "0056", "beneficiaries": 1700000, "beneficiaries_month": "2026-09", "beneficiaries_source": "元大投信官網"}
        older = {"code": "0056", "beneficiaries": 1600000, "beneficiaries_month": "2026-08", "beneficiaries_source": "元大投信官網"}
        none = {"code": "0056", "beneficiaries": None, "beneficiaries_month": None, "beneficiaries_source": None}
        k_line = {"code": "0056K", "beneficiaries": None}
        f.apply_fund_month([same, older, none, k_line], {"0056": row})
        self.assertEqual(none["monthly"], {"month": "2026-09", "aum": 8008.75, "nav": 56.75, "units": 14112534000.0,
                                           "beneficiaries": 1745584, "beneficiaries_natural": 1742995, "beneficiaries_legal": 2589,
                                           "subscribe": 65.78, "redeem": 74.96, "net": -9.18, "source": "投信投顧公會基金資料彙總表"})
        # 受益人數：同一個月用投信官網的，公會資料較新或官網沒有時用公會的
        self.assertEqual((same["beneficiaries"], same["beneficiaries_source"]), (1700000, "元大投信官網"))
        self.assertEqual((older["beneficiaries"], older["beneficiaries_month"], older["beneficiaries_source"]),
                         (1745584, "2026-09", "投信投顧公會基金資料彙總表"))
        self.assertEqual((none["beneficiaries"], none["beneficiaries_source"]), (1745584, "投信投顧公會基金資料彙總表"))
        self.assertIsNone(k_line["monthly"])  # 公會只列主要交易線

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
            self.assertEqual((a["monthly"]["month"], a["monthly"]["aum"], a["monthly"]["beneficiaries"]), ("2026-09", 8008.75, 1745584))
            self.assertEqual((a["beneficiaries"], a["beneficiaries_source"]), (1745584, "投信投顧公會基金資料彙總表"))
            self.assertIsNone(items["00757"]["monthly"])
            # 沒有官網成分股的 ETF 用公會月前十大；配息用公會資料（含發放日）
            self.assertEqual((a["holdings_source"], a["holdings_date"], a["holdings_top"][0]["asset_code"], a["holdings_top"][0]["qty"]),
                             ("投信投顧公會基金投資明細（月前十大）", "2026-09", "1303", None))
            self.assertEqual(a["last_ex"], {"ex_date": "2026-07-21", "cash": 1.35, "pay_date": "2026-08-10", "source": "投信投顧公會"})
            self.assertEqual(items["0050"]["holdings_source"], "元大投信官網")  # 官網有成分股就用官網的
            # 沒有官網正式淨值的 ETF 用公會當日正式淨值，折溢價跟著重算，資料庫的正式淨值也補上
            self.assertEqual((a["nav"], a["nav_source"], a["premium"]), (36.2, "投信投顧公會（正式淨值）", round((36.1 - 36.2) / 36.2 * 100, 4)))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT nav, nav_official FROM daily WHERE code='0056' AND date='2026-10-16'").fetchone(), (36.2, 36.2))
            con.close()
            # 一週內再跑：不重抓公會資料，沿用資料庫裡的
            # 直接驗證不再抓取；避免 Windows 複製唯讀目錄後無法 rmdir。
            from unittest.mock import patch
            for fixture_file in (fix / "sitca").glob("*.html"):
                fixture_file.unlink()
            with patch.object(f.sitca, "fetch", side_effect=AssertionError("快取有效時不應重抓公會資料")):
                self.assertEqual(f.main(args), 0)
            again = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            self.assertEqual((again["0056"]["mgmt_fee_text"], again["0056"]["fee"]), ("0.40%", 0.58))
            self.assertEqual(again["0056"]["monthly"], a["monthly"])
            self.assertEqual((again["0056"]["holdings_top"], again["0056"]["last_ex"]), (a["holdings_top"], a["last_ex"]))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*), MAX(data_month) FROM fund_info").fetchone(), (3, "2026-09"))
            self.assertEqual(con.execute("SELECT code, month, beneficiaries FROM fund_month ORDER BY code").fetchall(),
                             [("0050", "2026-09", 3794877), ("0056", "2026-09", 1745584)])
            self.assertEqual(con.execute("SELECT code, COUNT(*) FROM fund_top GROUP BY code").fetchall(), [("0050", 10), ("0056", 10)])
            con.close()
            self.assertTrue((Path(tmp) / "history" / "fund_month.csv").exists())


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
        con.executemany("INSERT INTO holding(code,date,asset_code,asset_name,kind,weight,qty,source) VALUES(?,?,?,?,?,?,?,?)", rows)
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

    def test_official_price_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(FIX), "--db", str(db), "--out", str(out)]), 0)
            payload = json.loads(out.read_text("utf-8"))
            # 收盤價只用交易所的官方收盤行情；沒有成交的寫明原因，不再寫入 Yahoo 查核紀錄
            self.assertEqual(payload["price_check"], {"交易所": 4, "無市價": 1})
            items = {i["code"]: i for i in payload["items"]}
            self.assertEqual((items["00757"]["price"], items["00757"]["price_check"]), (112.5, "交易所"))
            self.assertEqual((items["00679B"]["price_source"], items["00679B"]["price_date"]), ("櫃買中心收盤行情", "2026-10-16"))
            self.assertEqual((items["00631L"]["price"], items["00631L"]["price_note"]), (None, "當天沒有成交"))
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM price_check").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT price, price_check FROM daily WHERE code = '0050'").fetchone(), (52.35, "交易所"))
            con.close()

    def test_size_and_dividends_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            fix = fixtures_without(tmp, "yuanta")
            self.assertEqual(f.main(["--fixtures", str(fix), "--db", str(db), "--out", str(out)]), 0)
            items = {i["code"]: i for i in json.loads(out.read_text("utf-8"))["items"]}
            # 基金規模只採投信或公會公告；交易所單位數乘預估淨值不再顯示為官方規模。
            self.assertIsNone(items["0050"]["aum"])
            self.assertIsNone(items["00679B"]["aum"])
            self.assertIsNone(items["0050"]["aum_source"])
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
