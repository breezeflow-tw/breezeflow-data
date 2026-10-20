"""安聯投信官網（etf.allianzgi.com.tw，資料介面在 etf.allianzgi.com.tw/webapi/api/）。

官網前端使用的 JSON 介面（POST）。要先取得防偽權杖（AntiForgery/GetAntiForgeryToken），之後每個請求都帶
cookie 與 X-XSRF-TOKEN 標頭；回應格式為 {StatusCode, Message, Entries}，StatusCode 0 是成功、1 是查無資料：
- Fund/GetFundOverview：全部 ETF（內部代號 CFundNo、證券代號、最新正式淨值與淨值日期）
- Fund/GetFundIntradayNAV：全部 ETF 的即時預估淨值與更新時間
- Fund/GetFundYield：全部 ETF 的配息紀錄（除息日、發放日、每單位配息）；日期條件篩的是收益分配基準日
- Fund/GetFundDetail（FundNo）：上市日、經理人、經理費與保管費原文、風險等級、計價幣別
- Fund/GetFundTradeInfo（FundNo，Date 為 null 是最新一份）：申購買回清單，含淨資產、已發行單位數、
  與前日已發行單位差異數、受益人數與全部成分。CPcfdate 是清單適用的交易日，淨資產、單位數與成分是更早的
  淨值日期 CNavDt 那天的資料（投資海外的 ETF 晚兩個交易日）。成分表依表頭名稱對應欄位
  （舊清單沒有「序號」欄，代號與名稱有多餘空白）；附買回債券只有名稱與金額，沒有代號與權重，不收
"""
from __future__ import annotations

import http.cookiejar
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

ISSUER = "安聯"
SOURCE = "安聯投信官網"
SITE = "https://etf.allianzgi.com.tw"
BASE = SITE + "/webapi/api/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TWD = ("新台幣", "新臺幣", "TWD")
KINDS = {"股票": "股票", "期貨": "期貨"}  # 成分表名稱開頭 → 成分類型
SKIPPED = ("附買回債券",)  # 已知但不收的成分表


