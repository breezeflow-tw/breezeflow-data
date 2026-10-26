"""網站資料拆分：列表精簡檔只拿掉詳情專用欄位，單檔完整檔與 latest.json 相同。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import site_data


def item(code="0050", **extra):
    base = {
        "code": code, "name": "測試基金", "issuer": "測試投信", "category": "股票", "market": "上市",
        "price": 20.5, "change_pct": -1.2, "volume_lots": 1000, "price_check": "一致",
        "nav": 20.4, "nav_source": "投信官網正式淨值", "nav_date": "2026-10-22", "nav_source_url": "https://example.test/nav",
        "nav_prev": 20.6, "premium": 0.49, "premium_reason": None,
        "aum": 100.0, "aum_source": "投信官網", "aum_date": "2026-10-22", "aum_source_url": "https://example.test/aum",
        "units": 5, "units_source": "投信官網", "units_date": "2026-10-22", "units_source_url": "https://example.test/units",
        "units_change": 1, "mgmt_fee": 0.3, "mgmt_fee_text": "0.3%", "custodian_fee_text": "0.035%", "fee_source": "投信官網",
        "fee": 0.4, "manager": "測試經理人", "risk_class": "RR4", "listed_date": "2003-06-30", "index_name": "測試指數",
        "beneficiaries": 10, "beneficiaries_month": "2026-09", "beneficiaries_source": "公會",
        "beneficiaries_source_url": "https://example.test/b", "source_time": "",
        "stats": {"date": "2026-10-22", "pe": 15},
        "holdings_top": [{"asset_code": "2330", "asset_name": "台積電", "kind": "股票", "weight": 50.0, "qty": 1}],
        "holdings_source": "投信官網", "holdings_date": "2026-10-22", "holdings_source_url": "https://example.test/h",
        "holdings_changes": None, "next_ex": None,
        "last_ex": {"ex_date": "2026-07-21", "cash": 1.0, "pay_date": "2026-08-15", "source": "投信官網"},
        "expense": {"year": 2025, "mgmt": 0.3, "custodian": 0.03, "other": 0.01, "total": 0.34, "source": "公會"},
        "monthly": {"month": "2026-09", "aum": 99}, "sitca_monthly": {"month": "2026-09", "aum": 99},
        "flows": {"net_amount": 1.5, "net_rate": 0.2, "units_rate": 0.1, "aum_change": 2.0, "status": "可估算",
                  "reason": None, "as_of": "2026-10-22", "four_week_amount": 3.0, "positive_weeks": 3,
                  "available_weeks": 4, "events": [], "weeks": [{"start": "2026-10-19"}],
                  "sources": [{"date": "2026-10-22", "url": "https://example.test/f"}], "method": "說明",
                  "events_status": "待核對", "trading_days": ["2026-10-22"], "closed_days": [], "calendar": "官方開休市日期",
                  "latest_official_date": "2026-10-22"},
        "product": {"asset_class": "股票", "management": "被動式", "structure": "一般", "region": "台灣",
                    "strategy": "市值", "trading_currency": "TWD", "nav_currency": "TWD", "aum_currency": "TWD",
                    "main_code": None, "official_name": "測試證券投資信託基金", "source_url": "https://example.test/p",
                    "rules_url": "https://example.test/r", "region_basis": "公開說明書", "trading_unit": "1000"},
        "returns": {"price": {"1w": 1.0, "1m": 2.0}, "total": {"1w": 1.1, "1m": 2.2}},
    }
    base.update(extra)
    return base


def payload(*items):
    return {"generated_at": "2026-10-22T18:30:00+08:00", "trade_date": "2026-10-22", "source": "live",
            "returns_info": {"starts": {"1w": "2026-10-15"}}, "active_holdings": {"version": 1},
            "active_changes": None, "attributions": [], "count": len(items), "items": list(items)}


# 網站列表（行情、卡片、篩選、排序、排行榜、週資金流、資料查核）用到的欄位，精簡檔一定要有。
LIST_FIELDS = ("code", "name", "issuer", "category", "market", "price", "change_pct", "volume_lots", "price_check",
               "nav", "nav_source", "nav_date", "premium", "premium_reason", "aum", "aum_source", "aum_date",
               "units", "units_source", "units_date", "holdings_source", "holdings_date", "mgmt_fee",
               "mgmt_fee_text", "custodian_fee_text", "expense", "next_ex", "last_ex", "flows", "product", "returns")
LIST_NESTED = {
    "flows": ("net_amount", "net_rate", "units_rate", "aum_change", "reason", "as_of", "four_week_amount",
              "positive_weeks", "events"),
    "product": ("asset_class", "management", "structure", "region", "strategy", "trading_currency", "nav_currency",
                "aum_currency", "main_code"),
    "returns": ("price",),
}


class SiteDataTest(unittest.TestCase):
    def test_list_fields_are_never_detail_only(self):
        self.assertFalse(set(LIST_FIELDS) & set(site_data.DETAIL_ONLY))
        for key, fields in LIST_NESTED.items():
            self.assertFalse(set(fields) & set(site_data.DETAIL_ONLY_NESTED.get(key, ())), key)

    def test_summary_drops_only_detail_fields_and_keeps_new_ones(self):
        full = item(new_field="日後新增欄位")
        before = copy.deepcopy(full)
        slim = site_data.summary_item(full)
        self.assertEqual(full, before)  # 不修改原始資料
        for key in site_data.DETAIL_ONLY:
            self.assertNotIn(key, slim)
        for key in LIST_FIELDS:
            self.assertIn(key, slim)
        self.assertEqual(slim["new_field"], "日後新增欄位")
        self.assertEqual(slim["holdings_count"], 1)
        self.assertEqual(set(slim["flows"]), set(full["flows"]) - set(site_data.DETAIL_ONLY_NESTED["flows"]))
        self.assertEqual(slim["flows"]["net_amount"], 1.5)
        self.assertEqual(slim["product"]["trading_currency"], "TWD")
        self.assertNotIn("source_url", slim["product"])
        self.assertEqual(slim["returns"], {"price": {"1w": 1.0, "1m": 2.0}})
        self.assertEqual(slim["expense"], full["expense"])

    def test_missing_or_null_nested_values_stay_as_they_are(self):
        slim = site_data.summary_item(item(flows=None, product=None, returns=None, holdings_top=None))
        self.assertIsNone(slim["flows"])
        self.assertIsNone(slim["product"])
        self.assertIsNone(slim["returns"])
        self.assertEqual(slim["holdings_count"], 0)
        self.assertNotIn("holdings_top", slim)

    def test_details_keep_full_items_with_the_same_stamp(self):
        data = payload(item("0050"), item("00625K", holdings_top=[]))
        summary, details = site_data.build(data)
        self.assertEqual(list(details), ["0050", "00625K"])
        self.assertEqual(details["0050"]["item"], data["items"][0])
        self.assertEqual(details["0050"]["generated_at"], summary["split"]["generated_at"])
        self.assertEqual(summary["split"]["detail"], "etf/{code}.json")
        self.assertEqual([i["code"] for i in summary["items"]], ["0050", "00625K"])
        self.assertEqual(summary["items"][1]["holdings_count"], 0)
        for key in ("generated_at", "trade_date", "returns_info", "active_holdings", "attributions", "count"):
            self.assertEqual(summary[key], data[key])

    def test_unsafe_or_duplicate_codes_stop(self):
        for code in (None, "", "../x", "00 50", "0050a", "0050.json", "123"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                site_data.build(payload(item(code)))
        with self.assertRaisesRegex(ValueError, "重複"):
            site_data.build(payload(item("0050"), item("0050")))

    def test_write_replaces_old_details_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "etf").mkdir()
            (out / "etf/00847B.json").write_text("{}", "utf-8")  # 已排除的舊代號不能殘留
            (out / "etf/readme.txt").write_text("保留", "utf-8")
            data = payload(item("0050"), item("0056"))
            site_data.write(data, out)
            self.assertEqual(sorted(p.name for p in (out / "etf").glob("*.json")), ["0050.json", "0056.json"])
            self.assertTrue((out / "etf/readme.txt").exists())
            detail = json.loads((out / "etf/0056.json").read_text("utf-8"))
            self.assertEqual(detail["item"], data["items"][1])
            summary = json.loads((out / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["items"][0], site_data.summary_item(data["items"][0]))
            self.assertLess((out / "summary.json").stat().st_size, len(json.dumps(data, ensure_ascii=False)))

    def test_bad_code_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(ValueError):
                site_data.write(payload(item("0050"), item("../x")), out)
            self.assertFalse((out / "summary.json").exists())
            self.assertFalse((out / "etf").exists())


if __name__ == "__main__":
    unittest.main()
