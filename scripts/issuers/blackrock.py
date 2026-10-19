"""貝萊德投信官網（www.blackrock.com/tw）。

網站前面有 Akamai 防火牆：每 3 秒最多一個請求，被擋（HTTP 403 或「Access Denied」頁面）就停止。
- 產品篩選器 product-screener-v3.1.jsn：全部基金，productType 為 ISHARES_FUND_DATA 的是在台上市的 ETF
  （代號、產品頁網址、最新正式淨值與日期、計價幣別）
- 產品頁：申購買回清單（淨值日期的淨資產、已發行單位數、與前日已發行單位差異數）、掛牌日、追蹤指數、
  本益比與股價淨值比，以及成分股檔案的網址
- 成分股 CSV：全部成分、權重、股數或債券面額；日期在第一行，通常比淨值日期晚一天
- ETF 配息紀錄頁 /tw/etf-dividend-record：全部 ETF 的除息日、發放日與每單位配息（頁面裡的 JSON）
經理人、風險等級、保管費與受益人數官網沒有，由投信投顧公會的資料補上。產品頁的管理費只寫一個數字，
分段收費時看不出來，所以費率也用公會的契約原文。預估淨值在第三方網站，沿用證交所轉載的。
"""
from __future__ import annotations

import csv
import html
import http.cookiejar
import io
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from .common import num, ymd

ISSUER = "貝萊德"
SOURCE = "貝萊德投信官網"
SITE = "https://www.blackrock.com"
SCREENER = ("/tw/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/product-screener-v3/data/zh/"
            "tw-one/product-screener-backend-config&siteEntryPassthrough=true")
DIVIDENDS = "/tw/etf-dividend-record"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
KINDS = {"指數股票型": "股票", "Equity": "股票", "Fixed Income": "債券", "Futures": "期貨"}  # 現金、保證金不是成分
ZH_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


class Site:
    """保留 Cookie，每 3 秒最多送出一個請求；被防火牆擋下就停止，之後的請求都不再送出。"""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        )
        self.next_time = 0.0
        self.blocked = False

    def get(self, path: str, referer: str = "/tw") -> str:
        if self.blocked:
            raise RuntimeError(f"{path}：已被網站擋下，不再送出請求")
        headers = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9", "Referer": SITE + referer}
        time.sleep(max(0.0, self.next_time - time.monotonic()))
        self.next_time = time.monotonic() + 3
        try:
            with self.opener.open(urllib.request.Request(SITE + path, headers=headers), timeout=60) as resp:
                body = resp.read().decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                self.blocked = True
            raise RuntimeError(f"{path}：{exc}") from None
        except Exception as exc:  # noqa: BLE001
            if "Tunnel connection failed: 403" in str(exc):
                self.blocked = True
            raise RuntimeError(f"{path}：{exc}") from None
        if blocked_page(body):
            self.blocked = True
            raise RuntimeError(f"{path}：被防火牆擋下（Access Denied）")
        return body


def blocked_page(body: str) -> bool:
    return "<TITLE>Access Denied</TITLE>" in body[:2000] or "<title>Access Denied</title>" in body[:2000]


def clean(fragment: str) -> str | None:
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()
    return None if s in ("", "-", "--") else s


def zh_ymd(value) -> str | None:
    """2026年10月15日 → 2026-10-15。"""
    m = ZH_DATE.search(str(value or ""))
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def amount(value) -> float | None:
    """「新台幣 3,632,399,023.00」→ 3632399023.0。"""
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(value or ""))
    return num(m.group(0)) if m else None


# ---------- 解析 ----------

def parse_screener(data) -> list[dict]:
    """篩選器 → 在台上市的 ETF。沒有值的欄位是字串「-」。"""
    out = []
    for v in (data or {}).values() if isinstance(data, dict) else []:
        if not isinstance(v, dict) or v.get("productType") != "ISHARES_FUND_DATA" or not v.get("localExchangeTicker"):
            continue
        nav, asof = v.get("navAmount"), v.get("navAmountAsOf")
        out.append({"code": str(v["localExchangeTicker"]).strip(), "pid": str(v.get("portfolioId") or ""),
                    "url": v.get("productPageUrl"), "currency": v.get("seriesBaseCurrencyCode"),
                    "nav_official": num(nav.get("r")) if isinstance(nav, dict) else None,
                    "nav_date": ymd(asof.get("r")) if isinstance(asof, dict) else None})
    return [f for f in out if f["url"]]


