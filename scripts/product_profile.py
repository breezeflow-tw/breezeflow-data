"""商品維度與單位：官方編碼規則＋有來源的逐檔補充，未知值留空。"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
import product_metadata

ROOT = Path(__file__).resolve().parent.parent
CODE_SOURCE = "https://www.twse.com.tw/downloads/zh/ETF/ETFcode.pdf"
DUAL_SOURCE = "https://www.tpex.org.tw/zh-tw/product/etf/product/dual.html"


@lru_cache(maxsize=1)
def overrides():
    return json.loads((ROOT / "data/product_overrides.json").read_text("utf-8"))


def currency(value):
    return {"TWD":"TWD", "NTD":"TWD", "新台幣":"TWD", "新臺幣":"TWD", "台幣":"TWD",
            "USD":"USD", "美元":"USD", "CNY":"CNY", "RMB":"CNY", "人民幣":"CNY"}.get(str(value or "").strip())


def describe(code, name="", market=None):
    """編碼可判別產品結構，不能據此推斷投資地域或主題。"""
    valid = bool(re.fullmatch(r"00\d{2,4}(?:[ABCDKLMRSTUV])?", code))
    tail = code[-1:] if valid else ""
    foreign = tail in ("K", "C", "M", "S", "V")
    asset = {"A":"股票", "D":"債券", "B":"債券", "C":"債券", "K":"股票",
             "T":"多重資產", "U":"期貨", "V":"期貨"}.get(tail)
    if valid and tail.isdigit():
        asset = "股票"
    structure = "槓桿" if tail in ("L", "M") else "反向" if tail in ("R", "S") else "一般" if valid else None
    # 槓反可能追蹤股票、債券、商品或匯率，不從名稱推成股票。
    trading = ("USD" if re.search(r"[+＋]\s*(?:櫃)?U\s*$", name) else
               "CNY" if re.search(r"[+＋]\s*(?:櫃)?R\s*$", name) else None) if foreign else "TWD" if valid else None
    unit = 100 if foreign and market == "上櫃" else 1000 if valid and not foreign else None
    extra = overrides().get(code, {})
    profile = {"asset_class":asset, "management":"主動式" if tail in ("A", "D") else "被動式" if valid else None,
               "structure":structure, "region":None, "strategy":None, "trading_currency":trading,
               "trading_unit":unit, "main_code":None, "source":"交易所 ETF 編碼規則",
               "source_url":CODE_SOURCE, "checked_at":"2026-10-21", "category":None,
               "rules_url":CODE_SOURCE, "currency_basis":"交易所加掛外幣簡稱規則" if foreign else "交易所新臺幣交易線",
               "currency_source_url":DUAL_SOURCE if foreign and market == "上櫃" else CODE_SOURCE}
    profile.update(extra)
    return profile


def attach(items, issuer=None, funds=None):
    """幣別隨所採用的欄位來源，不以交易幣別覆蓋基金規模幣別。"""
    for item in items:
        code = item["code"]
        p = describe(code, item.get("name", ""), item.get("market"))
        product_metadata.enrich(p, code, funds or {})
        extra = overrides().get(code, {})
        p.update(extra)
        if extra.get("source_url") and "data_month" not in extra:
            p.pop("data_month", None)  # 公會月份不冒充人工核對之投信頁面的資料月份。
        item["product"] = p
        if not item.get("listed_date") and p.get("listed_date"):
            item["listed_date"] = p["listed_date"]
        r, f = (issuer or {}).get(code, {}), (funds or {}).get(code, {})
        # 基準幣別可取同基金申報；數值幣別仍須核對同一交易線及數值來源。
        ref, ref_code, _, _ = product_metadata.official_fund(code, funds or {})
        value_currency = currency(p.get("official_value_currency"))
        value_source = p.get("official_value_source")
        nav_source, aum_source = item.get("nav_source") or "", item.get("aum_source") or ""
        if "正式淨值" in nav_source and ("SITCA" in nav_source or "投信投顧公會" in nav_source):
            nav_currency = "TWD"  # 公會每日淨值解析器僅收 TWD。
        elif "官網" in nav_source:
            nav_currency = currency(r.get("currency")) or currency(f.get("currency"))
            if not nav_currency and ref_code == code:
                nav_currency = currency(ref.get("currency"))
            if not nav_currency and value_source and nav_source.startswith(value_source):
                nav_currency = value_currency
        elif "證交所" in nav_source:
            nav_currency = p["trading_currency"]  # 同一交易代號的預估淨值。
        else:
            nav_currency = None
        if "SITCA" in aum_source or "投信投顧公會" in aum_source:
            aum_currency = currency((item.get("sitca_monthly") or {}).get("currency"))
        elif "官網" in aum_source:
            aum_currency = currency(item.get("aum_currency")) or currency(r.get("currency"))
            if not aum_currency and value_source and aum_source.startswith(value_source):
                aum_currency = value_currency
        else:
            aum_currency = None
        p.update(nav_currency=nav_currency, aum_currency=aum_currency,
                 fund_currency=currency(f.get("currency")) or currency(r.get("currency")) or currency(ref.get("currency")) or p.get("fund_currency"))
        if p.get("category"):
            item["category"] = p["category"]
        if not item.get("price_check"):
            item["price_check"] = "狀態未提供" if item.get("price") is not None else "無市價"
        # 幣別無法對齊時不拿兩個價格計算折溢價。原始數值仍留給使用者核對。
        if item.get("premium") is not None and (not nav_currency or nav_currency != p["trading_currency"]):
            item["premium"] = None
            item["premium_reason"] = "市價與淨值幣別未確認一致"


def save(con, items):
    con.executemany("INSERT OR REPLACE INTO product_profile(code, profile_json) VALUES(?, ?)",
                    [(it["code"], json.dumps(it["product"], ensure_ascii=False)) for it in items])
