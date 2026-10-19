"""凱基投信官網（www.kgifund.com.tw）。

網站由伺服器產生頁面片段，以表單 POST 載入：
- /Fund/LatestNavVC：ETF 最新正式淨值與日期（依基金類型篩選）
- /Fund/Detail?fundID=：證券代號、掛牌日、計價幣別、經理費、保管費、風險等級、經理人
- /Fund/RedemptionVC：申購買回清單，含淨資產、發行單位數、與前日單位差異、淨值（標示資料日期）與成分；
  清單的資料日期通常比最新淨值晚一天（台股型晚一個營業日、債券型晚兩個）
- /Fund/DividendListVC：全部 ETF 的配息紀錄（基準日、每單位配息、除息日、發放日）
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
from datetime import date, timedelta
from pathlib import Path

from .common import flat_fee, num, ymd

ISSUER = "凱基"
SOURCE = "凱基投信官網"
BASE = "https://www.kgifund.com.tw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
ETF_TYPES = ("5328", "5295", "5396")  # 股票型ETF、債券型ETF、平衡型ETF


class Site:
    """保留 Cookie 的連線，每次請求間隔 1 秒，失敗最多重試 3 次。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )

    def open(self, path: str, data: list | None = None, referer: str = "/Home/ETF") -> str:
        headers = {"User-Agent": UA, "Referer": BASE + referer, "X-Requested-With": "XMLHttpRequest"}
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last = None
        for attempt in range(3):
            time.sleep(1 if attempt == 0 else 4)
            try:
                with self.opener.open(urllib.request.Request(BASE + path, data=body, headers=headers), timeout=40) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "Tunnel connection failed: 403" in str(exc):
                    break
        raise RuntimeError(f"{path}：{last}")


# ---------- 解析 ----------

def clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def flat_text(page: str) -> str:
    """把頁面轉成「欄位 | 內容 | 欄位 | 內容」的文字，方便依欄位名稱取值。"""
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page or "", flags=re.S)
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " | ", page)))
    return re.sub(r"(\|\s*)+", "| ", txt)


def field(txt: str, label: str) -> str | None:
    m = re.search(re.escape(label) + r"\s*\|\s*([^|]+?)\s*\|", txt)
    value = m.group(1).strip() if m else None
    return None if value in (None, "", "-", "—", "無") else value


def money(value) -> tuple[str | None, float | None]:
    """'TWD$33,367,387,239' → ('TWD', 33367387239.0)。"""
    m = re.match(r"\s*([A-Z]{3})\$\s*(-?[\d,.]+)", str(value or ""))
    return (m.group(1), num(m.group(2))) if m else (None, None)


def parse_nav_list(page: str) -> dict[str, dict]:
    """最新淨值列表 → {基金編號: {nav_official, nav_date}}。"""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
        fid = re.search(r"Detail\?fundID=(J\d+)", row)
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if fid and len(cells) >= 3 and ymd(cells[1]):
            out[fid.group(1)] = {"nav_official": num(cells[2]), "nav_date": ymd(cells[1])}
    return out


def parse_detail(page: str) -> dict:
    txt = flat_text(page)
    code = field(txt, "證券代號")
    mgmt = field(txt, "經理費(年)")
    return {
        "code": code.replace(" ", "") if code else None,
        "list_date": ymd(field(txt, "掛牌日")),
        "currency": field(txt, "計價幣別"),
        "mgmt_fee": flat_fee(mgmt),
        "mgmt_fee_text": mgmt,
        "custodian_fee_text": field(txt, "保管費(年)"),
        "risk_class": field(txt, "風險報酬等級"),
        "manager": field(txt, "基金經理人") or field(txt, "經理人"),
        "index_name": field(txt, "標的指數") or field(txt, "追蹤指數"),
    }


def parse_pcf(page: str) -> dict:
    """申購買回清單。淨值欄位名稱帶有資料日期，例如「(2026/10/16)每受益權單位淨資產價值(元)」。"""
    txt = flat_text(page)
    code = re.search(r"\(\s*([0-9]{4,6}[A-Z]?)\s*\)", txt)
    nav = re.search(r"\((\d{4}/\d{1,2}/\d{1,2})\)每受益權單位淨資產價值\(元\)\s*\|\s*([A-Z]{3}\$[\d,.]+)", txt)
    if not code or not nav:
        return {}
    currency, total = money(field(txt, "基金淨資產價值(元)"))
    holdings = []
    for t in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S):
        head = [clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S)]
        kind = "股票" if "股票代號" in head else "期貨" if "期貨代號" in head else None
        if not kind:
            continue
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
            cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
            if len(cells) < 4:
                continue
            row = dict(zip(head, cells))
            asset = row.get("股票代號") or row.get("期貨代號")
            ym = re.sub(r"\D", "", row.get("契約年月") or "")
            holdings.append({"asset_code": asset + (f"-{ym}" if ym else ""), "asset_name": row.get("股票名稱") or row.get("期貨名稱"),
                             "kind": kind, "weight": num(row.get("權重(%)")), "qty": num(row.get("股數") or row.get("口數"))})
    for ul in re.findall(r'<ul class="js-bond-list[^"]*"[^>]*>(.*?)</ul>', page, re.S):
        for li in re.findall(r'<li name="content"[^>]*>(.*?)</li>', ul, re.S):
            cells = [clean(c) for c in re.findall(r"<span[^>]*>(.*?)</span>", li, re.S)]
            if len(cells) >= 5:
                holdings.append({"asset_code": cells[0], "asset_name": cells[1], "kind": "債券",
                                 "weight": num(cells[4]), "qty": num(cells[2])})
    return {
        "code": code.group(1),
        "pcf_date": ymd(nav.group(1)),
        "pcf_nav": money(nav.group(2))[1],
        # 規模只收新台幣計價，外幣計價的不自行換算
        "aum": round(total / 1e8, 2) if total and currency == "TWD" else None,
        "units": num(field(txt, "已發行受益權單位總數")),
        "units_change": num(field(txt, "與前日已發行單位差異數")),
        "holdings": [h for h in holdings if h["asset_code"]],
    }


