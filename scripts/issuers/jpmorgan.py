"""摩根投信官網（am.jpmorgan.com/tw/zh/asset-management/twetf/，資料介面在 am.jpmorgan.com/FundsMarketingHandler/）。

官網前端使用的 JSON 介面（GET），單檔以 ISIN 查詢（參數名稱是 cusip）：
- fund-explorer：全部 ETF（代號 ticker、ISIN identifier）
- realtime/api/etf-price-tw：全部 ETF 的即時預估淨值（ICE 計算）與更新時間
- product-data：正式淨值與日期、規模、發行單位數、經理費與保管費、經理人、風險等級、上市日、
  申購買回清單摘要（與前日已發行單位差異數）與全部持股
- historicalData：配息紀錄（除息日、發放日、每單位配息）
投資海外的 ETF 正式淨值隔天才公布：收盤後的資料是前一天的淨值、規模與持股，但申購買回清單的
「與前日已發行單位差異數」已經是當天的，各自依標示的日期保存。
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from .common import num, ymd

ISSUER = "摩根"
SOURCE = "摩根投信官網"
BASE = "https://am.jpmorgan.com/FundsMarketingHandler/"
SITE = "https://am.jpmorgan.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PARAMS = {"country": "tw", "role": "twetf", "language": "zh", "userLoggedIn": "false"}
TWD = ("TWD", "新台幣", "新臺幣")
# 持股表 → 成分類型；選擇權、現金與其他不是成分
HOLDINGS = {"pcfEquityHoldings": "股票", "pcfBondHoldings": "債券", "pcfFutureHoldings": "期貨"}


class Api:
    """每秒最多送出一個請求，連線失敗最多重試 3 次；被擋（HTTP 403）就停止，之後的請求都不再送出。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context or ssl.create_default_context()))
        self.next_time = 0.0
        self.blocked = False

    def get(self, path: str, **params):
        if self.blocked:
            raise RuntimeError(f"{path}：已被網站擋下，不再送出請求")
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": SITE + "/tw/zh/asset-management/twetf/"}
        last = None
        for attempt in range(3):
            time.sleep(max(0.0, self.next_time - time.monotonic()) if attempt == 0 else 4)
            self.next_time = time.monotonic() + 1
            try:
                with self.opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8-sig"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 403:
                    self.blocked = True
                    break
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    self.blocked = True
                    break
        raise RuntimeError(f"{path}：{last}")


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return None if s in ("", "-", "--") else s


def pct_text(value) -> str | None:
    """官網的費率是數字（例如 0.75），原文寫成「0.75%」。"""
    n = num(value)
    return f"{n:g}%" if n is not None else None


# ---------- 解析 ----------

def parse_list(data) -> list[dict]:
    return [{"code": r["ticker"].strip(), "isin": r["identifier"].strip()}
            for r in (data if isinstance(data, list) else []) if isinstance(r, dict) and r.get("ticker") and r.get("identifier")]


def parse_estimates(data) -> dict[str, dict]:
    """即時預估淨值：inav 是預估淨值，inavDatetime 是更新時間（台北時間）。"""
    out = {}
    for r in (data or {}).get("marketData") or []:
        est, day = num(r.get("inav")), ymd(r.get("inavDatetime"))
        if r.get("securityCode") and est and day:
            out[r["securityCode"].strip()] = {"nav_estimate": est, "nav_estimate_date": day}
    return out


def pcf_rows(fund: dict) -> dict[str, tuple[str, float | None]]:
    """申購買回清單摘要：欄名是「日期 項目」，例如「2026/10/16 與前日已發行單位差異數」→ {項目: (日期, 數值)}。"""
    out = {}
    for r in ((fund.get("breakdowns") or {}).get("m12Details") or {}).get("data") or []:
        m = re.match(r"\s*(\d{4}/\d{1,2}/\d{1,2})\s*(.+)", r.get("name") or "")
        if m:
            out[m.group(2).strip()] = (ymd(m.group(1)), num(r.get("displayValue")))
    return out


