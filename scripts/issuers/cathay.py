"""國泰投信官網（www.cathaysite.com.tw，資料介面在 cwapi.cathaysite.com.tw）。

官網前端使用的 JSON 介面（GET，參數放在網址）。要帶瀏覽器的 User-Agent、Origin、Referer，否則會被防火牆擋下：
- api/ETF/GetETFList：全部 ETF（基金代碼 fundCode、證券代號、計價幣別、風險等級）
- api/ETF/GetRealTimeEstimateNavList：全部 ETF 的即時預估淨值；沒有另外標日期，以同一筆的市價日期為準
- api/ETF/GetETFDetailAllotList：全部 ETF 的配息概況，用來判斷哪些 ETF 有配息
- api/ETF/GetETFIntroduce：上市日、經理人；api/ETF/GetETFCharge：經理費、保管費；api/ETF/GetIndexInfo：追蹤指數
- api/BuySale/GetBuySale：最新申購買回清單，含淨資產、已發行單位數、與前日單位差異、淨值、受益人數；
  preDateC 是淨值與單位數的資料日期（清單本身是下一個交易日的）
- api/ETF/GetETFDetailBalList：資產配置，決定要查哪幾種成分明細
- api/ETF/GetETFDetail{Stock,Bond,Future,ETF,Fund}List：依資料日期查成分明細
- api/Fund/GetHistoryAllotInfo：單檔配息歷史（評價日、除息交易日、發放日、每單位配息）
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

from .common import flat_fee, num, ymd

ISSUER = "國泰"
SOURCE = "國泰投信官網"
BASE = "https://cwapi.cathaysite.com.tw/"
SITE = "https://www.cathaysite.com.tw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TWD = ("新台幣", "新臺幣")
# 資產配置項目 → (成分明細介面, 成分類型)
DETAIL_LISTS = {"股票": ("GetETFDetailStockList", "股票"), "債券": ("GetETFDetailBondList", "債券"),
                "期貨": ("GetETFDetailFutureList", "期貨"), "ETF": ("GetETFDetailETFList", "基金"),
                "基金": ("GetETFDetailFundList", "基金")}


class Api:
    """每秒最多送出一個請求，連線失敗最多重試 3 次；被防火牆擋下（HTTP 403）就停止，之後的請求都不再送出。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context or ssl.create_default_context()))
        self.blocked = False
        self.next_time = 0.0

    def get(self, path: str, **params):
        if self.blocked:
            raise RuntimeError(f"{path}：已被網站擋下，不再送出請求")
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"User-Agent": UA, "Origin": SITE, "Referer": SITE + "/", "Accept": "application/json, text/plain, */*"}
        last = None
        for attempt in range(3):
            time.sleep(max(0.0, self.next_time - time.monotonic()) if attempt == 0 else 4)
            self.next_time = time.monotonic() + 1
            try:
                with self.opener.open(urllib.request.Request(url, headers=headers), timeout=40) as resp:
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
    """回應格式為 {returnCode: "2000", result: ...}；「查無資料」代表沒有資料（回傳 None），其他不是 2000 的視為失敗。"""
    if isinstance(payload, dict) and payload.get("returnMessage") == "查無資料":
        return None
    if not isinstance(payload, dict) or str(payload.get("returnCode")) != "2000":
        message = payload.get("returnMessage") if isinstance(payload, dict) else payload
        raise RuntimeError(f"{path}：回應失敗（{message}）")
    return payload.get("result")


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return None if s in ("", "-", "--", "無") else s


# ---------- 解析 ----------

def parse_estimates(rows) -> dict[str, dict]:
    """即時預估淨值表 → {代號: {nav_estimate, nav_estimate_date}}。外幣計價的不收。"""
    out = {}
    for r in rows or []:
        est, day = num(r.get("estimateNav")), ymd(r.get("closingPriceDate"))
        if r.get("stockCode") and est and day and r.get("currency") in TWD:
            out[r["stockCode"]] = {"nav_estimate": est, "nav_estimate_date": day}
    return out


