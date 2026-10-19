"""野村投信官網（www.nomurafunds.com.tw/ETFWEB，資料介面在 www.nomurafunds.com.tw/API/ETFAPI/api/）。

官網前端使用的 JSON 介面（POST），回應格式為 {StatusCode: 0, Message, Entries}：
- Fund/GetFundList：全部 ETF（代號 CNo、最新正式淨值 CNetValue 與淨值日期 CDataDt）
- Fund/GetFundIntradayNAV：全部 ETF 的即時預估淨值與更新時間
- Fund/GetFundDetailPage（FundNo）：上市日、經理人、經理費與保管費原文、風險等級、追蹤指數、計價幣別，
  以及完整配息歷史（除息日、發放日）
- Fund/GetFundTradeInfoDate（FundNo）：有申購買回清單的日期，LatestDate 是最新一份；還沒上市的 ETF 沒有
- Fund/GetFundTradeInfo（FundNo、Date）：申購買回清單，含淨資產、已發行單位數、與前日單位差異、受益人數與全部成分。
  Date 是清單適用的交易日，淨值與單位數是更早的淨值日期 CNavDtStr 那天的資料
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from .common import flat_fee, num, ymd

ISSUER = "野村"
SOURCE = "野村投信官網"
BASE = "https://www.nomurafunds.com.tw/API/ETFAPI/api/"
SITE = "https://www.nomurafunds.com.tw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TWD = ("新台幣", "新臺幣", "TWD")
# 申購買回清單的成分表 → (成分類型, 代號, 名稱, 權重 %, 數量)；債券的數量是面額
HOLDINGS = {"Stocks": ("股票", "CStockCode", "CStockName", "CWeightsPct", "CQuantity"),
            "Bonds": ("債券", "CBondCode", "CBondName", "CHoldRatio", "CBalParValue"),
            "Futures": ("期貨", "CFuturesCode", "CFuturesName", "CWeightsPct", "CQuantity"),
            "Etfs": ("基金", "CStockCode", "CStockName", "CWeightsPct", "CQuantity")}


class Api:
    """每秒最多送出一個請求，連線失敗最多重試 3 次；被擋（HTTP 403）就停止，之後的請求都不再送出。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context or ssl.create_default_context()))
        self.next_time = 0.0
        self.blocked = False

    def post(self, path: str, **data):
        if self.blocked:
            raise RuntimeError(f"{path}：已被網站擋下，不再送出請求")
        body = json.dumps(data).encode()
        headers = {"User-Agent": UA, "Content-Type": "application/json; charset=utf-8", "Accept": "application/json, text/plain, */*",
                   "Origin": SITE, "Referer": SITE + "/ETFWEB/"}
        last = None
        for attempt in range(3):
            time.sleep(max(0.0, self.next_time - time.monotonic()) if attempt == 0 else 4)
            self.next_time = time.monotonic() + 1
            try:
                with self.opener.open(urllib.request.Request(BASE + path, data=body, headers=headers), timeout=60) as resp:
                    return unwrap(json.loads(resp.read().decode("utf-8-sig")), path)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 403:
                    self.blocked = True
                    break
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    self.blocked = True
                    break
        raise RuntimeError(f"{path}：{last}")


def unwrap(payload, path: str = ""):
    """StatusCode 不是 0 視為失敗。"""
    if not isinstance(payload, dict) or payload.get("StatusCode") != 0:
        raise RuntimeError(f"{path}：回應失敗（{payload.get('Message') if isinstance(payload, dict) else payload}）")
    return payload.get("Entries")


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return None if s in ("", "-", "--") else s


def index_name(value) -> str | None:
    """追蹤指數：去掉名稱後面的指數代碼（例如「(IX0194 Index)」）。只寫代碼、沒有名稱的（例如「1306 JP」）不收。"""
    s = re.sub(r"\s*[(（][A-Z0-9]+ Index[)）]$", "", text(value) or "")
    return None if not s or re.fullmatch(r"[A-Z0-9]+(?: [A-Z]{2})?", s) else s


# ---------- 解析 ----------

def parse_list(entries) -> list[dict]:
    return [{"code": r["CNo"].strip(), "nav_official": num(r.get("CNetValue")), "nav_date": ymd(r.get("CDataDt"))}
            for r in entries or [] if isinstance(r, dict) and r.get("CNo")]


def parse_estimates(entries) -> dict[str, dict]:
    """即時預估淨值：CEstimateNav 是預估淨值，CDataDt 是更新時間。"""
    out = {}
    for r in entries or []:
        est, day = num(r.get("CEstimateNav")), ymd(r.get("CDataDt"))
        if r.get("CFundId") and est and day:
            out[r["CFundId"].strip()] = {"nav_estimate": est, "nav_estimate_date": day}
    return out


