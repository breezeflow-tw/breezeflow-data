"""富邦投信 ETF 投資網（websys.fsit.com.tw/FubonETF）。

ASP.NET 網站，資料直接寫在頁面裡：
- /Trade/Pcf.aspx：預設頁的「選擇其他 ETF」選單列出全部 ETF 代號
- /Trade/Estimate.aspx：全部 ETF 的即時估計淨值與資料時間
- /Fund/Profile.aspx?stkId=：追蹤指數、上市日、風險等級、計價幣別、經理費、保管費
- /Fund/Assets.aspx?stkId=：最新一日（標示資料日期）的淨資產、在外流通單位數、每單位淨值與全部成分
- /Trade/Pcf.aspx?stkId=：最新申購買回清單，含與前日已發行單位差異數、上個月受益人數；
  清單的淨值日期標在「YYYY/MM/DD 每現金申購基數總價金差異額」這類欄位名稱上
- /Dividend/Dividend.aspx：全部 ETF 的配息紀錄（除息日、發放日、每單位分配金額），
  預設顯示今年，查其他期間要帶回頁面的表單欄位送出
"""
from __future__ import annotations

import html
import http.cookiejar
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from .common import flat_fee, num, ymd

ISSUER = "富邦"
SOURCE = "富邦投信官網"
BASE = "https://websys.fsit.com.tw/FubonETF"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
KINDS = {"股票": "股票", "債券": "債券", "期貨": "期貨", "基金": "基金", "受益憑證": "基金", "ETF": "基金"}
TWD = ("新台幣", "新臺幣")


