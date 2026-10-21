"""官方基本資料的分類、來源隔離與保存；所有輸入均為離線假資料。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import product_metadata as m
import product_profile as p
import fetch_etf as f
import sitca
import store


class MetadataTest(unittest.TestCase):
    def test_issuer_warning_and_parent_fund_do_not_define_geography(self):
        name=m.product_name("中國信託美日傘型基金之全球半導體基金（本子基金投資美國）")
        self.assertEqual(name,"全球半導體基金")
        self.assertEqual(m.region_label(name,"境內及境外","AH21")[0],"全球")
        self.assertEqual(m.strategy_label(name,"股票","被動式"),"半導體主題")
        name=m.product_name("中國信託優選投資等級美元債基金（本基金之配息）")
        self.assertEqual(m.region_label(name,"境內及境外","AH22")[0],"跨國（公告範圍）")

    def test_domestic_class_and_leverage_are_separate(self):
        fund={"name":"測試指數正向2倍基金","class_code":"AH13","investment_region":"境內及境外"}
        item={"code":"00998L"}
        p.attach([item],funds={"00998L":fund})
        self.assertEqual([item["product"][k] for k in ("asset_class","region","structure")],["股票","臺灣","槓桿"])

    def test_unknown_products_and_empty_scope_stay_unknown(self):
        self.assertEqual(m.region_label("美元基金",None,"AH22"),(None,None))
        self.assertEqual(m.region_label("美元基金","詳公開說明書","AH22"),(None,None))
        item={"code":"00999Z"};p.attach([item])
        self.assertIsNone(item["product"]["region"])
        self.assertIsNone(item["product"]["strategy"])

    def test_fresh_classification_and_changed_identity_override_reference(self):
        seed={"data_month":"2026-08","checked_at":"2026-09-01","funds":{"00998B":{"fund_id":"OLD","name":"舊基金","class_code":"AH22"}}}
        with patch.object(m,"reference",return_value=seed):
            fresh={"fund_id":"OLD","name":"新基金","class_code":"AH22","investment_region":"日本","data_month":"2026-09","fetched":"2026-10-10"}
            self.assertEqual(m.official_fund("00998B",{"00998B":fresh}), (fresh,"00998B","2026-09","2026-10-10"))
            changed={"fund_id":"NEW","name":"全新基金"}
            self.assertEqual(m.official_fund("00998B",{"00998B":changed})[0],changed)
            self.assertEqual(m.official_fund("00998B",{})[0]["name"],"舊基金")

    def test_foreign_line_shares_profile_but_not_value_currency(self):
        seed={"data_month":"2026-09","checked_at":"2026-10-21","funds":{"00636":{"fund_id":"TEST","name":"測試中國50基金","currency":"新台幣","class_code":"AH21","investment_region":"中國"}}}
        with patch.object(m,"reference",return_value=seed):
            item={"code":"00636K","name":"測試+U","nav":10,"nav_source":"測試投信官網（正式淨值）","aum":20,"aum_source":"測試投信官網"}
            p.attach([item])
            self.assertEqual(item["product"]["region"],"中國大陸")
            self.assertIsNone(item["product"]["nav_currency"])
            self.assertIsNone(item["product"]["aum_currency"])

    def test_official_value_currency_requires_matching_source(self):
        extra={"official_value_currency":"TWD","official_value_source":"測試甲投信官網"}
        with patch.object(p,"overrides",return_value={"00999U":extra}):
            for source,expected in (("測試甲投信官網","TWD"),("測試乙投信官網",None)):
                item={"code":"00999U","nav_source":source,"aum_source":source}
                p.attach([item])
                self.assertEqual(item["product"]["nav_currency"],expected)
                self.assertEqual(item["product"]["aum_currency"],expected)

    def test_listing_date_only_fills_missing_value(self):
        with patch.object(p,"overrides",return_value={"00999B":{"listed_date":"2026-01-02"}}):
            missing={"code":"00999B"};existing={"code":"00999B","listed_date":"2026-01-05"}
            p.attach([missing,existing])
            self.assertEqual(missing["listed_date"],"2026-01-02")
            self.assertEqual(existing["listed_date"],"2026-01-05")

    def test_manual_source_does_not_inherit_unrelated_month(self):
        with patch.object(p,"overrides",return_value={"0050":{"source_url":"https://example.invalid/product"}}):
            item={"code":"0050"};p.attach([item])
            self.assertNotIn("data_month",item["product"])
            self.assertEqual(item["product"]["classification_month"],"2026-09")

    def test_class_region_and_snapshot_dates_survive_database_rebuild(self):
        fixture=ROOT/"tests/fixtures/sitca"
        info=sitca.fetch(fixture)
        self.assertEqual(info["funds"]["0050"]["class_code"],"AH11")
        self.assertEqual(info["funds"]["0050"]["investment_region"],"台灣")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);con=store.open_db(root/"a.db",None)
            f.save_fund_info(con,info,"2026-10-21")
            store.dump(con,root/"history");con.close()
            con=store.open_db(root/"b.db",root/"history");con.close()
            loaded,checked=f.load_fund_info(root/"b.db")
            self.assertEqual(checked,"2026-10-21")
            self.assertEqual(loaded["funds"]["0050"]["class_code"],"AH11")
            self.assertEqual(loaded["funds"]["0050"]["investment_region"],"台灣")
            self.assertEqual(loaded["funds"]["0050"]["data_month"],"2026-09")


if __name__ == "__main__":
    unittest.main()