def product_items(page: str) -> dict[str, tuple[str | None, str | None]]:
    """產品頁的資料格：{欄位代號: (內容, 資料日期)}。"""
    out = {}
    for m in re.finditer(r'<div class="product-data-item col-(\w+)\s*">(.*?)<div class="data">(.*?)</div>', page or "", re.S):
        asof = re.search(r'<div class="as-of-date">(.*?)</div>', m.group(2), re.S)
        out.setdefault(m.group(1), (clean(m.group(3)), zh_ymd(asof.group(1)) if asof else None))
    return out


def parse_product(page: str) -> dict:
    items = product_items(page)
    if "totalOutstandingShares" not in items and "netAssetValue" not in items:
        return {}
    get = lambda k: items.get(k, (None, None))
    pe, pb = get("priceEarnings"), get("priceBook")
    csv_link = re.search(r'(/tw/products/\d+/fund/\d+\.ajax\?fileType=csv(?:&amp;|&)fileName=[^"\'&]+_holdings(?:&amp;|&)dataType=fund)', page)
    return {
        "aum_twd": amount(get("netAssetValue")[0]) if "新台幣" in (get("netAssetValue")[0] or "") else None,
        "units": amount(get("totalOutstandingShares")[0]),
        "units_change": amount(get("netChangeInOutstandingShares")[0]),
        # 與前日已發行單位差異數的資料日期就是淨值日期（當天與前一天的單位數相減）
        "pcf_date": get("netChangeInOutstandingShares")[1] or get("totalOutstandingShares")[1],
        "list_date": zh_ymd(get("listingDate")[0]),
        "index_name": get("indexSeriesName")[0],
        "stats": {"date": pe[1] or pb[1], "div_yield": None, "pe": amount(pe[0]), "pb": amount(pb[0]), "roe": None,
                  "beta": None, "volatility": None} if pe[0] or pb[0] else None,
        "holdings_url": html.unescape(csv_link.group(1)) if csv_link else None,
    }


def parse_holdings(raw: str) -> tuple[str | None, list[dict]]:
    """成分股 CSV：第一行是「截至,日期」，第三行是欄名。債券的 Ticker 是發行機構代號、不唯一，
    代號改用「Ticker 票面利率 到期日」。全球型 ETF 會有同代號、不同市場的股票（例如美國與德國的 MRK），
    重複的代號全部加上市場（債券加上名稱），不論排序都對得到同一檔；仍重複的再加序號。"""
    rows = list(csv.reader(io.StringIO(raw or "")))
    day = zh_ymd(rows[0][1]) if rows and len(rows[0]) > 1 and rows[0][0].strip() == "截至" else None
    if len(rows) < 3:
        return day, []
    header, found = [h.strip() for h in rows[2]], []
    for r in rows[3:]:
        if len(r) != len(header):
            continue
        h = dict(zip(header, (c.strip() for c in r)))
        kind = KINDS.get(h.get("Asset Class"))
        if not kind or not h.get("Ticker"):
            continue
        if "ETF" in (h.get("Sector") or ""):
            kind = "基金"  # 債券 ETF 的資產類別也寫 Fixed Income，產業欄寫「ETF(指數股票型基金)」
        code = h["Ticker"]
        if kind == "債券":
            code = " ".join(x for x in (h["Ticker"], h.get("Coupon (%)"), zh_ymd(h.get("Maturity"))) if x)
        extra = h.get("Name") if kind == "債券" else (h.get("Location") or h.get("Exchange"))
        found.append((code, extra, {"asset_name": h.get("Name") or None, "kind": kind, "weight": num(h.get("Weight (%)")),
                                    "qty": num(h.get("Par Value")) if kind == "債券" else num(h.get("Shares"))}))
    counts: dict[str, int] = {}
    for code, _, _ in found:
        counts[code] = counts.get(code, 0) + 1
    out, seen = [], {}
    for code, extra, h in found:
        if counts[code] > 1 and extra:
            code = f"{code} {extra}"
        seen[code] = seen.get(code, 0) + 1
        out.append({"asset_code": code + (f" #{seen[code]}" if seen[code] > 1 else ""), **h})
    return day, out


