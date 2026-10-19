"""中國信託投信官網（www.ctbcinvestments.com，資料介面在 www.ctbcinvestments.com.tw/API/）。

官網前端使用的 JSON 介面（POST）。先向 home/AuthToken 取得 token，之後每次請求都把 token 帶在網址與內容裡：
- etf/ETFList：全部 ETF（基金代碼 FID、CNO、最新正式淨值與日期、計價幣別、風險等級）
- etf/ETFINav：全部 ETF 的即時預估淨值與更新時間
- etf/ETFDetail（CNO）：追蹤指數、上市日、經理人、經理費與保管費原文，以及完整配息歷史（除息日、發放日）
- etf/Buyback（FID、公告日）：申購買回清單，含淨資產、已發行單位數、與前日單位差異、受益人數與全部成分。
  公告日是清單適用的交易日，淨值與單位數是更早的「淨值日期」那天的資料；查非交易日會是空的
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
from datetime import date, timedelta
from pathlib import Path

from .common import flat_fee, num, ymd

ISSUER = "中信"
SOURCE = "中信投信官網"
BASE = "https://www.ctbcinvestments.com.tw/API/"
SITE = "https://www.ctbcinvestments.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
KINDS = {"STOCK": "股票", "BOND": "債券", "FUTURE": "期貨", "FUTURES": "期貨", "FUND": "基金", "ETF": "基金"}
TWD = ("TWD", "新臺幣", "新台幣", "台幣")


class Api:
    """取得 token 後逐筆請求，每秒最多一個；連線失敗最多重試 3 次，被擋（HTTP 403）就停止。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context or ssl.create_default_context()))
        self.token = ""
        self.next_time = 0.0
        self.blocked = False

    def post(self, path: str, **data):
        if self.blocked:
            raise RuntimeError(f"{path}：已被網站擋下，不再送出請求")
        if not self.token and path != "home/AuthToken":
            self.token = (self.post("home/AuthToken") or {}).get("token") or ""
        url = BASE + path + "?" + urllib.parse.urlencode({"token": self.token})
        body = json.dumps({"token": self.token, **data}).encode()
        headers = {"User-Agent": UA, "Content-Type": "application/json; charset=utf-8", "Accept": "application/json, text/plain, */*",
                   "Origin": SITE, "Referer": SITE + "/"}
        last = None
        for attempt in range(3):
            time.sleep(max(0.0, self.next_time - time.monotonic()) if attempt == 0 else 4)
            self.next_time = time.monotonic() + 1
            try:
                with self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=60) as resp:
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
    """回應格式為 {ResultCode: 0, ResultMsg, Data}；ResultCode 不是 0 視為失敗。"""
    if not isinstance(payload, dict) or payload.get("ResultCode") != 0:
        raise RuntimeError(f"{path}：回應失敗（{payload.get('ResultMsg') if isinstance(payload, dict) else payload}）")
    return payload.get("Data")


def text(value) -> str | None:
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value or ""))).strip()
    return None if s in ("", "-", "NA", "無") else s


def rows(data, key: str = "Data") -> list[dict]:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


# ---------- 解析 ----------

def parse_list(data) -> list[dict]:
    return [{"code": r["ETF_ID"].strip(), "fid": r.get("FID"), "cno": r.get("CNO"), "currency": r.get("NAV_CURX"),
             "nav_official": num(r.get("NAV")), "nav_date": ymd(r.get("NAV_DT")), "risk_class": text(r.get("RISK_LV_NAME"))}
            for r in rows(data) if r.get("ETF_ID") and r.get("FID") and r.get("CNO")]


def parse_estimates(data) -> dict[str, dict]:
    """即時預估淨值：NAV 是預估淨值，UPD_DATE 是更新時間。"""
    out = {}
    for r in rows(data):
        est, day = num(r.get("NAV")), ymd(r.get("UPD_DATE"))
        if r.get("ETF_ID") and est and day:
            out[r["ETF_ID"].strip()] = {"nav_estimate": est, "nav_estimate_date": day}
    return out