def parse_dividends(page: str) -> list[dict]:
    """配息列表：每檔一個區塊，標題為「名稱(代號)」，表格欄位以 data-th 標示。"""
    out = []
    blocks = re.split(r'<div class="LatestFundNav-info DividendList', page or "")
    for block in blocks[1:]:
        title = re.search(r"Detail\?fundID=J\d+\">([^<]*)</a>", block)
        code = re.search(r"\(\s*([0-9]{4,6}[A-Z]?)\s*\)", html.unescape(title.group(1))) if title else None
        if not code:
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
            cells = {k: clean(v) for k, v in re.findall(r'<td data-th="([^"]+)"[^>]*>(.*?)</td>', row, re.S)}
            ex_date = ymd(cells.get("除息日"))
            if ex_date:
                out.append({"code": code.group(1), "ex_date": ex_date, "pay_date": ymd(cells.get("發放日")),
                            "cash": num(cells.get("每單位配息金額"))})
    return out


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

        def load(_name, path, data=None, referer="/Home/ETF"):
            return site.open(path, data, referer)

    try:
        navs = parse_nav_list(load("nav", "/Fund/LatestNavVC",
                                   [("fundType[]", t) for t in ETF_TYPES] + [("keyword", ""), ("ismyOB", "false"), ("isMobile", "false")],
                                   "/Fund/LatestNav?isMoreETF=true"))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用凱基資料。", file=sys.stderr)
        return {}
    if not navs:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 淨值列表），本次不使用凱基資料。", file=sys.stderr)
        return {}

    records = {}
    for fid, nav in sorted(navs.items()):
        try:
            info = parse_detail(load(f"detail_{fid}", f"/Fund/Detail?fundID={fid}"))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {fid} 基金資料抓取失敗（{exc}）", file=sys.stderr)
            continue
        code = info.pop("code")
        currency = info.pop("currency")
        if not code:
            continue
        rec = {"code": code, "issuer": ISSUER, "source": SOURCE, "beneficiaries": None, "beneficiaries_month": None,
               "stats": None, "units_change": None, "holdings": [], "dividends": [], **info, **nav}
        rec["currency"] = "TWD" if currency in ("新臺幣", "新台幣") else None
        rec["source_url"] = f"{BASE}/Fund/RedemptionList?fundNo={fid}"
        if currency not in (None, "新臺幣", "新台幣"):
            rec["nav_official"] = None  # 外幣計價的淨值不和新台幣市價比較
        # 申購買回清單：從明天往回找最新一份（週末、假日沒有清單）
        for k in range(-1, 5):
            qdate = today - timedelta(days=k)
            try:
                pcf = parse_pcf(load(f"pcf_{fid}", "/Fund/RedemptionVC",
                                     [("fundID", fid), ("queryDate", qdate.strftime("%Y/%m/%d"))],
                                     f"/Fund/RedemptionList?fundNo={fid}"))
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 申購買回清單抓取失敗（{exc}）", file=sys.stderr)
                break
            if pcf.get("code") == code:
                rec.update({k_: pcf[k_] for k_ in ("pcf_date", "pcf_nav", "aum", "units", "units_change", "holdings")})
                break
        records[code] = rec

    # 配息：資料庫還沒有凱基紀錄時查完整歷史，之後只查最近一年多
    start = date(2010, 1, 1) if not dividend_codes else today - timedelta(days=400)
    try:
        page = load("dividends", "/Fund/DividendListVC",
                    [("sDate", start.strftime("%Y/%m/%d")), ("eDate", (today + timedelta(days=90)).strftime("%Y/%m/%d")),
                     ("type", "1"), ("keyword", "")], "/Fund/DividendList?Type=ETF")
        for d in parse_dividends(page):
            if d["code"] in records:
                records[d["code"]]["dividends"].append(d)
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息紀錄抓取失敗（{exc}）", file=sys.stderr)
    return records