def parse_dividends(page: str) -> dict[str, list[dict]]:
    """配息紀錄頁裡的 funds-data：{代號: [配息]}。recordDate 是月底的評價日，不是收益分配基準日，不收。"""
    m = re.search(r'<div id="funds-data" class="data-source"[^>]*>(.*?)</div>', page or "", re.S)
    try:
        funds = json.loads(html.unescape(m.group(1))) if m else []
    except ValueError:
        return {}
    out = {}
    for f in funds if isinstance(funds, list) else []:
        code = str(f.get("fundCode") or "").strip()
        out[code] = [{"code": code, "ex_date": ymd(d.get("exDate")), "pay_date": ymd(d.get("payableDate")),
                      "cash": num(d.get("dividendRate"))} for d in f.get("details") or [] if ymd(d.get("exDate"))]
    return out


# ---------- 抓取 ----------

def fetch(get_json=None, fixtures: Path | None = None, context: ssl.SSLContext | None = None,
          today: date | None = None, **_) -> dict[str, dict]:
    site = None
    if fixtures:
        def call(name, _path, _referer=None):
            path = fixtures / name
            if not path.exists():
                raise RuntimeError(f"沒有測試資料 {path.name}")
            return path.read_text("utf-8-sig")
    else:
        site = Site(context)

        def call(_name, path, referer="/tw"):
            return site.get(path, referer)

    try:
        funds = parse_screener(json.loads(call("screener.json", SCREENER, "/tw/products/products-list")))
    except (RuntimeError, ValueError) as exc:
        print(f"警告：{SOURCE}連不上（{exc}），本次不使用貝萊德資料。", file=sys.stderr)
        return {}
    if not funds:
        print(f"警告：{SOURCE}格式改變（找不到 ETF 列表），本次不使用貝萊德資料。", file=sys.stderr)
        return {}
    try:
        dividends = parse_dividends(call("dividends.html", DIVIDENDS))
    except RuntimeError as exc:
        print(f"警告：{SOURCE}配息紀錄抓取失敗（{exc}）", file=sys.stderr)
        dividends = {}

    # 篩選器一次就有全部 ETF 的正式淨值；產品頁與成分股逐檔補上，被擋下時其餘 ETF 只留篩選器與配息資料
    records = {}
    for f in funds:
        code, twd = f["code"], f["currency"] == "TWD"
        rec = records[code] = {
            "code": code, "issuer": ISSUER, "source": SOURCE,
            "source_url": urllib.parse.urljoin(SITE, f["url"]), "currency": "TWD" if twd else None, "nav_estimate": None, "nav_estimate_date": None,
            "beneficiaries": None, "beneficiaries_month": None, "manager": None, "risk_class": None,
            "mgmt_fee": None, "mgmt_fee_text": None, "custodian_fee_text": None, "stats": None, "index_name": None,
            "list_date": None, "aum": None, "units": None, "units_change": None, "holdings": [],
            "nav_official": f["nav_official"] if twd else None, "nav_date": f["nav_date"],
            "dividends": dividends.get(code, []) if twd else []}  # 外幣計價的配息金額不是新台幣，不收
        if site and site.blocked:
            continue
        try:
            product = parse_product(call(f"product_{code}.html", f["url"]))
        except RuntimeError as exc:
            print(f"警告：{SOURCE} {code} 產品頁抓取失敗（{exc}）", file=sys.stderr)
            continue
        if not product:
            print(f"警告：{SOURCE} {code} 產品頁格式改變，只用篩選器的淨值。", file=sys.stderr)
            continue
        aum, link = product.pop("aum_twd"), product.pop("holdings_url")
        rec.update(product)
        rec["holdings_url"] = urllib.parse.urljoin(SITE, link) if link else None
        rec["aum"] = round(aum / 1e8, 2) if aum and twd else None
        if link:
            try:
                rec["holdings_date"], rec["holdings"] = parse_holdings(call(f"holdings_{code}.csv", link, f["url"]))
            except RuntimeError as exc:
                print(f"警告：{SOURCE} {code} 成分股抓取失敗（{exc}）", file=sys.stderr)
    if site and site.blocked:
        print(f"警告：{SOURCE}擋下了請求，部分 ETF 本次只有篩選器的淨值與配息。", file=sys.stderr)
    return records
