"""投信投顧公會（www.sitca.org.tw）的境內基金資料。

補上尚未接入投信官網的 ETF 的經理費、保管費、經理人與風險等級，並提供全部 ETF 的年度實際費用比率：
- ROC/Industry/IN2105.aspx 基金基本資料表（每月第 10 個營業日更新）
  - 「標準」欄位組：受益憑證代號（就是 ETF 證券代號）、基金統編、經理人、風險等級、基金成立日、計價幣別
  - 「費率、銀行與規定」欄位組：契約上的經理費率、保管費率（分段收費的多半只寫區間或「載明於公開說明書」）
- ROC/Industry/IN2211.aspx 各項費用比率：選年度與「全年」，列出每檔基金當年實際收取的經理費、保管費、
  其他費用與合計（含交易成本）占平均淨資產的比率
- ROC/Industry/IN2201.aspx 基金資料彙總表-明細（月）：月底基金規模、單位淨值、受益權單位數、
  自然人／法人受益人數與當月申購、買回總金額（新台幣）
- ROC/Industry/IN2629.aspx 基金投資明細-月前十大：每檔基金月底前十大投資標的與占淨資產比例。
  依基金類型查詢（指數股票型 AH*、主動式 AL*），表格只有基金名稱，用名稱對到基本資料表的代號
- ROC/Industry/IN2213.aspx 基金配息資料：當月除息的配息基準日、除息日、發放日與每單位分派金額
各表以基金統編串接。年度比率只收整年都在運作的基金：年中才成立的比率只涵蓋幾個月，不能當作年費率。

網站是 ASP.NET 表單：先 GET 拿隱藏欄位，再帶條件 POST 查詢。改年度時伺服器會重設月份選單，
所以「年度」與「全年」要分兩次送出。資料每月才更新，主程式每週最多抓一次。
"""
from __future__ import annotations

import html
import http.cookiejar
import re
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from issuers.common import num

SOURCE = "投信投顧公會"
BASE = "https://www.sitca.org.tw/ROC/Industry/"
BASIC = "IN2105.aspx?pid=IN2212_02"
RATIO = "IN2211.aspx?pid=IN2222_01"
SUMMARY = "IN2201.aspx?pid=IN2221_01"
TOP10 = "IN2629.aspx?pid=IN22601_04"
DIVIDEND = "IN2213.aspx?pid=IN2222_03"
ETF_CLASSES = ("AH", "AL")  # 指數股票型、主動式交易所交易基金
PRE = "ctl00$ContentPlaceHolder1$"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
REFRESH_DAYS = 7