def distributing(rows) -> dict[str, str | None]:
    """配息概況 → {有配息的基金代碼: 最近一次配息年月}（累積型不配息）。"""
    return {r["fundCode"]: r.get("allotYearMonth") for r in rows or []
            if r.get("fundCode") and r.get("isAllot") and r.get("allotPeriod") != "累積型"}


def recent(year_month: str | None, today: date, months: int = 2) -> bool:
    """最近一次配息年月在 months 個月內（或不知道）才需要重查：新公布的配息從公布到發放大約兩個月。"""
    m = re.match(r"(\d{4})/(\d{1,2})", str(year_month or ""))
    return not m or (today.year - int(m.group(1))) * 12 + today.month - int(m.group(2)) <= months


def parse_pcf(r) -> dict:
    """申購買回清單。淨值、淨資產、單位數與單位差異都是 preDateC 那天的資料。"""
    if not r or not ymd(r.get("preDateC")):
        return {}
    twd = r.get("currency") in TWD
    total = num(r.get("aum"))
    month = re.match(r"(\d{4})/(\d{1,2})", str(r.get("dataDate") or ""))
    return {
        "nav_date": ymd(r["preDateC"]),
        "currency": "TWD" if twd else None,
        # 外幣計價的淨值不和新台幣市價比較，規模也不自行換算
        "nav_official": num(r.get("nav")) if twd else None,
        "aum": round(total / 1e8, 2) if total and twd else None,
        "units": num(r.get("totUnit")),
        "units_change": num(r.get("diffUnit")),
        "beneficiaries": num(r.get("benefiCount")),
        "beneficiaries_month": f"{month.group(1)}-{int(month.group(2)):02d}" if month else None,
    }


def detail_kinds(rows) -> list[str]:
    """資產配置 → 要查的成分類型（依 DETAIL_LISTS 的順序）。「期貨（名目本金）」視為期貨，「附買回債券」不是債券成分。"""
    items = {re.sub(r"[（(].*", "", str(r.get("item") or "")).strip() for r in rows or []}
    return [k for k in DETAIL_LISTS if k in items]


def parse_holdings(item: str, rows) -> list[dict]:
    out = []
    for r in rows or []:
        if item == "股票":
            h = (r.get("stockCode"), r.get("stockName"), r.get("weights"), r.get("volumn"))
        elif item == "債券":
            h = (r.get("bondNo"), r.get("bondName"), r.get("ntMkval"), r.get("parValue"))
        elif item == "期貨":
            ym = re.sub(r"\D", "", str(r.get("ftDate") or ""))
            h = (f"{r.get('ftNo')}-{ym}" if r.get("ftNo") and ym else r.get("ftNo"), r.get("ftName"), r.get("ntMkval"), r.get("volumn"))
        elif item == "ETF":
            h = (r.get("skCode"), r.get("skName"), r.get("ntMkval"), r.get("volumn"))
        else:  # 基金：網站只列持有單位數，沒有權重
            h = (r.get("fnNo"), r.get("fnName"), None, r.get("ntMkval"))
        code = text(h[0])
        if code:
            out.append({"asset_code": code, "asset_name": text(h[1]), "kind": DETAIL_LISTS[item][1],
                        "weight": num(h[2]), "qty": num(h[3])})
    return out


