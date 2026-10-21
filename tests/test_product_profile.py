"""商品口徑離線回歸：假資料不連網，不補猜缺失的幣別或主題。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import product_profile as p
import fetch_etf as f
import store


class ProductProfileTest(unittest.TestCase):
    def test_active_bonds_and_equities_are_independent_dimensions(self):
        for code in ("00980D", "00981D", "00982D", "00983D", "00984D", "00985D", "00986D", "00987D"):
            self.assertEqual(f.classify(code, "測試收益"), "主動式")
            self.assertEqual(p.describe(code)["management"], "主動式")
            self.assertEqual(p.describe(code)["asset_class"], "債券")
        self.assertEqual(p.describe("00981A")["asset_class"], "股票")

    def test_multifactor_products_and_unknown_do_not_become_market_cap(self):
        self.assertEqual(p.describe("00980T")["asset_class"], "多重資產")
        self.assertIsNone(p.describe("00980T")["region"])
        self.assertEqual(f.classify("009999", "測試新品種"), "待分類")
        self.assertIsNone(p.describe("00999Z")["management"])
        self.assertIsNone(p.describe("00631L")["asset_class"])
        self.assertEqual(p.describe("00631L")["structure"], "槓桿")

    def test_verified_theme_examples(self):
        for code in ("00875", "009828"):
            self.assertEqual(f.classify(code, "測試名稱"), "主題型")
            self.assertEqual(p.describe(code)["strategy"], "科技主題")
            self.assertTrue(p.describe(code)["source_url"].startswith("https://"))

    def test_dollar_bond_line_has_its_own_units_and_main_code(self):
        product = p.describe("00687C", "國泰20年美債+櫃U", "上櫃")
        self.assertEqual((product["asset_class"], product["trading_currency"], product["trading_unit"]), ("債券", "USD", 100))
        self.assertEqual(product["main_code"], "00687B")
        self.assertEqual(f.main_line("00687C"), "00687B")
        self.assertEqual(p.describe("00687B")["trading_unit"], 1000)

    def test_foreign_currency_is_not_inferred_from_suffix_alone(self):
        self.assertEqual(p.describe("00625K", "測試+R")["trading_currency"], "CNY")
        self.assertEqual(p.describe("00636K", "測試+U")["trading_currency"], "USD")
        self.assertIsNone(p.describe("00999K", "未確認名稱")["trading_currency"])
        self.assertEqual(p.describe("00636K", "測試+U")["trading_unit"], 100)
        self.assertIsNone(p.describe("00999K", "未確認名稱")["trading_unit"])

    def test_currency_mismatch_cannot_create_a_premium(self):
        item = {"code":"00687C", "name":"測試", "price":10, "nav":300, "premium":-96.67, "nav_source":"測試投信官網（正式淨值）"}
        p.attach([item], {"00687C":{"currency":"TWD"}})
        self.assertIsNone(item["premium"])
        self.assertEqual(item["price_check"], "狀態未提供")
        self.assertEqual(item["product"]["nav_currency"], "TWD")
        self.assertEqual(item["product"]["trading_currency"], "USD")

    def test_nav_aum_and_fund_currency_are_not_interchangeable(self):
        item = {"code":"00687C", "name":"測試", "nav_source":"證交所（投信預估淨值）", "aum_source":"投信投顧公會（SITCA）月統計", "sitca_monthly":{"currency":"TWD"}, "price":10,"premium":1}
        p.attach([item], funds={"00687C":{"currency":"USD"}})
        self.assertEqual(item["product"]["aum_currency"], "TWD")
        self.assertEqual(item["product"]["nav_currency"], "USD")
        self.assertEqual(item["premium"], 1)

    def test_unknown_aum_currency_stays_unknown(self):
        item = {"code":"0050", "aum":100, "aum_source":"測試投信官網", "price":None}
        p.attach([item])
        self.assertIsNone(item["product"]["aum_currency"])
        self.assertEqual(item["price_check"], "無市價")

    def test_quality_total_includes_missing_status(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)/"latest.json"
            items=[{"price":10,"price_check":"一致"},{"price":1,"price_check":None},{"price":None}]
            f.save_json(items, "2026-01-02", "fixture", target)
            payload=json.loads(target.read_text("utf-8"))
            self.assertEqual(payload["price_check"], {"一致":1,"狀態未提供":1,"無市價":1})
            self.assertEqual(sum(payload["price_check"].values()), payload["count"])

    def test_profile_survives_csv_database_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/"test.db"
            con=store.open_db(db,None)
            item={"code":"00984D"}
            p.attach([item]);p.save(con,[item]);con.commit()
            store.dump(con,Path(td)/"history");con.close()
            restored=store.open_db(Path(td)/"restored.db",Path(td)/"history")
            self.assertEqual(json.loads(restored.execute("SELECT profile_json FROM product_profile").fetchone()[0])["asset_class"], "債券")
            restored.close()

    def test_excluded_product_is_removed_from_new_table(self):
        with tempfile.TemporaryDirectory() as td:
            con=store.open_db(Path(td)/"test.db",None)
            items=[{"code":"00847B"},{"code":"00984D"}]
            p.attach(items);p.save(con,items);f.purge_excluded(con)
            self.assertEqual(con.execute("SELECT code FROM product_profile").fetchall(),[("00984D",)])
            con.close()

if __name__ == "__main__":
    unittest.main()