class Site:
    """保留 Cookie 的連線，每次請求間隔 1 秒，失敗最多重試 2 次。全部基金的查詢結果約 6MB，逾時放寬。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )

    def page(self, path: str, data: dict | None = None) -> str:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"User-Agent": UA, "Referer": BASE + path}
        last = None
        for attempt in range(2):
            time.sleep(1 if attempt == 0 else 10)
            try:
                with self.opener.open(urllib.request.Request(BASE + path, data=body, headers=headers), timeout=240) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    break
        raise RuntimeError(f"{path}：{last}")

    def query(self, path: str, *steps: dict, start: str | None = None) -> str:
        """依序送出表單：每一步都帶回上一頁的隱藏欄位與選單目前的值，再換上這一步指定的條件。
        start 是已經拿到的查詢頁，同一頁要查很多次時不必每次重新 GET。"""
        page = start or self.page(path)
        for step in steps:
            page = self.page(path, {**form_fields(page), **{PRE + k: v for k, v in step.items()}})
        return page


# ---------- 解析 ----------

def clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def form_fields(page: str) -> dict[str, str]:
    """表單目前的值：隱藏欄位（__VIEWSTATE 等）與各選單選中的選項。"""
    form = page[page.find("<form"):]
    fields = {}
    for tag in re.findall(r'<input[^>]*type="hidden"[^>]*>', form):
        name, value = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    for name, options in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        chosen = (re.search(r'<option[^>]*selected="selected"[^>]*value="([^"]*)"', options)
                  or re.search(r'<option[^>]*value="([^"]*)"', options))
        fields[name] = html.unescape(chosen.group(1)) if chosen else ""
    return fields


def options(page: str, name: str) -> list[str]:
    m = re.search(r'name="%s"[^>]*>(.*?)</select>' % re.escape(PRE + name), page or "", re.S)
    return re.findall(r'<option[^>]*value="([^"]*)"', m.group(1)) if m else []


def selected(page: str, name: str) -> str | None:
    m = re.search(r'name="%s"[^>]*>(.*?)</select>' % re.escape(PRE + name), page or "", re.S)
    chosen = re.search(r'<option[^>]*selected="selected"[^>]*value="([^"]*)"', m.group(1)) if m else None
    return chosen.group(1) if chosen else None


def table(page: str) -> list[dict[str, str]]:
    """結果表格 → 每列 {欄名: 內容}。多列表頭（rowspan／colspan）展開成「上層/下層」的欄名，欄名去掉空白。"""
    tables = re.findall(r"<table[^>]*>(.*?)</table>", page or "", re.S)
    if not tables:
        return []
    rows = [re.findall(r"<t[hd]([^>]*)>(.*?)</t[hd]>", tr, re.S)
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", max(tables, key=lambda t: t.count("<tr")), re.S)]
    # 資料列的第一格是類型代號（例如 AA1、AH11），之前的都是表頭
    n_head = next((i for i, r in enumerate(rows) if r and re.fullmatch(r"[A-Z]{2}\d+", clean(r[0][1]))), len(rows))
    grid: dict[tuple[int, int], str] = {}
    for r, cells in enumerate(rows[:n_head]):
        c = 0
        for attrs, text in cells:
            while (r, c) in grid:
                c += 1
            span = {k.lower(): int(v) for k, v in re.findall(r"(rowspan|colspan)\s*=\s*[\"']?(\d+)", attrs, re.I)}
            for dr in range(span.get("rowspan", 1)):
                for dc in range(span.get("colspan", 1)):
                    grid[(r + dr, c + dc)] = re.sub(r"\s+", "", clean(text))
            c += span.get("colspan", 1)
    width = max((c for _, c in grid), default=-1) + 1
    names = []
    for c in range(width):
        parts = []
        for r in range(n_head):
            if grid.get((r, c)) and (not parts or parts[-1] != grid[(r, c)]):
                parts.append(grid[(r, c)])
        names.append("/".join(parts))
    return [dict(zip(names, (clean(t) for _, t in cells))) for cells in rows[n_head:] if len(cells) == width]


def column(row: dict, *keywords: str) -> str | None:
    """欄名同時包含所有關鍵字的那一欄。"""
    for name, value in row.items():
        if all(k in name for k in keywords):
            return value
    return None


def blank(value: str | None) -> str | None:
    return None if value in (None, "", "-", "--", "N/A", "無") else value


def parse_basic(std_page: str, fee_page: str) -> dict[str, dict]:
    """基本資料表兩種欄位組 → {ETF 代號: {...}}。受益憑證代號不是證券代號格式的（一般共同基金）不收。"""
    fees = {column(r, "基金統編"): r for r in table(fee_page)}
    out = {}
    for r in table(std_page):
        code, fund_id = column(r, "受益憑證代號"), column(r, "基金統編")
        if not code or not re.fullmatch(r"\d{4,6}[A-Z]?", code):
            continue
        fee = fees.get(fund_id, {})
        inception = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", column(r, "基金成立日") or "")
        out[code] = {
            "fund_id": fund_id,
            "name": column(r, "基金名稱"),
            "manager": blank(column(r, "經理人姓名")),
            "risk_class": blank(column(r, "風險等級")),
            "currency": blank(column(r, "計價幣別")),
            "inception": "-".join(inception.groups()) if inception else None,
            "mgmt_fee_text": blank(column(fee, "基金管理費率")),
            "custodian_fee_text": blank(column(fee, "基金保管費率")),
        }
    return out


def parse_ratios(page: str) -> dict[str, dict]:
    """各項費用比率 → {基金統編: {mgmt, custodian, other, total}}，單位 %。"""
    def pct(value):
        return num((value or "").replace("%", ""))

    out = {}
    for r in table(page):
        fund_id = column(r, "基金統編")
        total = pct(column(r, "合計", "比率"))
        if fund_id and total is not None:
            out[fund_id] = {"mgmt": pct(column(r, "經理費", "比率")), "custodian": pct(column(r, "保管費", "比率")),
                            "other": pct(column(r, "其他項費用", "比率")), "total": total}
    return out


def parse_summary(page: str) -> dict[str, dict]:
    """基金資料彙總表（月）→ {基金統編: {...}}。金額與淨值都是新台幣（外幣計價的基金由公會換算）。"""
    def count(value):
        n = num(value)
        return int(n) if n is not None else None

    out = {}
    for r in table(page):
        fund_id, aum = column(r, "基金統編"), num(column(r, "基金規模(台幣)"))
        if fund_id and aum is not None:
            out[fund_id] = {
                "aum_twd": aum,
                "nav": num(column(r, "單位淨值(台幣)")),
                "units": num(column(r, "受益權單位數")),
                "beneficiaries": count(column(r, "總受益人數")),
                "beneficiaries_natural": count(column(r, "自然人受益人數")),
                "beneficiaries_legal": count(column(r, "法人受益人數")),
                "subscribe_twd": num(column(r, "本月申購總金額(台幣)")),
                "redeem_twd": num(column(r, "本月買回總金額(台幣)")),
            }
    return out


def name_key(name: str | None) -> str:
    """基金名稱比對用：統一全形半形與相容字（兩張表的「金」可能是 U+91D1 與 U+F90A），去掉括號裡的警語與全部空白。"""
    s = unicodedata.normalize("NFKC", name or "")
    return re.sub(r"\s+", "", re.sub(r"\([^()]*\)", "", s))


def asset_kind(text: str) -> str:
    """標的種類 → 成分類型。公會的分類例如「國內上市」「國外股票(含承銷)」「公司債 無擔保」「國外掛牌交易之ETF」。"""
    if "債" in text:
        return "債券"
    if "ETF" in text or "基金" in text:
        return "基金"
    if "期貨" in text:
        return "期貨"
    if any(k in text for k in ("股票", "上市", "上櫃", "興櫃", "存託憑證")):
        return "股票"
    return "其他"


def parse_top10(page: str) -> dict[str, list[dict]]:
    """基金投資明細-月前十大 → {基金名稱: [持股]}。基金名稱跨十列（rowspan），每檔後面接一列「合計」。"""
    tables = re.findall(r"<table[^>]*>(.*?)</table>", page or "", re.S)
    if not tables:
        return {}
    out, fund = {}, None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", max(tables, key=lambda t: t.count("<tr")), re.S):
        cells = [clean(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        if len(cells) == 10:
            fund, cells = cells[0], cells[1:]
        if len(cells) != 9 or not fund or not re.fullmatch(r"\d+", cells[0]):
            continue  # 表頭、合計列
        rank, kind, code, name, _amount, _guarantor, _sub, _units, weight = cells
        out.setdefault(fund, []).append({"rank": int(rank), "asset_code": blank(code), "asset_name": blank(name),
                                         "kind": asset_kind(kind), "weight": num(weight)})
    return out


def parse_dividends(page: str) -> list[dict]:
    """基金配息資料 → [{fund_id, ex_date, pay_date, cash}]。只收新台幣配息。"""
    def day(value):
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", value or "")
        return "-".join(m.groups()) if m else None

    return [{"fund_id": column(r, "基金統編"), "ex_date": day(column(r, "除息日")), "pay_date": day(column(r, "發放日")),
             "cash": num(column(r, "每單位分派金額"))}
            for r in table(page) if column(r, "配息幣別") == "TWD" and day(column(r, "除息日")) and column(r, "基金統編")]


# ---------- 抓取 ----------

def fetch(fixtures: Path | None = None, context: ssl.SSLContext | None = None, today: date | None = None) -> dict:
    """回傳 {"month": 基本資料年月, "year": 費用比率年度, "funds": {ETF 代號: {...}},
    "summary": {"month": 彙總表年月, "funds": {ETF 代號: {...}}}, "top10": {"month": 年月, "funds": {ETF 代號: [持股]}},
    "dividends": [{code, ex_date, pay_date, cash}]}；基本資料抓不到時回傳 {}。"""
    today = today or date.today()
    if fixtures:
        def load(name, *_steps, start=None):
            path = fixtures / f"{name}.html"
            return path.read_text("utf-8") if path.exists() else ""
    else:
        site = Site(context)
        names = {"basic_std": BASIC, "basic_fee": BASIC, "summary": SUMMARY}

        def load(name, *steps, start=None):
            path = names.get(name) or (TOP10 if name.startswith("top10") else DIVIDEND if name.startswith("dividend") else RATIO)
            return site.query(path, *steps, start=start)

    try:
        std = load("basic_std", {"ddlQ_Column": "1", "BtnQuery": "查詢"})
        fee = load("basic_fee", {"ddlQ_Column": "2", "BtnQuery": "查詢"})
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不更新公會資料。", file=sys.stderr)
        return {}
    funds = parse_basic(std, fee)
    if not funds:
        print(f"警告：{SOURCE}基金基本資料格式改變（找不到 ETF），本次不更新公會資料。", file=sys.stderr)
        return {}
    month = selected(std, "ddlQ_YYYYMM") or ""

    # 年度費用比率：去年的全年資料還沒公布時（年初）改用前年
    ratios, year = {}, None
    for y in (today.year - 1, today.year - 2):
        try:
            ratios = parse_ratios(load(f"ratio_{y}", {"ddlQ_Y": str(y)}, {"ddlQ_M": "Year", "BtnQuery": "查詢"}))
        except RuntimeError as exc:
            print(f"警告：{SOURCE}費用比率抓取失敗（{exc}）", file=sys.stderr)
            break
        if ratios:
            year = y
            break
    for f in funds.values():
        r = ratios.get(f["fund_id"])
        # 年中才成立的基金，全年比率只涵蓋幾個月，不收
        full_year = year and f["inception"] and f["inception"] < f"{year}-01-01"
        f["expense"] = {"year": year, **r} if r and full_year else None

    # 基金資料彙總表：最新一個月的全部基金
    try:
        page = load("summary", {"rdo1": "rbComid", "ddlQ_Comid": "", "BtnQuery": "查詢"})
    except RuntimeError as exc:
        print(f"警告：{SOURCE}基金資料彙總表抓取失敗（{exc}）", file=sys.stderr)
        page = ""
    by_id, ym = parse_summary(page), selected(page, "ddlQ_YM") or ""
    summary = {code: by_id[f["fund_id"]] for code, f in funds.items() if f["fund_id"] in by_id}
    return {"month": yyyymm(month), "year": year, "funds": funds,
            "summary": {"month": yyyymm(ym), "funds": summary} if summary and yyyymm(ym) else {},
            "top10": fetch_top10(load, funds), "dividends": fetch_dividends(load, funds)}


def fetch_top10(load, funds: dict) -> dict:
    """月前十大：依 ETF 類型逐一查詢，用基金名稱對到代號（名稱重複、對不到的不收）。"""
    try:
        start = load("top10")
        found: dict[str, list] = {}
        for cls in (c for c in options(start, "ddlQ_Class") if c.startswith(ETF_CLASSES)):
            found.update(parse_top10(load(f"top10_{cls}", {"rdo1": "rbClass", "ddlQ_Class": cls, "BtnQuery": "查詢"}, start=start)))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}月前十大抓取失敗（{exc}）", file=sys.stderr)
        return {}
    keys: dict[str, str | None] = {}
    for code, f in funds.items():
        k = name_key(f["name"])
        keys[k] = None if k in keys else code
    top = {keys[name_key(n)]: rows for n, rows in found.items() if keys.get(name_key(n))}
    ym = yyyymm(selected(start, "ddlQ_YM") or "")
    return {"month": ym, "funds": top} if top and ym else {}


def fetch_dividends(load, funds: dict) -> list[dict]:
    """配息資料：最新兩個月（每月第 10 個營業日公布上個月的除息紀錄）。"""
    by_id = {f["fund_id"]: code for code, f in funds.items()}
    out = []
    try:
        start = load("dividend")
        for ym in options(start, "ddlQ_YM")[-2:]:
            page = load(f"dividend_{ym}", {"ddlQ_YM": ym, "rdo1": "rbComid", "ddlQ_Comid": "", "BtnQuery": "查詢"}, start=start)
            out += [{"code": by_id[d.pop("fund_id")], **d} for d in parse_dividends(page) if d["fund_id"] in by_id]
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息資料抓取失敗（{exc}）", file=sys.stderr)
    return out


def yyyymm(value: str) -> str | None:
    return f"{value[:4]}-{value[4:]}" if re.fullmatch(r"\d{6}", value or "") else None