def parse_dividends(code: str, result) -> list[dict]:
    """配息歷史：transDate 是除息交易日、lendingDate 是發放日。"""
    out = []
    for r in (result or {}).get("fundAllotInfoList") or []:
        ex_date = ymd(r.get("transDate"))
        if ex_date:
            out.append({"code": code, "ex_date": ex_date, "pay_date": ymd(r.get("lendingDate")), "cash": num(r.get("allotMoney"))})
    return out


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None,
          dividend_codes: set[str] | None = None, today: date | None = None, **_) -> dict[str, dict]:
    today = today or date.today()
    dividend_codes = dividend_codes or set()
    api = None
    if fixtures:
        def call(name, _path, **_params):
            path = fixtures / f"{name}.json"
            if not path.exists():
                raise RuntimeError(f"沒有測試資料 {path.name}")
            return unwrap(json.loads(path.read_text("utf-8")), name)
    else:
        api = Api(context)

        def call(_name, path, **params):
            return api.get(path, **params)

    try:
        funds = call("list", "api/ETF/GetETFList", FundType="", Keyword="", CurrentPage=1, PerPageCount=200)
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用國泰資料。", file=sys.stderr)
        return {}
    funds = [f for f in funds or [] if f.get("fundCode") and f.get("stockCode")]
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 列表），本次不使用國泰資料。", file=sys.stderr)
        return {}
    try:
        estimates = parse_estimates(call("estimate", "api/ETF/GetRealTimeEstimateNavList"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}即時預估淨值抓取失敗（{exc}）", file=sys.stderr)
        estimates = {}
    try:
        payers = distributing(call("allot", "api/ETF/GetETFDetailAllotList", Keyword="", orderBy=8))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息概況抓取失敗（{exc}），每檔都查配息紀錄", file=sys.stderr)
        payers = {f["fundCode"]: None for f in funds}

    records = {}
    for f in funds:
        if api and api.blocked:
            print(f"警告：{SOURCE}擋下了請求，其餘 ETF 本次不抓。", file=sys.stderr)
            break
        code, fid = f["stockCode"].strip(), f["fundCode"].strip()
        twd = f.get("currency") in TWD
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE, "nav_official": None, "nav_date": None,
               "nav_estimate": None, "nav_estimate_date": None, "units_change": None, "aum": None, "units": None,
               "beneficiaries": None, "beneficiaries_month": None, "mgmt_fee": None, "mgmt_fee_text": None,
               "custodian_fee_text": None, "manager": None, "risk_class": text(f.get("riskLevel")), "list_date": None,
               "index_name": None, "stats": None, "holdings": [], "dividends": [],
               **(estimates.get(code, {}) if twd else {})}
        try:
            intro = call(f"intro_{fid}", "api/ETF/GetETFIntroduce", fundCode=fid) or {}
            charge = call(f"charge_{fid}", "api/ETF/GetETFCharge", fundCode=fid) or {}
            pcf = parse_pcf(call(f"pcf_{fid}", "api/BuySale/GetBuySale", FundCode=fid, IsTest="false"))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        mgmt = text(charge.get("managerCharge"))
        rec.update({"list_date": ymd(intro.get("listedDate")), "manager": text(intro.get("manager")),
                    "mgmt_fee": flat_fee(mgmt), "mgmt_fee_text": mgmt, "custodian_fee_text": text(charge.get("storageCharge")),
                    **pcf})
        rec["source_url"] = f"https://www.cathaysite.com.tw/ETF/purchase?code={fid}"
        if "主動" not in str(f.get("etfTypeName")):  # 主動式 ETF 沒有追蹤指數
            try:
                rec["index_name"] = text((call(f"index_{fid}", "api/ETF/GetIndexInfo", fundCode=fid) or {}).get("indexName"))
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 追蹤指數抓取失敗（{exc}）", file=sys.stderr)
        if rec["nav_date"]:
            day = rec["nav_date"]
            try:
                for item in detail_kinds(call(f"bal_{fid}", "api/ETF/GetETFDetailBalList", FundCode=fid, SearchDate=day)):
                    rows = call(f"{DETAIL_LISTS[item][0]}_{fid}", f"api/ETF/{DETAIL_LISTS[item][0]}", FundCode=fid, SearchDate=day)
                    rec["holdings"] += parse_holdings(item, rows)
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 成分明細抓取失敗（{exc}）", file=sys.stderr)
                rec["holdings"] = []  # 不完整的成分不存
        # 配息（外幣計價的配息金額不是新台幣，不收）：資料庫還沒有這檔的國泰紀錄時查成立以來全部；
        # 之後只在最近兩個月有新配息時查今年與去年
        if twd and fid in payers and (code not in dividend_codes or recent(payers[fid], today)):
            try:
                result = call(f"dividends_{fid}", "api/Fund/GetHistoryAllotInfo", fundCode=fid,
                              IsFromBorn="false" if code in dividend_codes else "true",
                              StartYear=today.year - 1, EndYear=today.year, CurrentPage=1, PerPageCount=999)
                rec["dividends"] = parse_dividends(code, result)
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 配息紀錄抓取失敗（{exc}）", file=sys.stderr)
        records[code] = rec
    return records