def parse_detail(entries, code: str) -> dict:
    """基金資料頁；找不到這檔基金時回傳 {}。"""
    d = (entries or {}).get("FundDetail") or {}
    if str(d.get("CNo") or "").strip() != code:
        return {}
    mgmt = text(d.get("CManagerFee"))
    return {
        "currency": text(d.get("CCgCurrencyTypeName")),
        "index_name": index_name(d.get("CIndexCode")),
        "list_date": ymd(d.get("CListingDt")),
        "manager": text(d.get("CManager")),
        "risk_class": text(d.get("CCgRiskLevelName")),
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": text(d.get("CCustodyFee")),
        # 配息歷史：CExDate 是除息日、CPayableDate 是發放日、CBaseDate 是收益分配基準日
        "dividends": [{"code": code, "ex_date": ymd(r.get("CExDate")), "pay_date": ymd(r.get("CPayableDate")),
                       "cash": num(r.get("CPerShare"))}
                      for r in ((entries or {}).get("FundYield") or {}).get("Data") or [] if ymd(r.get("CExDate"))],
    }


def parse_pcf(entries) -> dict:
    """申購買回清單。淨資產、單位數、單位差異與成分都是淨值日期那天的資料；受益人數另有資料日期。"""
    t = entries if isinstance(entries, dict) else {}
    day = ymd(t.get("CNavDtStr") or t.get("CNavDt"))
    if not t.get("CFundId") or not day:
        return {}
    holdings = []
    for key, (kind, code_key, name_key, weight_key, qty_key) in HOLDINGS.items():
        for h in t.get(key) or []:
            code = text(h.get(code_key))
            ym = re.sub(r"\D", "", str(h.get("CContractYm") or ""))
            if code:
                holdings.append({"asset_code": code + (f"-{ym}" if kind == "期貨" and ym else ""), "asset_name": text(h.get(name_key)),
                                 "kind": kind, "weight": num(h.get(weight_key)), "qty": num(h.get(qty_key))})
    total = num(t.get("CAnceTotalAv"))
    counted = ymd(t.get("CBeneficiariesCountDate"))
    return {
        "code": t["CFundId"].strip(),
        "pcf_date": day,
        "aum": round(total / 1e8, 2) if total else None,
        "units": num(t.get("CAnceTotalIssues")),
        "units_change": num(t.get("CAnceIssuesDiff")),
        "beneficiaries": num(t.get("CBeneficiariesCount")),
        "beneficiaries_month": counted[:7] if counted else None,
        # 債券 ETF 的基金資料頁只寫指數代碼，指數名稱在清單的債券指標裡
        "bond_index": text((t.get("BondProperty") or {}).get("CIndexName")),
        "holdings": holdings,
    }


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None,
          today: date | None = None, **_) -> dict[str, dict]:
    today = today or date.today()
    api = None
    if fixtures:
        def call(name, _path, **_data):
            path = fixtures / f"{name}.json"
            if not path.exists():
                raise RuntimeError(f"沒有測試資料 {path.name}")
            return unwrap(json.loads(path.read_text("utf-8")), name)
    else:
        api = Api(context)

        def call(_name, path, **data):
            return api.post(path, **data)

    try:
        funds = parse_list(call("list", "Fund/GetFundList", Type=2, Keyword="", No="", FundType=0, PageIndex=1, PageSize=200))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用野村資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 列表），本次不使用野村資料。", file=sys.stderr)
        return {}
    try:
        estimates = parse_estimates(call("inav", "Fund/GetFundIntradayNAV", Type=5, Keyword="", FundNo="", FundType=0))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}即時預估淨值抓取失敗（{exc}）", file=sys.stderr)
        estimates = {}

    records = {}
    for f in funds:
        if api and api.blocked:
            print(f"警告：{SOURCE}擋下了請求，其餘 ETF 本次不抓。", file=sys.stderr)
            break
        code = f["code"]
        try:
            detail = parse_detail(call(f"detail_{code}", "Fund/GetFundDetailPage", FundNo=code, IsPreview=False, SearchType=1,
                                       Year=today.year, SDate="1900-01-01T00:00:00", EDate="2200-01-01T00:00:00"), code)
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        if not detail:
            print(f"警告：{SOURCE} {code} 基金資料格式改變，本次不使用這檔。", file=sys.stderr)
            continue
        twd = detail.pop("currency") in TWD
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE,
               "source_url": f"{SITE}/ETFWEB/product-description?fundNo={code}&tab=basic",
               "currency": "TWD" if twd else None, "nav_estimate": None, "nav_estimate_date": None,
               "units_change": None, "aum": None, "units": None, "beneficiaries": None, "beneficiaries_month": None,
               "stats": None, "holdings": [], **detail,
               "nav_official": f["nav_official"] if twd else None, "nav_date": f["nav_date"],
               **(estimates.get(code, {}) if twd else {})}
        if not twd:
            rec["dividends"] = []  # 外幣計價的配息金額不是新台幣，不收
        try:
            latest = (call(f"tradedate_{code}", "Fund/GetFundTradeInfoDate", Type=1, FundNo=code, Keyword="") or {}).get("LatestDate")
            pcf = parse_pcf(call(f"trade_{code}", "Fund/GetFundTradeInfo", Type=1, FundNo=code, Keyword="", Date=latest)) if latest else {}
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 申購買回清單抓取失敗（{exc}）", file=sys.stderr)
            pcf = {}
        if pcf.get("code") == code:
            rec["index_name"] = rec["index_name"] or pcf["bond_index"]
            if not twd:
                pcf["aum"] = None  # 規模只收新台幣計價，外幣計價的不自行換算
            rec.update({k: v for k, v in pcf.items() if k not in ("code", "bond_index")})
        records[code] = rec
    return records
