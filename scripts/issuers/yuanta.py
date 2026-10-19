"""元大投信 ETF 官網（www.yuantaetfs.com）。

資料來自官網前端使用的 JSON 介面 etfapi.yuantaetfs.com：
- ETFInformation/GetETFInformation：全部 ETF 基本資料、經理費、保管費
- PCF/Daily：每檔的申購買回清單，含正式淨值、淨資產、發行單位數、受益人數、成分權重
- ETFInformation/FactSet/{FUND_ID}：投資組合統計（現金殖利率、本益比等）
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import flat_fee, ymd

ISSUER = "元大"
SOURCE = "元大投信官網"
BASE = "https://etfapi.yuantaetfs.com/ectranslation"
COMMON = {"CompanyName": "YUANTAFUNDS", "PageName": "/", "AppName": "ETF", "Platform": "ETF"}


def api_url(api_type: str, func: str, device_id: str, **params) -> str:
    # 官網把後台資料走 /api/trans、行情資料走 /api/bridge，Device 參數也不同
    path, device = ("/api/bridge", "3") if api_type == "ETFAPI" else ("/api/trans", "4")
    query = {**COMMON, "APIType": api_type, "FuncId": func, "DeviceId": device_id, "Device": device, **params}
    return f"{BASE}{path}?{urllib.parse.urlencode(query)}"


def unwrap(payload):
    """後台介面包一層 {ResultCode, Data}；ResultCode 不是 0 代表失敗。行情介面直接回傳資料。"""
    if isinstance(payload, dict) and "ResultCode" in payload:
        return payload.get("Data") if payload.get("ResultCode") == 0 else None
    return payload


def official_fee(text) -> float | None:
    """官網公布的實際費率，例如「約0.0682%  (資料日期：2026/10/16)」。"""
    m = re.match(r"\s*約?\s*([\d.]+)\s*%", str(text or ""))
    return float(m.group(1)) if m else None


def parse_info(rows) -> dict[str, dict]:
    result = {}
    for e in rows or []:
        code = str(e.get("STK_CD") or "").strip()
        if not code or e.get("DELISTING"):
            continue
        mgmt_text = (e.get("MANAGEMENT_FEE") or "").strip() or None
        result[code] = {
            "code": code,
            "fund_id": str(e.get("FUND_ID") or "").strip() or None,
            "mgmt_fee": official_fee(e.get("MANAGEMENT_FEE_TEXT")) or flat_fee(mgmt_text),
            "mgmt_fee_text": mgmt_text,
            "custodian_fee_text": (e.get("CUSTODIAN_FEE") or "").strip() or None,
            "manager": (e.get("MANAGER") or "").strip() or None,
            "risk_class": (e.get("RISK_CLASS") or "").strip() or None,
            "list_date": ymd(e.get("LIST_DATE")),
            "index_name": (e.get("INDEX_FUND_NAME") or "").strip() or None,
        }
    return result


def parse_pcf(payload) -> dict:
    """申購買回清單。期貨型等沒有清單的 ETF，PCF 為 null，全部欄位回傳 None。"""
    pcf = (payload or {}).get("PCF") or {}
    bf = pcf.get("Beneficiary") or {}
    totalav = pcf.get("totalav")
    holdings = []
    weights = (payload or {}).get("FundWeights") or {}
    for key, kind in (("StockWeights", "股票"), ("BondWeights", "債券"), ("ETFWeights", "基金"), ("FutureWeights", "期貨")):
        for w in weights.get(key) or []:
            if not isinstance(w, dict):
                continue
            holdings.append({
                "asset_code": str(w.get("code") or "").strip() + (f"-{w['ym']}" if w.get("ym") else ""),
                "asset_name": w.get("name"),
                "kind": kind,
                "weight": w.get("weights"),
                "qty": w.get("qty"),
            })
    month = str(bf.get("doc_ym") or "")
    return {
        "nav_official": pcf.get("nav"),
        "nav_date": ymd(pcf.get("trandate")),
        "aum": round(totalav / 1e8, 2) if totalav else None,
        "units": pcf.get("osunit"),
        "units_change": pcf.get("issuesdiff"),  # 當日申購買回造成的發行單位數增減
        "beneficiaries": bf.get("bf_cnt_a"),
        "beneficiaries_month": f"{month[:4]}-{month[4:6]}" if len(month) >= 6 else None,
        "holdings": holdings,
    }


def parse_factset(data) -> dict | None:
    if not isinstance(data, dict) or not data.get("DATA_DATE"):
        return None
    return {"date": ymd(data["DATA_DATE"]), "div_yield": data.get("COL01"), "pe": data.get("COL02"),
            "pb": data.get("COL03"), "roe": data.get("COL04"), "beta": data.get("COL05"),
            "volatility": data.get("COL06")}


def fetch(get_json, fixtures: Path | None = None, context=None, **_) -> dict[str, dict]:
    """get_json(url) 由主程式提供（含重試與被擋偵測）。fixtures 指定時改讀本機測試資料。"""
    device_id = str(uuid.uuid4())

    def load(name: str, api_type: str, func: str, **params):
        if fixtures:
            path = fixtures / f"{name}.json"
            return json.loads(path.read_text("utf-8")) if path.exists() else None
        try:
            time.sleep(0.3)  # 放慢速度，避免造成官網負擔
            return get_json(api_url(api_type, func, device_id, **params))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {func} 抓取失敗（{exc}）", file=sys.stderr)
            return None

    info = parse_info(unwrap(load("info", "ETFBackstage", "ETFInformation/GetETFInformation")))
    if not info:
        print(f"警告：{SOURCE}連不上或格式改變，本次不使用元大資料。", file=sys.stderr)
        return {}

    def detail(code: str):
        rec = info[code]
        pcf = parse_pcf(load(f"pcf_{code}", "ETFAPI", "PCF/Daily", ticker=code))
        stats = None
        if rec["fund_id"]:
            stats = parse_factset(unwrap(load(f"factset_{rec['fund_id']}", "ETFBackstage",
                                              f"ETFInformation/FactSet/{rec['fund_id']}")))
        return code, {**rec, **pcf, "stats": stats, "issuer": ISSUER, "source": SOURCE,
                      "source_url": f"https://www.yuantaetfs.com/tradeInfo/pcf/{code}"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        return dict(pool.map(detail, sorted(info)))