def parse_product(data, code: str) -> dict:
    """單檔資料；找不到這檔基金時回傳 {}。"""
    fund = (data or {}).get("fundData") or {}
    sc = fund.get("shareClass") or {}
    if not sc:
        return {}
    nav, fees = sc.get("nav") or {}, sc.get("fees") or {}
    twd = (sc.get("currencyCode") or fund.get("currencyCode")) in TWD
    total = num(sc.get("netGrossAssetClass"))
    risk = next((r.get("fundRiskRatingValue") for r in fund.get("fundIndustryRiskRating") or [] if r.get("fundRiskRatingValue")), None)
    managers = [m["fullName"].strip() for m in fund.get("fundManagers") or [] if (m.get("fullName") or "").strip()]
    change = pcf_rows(fund).get("與前日已發行單位差異數", (None, None))
    holdings, day = [], None
    for key, kind in HOLDINGS.items():
        table = (fund.get("holdings") or {}).get(key) or {}
        for h in table.get("data") or []:
            # 債券的 ticker 是發行機構代號、不唯一，改用 ISIN
            asset = (text(h.get("securityIsin")) if kind == "債券" else None) or text(h.get("securityTicker")) or text(h.get("securityIsin"))
            if asset:
                holdings.append({"asset_code": asset, "asset_name": text(h.get("securityDescription")), "kind": kind,
                                 "weight": num(h.get("marketValuePercent")), "qty": num(h.get("shares"))})
                day = day or ymd(table.get("effectiveDate") or h.get("navDate"))
    mgmt = pct_text(fees.get("managementFee"))
    return {
        "twd": twd,
        "asset_date": ymd(nav.get("date")),
        "nav_official": num(nav.get("price")) if twd else None,
        "nav_date": ymd(nav.get("date")),
        "aum": round(total / 1e8, 2) if total and twd else None,
        "units": num(sc.get("sharesOutstanding")),
        # 申購買回清單的單位差異有自己的日期；海外 ETF 會比淨值日期晚一天
        "units_change": change[1],
        "pcf_date": change[0],
        "manager": "、".join(managers) or None,
        "risk_class": text(risk),
        "list_date": ymd(sc.get("listingDate")),
        "mgmt_fee": num(fees.get("managementFee")),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": pct_text(fees.get("custodianFee")),
        "holdings": holdings,
        "holdings_date": day,
    }


def parse_dividends(data, code: str) -> list[dict]:
    return [{"code": code, "ex_date": ymd(r.get("exDate")), "pay_date": ymd(r.get("payDate")), "cash": num(r.get("dividendAmount"))}
            for r in (data or {}).get("fundDistribution") or [] if ymd(r.get("exDate"))]


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None,
          today: date | None = None, **_) -> dict[str, dict]:
    api = None
    if fixtures:
        def call(name, _path, **_params):
            path = fixtures / f"{name}.json"
            if not path.exists():
                raise RuntimeError(f"沒有測試資料 {path.name}")
            return json.loads(path.read_text("utf-8"))
    else:
        api = Api(context)

        def call(_name, path, **params):
            return api.get(path, **params)

    try:
        funds = parse_list(call("list", "fund-explorer", **PARAMS, fundType="etf"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用摩根資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 列表），本次不使用摩根資料。", file=sys.stderr)
        return {}
    try:
        estimates = parse_estimates(call("inav", "realtime/api/etf-price-tw"))
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
            product = parse_product(call(f"product_{code}", "product-data", cusip=f["isin"], **PARAMS), code)
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        if not product:
            print(f"警告：{SOURCE} {code} 基金資料格式改變，本次不使用這檔。", file=sys.stderr)
            continue
        twd = product.pop("twd")
        try:
            dividends = parse_dividends(call(f"history_{code}", "historicalData", cusip=f["isin"], **PARAMS), code)
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 配息紀錄抓取失敗（{exc}）", file=sys.stderr)
            dividends = []
        records[code] = {"code": code, "issuer": ISSUER, "source": SOURCE,
                         "source_url": BASE + "product-data?" + urllib.parse.urlencode({"cusip": f["isin"], **PARAMS}),
                         "currency": "TWD" if twd else None, "nav_estimate": None, "nav_estimate_date": None,
                         "index_name": None, "beneficiaries": None, "beneficiaries_month": None, "stats": None, **product,
                         **(estimates.get(code, {}) if twd else {}),
                         "dividends": dividends if twd else []}  # 外幣計價的配息金額不是新台幣，不收
    return records