class Api:
    """每秒最多送出一個請求，連線失敗最多重試 3 次；被擋（HTTP 403）就停止，之後的請求都不再送出。
    第一次查詢前先取得防偽權杖，cookie 由 CookieJar 保存。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.next_time = 0.0
        self.blocked = False
        self.token = None

    def send(self, path: str, data: dict | None = None):
        if self.blocked:
            raise RuntimeError(f"{path}：已被網站擋下，不再送出請求")
        headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Origin": SITE, "Referer": SITE + "/"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers.update({"Content-Type": "application/json", "X-XSRF-TOKEN": self.token or ""})
        last = None
        for attempt in range(3):
            time.sleep(max(0.0, self.next_time - time.monotonic()) if attempt == 0 else 4)
            self.next_time = time.monotonic() + 1
            try:
                with self.opener.open(urllib.request.Request(BASE + path, data=body, headers=headers), timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8-sig"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 403:
                    self.blocked = True
                    break
                if exc.code == 400:  # 權杖無效或參數錯誤，重試也一樣
                    break
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    self.blocked = True
                    break
        raise RuntimeError(f"{path}：{last}")

    def post(self, path: str, **data):
        if not self.token:
            token = (self.send("AntiForgery/GetAntiForgeryToken") or {}).get("token")
            if not token:
                raise RuntimeError("取不到防偽權杖")
            self.token = token
        return unwrap(self.send(path, data), path)


def unwrap(payload, path: str = ""):
    """StatusCode 0 回傳 Entries，1 是查無資料（回傳 None），其他視為失敗。"""
    if isinstance(payload, dict) and payload.get("StatusCode") in (0, 1):
        return payload.get("Entries") if payload["StatusCode"] == 0 else None
    raise RuntimeError(f"{path}：回應失敗（{payload.get('Message') if isinstance(payload, dict) else payload}）")


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return None if s in ("", "-", "--") else s


# ---------- 解析 ----------

def parse_overview(entries) -> list[dict]:
    return [{"fund_no": text(r.get("CFundNo")), "code": text(r["CSecuritiesCode"]),
             "nav_official": num(r.get("CLatestNAV")) or None, "nav_date": ymd(r.get("CLatestNAVDate"))}
            for r in entries or [] if isinstance(r, dict) and text(r.get("CSecuritiesCode")) and text(r.get("CFundNo"))]


def parse_estimates(entries) -> dict[str, dict]:
    """即時預估淨值：CEstimateNav 是預估淨值，CDataDt 是更新時間。"""
    out = {}
    for r in entries or []:
        code, est, day = text(r.get("CStockNo")), num(r.get("CEstimateNav")), ymd(r.get("CDataDt"))
        if code and est and day:
            out[code] = {"nav_estimate": est, "nav_estimate_date": day}
    return out


def parse_dividends(entries) -> dict[str, list[dict]]:
    """配息紀錄：CExDate 是除息日、CPayableDate 是發放日、CPerShare 是每單位配息。"""
    out = {}
    for f in entries or []:
        code = text(f.get("CSecuritiesCode"))
        if code:
            out[code] = [{"code": code, "ex_date": ymd(r.get("CExDate")), "pay_date": ymd(r.get("CPayableDate")),
                          "cash": num(r.get("CPerShare"))}
                         for r in f.get("FundYieldList") or [] if ymd(r.get("CExDate"))]
    return out


def parse_detail(entries, code: str) -> dict:
    """基金資料頁；找不到這檔基金時回傳 {}。主動式 ETF 不追蹤指數，沒有追蹤指數。"""
    d = entries if isinstance(entries, dict) else {}
    if text(d.get("CSecuritiesCode")) != code:
        return {}
    mgmt = text(d.get("CManagerFee"))
    risk = re.search(r"RR[1-5]", d.get("CCgRiskLevelStr") or "")
    return {
        "currency": text(d.get("CCgCurrencyTypeStr")),
        "list_date": ymd(d.get("CListingDt")),
        "manager": text(d.get("CManager")),
        "risk_class": risk.group(0) if risk else None,
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": text(d.get("CCustodyFee")),
    }


def parse_tables(tables, code: str = "") -> list[dict]:
    """申購買回清單的成分表。期貨代號加上契約年月（例如 TX-202611），與其他投信一致。"""
    out = []
    for t in tables or []:
        title = text(t.get("TableTitle")) or ""
        kind = next((v for k, v in KINDS.items() if title.startswith(k)), None)
        if not kind:
            if not title.startswith(SKIPPED):
                print(f"警告：{SOURCE} {code} 申購買回清單有不認得的成分表「{title}」，沒有收錄。", file=sys.stderr)
            continue
        cols = [text(c.get("Name")) for c in t.get("Columns") or []]
        for row in t.get("Rows") or []:
            cell = dict(zip(cols, (text(v) for v in row)))
            asset = cell.get(f"{kind}代號")
            if not asset:
                continue
            ym = re.sub(r"\D", "", cell.get("契約年月") or "")
            out.append({"asset_code": asset + (f"-{ym}" if kind == "期貨" and ym else ""),
                        "asset_name": cell.get(f"{kind}名稱"), "kind": kind,
                        "weight": num((cell.get("權重(%)") or "").rstrip("%")),
                        "qty": num(cell.get("股數") or cell.get("口數"))})
    return out


def parse_pcf(entries) -> dict:
    """申購買回清單。淨資產、單位數、單位差異與成分都是淨值日期 CNavDt 那天的資料；受益人數另有資料日期。"""
    t = entries if isinstance(entries, dict) else {}
    code, day = text(t.get("CSecuritiesCode")), ymd(t.get("CNavDt"))
    if not code or not day:
        return {}
    total = num(t.get("CAnceTotalAv"))
    counted = ymd(t.get("CBeneficiariesCountDate"))
    return {
        "code": code,
        "pcf_date": day,
        "pcf_announced": ymd(t.get("CPcfdate")),  # 清單日期（清單適用的交易日，前一個營業日傍晚公告）
        "currency": text(t.get("CEtfindexCurrency")),
        "aum": round(total / 1e8, 2) if total else None,
        "units": num(t.get("CAnceTotalIssues")),
        "units_change": num(t.get("CAnceIssuesDiff")),
        "pcf_nav": num(t.get("CAnceNav")) or None,
        "beneficiaries": num(t.get("CBeneficiariesCount")),
        "beneficiaries_month": counted[:7] if counted else None,
        "holdings": parse_tables(t.get("DynamicTableData"), code),
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
        funds = parse_overview(call("overview", "Fund/GetFundOverview", Keyword="", FundNo="", FundType=-1, PageSize=999, PageIndex=1))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用安聯資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 列表），本次不使用安聯資料。", file=sys.stderr)
        return {}
    try:
        estimates = parse_estimates(call("inav", "Fund/GetFundIntradayNAV", Keyword="", FundNo="", FundType=-1))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}即時預估淨值抓取失敗（{exc}）", file=sys.stderr)
        estimates = {}
    try:
        dividends = parse_dividends(call("yield", "Fund/GetFundYield", Keyword="", FundNo="", FundType=-1,
                                         SDate="2000-01-01", EDate=f"{today.year + 1}-12-31"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息紀錄抓取失敗（{exc}）", file=sys.stderr)
        dividends = None

    records = {}
    for f in funds:
        if api and api.blocked:
            print(f"警告：{SOURCE}擋下了請求，其餘 ETF 本次不抓。", file=sys.stderr)
            break
        code = f["code"]
        try:
            detail = parse_detail(call(f"detail_{code}", "Fund/GetFundDetail", FundNo=f["fund_no"], IsPreview=False), code)
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        if not detail:
            print(f"警告：{SOURCE} {code} 基金資料格式改變，本次不使用這檔。", file=sys.stderr)
            continue
        twd = detail.pop("currency") in TWD
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE, "source_url": f"{SITE}/etf-info/{f['fund_no']}",
               "currency": "TWD" if twd else None, "nav_estimate": None, "nav_estimate_date": None,
               "units_change": None, "aum": None, "units": None, "beneficiaries": None, "beneficiaries_month": None,
               "index_name": None, "stats": None, "holdings": [], **detail,
               "nav_official": f["nav_official"] if twd else None, "nav_date": f["nav_date"],
               **(estimates.get(code, {}) if twd else {})}
        if dividends is not None:
            rec["dividends"] = dividends.get(code, []) if twd else []  # 外幣計價的配息金額不是新台幣，不收
        try:
            pcf = parse_pcf(call(f"trade_{code}", "Fund/GetFundTradeInfo", FundNo=f["fund_no"], Date=None))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 申購買回清單抓取失敗（{exc}）", file=sys.stderr)
            pcf = {}
        if pcf.get("code") == code:
            if not twd or pcf["currency"] not in TWD:
                pcf["aum"] = pcf["pcf_nav"] = None  # 規模與淨值只收新台幣計價，外幣計價的不自行換算
            rec.update({k: v for k, v in pcf.items() if k not in ("code", "currency")})
        records[code] = rec
    return records