class Site:
    """保留 Cookie 的連線，每次請求間隔 1 秒，失敗最多重試 3 次。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )

    def open(self, path: str, data: dict | None = None, referer: str = "/index.aspx") -> str:
        headers = {"User-Agent": UA, "Referer": BASE + referer, "Accept-Language": "zh-TW,zh;q=0.9"}
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last = None
        for attempt in range(3):
            time.sleep(1 if attempt == 0 else 5)
            try:
                # 配息完整歷史約 3MB，伺服器要二十多秒才產生完，逾時放寬
                with self.opener.open(urllib.request.Request(BASE + path, data=body, headers=headers), timeout=90) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    break
        raise RuntimeError(f"{path}：{last}")


# ---------- 解析 ----------

def clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def blank(value: str | None) -> str | None:
    return None if value in (None, "", "-", "--", "—", "無") else value


def pairs(page: str) -> dict[str, str]:
    """頁面上「<p>欄位</p><p>內容</p>」的資料 → {欄位: 內容}。內容是清單時（例如分段費率）以「；」串接。"""
    out: dict[str, str] = {}
    for label, value in re.findall(r"<p[^>]*>([^<]+)</p>\s*<p[^>]*>(.*?)</p>", page or "", re.S):
        items = [clean(i) for i in re.findall(r"<li[^>]*>(.*?)</li>", value, re.S)] or [clean(value)]
        out.setdefault(clean(label), "；".join(i for i in items if i))
    return out


def amount(value) -> float | None:
    """'NT$17,984,642,659'、'NT$-111,306'、'1,870,540,000' → 數字。"""
    m = re.search(r"-?[\d,]*\d(?:\.\d+)?", str(value or ""))
    return num(m.group()) if m else None


def single_rate(text: str | None) -> float | None:
    """只有一個費率時換成數字：「0.15%」或只有一段的「0億(含)以上 : 0.15%」；多段級距不換算。"""
    if not text or "；" in text:
        return None
    return flat_fee(re.sub(r"^\s*0\s*億\s*\(含\)\s*以上\s*[:：]\s*", "", text))


def parse_codes(page: str) -> list[str]:
    """申購買回清單頁的「選擇其他 ETF」選單 → 全部代號。"""
    return list(dict.fromkeys(re.findall(r"Pcf\.aspx\?stkId=([0-9A-Z]+)&", page or "")))


def page_code(page: str) -> str | None:
    """頁面標題上的代號：基金檔案、基金資產頁是「代號 / 簡稱」，申購買回清單是「代號 簡稱」。"""
    m = (re.search(r'<h5 class="mo-mb22">\s*([0-9A-Z]+)\s*/', page or "")
         or re.search(r'<h6 class="top blue3[^"]*">\s*([0-9A-Z]+)\s', page or ""))
    return m.group(1) if m else None


def parse_estimates(page: str) -> dict[str, dict]:
    """即時估計淨值表：代號、昨收淨值、預估淨值、漲跌、昨收市價、最新市價、漲跌、折溢價、追蹤差距、資料時間、基金營業日。"""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) != 11 or "name_txt" not in row:
            continue
        code = clean(re.split(r"<br", cells[0])[0])
        when = ymd(clean(cells[9]))
        est = num(clean(cells[2]))
        if code and est and when:
            out.setdefault(code, {"nav_estimate": est, "nav_estimate_date": when})
    return out


def parse_profile(page: str) -> dict:
    d = pairs(page)
    mgmt = blank(d.get("經理費(年)"))
    return {
        "list_date": ymd(d.get("基金上市日")),
        "currency": blank(d.get("計價幣別")),
        "mgmt_fee": single_rate(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": blank(d.get("保管費(年)")),
        "risk_class": blank(d.get("風險等級")),
        "index_name": blank(d.get("追蹤指數")),
    }


def labelled(d: dict[str, str], prefix: str) -> tuple[str | None, str | None]:
    """找出「prefix(幣別或單位)」形式的欄位，回傳 (括號內文字, 內容)。"""
    for k, v in d.items():
        m = re.fullmatch(re.escape(prefix) + r"\s*\((.+)\)", k)
        if m:
            return m.group(1), v
    return None, None


def parse_assets(page: str) -> dict:
    """基金資產：資料日期、淨資產、在外流通單位數、每單位淨值，以及各類成分表（股票、債券、期貨…）。"""
    day = re.search(r"資料日期[：:]\s*(\d{4}/\d{1,2}/\d{1,2})", page or "")
    if not day:
        return {}
    d = pairs(page)
    cur_total, total = labelled(d, "基金淨資產")
    cur_nav, nav = labelled(d, "基金每單位淨值")
    _, units = labelled(d, "基金在外流通單位數")
    holdings = []
    for label, table in re.findall(r"<h6[^>]*>\s*([^<]+?)\s*</h6>\s*<div[^>]*>\s*<table[^>]*>(.*?)</table>", page, re.S):
        rows = [[clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)]
        if not rows:
            continue
        head = rows[0]

        def col(*names, prefix=False):
            for i, h in enumerate(head):
                if h in names or (prefix and any(h.endswith(n) for n in names)):
                    return i
            return None

        c_code, c_name = col("代碼", "代號", prefix=True), col("名稱", prefix=True)
        c_qty, c_weight = col("股數", "口數", "面額", "單位數", "數量"), col("權重(%)")
        if c_code is None or c_weight is None:
            continue
        for cells in rows[1:]:
            if len(cells) != len(head) or "合計" in cells[0] or not cells[c_code]:
                continue
            holdings.append({
                "asset_code": cells[c_code],
                "asset_name": cells[c_name] if c_name is not None else None,
                "kind": KINDS.get(label, label),
                "weight": num(cells[c_weight]),
                "qty": num(cells[c_qty]) if c_qty is not None else None,
            })
    twd = cur_total in TWD and cur_nav in TWD
    total = amount(total)
    return {
        "nav_date": ymd(day.group(1)),
        # 外幣計價的淨值不和新台幣市價比較，規模也不自行換算
        "nav_official": amount(nav) if cur_nav in TWD else None,
        "aum": round(total / 1e8, 2) if total and twd else None,
        "units": amount(units),
        "holdings": holdings,
    }


def parse_pcf(page: str) -> dict:
    """申購買回清單。淨值日期取自「YYYY/MM/DD 每現金申購基數總價金差異額」等欄位名稱。"""
    d = pairs(page)
    day = next((ymd(m.group(1)) for k in d for m in [re.match(r"(\d{4}/\d{1,2}/\d{1,2})\s*每\S*申購基數", k)] if m), None)
    bene = next(((m.group(1), v) for k, v in d.items() for m in [re.match(r"上一個月之受益人人數\s*\((\d{4}/\d{1,2})\)", k)] if m),
                (None, None))
    return {
        "date": day,
        "nav": amount(d.get("每受益權單位淨資產價值")),
        "units": amount(d.get("已發行受益權單位總數")),
        "units_change": amount(d.get("與前日已發行單位差異數")),
        "beneficiaries": amount(bene[1]),
        "beneficiaries_month": bene[0].replace("/", "-") if bene[0] else None,
    }


def parse_dividends(page: str) -> list[dict]:
    """配息表：每筆一張卡片，標題連到 Detail.aspx?stkId=代號，內容是「<span>欄位</span><em>內容</em>」。"""
    out, seen = [], set()
    for block in re.split(r'<p class="title">', page or "")[1:]:
        code = re.search(r"stkId=([0-9A-Z]+)", block[:600])
        fields: dict[str, str] = {}
        for k, v in re.findall(r"<span>([^<]*)</span>\s*<em>(.*?)</em>", block, re.S):
            fields.setdefault(clean(k), clean(v))
        ex_date = ymd(fields.get("收益分配除息日"))
        if not code or not ex_date or fields.get("配息幣別") not in TWD or (code.group(1), ex_date) in seen:
            continue
        seen.add((code.group(1), ex_date))
        out.append({"code": code.group(1), "ex_date": ex_date, "pay_date": ymd(fields.get("收益分配發放日")),
                    "cash": num(fields.get("每單位分配金額(元)"))})
    return out


def form_fields(page: str) -> dict[str, str]:
    """ASP.NET 表單的隱藏欄位（__VIEWSTATE 等），送出查詢時要原樣帶回。"""
    form = page[page.find('<form method="post"'):]
    fields = {}
    for tag in re.findall(r'<input[^>]*type="hidden"[^>]*>', form):
        name, value = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return fields


def dividend_query(page: str, start: date, end: date) -> dict[str, str] | None:
    """配息頁「自訂區間」查詢要送出的表單內容；找不到表單時回傳 None。"""
    m = re.search(r'name="([^"]*\$)ddlFundType"', page or "")
    fields = form_fields(page or "")
    if not m or "__VIEWSTATE" not in fields:
        return None
    pre = m.group(1)
    # 年度選單也會一起送出，要用頁面上有的選項，否則伺服器會拒絕
    year = re.search(r'ddlRange"[^>]*>.*?<option selected="selected" value="(\d{4})"', page, re.S)
    return {**fields, pre + "ddlFundType": "All", pre + "contact": "2", pre + "ddlRange": year.group(1) if year else str(end.year),
            pre + "sDate": start.strftime("%Y/%m/%d"), pre + "eDate": end.strftime("%Y/%m/%d"),
            pre + "time_dividend": "0", pre + "btnSearch": "查 詢"}


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None,
          dividend_codes: set[str] | None = None, today: date | None = None, **_) -> dict[str, dict]:
    today = today or date.today()
    if fixtures:
        def load(name, *_args, **_kw):
            path = fixtures / f"{name}.html"
            return path.read_text("utf-8") if path.exists() else ""
    else:
        site = Site(context)

        def load(_name, path, data=None, referer="/index.aspx"):
            return site.open(path, data, referer)

    try:
        codes = parse_codes(load("list", "/Trade/Pcf.aspx"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用富邦資料。", file=sys.stderr)
        return {}
    if not codes:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 選單），本次不使用富邦資料。", file=sys.stderr)
        return {}
    try:
        estimates = parse_estimates(load("estimate", "/Trade/Estimate.aspx"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}即時估計淨值抓取失敗（{exc}）", file=sys.stderr)
        estimates = {}

    records = {}
    # 估計淨值表另有選單沒列的代號（例如以人民幣交易的 00625K 富邦上証+R），網站沒有它們的基金頁面，不查
    for code in codes:
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE, "nav_official": None, "nav_date": None,
               "nav_estimate": None, "nav_estimate_date": None, "units_change": None, "aum": None, "units": None,
               "beneficiaries": None, "beneficiaries_month": None, "mgmt_fee": None, "mgmt_fee_text": None,
               "custodian_fee_text": None, "manager": None, "risk_class": None, "list_date": None, "index_name": None,
               "stats": None, "holdings": [], "dividends": [], **estimates.get(code, {})}
        # 頁面標題的代號要和查詢的一致才採用，以免網站對沒有的代號改顯示別檔基金
        try:
            page = load(f"profile_{code}", f"/Fund/Profile.aspx?stkId={code}", referer="/Fund/ETFSeries.aspx")
            if page_code(page) != code:
                print(f"警告：{SOURCE}查不到 {code} 的基金檔案", file=sys.stderr)
                continue
            info = parse_profile(page)
            page = load(f"assets_{code}", f"/Fund/Assets.aspx?stkId={code}", referer=f"/Fund/Profile.aspx?stkId={code}")
            assets = parse_assets(page) if page_code(page) == code else {}
            page = load(f"pcf_{code}", f"/Trade/Pcf.aspx?stkId={code}&lan=TW", referer="/Trade/Pcf.aspx")
            pcf = parse_pcf(page if page_code(page) == code else "")
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        currency = info.pop("currency")
        rec.update(info)
        rec.update(assets)
        rec["currency"] = "TWD" if currency in TWD else None
        rec["source_url"] = f"{BASE}/Fund/Assets.aspx?stkId={code}"
        rec["holdings_url"] = rec["source_url"]
        if currency not in (None, *TWD):
            rec["nav_official"] = rec["nav_estimate"] = None  # 外幣計價的淨值不和新台幣市價比較
        rec["beneficiaries"], rec["beneficiaries_month"] = pcf["beneficiaries"], pcf["beneficiaries_month"]
        # 申購買回清單和基金資產是同一天的資料，單位增減才算在這一天
        same_day = pcf["date"] == rec["nav_date"] if pcf["date"] else (
            pcf["units"] is not None and pcf["units"] == rec["units"] and pcf["nav"] == rec["nav_official"])
        if rec["nav_date"] and same_day:
            rec["units_change"] = pcf["units_change"]
        records[code] = rec

    # 配息：預設頁是今年的紀錄。資料庫還沒有富邦紀錄時，另外查 2006 年以來的完整歷史；
    # 完整歷史沒查成功就整批不用，下次再查，避免只存到今年的紀錄之後就不再回補
    try:
        page = load("dividends", "/Dividend/Dividend.aspx")
        dividends = parse_dividends(page)
        if not dividend_codes:
            start = date(2006, 1, 1)
            query = dividend_query(page, start, date(today.year, 12, 31))
            full = load("dividends_all", "/Dividend/Dividend.aspx", query, referer="/Dividend/Dividend.aspx") if query else ""
            # 查詢成功時，回傳頁面的起始日欄位會帶著送出的日期
            if not re.search(r'name="[^"]*sDate"[^>]*value="%s"' % start.strftime("%Y/%m/%d"), full):
                raise RuntimeError("完整歷史查詢沒有成功")
            dividends += parse_dividends(full)
        seen = set()
        for d in dividends:
            if d["code"] in records and (d["code"], d["ex_date"]) not in seen:
                seen.add((d["code"], d["ex_date"]))
                records[d["code"]]["dividends"].append(d)
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息紀錄抓取失敗（{exc}）", file=sys.stderr)
    return records
