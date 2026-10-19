"""統一投信官網（www.ezmoney.com.tw）。

網站是伺服器端產生的頁面，資料以 JSON 嵌在 HTML 的 data-content 屬性裡：
- /ETF/Fund/Info?fundCode=XXX：基金資料（DataFund）、淨資產與成分明細（DataAsset）、基金介紹表（費用、經理人）
- /ETF/Fund/Div：選擇基金後送出表單，回傳近五年配息紀錄（含除息交易日、配息發放日）

網站有防機器人機制：第一次連線會先設定 Cookie 再轉址，偶爾也會中斷連線，
所以用保留 Cookie 的連線、放慢速度並重試。
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from .common import flat_fee, roc_ymd, ymd

ISSUER = "統一"
SOURCE = "統一投信官網"
BASE = "https://www.ezmoney.com.tw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
KINDS = {"ST": "股票", "BD": "債券", "GD": "期貨", "ETF": "基金", "FD": "基金"}


class KeepPostRedirect(urllib.request.HTTPRedirectHandler):
    """防機器人機制會對表單送出回 307 轉址；依 HTTP 規範 307/308 要用同樣的方法與內容重送，
    但 Python 預設不處理 POST 的 307，這裡補上。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code in (307, 308) and req.data is not None:
            # 不沿用舊的 Cookie 標頭：轉址回應剛設定的通行 Cookie 要由 Cookie 處理器重新帶上，否則會無限轉址
            headers = {k: v for k, v in req.header_items() if k.lower() not in ("cookie", "content-length")}
            return urllib.request.Request(newurl, data=req.data, headers=headers,
                                          origin_req_host=req.origin_req_host, unverifiable=True, method="POST")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Site:
    """保留 Cookie 的連線。每次請求間隔 2 秒，連線被中斷時最多重試 3 次（間隔 10、25 秒）。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            KeepPostRedirect(),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )

    def get(self, path: str, data: dict | None = None, referer: str | None = None) -> str:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"}
        if referer:
            headers["Referer"] = BASE + referer
        last = None
        # 被防機器人機制擋下時要等一段時間才會解除，重試間隔逐次拉長
        for attempt in range(3):
            time.sleep((2, 10, 25)[attempt])
            try:
                req = urllib.request.Request(BASE + path, data=body, headers=headers)
                with self.opener.open(req, timeout=40) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 - 防機器人機制常直接中斷連線
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    break
        raise RuntimeError(f"{path}：{last}")


# ---------- 解析 ----------

def data_content(page: str, element_id: str):
    """取出 <div id="..." data-content="..."> 裡的 JSON。部分欄位被跳脫兩次，解不開就再還原一次。"""
    m = re.search(r'id="%s" data-content="([^"]*)"' % re.escape(element_id), page)
    if not m:
        return None
    raw = html.unescape(m.group(1))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(html.unescape(raw))


def cell_text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def table_rows(fragment: str) -> list[list[str]]:
    return [[cell_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
            for r in re.findall(r"<tr[^>]*>(.*?)</tr>", fragment, re.S)]


def parse_fund_list(page: str) -> tuple[dict[str, str], str | None]:
    """配息查詢頁：基金選單（fundCode → ETF 代號）與表單驗證碼。"""
    funds = {}
    for value, label in re.findall(r'<option[^>]*value="([^"]+)"[^>]*>([^<]*)</option>', page):
        m = re.match(r"\s*(\w+)\s", label + " ")
        if value and m:
            funds[value] = m.group(1)
    form = page[page.find('<form method="post"'):]
    tok = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', form)
    return funds, tok.group(1) if tok else None


def parse_intro(intro_html: str) -> dict[str, str]:
    """基金介紹表 → {欄位名稱: 內容}，同一列可能有兩組欄位。"""
    result = {}
    for cells in table_rows(html.unescape(intro_html or "")):
        for i in range(0, len(cells) - 1, 2):
            result.setdefault(cells[i], cells[i + 1])
    return result


def parse_info(page: str) -> dict | None:
    fund = data_content(page, "DataFund") or {}
    code = str(fund.get("sStockNo") or "").strip()
    if not code:
        return None
    intro = parse_intro(fund.get("mFundIntro"))
    assets = {a.get("AssetCode"): a for a in data_content(page, "DataAsset") or []}

    holdings, dates = [], set()
    for a in assets.values():
        for d in a.get("Details") or []:
            if d.get("TranDate"):
                dates.add(ymd(d["TranDate"]))
            mth = re.sub(r"\D", "", d.get("MTH") or "")
            holdings.append({
                "asset_code": str(d.get("DetailCode") or "").strip() + (f"-{mth}" if mth else ""),
                "asset_name": (d.get("DetailName") or "").strip() or None,
                "kind": KINDS.get(a.get("AssetCode"), a.get("AssetName") or "其他"),
                "weight": d.get("NavRate"),
                "qty": d.get("Share"),
            })

    def value(key):
        a = assets.get(key)
        return a.get("Value") if a else None

    # 規模只收新台幣計價，外幣計價的不自行換算
    total = value("NAV") if (assets.get("NAV") or {}).get("MoneyType") == "NTD" else None
    index = intro.get("追蹤標的指數")
    mgmt = intro.get("經理費(年)") or None
    return {
        "code": code,
        "nav_official": value("P_UNIT"),
        # 淨值日期以成分明細的交易日為準；債券、海外型通常比網頁更新日早一天
        "nav_date": max(dates) if dates else None,
        "aum": round(total / 1e8, 2) if total else None,
        "units": value("OUT_UNIT"),
        "beneficiaries": None,
        "beneficiaries_month": None,
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": intro.get("保管費(年)") or None,
        "manager": intro.get("基金經理人") or None,
        "risk_class": intro.get("風險收益等級") or None,
        "list_date": ymd(intro.get("掛牌上市日期") or intro.get("掛牌上櫃日期")),
        "index_name": None if index in (None, "", "無", "-") else index,
        "stats": None,
        "holdings": holdings,
    }


def parse_dividends(page: str) -> list[dict] | None:
    """配息查詢結果。手機版表格每格前面帶欄位名稱，先去掉再解析。
    找不到結果表格代表查詢沒成功（回傳 None）；有表格但沒有資料列代表這檔沒有配息（回傳空清單）。"""
    if "除息交易日" not in page:
        return None
    result = []
    for t in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S):
        rows = table_rows(t)
        if not rows or "除息交易日" not in rows[0]:
            continue
        header = rows[0]
        for cells in rows[1:]:
            rec = {h: (c[len(h):].strip() if c.startswith(h) else c) for h, c in zip(header, cells)}
            m = re.match(r"\s*(\w+)\s", (rec.get("基金名稱") or "") + " ")
            ex_date = roc_ymd(rec.get("除息交易日"))
            if not m or not ex_date:
                continue
            try:
                cash = float(rec.get("每單位配息") or "")
            except ValueError:
                cash = None
            result.append({"code": m.group(1), "ex_date": ex_date, "pay_date": roc_ymd(rec.get("配息發放日")),
                           "cash": cash})
    return result


def roc_month(d: date) -> str:
    return f"{d.year - 1911}/{d.month:02d}"


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None, **_) -> dict[str, dict]:
    if fixtures:
        def load(name, *_args, **_kw):
            path = fixtures / f"{name}.html"
            if not path.exists():
                raise RuntimeError(f"沒有測試資料 {path.name}")
            return path.read_text("utf-8")
    else:
        site = Site(context)

        def load(_name, path, data=None, referer=None):
            return site.get(path, data, referer)

    try:
        funds, token = parse_fund_list(load("div", "/ETF/Fund/Div"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用統一資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到基金選單），本次不使用統一資料。", file=sys.stderr)
        return {}

    today = date.today()
    start = roc_month(date(today.year - 5, today.month, 1))
    records = {}
    for fund_code, code in sorted(funds.items()):
        try:
            rec = parse_info(load(f"info_{fund_code}", f"/ETF/Fund/Info?fundCode={fund_code}"))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        if not rec:
            continue
        rec["dividends"] = []
        divs, error = None, None
        for attempt in range(2):
            try:
                # 每次查詢前重新開啟配息頁，拿新的表單驗證碼；連續送出表單容易被防機器人機制擋下
                if attempt or not token or not fixtures:
                    _, token = parse_fund_list(load("div", "/ETF/Fund/Div"))
                page = load(f"div_{fund_code}", "/ETF/Fund/Div",
                            {"__RequestVerificationToken": token, "start": start, "end": roc_month(today),
                             "Fund": fund_code}, "/ETF/Fund/Div")
                divs = parse_dividends(page)
                if divs is not None:
                    break
                error = "查詢結果沒有配息表格"
            except RuntimeError as exc:
                error = exc
        if divs is None:
            print(f"警告：{SOURCE} {code} 配息紀錄抓取失敗（{error}）", file=sys.stderr)
        else:
            rec["dividends"] = [d for d in divs if d["code"] == rec["code"]]
        records[rec["code"]] = {**rec, "issuer": ISSUER, "source": SOURCE,
                                "source_url": f"{BASE}/ETF/Fund/Info?fundCode={fund_code}"}
    return records
