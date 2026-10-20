"""主動式 ETF 持股資金動向（active_holdings）的離線測試：自建小資料庫模擬多檔 ETF 逐日的官網完整清單與官方股價。
開休市日期用證交所 2026 年官方表；ETF、股票與數值都是合成樣本。"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import active_holdings as ah  # noqa: E402
import market_calendar as mc  # noqa: E402
import official_quotes as oq  # noqa: E402
import store  # noqa: E402

CAL = mc.Calendar(mc.parse(json.loads((ROOT / "tests" / "fixtures" / "calendar.json").read_text("utf-8"))), {2026})
DAYS = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
SITE = "野村投信官網"  # 合成樣本沿用已查核日期欄位的來源名稱；所有數值仍是測試資料


def base_holdings():
    """每檔 ETF 每天的持股 {股票: 股數}，之後各測試再改。"""
    a1 = {"2330": 100000, "2454": 50000, "2882": 30000, "2002": 10000, "1301": 10000, "6666": 4000, "2317": 8000}
    a2 = {"2330": 200000, "1301": 20000, "3008": 5000, "2317": 6000}
    return {"00981A": {d: dict(a1) for d in DAYS}, "00985A": {d: dict(a2) for d in DAYS},
            "00991A": {d: {"2330": 70000} for d in DAYS if d != "2026-10-15"}}  # 00991A 缺 10/15 的清單


class ActiveHoldingsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        store.ensure_schema(self.con)
        mc.save(self.con, mc.parse(json.loads((ROOT / "tests" / "fixtures" / "calendar.json").read_text("utf-8"))), "2026-10-20T08:50:00+08:00")
        self.h = base_holdings()
        # 10/20：00981A 加碼 2330、減碼 2454、6666 增加但當天沒有股價、2882 不變（單位數增加）；00985A 加碼 2330
        self.h["00981A"]["2026-10-20"].update({"2330": 101000, "2454": 49500, "6666": 4500})
        self.h["00985A"]["2026-10-20"].update({"2330": 202000})
        # 兩檔 ETF 的 1301 同日同比例 ×1.05：疑似除權
        self.h["00981A"]["2026-10-20"]["1301"] = 10500
        self.h["00985A"]["2026-10-20"]["1301"] = 21000
        # 00985A 刪除 3008、新增 3009，股數相同：疑似代碼變更
        del self.h["00985A"]["2026-10-20"]["3008"]
        self.h["00985A"]["2026-10-20"]["3009"] = 5000
        # 00981A 只有一檔持有的 2002 照配股率 0.1 增加：疑似配股入帳
        self.h["00981A"]["2026-10-19"]["2002"] = self.h["00981A"]["2026-10-20"]["2002"] = 11000
        # 00981A 先買後賣 2603（10/14 +1000、10/16 −1000）
        for d in ("2026-10-14", "2026-10-15"):
            self.h["00981A"][d]["2603"] = 1000
        # 00985A 10/20 的 2317 有加碼；00981A 10/20 的 2317 缺股數
        self.h["00985A"]["2026-10-20"]["2317"] = 7000
        self.missing_qty = {("00981A", "2026-10-20", "2317")}

        rows = []
        for etf, days in self.h.items():
            for d, stocks in days.items():
                for a, q in stocks.items():
                    q = None if (etf, d, a) in self.missing_qty else q
                    rows.append((etf, d, a, f"股票{a}", "股票", 1.0, q, SITE, f"https://example.test/{etf}"))
                # 債券、期貨、外國股票不算
                rows += [(etf, d, "XS0001", "債券", "債券", 1.0, 100, SITE, "u"), (etf, d, "TX-202611", "台指期", "期貨", 1.0, 2, SITE, "u"),
                         (etf, d, "NVDA US", "NVIDIA", "股票", 1.0, 10, SITE, "u")]
        # 投信投顧公會月前十大不是完整清單：source 不是投信官網
        rows += [("00982A", d, "2330", "台積電", "股票", 5.0, None, "投信投顧公會基金投資明細（月前十大）", "u") for d in DAYS]
        self.con.executemany("INSERT INTO holding(code, date, asset_code, asset_name, kind, weight, qty, source, source_url) "
                             "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        # 00981A 的清單證據（取得時間）：其他 ETF 沒有，取得時間為 null
        for d in DAYS:
            ah.save_list_evidence(self.con, "00981A", d, SITE, "https://example.test/00981A",
                                  [{"asset_code": a, "kind": "股票", "weight": 1.0, "qty": q} for a, q in self.h["00981A"][d].items()],
                                  f"{d}T20:00:00+08:00")
        # 官方收盤價：10/13 起每天都有；6666 在 10/20 沒有成交
        price = {"2330": 1000.0, "2454": 1500.0, "2882": 60.0, "2002": 25.0, "1301": 50.0, "6666": 200.0, "2317": 200.0,
                 "3008": 2500.0, "3009": 2500.0, "2603": 100.0}
        closes = []
        for d in DAYS:
            for a, p in price.items():
                p = 110.0 if (a, d) == ("2603", "2026-10-16") else p
                closes.append((a, d, "上市", None if (a, d) == ("6666", "2026-10-20") else p, "11549"))
        self.con.executemany("INSERT INTO stock_close(code, date, market, close, dataset, name) VALUES(?, ?, ?, ?, ?, ?)",
                             [(*r, f"股票{r[0]}") for r in closes])
        self.con.execute("INSERT INTO stock_event(code, date, kind, stock_dividend_ratio, source, source_url) VALUES"
                         "('2002', '2026-10-01', '權', 0.1, '證交所除權除息預告', 'u')")
        # 發行單位數：00981A 10/19 → 10/20 增加 1%
        self.con.executemany("INSERT INTO fund_daily(code, date, units, source, source_url) VALUES(?, ?, ?, ?, ?)",
                             [("00981A", "2026-10-19", 1000000, SITE, "u"), ("00981A", "2026-10-20", 1010000, SITE, "u")])
        self.items = [{"code": c, "name": f"ETF{c}"} for c in ("0050", "00981A", "00985A", "00991A", "00982A")]
        self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)

    def tearDown(self):
        self.con.close()

    def stock(self, period, code):
        return next((s for s in self.flow["periods"][period]["stocks"] if s["asset_code"] == code), None)

    def test_contract_and_day_period(self):
        f = self.flow
        self.assertEqual((f["version"], f["as_of"], f["price_history_from"], sorted(f["periods"])), (1, "2026-10-20", "2026-10-13", ["1d", "1m", "1w"]))
        self.assertEqual([a["dataset_url"] for a in f["attributions"]], ["https://data.gov.tw/dataset/11549"])
        d = f["periods"]["1d"]
        self.assertEqual((d["status"], d["baseline"], d["start"], d["end"], d["days"]), ("可計算", "2026-10-19", "2026-10-20", "2026-10-20", ["2026-10-20"]))
        # 00991A 也有 10/19、10/20 的清單，列入單日；公會前十大不是完整清單；0050 不是主動式
        self.assertEqual(d["included_etfs"], ["00981A", "00985A", "00991A"])
        self.assertEqual(d["excluded_etfs"], [{"code": "00982A", "reason": "沒有投信官網的完整持股清單（投信投顧公會的前十大不是完整清單）"}])
        s = self.stock("1d", "2330")
        self.assertEqual((s["buy_etfs"], s["sell_etfs"], s["net_shares"], s["net_amount"], s["buy_amount"], s["sell_amount"], s["complete"]),
                         (2, 0, 3000, 0.03, 0.03, 0.0, True))
        self.assertEqual(s["prices"], [{"date": "2026-10-20", "close": 1000.0, "market": "上市", "dataset": "11549"}])
        c = next(x for x in s["etfs"] if x["code"] == "00981A")
        self.assertEqual((c["name"], c["qty_prev"], c["qty"], c["qty_change"], c["amount"], c["action"], c["prev_date"], c["date"]),
                         ("ETF00981A", 100000, 101000, 1000, 0.01, "加碼", "2026-10-19", "2026-10-20"))
        # 每萬單位持股：股數增加 1%、單位數也增加 1%，配置密度不變；00985A 沒有單位數
        self.assertEqual((c["unit_qty_prev"], c["unit_qty"], c["unit_qty_change"]), (1000.0, 1000.0, 0.0))
        self.assertEqual(next(x for x in s["etfs"] if x["code"] == "00985A")["unit_qty"], None)
        sell = self.stock("1d", "2454")
        self.assertEqual((sell["sell_etfs"], sell["net_amount"], sell["sell_amount"], sell["etfs"][0]["action"]), (1, -0.0075, 0.0075, "減碼"))
        # 股數沒變（只有單位數增加）不算賣出；債券、期貨、外國股票不納入
        for code in ("2882", "XS0001", "TX-202611", "NVDA US"):
            self.assertIsNone(self.stock("1d", code), code)
        # 依淨增減估值由大到小，空值最後
        nets = [s["net_amount"] for s in d["stocks"]]
        self.assertEqual(nets, sorted([n for n in nets if n is not None], reverse=True) + [n for n in nets if n is None])
        json.dumps(f, allow_nan=False)  # 只有有限數字或 null

    def test_corporate_actions_are_excluded_not_zero(self):
        d = self.flow["periods"]["1d"]
        reasons = {(e["asset_code"], e["date"]): (e["reason"], e["etfs"]) for e in d["excluded_stocks"]}
        # 兩檔 ETF 同日同比例：整檔股票排除，不出現在排行
        self.assertTrue(reasons[("1301", "2026-10-20")][0].startswith("疑似除權、分割或減資"))
        self.assertEqual(reasons[("1301", "2026-10-20")][1], ["00981A", "00985A"])
        self.assertIsNone(self.stock("1d", "1301"))
        # 2330 在兩檔 ETF 都剛好增加 1%，但 00991A 的 2330 沒變：不是公司行動，照常計算
        self.assertNotIn(("2330", "2026-10-20"), reasons)
        self.assertIsNotNone(self.stock("1d", "2330"))
        # 代碼變更：00985A 的 3008、3009 排除
        self.assertTrue(reasons[("3008", "2026-10-20")][0].startswith("疑似代碼變更"))
        self.assertEqual(reasons[("3009", "2026-10-20")][1], ["00985A"])
        self.assertIsNone(self.stock("1d", "3009"))
        # 配股入帳在 10/19（1d 期間外）；1w 期間內要排除
        self.assertNotIn(("2002", "2026-10-19"), reasons)
        w = {(e["asset_code"], e["date"]): e["reason"] for e in self.flow["periods"]["1w"]["excluded_stocks"]}
        self.assertEqual(w[("2002", "2026-10-19")], "疑似配股入帳：除權日 2026-10-01，配股率 0.1")

    def test_missing_values_stay_null_but_keep_counts(self):
        # 6666 當天沒有股價：金額為空值，檔數與股數保留，並標明缺值日期
        s = self.stock("1d", "6666")
        self.assertEqual((s["net_amount"], s["buy_amount"], s["buy_etfs"], s["net_shares"], s["complete"], s["missing_price_dates"]),
                         (None, None, 1, 500, False, ["2026-10-20"]))
        self.assertIn("缺 2026-10-20 的官方收盤價", s["etfs"][0]["reason"])
        # 2317：00981A 的清單缺股數（不當成 0），只保留 00985A 的部分，金額為空值並列出被排除的 ETF
        s = self.stock("1d", "2317")
        self.assertEqual((s["buy_etfs"], s["net_shares"], s["net_amount"], s["complete"]), (1, 1000, None, False))
        self.assertEqual(s["excluded_etfs"], [{"code": "00981A", "date": "2026-10-20", "reason": "清單列了這檔股票卻缺股數，不當成 0"}])
        self.assertEqual([c["code"] for c in s["etfs"]], ["00985A"])

    def test_week_sums_daily_values(self):
        w = self.flow["periods"]["1w"]
        self.assertEqual((w["status"], w["baseline"], w["days"]),
                         ("可計算", "2026-10-13", ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]))
        # 00991A 缺 10/15 的清單：整段排除，不把相隔兩天的清單當成一天
        self.assertEqual(w["included_etfs"], ["00981A", "00985A"])
        self.assertIn({"code": "00991A", "reason": "缺 2026-10-15的完整持股清單"}, w["excluded_etfs"])
        # 先買後賣：淨股數 0（不變），金額是逐日日值加總（10/14 買 1,000 股 × 100、10/16 賣 1,000 股 × 110）
        s = self.stock("1w", "2603")
        self.assertEqual((s["buy_etfs"], s["sell_etfs"], s["net_shares"], s["buy_amount"], s["sell_amount"], s["net_amount"]),
                         (0, 0, 0, 0.001, 0.0011, -0.0001))
        self.assertEqual(s["etfs"][0]["action"], "不變")
        self.assertEqual([p["date"] for p in s["prices"]], ["2026-10-14", "2026-10-16"])
        # 證據：列入 ETF 的期初、期末清單；00981A 有取得時間
        ev = {(e["code"], e["date"]): e for e in w["lists"]}
        self.assertEqual(sorted(ev), [("00981A", "2026-10-13"), ("00981A", "2026-10-20"), ("00985A", "2026-10-13"), ("00985A", "2026-10-20")])
        e = ev[("00981A", "2026-10-20")]
        self.assertEqual((e["source"], e["stock_rows"], e["missing_qty"], e["first_observed_at"]), (SITE, 7, 1, "2026-10-20T20:00:00+08:00"))
        self.assertEqual(e["rows"], 10)  # 7 檔台股（2603 已在 10/16 賣掉）＋ 債券、期貨、外國股票各 1
        self.assertIsNone(ev[("00985A", "2026-10-20")]["observed_at"])

    def test_month_without_price_history_is_not_ranked(self):
        m = self.flow["periods"]["1m"]
        self.assertEqual((m["status"], m["reason_code"], m["baseline"], m["stocks"]), ("資料不足", "no_prices", "2026-09-18", []))
        self.assertIn("官方收盤價自 2026-10-13 起保存", m["reason"])

    def test_no_prices_or_calendar(self):
        self.con.execute("DELETE FROM stock_close")
        f = ah.build(self.con, self.items, "2026-10-20", CAL)
        self.assertIsNone(f["as_of"])
        self.assertEqual({p["reason_code"] for p in f["periods"].values()}, {"no_prices"})
        self.assertTrue(all(p["status"] == "資料不足" and p["stocks"] == [] for p in f["periods"].values()))
        self.con.execute("INSERT INTO stock_close(code, date, market, close, dataset) VALUES('2330', '2026-10-20', '上市', 1000, '11549')")
        f = ah.build(self.con, self.items, "2026-10-20", mc.Calendar({}, set()))
        self.assertEqual({p["reason_code"] for p in f["periods"].values()}, {"no_calendar"})

    def test_invalid_close_and_quantity_never_become_flow(self):
        for value in (-1, float("inf"), "NaN", "--", 0):
            with self.subTest(close=value):
                self.con.execute("UPDATE stock_close SET close=? WHERE code='2330' AND date='2026-10-20'", (value,))
                self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
                s = self.stock("1d", "2330")
                self.assertIsNone(s["net_amount"])
                self.assertTrue(s["counts_complete"])
                self.assertEqual(s["prices"], [])
                json.dumps(self.flow, allow_nan=False)
        for value in (-1, float("inf"), "NaN"):
            with self.subTest(qty=value):
                self.con.execute("UPDATE holding SET qty=? WHERE code='00981A' AND asset_code='2330' AND date='2026-10-20'", (value,))
                self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
                s = self.stock("1d", "2330")
                self.assertFalse(s["counts_complete"])
                self.assertEqual([c["code"] for c in s["etfs"]], ["00985A"])
                json.dumps(self.flow, allow_nan=False)

    def test_foreign_numeric_code_cannot_borrow_taiwan_price(self):
        self.con.execute("UPDATE holding SET asset_name='日本同碼公司' WHERE code='00981A' AND asset_code='2330'")
        self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
        s = self.stock("1d", "2330")
        self.assertFalse(s["complete"])
        self.assertFalse(s["counts_complete"])
        self.assertEqual(s["net_shares"], 2000)
        self.assertIn("台股身分", s["excluded_etfs"][0]["reason"])
        self.assertTrue(ah.stock_identity("2330", "臺灣積體電路製造", {"2330": {"台積電"}}))
        self.assertFalse(ah.stock_identity("2330", "台灣積體電路製造", {"2330": {"其他公司"}}))
        self.assertFalse(ah.stock_identity("2330", "台積電", {}))

    def test_unknown_list_date_is_excluded(self):
        self.con.execute("UPDATE holding SET source='日期待確認投信官網' WHERE code='00981A'")
        self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
        p = self.flow["periods"]["1d"]
        self.assertNotIn("00981A", p["included_etfs"])
        self.assertTrue(any(e["code"] == "00981A" and "持股基準日" in e["reason"] for e in p["excluded_etfs"]))
        self.assertTrue(all(e["date_basis"] for e in p["lists"]))

    def test_single_holder_large_jump_and_price_adjustment(self):
        self.con.execute("UPDATE holding SET qty=60000 WHERE code='00981A' AND asset_code='2882' AND date='2026-10-20'")
        self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
        self.assertIsNone(self.stock("1d", "2882"))
        self.assertTrue(any(e["asset_code"] == "2882" and "重大跳變" in e["reason"] for e in self.flow["periods"]["1d"]["excluded_stocks"]))
        # 小幅減資也可能沒有第二個持有者；股數與股價反向調整時保守排除。
        self.con.execute("UPDATE holding SET qty=27000 WHERE code='00981A' AND asset_code='2882' AND date='2026-10-20'")
        self.con.execute("UPDATE stock_close SET close=? WHERE code='2882' AND date='2026-10-20'", (60 / .9,))
        self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
        self.assertIsNone(self.stock("1d", "2882"))
        self.assertTrue(any(e["asset_code"] == "2882" and "反向調整" in e["reason"] for e in self.flow["periods"]["1d"]["excluded_stocks"]))

    def test_large_subscription_with_matching_shares_is_not_jump(self):
        self.con.execute("UPDATE holding SET qty=60000 WHERE code='00981A' AND asset_code='2882' AND date='2026-10-20'")
        self.con.execute("UPDATE fund_daily SET units=2000000 WHERE code='00981A' AND date='2026-10-20'")
        self.flow = ah.build(self.con, self.items, "2026-10-20", CAL)
        self.assertAlmostEqual(self.stock("1d", "2882")["net_amount"], .018)


class PipelineTest(unittest.TestCase):
    def test_pipeline_outputs_contract_and_evidence(self):
        import tempfile
        import fetch_etf as f
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(ROOT / "tests" / "fixtures"), "--db", str(db), "--out", str(out)]), 0)
            payload = json.loads(out.read_text("utf-8"))
            flow = payload["active_holdings"]
            self.assertEqual((flow["version"], sorted(flow["periods"])), (1, ["1d", "1m", "1w"]))
            # 測試資料沒有台股股價：期間都是資料不足、不出排行，但仍有狀態與原因
            for p in flow["periods"].values():
                self.assertEqual((p["status"], p["reason_code"], p["stocks"]), ("資料不足", "no_prices", []))
            self.assertIn("active_changes", payload)  # 既有欄位保留
            con = sqlite3.connect(db)
            # 每份官網完整清單都記下出處與完整度證據
            n = con.execute("SELECT COUNT(*) FROM holding_list WHERE complete = 1").fetchone()[0]
            self.assertEqual(n, con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT code, date FROM holding)").fetchone()[0])
            self.assertTrue(n > 0)
            con.close()
            self.assertTrue(any((Path(tmp) / "history" / "holding_lists").glob("*.csv")))


class ParseTest(unittest.TestCase):
    def test_nonfinite_prices_and_legacy_schema(self):
        rows = oq.parse_stocks([{"Date": "1151020", "Code": "2330", "Name": "台積電", "ClosingPrice": x}
                                for x in ("-1", "NaN", "Infinity", "--", "0")], [], {"2330"})
        self.assertEqual([r["close"] for r in rows], [None] * 5)
        self.assertTrue(all(r["name"] == "台積電" for r in rows))
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE stock_close(code TEXT, date TEXT, market TEXT, close REAL, dataset TEXT, first_observed_at TEXT, observed_at TEXT, PRIMARY KEY(code,date))")
        store.ensure_schema(con)
        self.assertIn("name", {r[1] for r in con.execute("PRAGMA table_info(stock_close)")})
        oq.save_stocks(con, rows[:1], "2026-10-20T20:00:00+08:00")
        self.assertEqual(con.execute("SELECT name, close FROM stock_close").fetchone(), ("台積電", None))
        con.close()

    def test_stock_prices_and_events(self):
        twse = [{"Date": "1151020", "Code": "2330", "ClosingPrice": "1,005.00"}, {"Date": "1151020", "Code": "0050", "ClosingPrice": "108"},
                {"Date": "1151020", "Code": "2454", "ClosingPrice": "0.00"}, {"Date": "1151020", "Code": "9999", "ClosingPrice": "10"}]
        tpex = [{"Date": "1151020", "SecuritiesCompanyCode": "6488", "Close": "500.00"}]
        rows = oq.parse_stocks(twse, tpex, {"2330", "2454", "6488", "0050"})
        # 只收持有的台股股票；ETF 不收；沒有成交存成空值
        self.assertEqual([(r["code"], r["market"], r["close"], r["dataset"]) for r in rows],
                         [("2330", "上市", 1005.0, "11549"), ("2454", "上市", None, "11549"), ("6488", "上櫃", 500.0, "11370")])
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        oq.save_stocks(con, rows, "2026-10-21T08:50:00+08:00")
        oq.save_stocks(con, [{**rows[0], "close": 1010.0}], "2026-10-21T18:00:00+08:00")
        self.assertEqual(con.execute("SELECT close, first_observed_at, observed_at FROM stock_close WHERE code='2330'").fetchone(),
                         (1010.0, "2026-10-21T08:50:00+08:00", "2026-10-21T18:00:00+08:00"))
        events = ah.parse_events(
            [{"Date": "1151028", "Code": "2025", "Exdividend": "權", "StockDividendRatio": "", "SubscriptionRatio": "0.23"},
             {"Date": "1151020", "Code": "0056", "Exdividend": "息"}],
            [{"ExRrightsExDividendDate": "1151028", "SecuritiesCompanyCode": "2063", "ExRrightsExDividend": "除權息",
              "StockDividendRatio": "0.09999999", "SubscriptionRatioToNewSharesIssued": "0.00000000"}])
        self.assertEqual([(e["code"], e["date"], e["stock_dividend_ratio"], e["subscription_ratio"]) for e in events],
                         [("2025", "2026-10-28", None, 0.23), ("2063", "2026-10-28", 0.09999999, None)])
        ah.save_events(con, events, "2026-10-21T08:50:00+08:00")
        self.assertEqual(ah.load_events(con), {"2063": [("2026-10-28", 0.09999999)]})
        con.close()


if __name__ == "__main__":
    unittest.main()
