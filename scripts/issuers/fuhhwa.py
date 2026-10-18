"""復華投信官網（www.fhtrust.com.tw）。

官網前端使用的 JSON 介面（GET，皆在 /api/ 底下）：
- ETFRTnav：全部 ETF 的基金編號（ETF01…）、預估淨值
- fund?fundID=：正式淨值（navDate）、經理費、保管費、風險等級、追蹤指數、上市日、經理人
- assets?fundID=&qDate=：指定日期的淨資產、發行單位數、正式淨值與全部持股權重
- ETFPcf?fundID=&pcfDate=：申購買回清單；清單裝的是較早營業日的資料（actBuyValDate 標示日期，
  台股型通常晚一個營業日、債券與海外型晚兩個），其中 qDiff 為該日申購買回造成的單位數增減
- fundDividend：配息紀錄（除息日 divDate、發放日 grantDate），一次最多查約兩年
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import flat_fee, num, ymd

ISSUER = "復華"
SOURCE = "復華投信官網"
BASE = "https://www.fhtrust.com.tw/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
DIVIDEND_WINDOW_DAYS = 730  # 配息查詢一次最多約兩年


class Site:
    """保留 Cookie 的 JSON 連線，每次請求間隔 1 秒，失敗最多重試 3 次。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )

    def get(self, path: str, params: dict | None = None):
        url = f"{BASE}/{path}" + (f"?{urllib.parse.urlencode(params, safe='/')}" if params else "")
        headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                   "Referer": "https://www.fhtrust.com.tw/ETF"}
        last = None
        for attempt in range(3):
            time.sleep(1 if attempt == 0 else 4)
            try:
                with self.opener.open(urllib.request.Request(url, headers=headers), timeout=40) as resp:
                    return json.loads(resp.read().decode("utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    break
        raise RuntimeError(f"{path}：{last}")


def result(payload):
    """回應格式為 {result: ..., status: 0}；status 不是 0 視為失敗。"""
    if isinstance(payload, dict) and payload.get("status", 0) == 0:
        return payload.get("result")
    return None


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return None if s in ("", "—", "-") else s


def pct(value) -> float | None:
    return num(str(value or "").replace("%", ""))


def slash(d: date) -> str:
    return d.strftime("%Y/%m/%d")


# ---------- 解析 ----------

def parse_rtnav(rows) -> dict[str, dict]:
    """即時淨值清單：pnav 是預估淨值，日期取自更新時間。"""
    out = {}
    for r in rows or []:
        code, fund_id = text(r.get("etfCode")), text(r.get("fundID"))
        if code and fund_id:
            out[code] = {"fund_id": fund_id, "nav_estimate": num(r.get("pnav")),
                         "nav_estimate_date": ymd(r.get("modifiedTime"))}
    return out


def parse_fund(x) -> dict:
    x = (x[0] if isinstance(x, list) and x else x) or {}
    mgmt = text(x.get("ec012"))
    managers = [text(m.get("name")) for m in x.get("manager") or [] if text(m.get("name"))]
    return {
        "nav_official": num(x.get("pnav")),
        "nav_date": ymd(x.get("navDate")),
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": text(x.get("ec013")),
        "risk_class": text(x.get("ec009")),
        "index_name": text(x.get("ec038")),
        "list_date": ymd(x.get("listDate")),
        "manager": "、".join(managers) or None,
        "currency": text(x.get("ec006")),
        "ec001": x.get("ec001"),
        # 收益分配時程（例如「每月底」）；不配息的基金為空白或「0」
        "distributes": text(x.get("ec040")) not in (None, "0"),
    }


def parse_assets(x, twd: bool = True) -> dict:
    """指定日期的資產明細。外幣計價的基金不自行換算規模。"""
    x = (x[0] if isinstance(x, list) and x else x) or {}
    total = num(x.get("pcf_FundNav"))
    holdings = []
    for h in x.get("detail") or []:
        code = text(h.get("stockid"))
        if code:
            holdings.append({"asset_code": code, "asset_name": text(h.get("stockname")), "kind": text(h.get("ftype")) or "其他",
                             "weight": pct(h.get("prate_addaccint")), "qty": num(h.get("qshare"))})
    return {
        "assets_date": ymd(x.get("dDate")),
        "assets_nav": num(x.get("pcf_Fundpnav")),
        "aum": round(total / 1e8, 2) if total and twd else None,
        "units": num(x.get("pcf_FundQissue")),
        "holdings": holdings,
    }


def parse_pcf(rows) -> tuple[str | None, float | None]:
    """申購買回清單 → (資料日期 actBuyValDate, 單位數增減 qDiff)。查無清單回傳 (None, None)。"""
    for r in rows or []:
        if r.get("postDate"):
            return ymd(r.get("actBuyValDate")), num(r.get("qDiff"))
    return None, None


def parse_dividends(rows, code: str) -> list[dict]:
    out = []
    for f in rows or []:
        for d in f.get("dividend") or []:
            ex_date = ymd(d.get("divDate"))
            if ex_date:
                out.append({"code": code, "ex_date": ex_date, "pay_date": ymd(d.get("grantDate")), "cash": num(d.get("mDiv"))})
    return out


def dividend_windows(start: date, end: date) -> list[tuple[date, date]]:
    """把查詢區間切成每段最多兩年。"""
    windows, s = [], start
    while s <= end:
        e = min(end, s + timedelta(days=DIVIDEND_WINDOW_DAYS))
        windows.append((s, e))
        s = e + timedelta(days=1)
    return windows


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None,
          dividend_codes: set[str] | None = None, today: date | None = None, **_) -> dict[str, dict]:
    """dividend_codes：資料庫已有本投信配息紀錄的代號，只補最近一年；其他代號從上市日起完整回補。"""
    today = today or date.today()
    if fixtures:
        def get(name, *_args):
            path = fixtures / f"{name}.json"
            if not path.exists():
                return None  # 測試資料沒有的請求視為查無資料
            return json.loads(path.read_text("utf-8"))
    else:
        site = Site(context)

        def get(_name, path, params=None):
            return site.get(path, params)

    try:
        funds = parse_rtnav(result(get("rtnav", "ETFRTnav")))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用復華資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 清單），本次不使用復華資料。", file=sys.stderr)
        return {}

    records = {}
    for code, base in sorted(funds.items()):
        fid = base["fund_id"]
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE, "beneficiaries": None, "beneficiaries_month": None,
               "stats": None, "units_change": None, "holdings": [], "dividends": [],
               "nav_estimate": base["nav_estimate"], "nav_estimate_date": base["nav_estimate_date"]}
        try:
            info = parse_fund(result(get(f"fund_{fid}", "fund", {"fundID": fid})))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            records[code] = rec
            continue
        ec001, currency, distributes = info.pop("ec001"), info.pop("currency"), info.pop("distributes")
        rec.update(info)
        nav_date = rec["nav_date"]

        if nav_date:
            try:
                assets = parse_assets(result(get(f"assets_{fid}", "assets", {"fundID": fid, "qDate": nav_date.replace("-", "/")})),
                                      twd=currency in (None, "新臺幣", "新台幣"))
                if assets["assets_date"] == nav_date:
                    rec.update({k: assets[k] for k in ("aum", "units", "holdings")})
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 資產明細抓取失敗（{exc}）", file=sys.stderr)
            # 申購買回清單：往後找資料日期等於正式淨值日期的那份（週末、假日沒有清單；債券型晚兩個營業日）
            nd = datetime.strptime(nav_date, "%Y-%m-%d").date()
            for k in range(1, 7):
                pdate = nd + timedelta(days=k)
                if pdate > today + timedelta(days=1):
                    break
                try:
                    data_date, change = parse_pcf(result(get(f"pcf_{fid}_{pdate.isoformat()}", "ETFPcf",
                                                              {"fundID": fid, "pcfDate": slash(pdate)})))
                except RuntimeError:
                    break
                if data_date == nav_date:
                    rec["units_change"] = change
                    break
                if data_date and data_date > nav_date:
                    break

        # 配息：不配息的基金不查；已有紀錄的只補最近一年，新的從上市日起完整回補
        if not distributes:
            records[code] = rec
            continue
        if dividend_codes is not None and code in dividend_codes:
            start = today - timedelta(days=365)
        else:
            start = datetime.strptime(rec["list_date"], "%Y-%m-%d").date() if rec.get("list_date") else today - timedelta(days=365)
        seen = {}
        for s, e in dividend_windows(start, today):
            try:
                rows = result(get(f"dividends_{fid}", "fundDividend",
                                  {"m": "fund", "fundID": fid, "sDate": slash(s), "eDate": slash(e), "dateType": "divDate",
                                   "ec001": ec001 if ec001 is not None else 3}))
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 配息紀錄抓取失敗（{exc}）", file=sys.stderr)
                break
            for d in parse_dividends(rows, code):
                seen[d["ex_date"]] = d
        rec["dividends"] = [seen[k] for k in sorted(seen)]
        records[code] = rec
    return records