def parse_detail(data, code: str) -> dict:
    d = (rows(data, "FundDetail") or [{}])[0]
    index = re.split(r"<br\s*/?>", d.get("ExponName") or "")[0]  # 中文名稱在前，英文名稱換行在後
    mgmt = text(d.get("ManagerCost"))
    return {
        "currency": text(d.get("CurType")),
        "index_name": text(index),
        "list_date": ymd(d.get("FundAppear")),
        "manager": text(d.get("Manager")),
        "risk_class": text(d.get("RiskLv")),
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": text(d.get("PreserveCost")),
        # 配息歷史：DIVIDENT_DATE 是除息日、RELEASE_DATE 是發放日、BASE_DATE 是收益分配基準日
        "dividends": [{"code": code, "ex_date": ymd(r.get("DIVIDENT_DATE")), "pay_date": ymd(r.get("RELEASE_DATE")),
                       "cash": num(r.get("UNIT_NET"))} for r in rows(data, "DivHist") if ymd(r.get("DIVIDENT_DATE"))],
    }


def parse_pcf(data) -> dict:
    """申購買回清單。淨資產、單位數、單位差異與成分都是「淨值日期」那天的資料。"""
    r = (rows(data) or [{}])[0]
    day = ymd(r.get("淨值日期") or r.get("每受益權單位淨資產價值DATE"))
    if not r.get("ETF_ID") or not day:
        return {}
    total = num(r.get("基金淨資產價值"))
    month = re.match(r"(\d{4})/(\d{1,2})", str(r.get("NUM_DATE") or ""))
    holdings = []
    for group in rows(data, "Detail"):
        kind = KINDS.get(str(group.get("Code") or "").upper())
        if not kind:
            continue  # 現金、保證金、應收應付等不是成分
        for h in group.get("Data") or []:
            code = text(h.get("code_"))
            ym = re.sub(r"\D", "", str(h.get("ym_") or ""))
            if code:
                holdings.append({"asset_code": code + (f"-{ym}" if kind == "期貨" and ym else ""), "asset_name": text(h.get("name_")),
                                 "kind": kind, "weight": num(h.get("weights_")), "qty": num(h.get("qty_"))})
    return {
        "code": r["ETF_ID"].strip(),
        "pcf_date": day,
        # 規模只收新台幣計價，外幣計價的不自行換算
        "aum": round(total / 1e8, 2) if total and r.get("幣別") in TWD else None,
        "units": num(r.get("已發行受益權單位總數")),
        "units_change": num(r.get("與前日已發行單位差異數")),
        "beneficiaries": num(r.get("NUM_")),
        "beneficiaries_month": f"{month.group(1)}-{int(month.group(2)):02d}" if month else None,
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
        funds = parse_list(call("list", "etf/ETFList", IsWithETF="Y"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用中信資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 列表），本次不使用中信資料。", file=sys.stderr)
        return {}
    try:
        estimates = parse_estimates(call("inav", "etf/ETFINav"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}即時預估淨值抓取失敗（{exc}）", file=sys.stderr)
        estimates = {}

    # 申購買回清單依公告日查：第一檔從三天後往前找到有清單的那天，其他檔從同一天開始找
    candidates = [today + timedelta(days=k) for k in range(3, -6, -1)]
    records = {}
    for f in funds:
        if api and api.blocked:
            print(f"警告：{SOURCE}擋下了請求，其餘 ETF 本次不抓。", file=sys.stderr)
            break
        code, twd = f["code"], f["currency"] in TWD
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE,
               "source_url": "https://www.ctbcinvestments.com.tw/Product/List?Kind=2&ProductEtfNo=1",
               "currency": "TWD" if twd else None, "nav_estimate": None, "nav_estimate_date": None,
               "units_change": None, "aum": None, "units": None, "beneficiaries": None, "beneficiaries_month": None,
               "stats": None, "holdings": [], "dividends": [],
               "nav_official": f["nav_official"] if twd else None, "nav_date": f["nav_date"], "risk_class": f["risk_class"],
               **(estimates.get(code, {}) if twd else {})}
        try:
            detail = parse_detail(call(f"detail_{f['cno']}", "etf/ETFDetail", CNO=f["cno"]), code)
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        detail.pop("currency")
        dividends, risk = detail.pop("dividends"), detail.pop("risk_class")
        rec.update(detail)
        rec["risk_class"] = risk or f["risk_class"]
        rec["dividends"] = dividends if twd else []  # 外幣計價的配息金額不是新台幣，不收
        for i, day in enumerate(candidates):
            try:
                pcf = parse_pcf(call(f"pcf_{f['fid']}", "etf/Buyback", FID=f["fid"], StartDate=day.isoformat()))
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 申購買回清單抓取失敗（{exc}）", file=sys.stderr)
                break
            if pcf.get("code") == code:
                candidates = candidates[i:]
                if not twd:
                    pcf["aum"] = None
                rec.update({k: v for k, v in pcf.items() if k != "code"})
                break
        records[code] = rec
    return records
