"""群益投信官網（www.capitalfund.com.tw）。

官網前端使用的 JSON 介面（皆在 /CFWeb/api/etf/ 底下）：
- items：全部 ETF 的基金編號與代號
- nav：預估淨值（date1）與前一日正式淨值
- buyback：申購買回清單，含正式淨值（date2）、淨資產、發行單位數、當日申購買回增減單位數、成分
- detail/{基金編號}：經理費、保管費、經理人、上市日、追蹤指數
- dividendDataHistory：全部 ETF 的歷年配息（評價日、最後買進日、除息日、發放日、每單位配息）
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

from .common import flat_fee, num, ymd

ISSUER = "群益"
SOURCE = "群益投信官網"
BASE = "https://www.capitalfund.com.tw/CFWeb/api/etf"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


class Site:
    """保留 Cookie 的 JSON 連線，每次請求間隔 1 秒，失敗最多重試 3 次。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )

    def call(self, path: str, body=None, post: bool = True):
        headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                   "Referer": "https://www.capitalfund.com.tw/etf"}
        data = None
        if post:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        last = None
        for attempt in range(3):
            time.sleep(1 if attempt == 0 else 4)
            try:
                req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST" if post else "GET")
                with self.opener.open(req, timeout=40) as resp:
                    payload = json.loads(resp.read().decode("utf-8-sig"))
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    raise RuntimeError(f"{path}：{exc}") from exc
        else:
            raise RuntimeError(f"{path}：{last}")
        return unwrap(payload)


def unwrap(payload):
    """回應格式為 {code: 200, data: ...}；code 不是 200 視為失敗。"""
    if isinstance(payload, dict) and "code" in payload:
        return payload.get("data") if payload.get("code") == 200 else None
    return payload


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s or None


# ---------- 解析 ----------

def parse_items(rows) -> dict[str, str]:
    """基金編號 → ETF 代號。"""
    return {str(r["fundNo"]).strip(): str(r["stockNo"]).strip() for r in rows or [] if r.get("fundNo") and r.get("stockNo")}


def parse_nav_list(rows) -> dict[str, dict]:
    """預估淨值清單：nav 是當天預估淨值（date1），closeNav 盤後才有，正式淨值以申購買回清單為準。"""
    result = {}
    for r in rows or []:
        code = str(r.get("stocNo") or "").strip()
        if code:
            result[code] = {"nav_estimate": num(r.get("nav")), "nav_estimate_date": ymd(r.get("date1"))}
    return result


def futures_code(name: str, desc: str | None) -> str:
    """期貨代號：TE202610 → TE-202610；英文長名稱去掉空白。"""
    m = re.match(r"(.*?)(\d{6})$", (name or "").strip())
    if m:
        return re.sub(r"\s+", "", m.group(1)) + "-" + m.group(2)
    return re.sub(r"\s+", "", name or desc or "")


def parse_buyback(data) -> dict:
    pcf = (data or {}).get("pcf") or {}
    holdings = []
    for s in (data or {}).get("stocks") or []:
        holdings.append({"asset_code": text(s.get("stocNo")), "asset_name": text(s.get("stocName")), "kind": "股票",
                         "weight": s.get("weight"), "qty": s.get("share")})
    for b in (data or {}).get("bonds") or []:
        holdings.append({"asset_code": text(b.get("bondNo")), "asset_name": text(b.get("bondName")), "kind": "債券",
                         "weight": b.get("weight"), "qty": b.get("faceValue")})
    for f in (data or {}).get("futures") or []:
        holdings.append({"asset_code": futures_code(f.get("txEname"), f.get("txDesc")), "asset_name": text(f.get("txDesc")),
                         "kind": "期貨", "weight": f.get("weight"), "qty": f.get("lot")})
    total = pcf.get("nav")
    twd = (pcf.get("dollar") or "").strip() == "TWD"
    return {
        "nav_official": pcf.get("pUnit") if twd else None,
        "nav_date": ymd(pcf.get("date2")),
        # 規模只收新台幣計價，外幣計價的不自行換算
        "aum": round(total / 1e8, 2) if total and twd else None,
        "units": pcf.get("totUnit"),
        "units_change": pcf.get("disUnit"),
        "holdings": [h for h in holdings if h["asset_code"]],
    }


def parse_detail(data) -> dict:
    d = data or {}
    managers = [text(m.get("managerName")) for m in d.get("managers") or [] if text(m.get("managerName"))]
    mgmt = text(d.get("manageCharge"))
    return {
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": text(d.get("storageCharge")),
        "manager": "、".join(managers) or None,
        "list_date": ymd(d.get("listingDate")),
        "index_name": text(d.get("indexName")),
    }


def parse_dividends(rows) -> list[dict]:
    """interestDate 是除息交易日，getinterestDate 是最後買進日（除息前一個交易日），assignDate 是發放日。"""
    result = []
    for r in rows or []:
        code = text(r.get("etfNo"))
        ex_date = ymd(r.get("interestDate"))
        if code and ex_date:
            result.append({"code": code, "ex_date": ex_date, "pay_date": ymd(r.get("assignDate")), "cash": num(r.get("amt"))})
    return result


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None, **_) -> dict[str, dict]:
    if fixtures:
        def call(name, *_args, **_kw):
            path = fixtures / f"{name}.json"
            if not path.exists():
                raise RuntimeError(f"沒有測試資料 {path.name}")
            return unwrap(json.loads(path.read_text("utf-8")))
    else:
        site = Site(context)

        def call(_name, path, body=None, post=True):
            return site.call(path, body, post)

    try:
        funds = parse_items(call("items", "/items", None))
        navs = parse_nav_list(call("nav", "/nav", None))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用群益資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到基金清單），本次不使用群益資料。", file=sys.stderr)
        return {}

    dividends: dict[str, list] = {}
    try:
        for d in parse_dividends(call("dividends", "/dividendDataHistory",
                                      {"searchType": 3, "interestCycle": "", "fundid": ""})):
            dividends.setdefault(d["code"], []).append(d)
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息紀錄抓取失敗（{exc}）", file=sys.stderr)

    records = {}
    for fund_no, code in sorted(funds.items()):
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE, "risk_class": None, "beneficiaries": None,
               "beneficiaries_month": None, "stats": None, **navs.get(code, {})}
        try:
            rec.update(parse_buyback(call(f"buyback_{fund_no}", "/buyback", {"fundId": fund_no, "date": None})))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 申購買回清單抓取失敗（{exc}）", file=sys.stderr)
        try:
            rec.update(parse_detail(call(f"detail_{fund_no}", f"/detail/{fund_no}", post=False)))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
        rec["dividends"] = dividends.get(code, [])
        records[code] = rec
    # 已下市基金的歷史配息也保留，研究時用得到
    for code, divs in dividends.items():
        if code not in records:
            records[code] = {"code": code, "issuer": ISSUER, "source": SOURCE, "dividends": divs, "delisted": True}
    return records
