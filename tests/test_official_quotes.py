"""交易所 OpenAPI 收盤行情原值保存的離線測試（欄位格式照兩個端點的實際回應，數值為合成樣本）。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_etf as f  # noqa: E402
import official_quotes as oq  # noqa: E402
import store  # noqa: E402

TWSE = [{"Date": "1151019", "Code": "0050", "Name": "元大台灣50", "TradeVolume": "25,300,000", "TradeValue": "1,320,000,000",
         "OpeningPrice": "52.00", "HighestPrice": "52.50", "LowestPrice": "51.90", "ClosingPrice": "52.35", "Change": "0.4500",
         "Transaction": "30000"},
        # 沒有成交：價格是空白，成交股數 0
        {"Date": "1151019", "Code": "00999X", "Name": "測試", "TradeVolume": "0", "TradeValue": "0", "OpeningPrice": "",
         "HighestPrice": "", "LowestPrice": "", "ClosingPrice": "0.00", "Change": "0.0000", "Transaction": "0"},
        {"Date": "1151019", "Code": "2330", "Name": "台積電", "ClosingPrice": "1500.00"},  # 個股不收
        {"Code": "0056", "Name": "元大高股息", "ClosingPrice": "36.10"}]  # 沒有資料日期不收
TPEX = [{"Date": "1151020", "SecuritiesCompanyCode": "00679B", "CompanyName": "元大美債20年", "Close": "28.00", "Change": "-0.03 ",
         "Open": "28.05", "High": "28.10", "Low": "27.95", "TradingShares": "9,500,000", "TransactionAmount": "266,000,000",
         "TransactionNumber": "1,200"}]


class OfficialQuotesTest(unittest.TestCase):
    def test_parse(self):
        rows = {r["code"]: r for r in oq.parse(TWSE, TPEX)}
        self.assertEqual(sorted(rows), ["0050", "00679B", "00999X"])
        a = rows["0050"]
        self.assertEqual((a["date"], a["market"], a["close"], a["change"], a["volume_shares"], a["trade_value"], a["transactions"]),
                         ("2026-10-19", "上市", 52.35, 0.45, 25300000.0, 1320000000.0, 30000.0))
        self.assertEqual((a["dataset"], a["source_url"]), ("盤後資訊 > 個股日成交資訊", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"))
        # 價格 0、空白是沒有成交，存成空值；成交股數 0 照存
        z = rows["00999X"]
        self.assertEqual((z["close"], z["open"], z["volume_shares"]), (None, None, 0.0))
        b = rows["00679B"]
        self.assertEqual((b["date"], b["market"], b["close"], b["change"], b["transactions"], b["dataset"]),
                         ("2026-10-20", "上櫃", 28.0, -0.03, 1200.0, "上櫃股票行情"))

    def test_save_keeps_first_observation(self):
        con = sqlite3.connect(":memory:")
        store.ensure_schema(con)
        oq.save(con, oq.parse(TWSE, TPEX), "2026-10-19T18:00:00+08:00")
        changed = [{**r, "close": 52.40} if r["code"] == "0050" else r for r in oq.parse(TWSE, TPEX)]
        oq.save(con, changed, "2026-10-20T08:50:00+08:00")
        self.assertEqual(con.execute("SELECT close, first_observed_at, observed_at FROM exchange_daily WHERE code='0050'").fetchone(),
                         (52.40, "2026-10-19T18:00:00+08:00", "2026-10-20T08:50:00+08:00"))
        con.close()

    def test_attributions(self):
        a = oq.attributions(oq.parse(TWSE, TPEX))
        self.assertEqual([(x["provider"], x["dataset_url"], x["data_date"]) for x in a],
                         [("臺灣證券交易所", "https://data.gov.tw/dataset/11549", "2026-10-19"),
                          ("證券櫃檯買賣中心", "https://data.gov.tw/dataset/11370", "2026-10-20")])
        self.assertTrue(all(x["license"] == "政府資料開放授權條款－第1版" and x["license_url"] == "https://data.gov.tw/license" for x in a))
        self.assertEqual(oq.attributions([]), [])

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "etf.db", Path(tmp) / "latest.json"
            self.assertEqual(f.main(["--fixtures", str(ROOT / "tests" / "fixtures"), "--db", str(db), "--out", str(out)]), 0)
            payload = json.loads(out.read_text("utf-8"))
            # 測試資料的上市行情沒有資料日期，只有上櫃那一筆寫入
            self.assertEqual([x["dataset"] for x in payload["attributions"]], ["上櫃股票行情"])
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT code, date, close FROM exchange_daily").fetchall(), [("00679B", "2026-10-16", 28.0)])
            con.close()
            # 依日期分檔保存
            self.assertTrue((Path(tmp) / "history" / "exchange_daily" / "2026-10-16.csv").exists())


if __name__ == "__main__":
    unittest.main()
