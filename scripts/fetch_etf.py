#!/usr/bin/env python3
"""抓取台股全市場 ETF 的市價、預估淨值、折溢價與成交量，寫入 SQLite 並輸出網頁用的 JSON。

只用 Python 標準函式庫，GitHub Actions 上不需安裝套件。

用法：
    python scripts/fetch_etf.py                 # 從證交所 / 櫃買中心抓即時資料
    python scripts/fetch_etf.py --fixtures DIR  # 用本機測試資料跑完整流程（離線測試用）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import returns
import store
import fund_flows
import sitca
from issuers import capital, cathay, fubon, fuhhwa, kgi, uni, yuanta

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "etf.db"
META_PATH = ROOT / "data" / "etf_meta.csv"
OUT_PATH = ROOT / "web" / "data" / "latest.json"

TW = timezone(timedelta(hours=8))

SOURCES = {
    # 證交所「ETF 預估淨值」頁面背後的資料，上市與上櫃 ETF 都在裡面
    "nav": "https://mis.twse.com.tw/stock/data/all_etf.txt",
    # 上市個股與 ETF 當日收盤行情
    "twse": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    # 上櫃當日收盤行情（含發行單位數 Capitals）
    "tpex": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
    # 上市基金基本資料：發行單位數、追蹤指數、上市日期
    "fund": "https://openapi.twse.com.tw/v1/opendata/t187ap47_L",
    # 除權除息預告表
    "div_twse": "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL",
    "div_tpex": "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost",
}

# 市價與成交量的主要來源；上市代號加 .TW，上櫃加 .TWO
YAHOO_CHART = "https://{host}/v8/finance/chart/{symbol}?range=10d&interval=1d"
# 每個交易日抓一次五年日線（含配息紀錄）計算區間報酬，同一份資料也拿來比對收盤價
YAHOO_HISTORY = "https://{host}/v8/finance/chart/{symbol}?period1={start}&period2={end}&interval=1d&events=div"
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

# 已接入的投信官網；淨值、規模、費用、成分股以這裡的資料為主
ISSUER_MODULES = {"yuanta": yuanta, "uni": uni, "capital": capital, "fuhhwa": fuhhwa, "kgi": kgi, "fubon": fubon,
                  "cathay": cathay}


# ---------- 下載 ----------

# 照常驗證憑證鏈與網域名稱，只關閉 Python 3.13 起預設的 X.509 嚴格模式：
# 部分投信網站（例如元大）的憑證缺少 Subject Key Identifier，瀏覽器與 curl 都接受，嚴格模式會拒絕。
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)

def http_get_json(url: str, retries: int = 4, first_delay: float = 2) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (breezeflow-data)"})
    delay = first_delay
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                raw = resp.read().decode("utf-8-sig")
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - 網路錯誤一律重試
            # 被網路政策擋下（代理伺服器拒絕連線）不是暫時性錯誤，重試沒有用
            if attempt == retries or "Tunnel connection failed: 403" in str(exc):
                raise RuntimeError(f"下載失敗：{url}（{exc}）") from exc
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def fetch_yahoo(symbols: list[str], since: datetime | None = None) -> dict[str, object]:
    """逐檔抓 Yahoo 股市日線；給 since 時抓從那天起的完整日線與配息紀錄。
    前 10 檔全部失敗就放棄（多半是被擋），避免白跑幾百次。"""
    def url(host, symbol):
        if since is None:
            return YAHOO_CHART.format(host=host, symbol=symbol)
        return YAHOO_HISTORY.format(host=host, symbol=symbol, start=int(since.timestamp()),
                                    end=int(datetime.now(TW).timestamp()) + 86400)

    def one(symbol):
        for host in YAHOO_HOSTS:
            try:
                return symbol, http_get_json(url(host, symbol), retries=1, first_delay=1)
            except RuntimeError:
                continue
        return symbol, None

    result = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        head = dict(pool.map(one, symbols[:10]))
        if symbols and not any(head.values()):
            print("警告：Yahoo 股市連不上，本次市價與成交量改用交易所資料。", file=sys.stderr)
            return {}
        result.update(head)
        result.update(pool.map(one, symbols[10:]))
    missing = [s for s, v in result.items() if v is None]
    if missing:
        print(f"警告：Yahoo 有 {len(missing)} 檔沒抓到：{', '.join(missing[:10])}", file=sys.stderr)
    return {s: v for s, v in result.items() if v is not None}


def load_sources(fixtures: Path | None) -> dict[str, object]:
    data = {}
    for key, url in SOURCES.items():
        if fixtures:
            path = fixtures / f"{key}.json"
            data[key] = json.loads(path.read_text("utf-8")) if path.exists() else []
        else:
            try:
                data[key] = http_get_json(url)
            except RuntimeError as exc:
                # 任一來源失敗都繼續；證交所 MIS 會擋海外 IP，此時淨值欄位留空
                print(f"警告：{exc}", file=sys.stderr)
                data[key] = []
    return data


# ---------- 解析 ----------

def num(value) -> float | None:
    """把 '1,234.50'、'+0.35'、'-'、'' 等字串轉成數字，無效值回傳 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "N/A", "除息", "除權"):
        return None
    s = re.sub(r"^[+＋]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def clean_name(name: str) -> str:
    """去掉名稱後面的法定警語與冗長字尾，例如「(本基金並無保證收益及配息)」「ETF證券投資信託基金」。"""
    name = re.sub(r"[（(][^）)]*[）)]$", "", str(name or "").strip())
    return re.sub(r"(ETF)?證券投資信託基金$", "", name).strip()


def ymd(value) -> str | None:
    """'20261016' → '2026-10-16'。"""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", str(value or "").strip())
    return "-".join(m.groups()) if m else None


def parse_nav(payload) -> dict[str, dict]:
    """all_etf.txt 格式：{"a1": [{"msgArray": [{a: 代號, b: 名稱, e: 市價, f: 預估淨值,
    g: 預估折溢價 %, h: 前一營業日淨值, i: 日期, j: 時間}, ...]}, ...]}，每個 a1 元素是一家投信。"""
    result = {}
    blocks = payload.get("a1", []) if isinstance(payload, dict) else []
    for block in blocks:
        for row in block.get("msgArray", []) or []:
            code = str(row.get("a", "")).strip()
            if not code:
                continue
            price, nav = num(row.get("e")), num(row.get("f"))
            premium = num(row.get("g"))
            if premium is None and price and nav:
                premium = (price - nav) / nav * 100
            result[code] = {
                "code": code,
                "name": clean_name(row.get("b", "")),
                "price": price,
                "nav": nav,
                "premium": premium,
                "nav_prev": num(row.get("h")),
                "date": ymd(row.get("i")),
                "source_date": str(row.get("i", "")).strip(),
                "source_time": str(row.get("j", "")).strip(),
            }
    return result


def pick(row: dict, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def is_etf(code: str) -> bool:
    """台股 ETF 代號都是 00 開頭（0050、006208、00679B）；ETN 是 02 開頭，不收。"""
    return code.startswith("00") and len(code) >= 4


def roc_date(value) -> str | None:
    """民國日期 '1151016' → '2026-10-16'。"""
    m = re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", str(value or "").strip())
    if not m:
        return None
    return f"{int(m.group(1)) + 1911}-{m.group(2)}-{m.group(3)}"


def parse_quotes(rows, market: str) -> dict[str, dict]:
    """證交所與櫃買的收盤行情欄位名稱不同，這裡統一成 close / change / volume_lots。"""
    result = {}
    for row in rows if isinstance(rows, list) else []:
        code = str(pick(row, "Code", "SecuritiesCompanyCode") or "").strip()
        if not is_etf(code):
            continue
        close = num(pick(row, "ClosingPrice", "Close"))
        change = num(pick(row, "Change"))
        shares = num(pick(row, "TradeVolume", "TradingShares"))
        prev = close - change if close is not None and change is not None else None
        result[code] = {
            "name": clean_name(pick(row, "Name", "CompanyName")),
            "date": roc_date(pick(row, "Date")),
            "market": market,
            "close": close,
            "change_pct": (change / prev * 100) if prev else None,
            "volume_lots": round(shares / 1000) if shares is not None else None,
            "units": num(pick(row, "Capitals")),
        }
    return result


def parse_yahoo(payloads: dict[str, object]) -> dict[str, dict[str, dict]]:
    """Yahoo 日線 → {代號: {日期: {close, change_pct, volume_lots}}}。成交量單位是股，換算成張。"""
    result = {}
    for symbol, payload in payloads.items():
        try:
            chart = payload["chart"]["result"][0]
            stamps = chart.get("timestamp") or []
            quote = chart["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError):
            continue
        days, prev = {}, None
        for ts, close, vol in zip(stamps, quote.get("close") or [], quote.get("volume") or []):
            if close is None:
                continue
            date = datetime.fromtimestamp(ts, TW).strftime("%Y-%m-%d")
            days[date] = {
                "close": round(close, 4),
                "change_pct": (close - prev) / prev * 100 if prev else None,
                "volume_lots": round(vol / 1000) if vol is not None else None,
            }
            prev = close
        result[symbol.split(".")[0]] = days
    return result


def reconcile(yahoo: dict, exchange: dict, date: str) -> tuple[dict, list[dict]]:
    """收盤價以 Yahoo 為主、交易所為查核，不一致時採用交易所並記錄差異。
    成交量一律用交易所的官方總量：實測 Yahoo 的成交量系統性偏低約 1～8%，屬於統計範圍不同，不算錯誤。"""
    quotes, diffs = {}, []
    for code in sorted(set(yahoo) | set(exchange)):
        y = yahoo.get(code, {}).get(date)
        e = exchange.get(code, {})
        e_ok = e.get("close") is not None and e.get("date") in (None, date)
        q = {k: e.get(k) for k in ("name", "market", "units", "date")}
        if y and e_ok:
            price_bad = abs(y["close"] - e["close"]) > 0.005
            if price_bad:
                diffs.append({"code": code, "field": "close", "yahoo": y["close"], "exchange": e["close"]})
            src = e if price_bad else y
            q.update(close=src["close"], change_pct=src["change_pct"] if src["change_pct"] is not None else e["change_pct"],
                     volume_lots=e["volume_lots"] if e["volume_lots"] is not None else y["volume_lots"],
                     check="已修正" if price_bad else "一致")
        elif y:
            q.update(close=y["close"], change_pct=y["change_pct"], volume_lots=y["volume_lots"], check="未查核")
        elif e_ok:
            q.update(close=e["close"], change_pct=e["change_pct"], volume_lots=e["volume_lots"], check="交易所")
        else:
            q.update(close=None, change_pct=None, volume_lots=e.get("volume_lots"), check=None)
        quotes[code] = q
    return quotes, diffs


def parse_fund(rows) -> dict[str, dict]:
    """證交所基金基本資料彙總表（只含上市 ETF）。"""
    result = {}
    for row in rows if isinstance(rows, list) else []:
        code = str(row.get("基金代號", "")).strip()
        if not is_etf(code):
            continue
        index = str(row.get("標的指數/追蹤指數名稱", "")).strip()
        result[code] = {
            "units": num(row.get("發行單位數/轉換數")),
            "index_name": None if index in ("", "不適用") else index,
            "listed_date": roc_date(row.get("上市日期")),
            "fund_type": str(row.get("基金類型", "")).strip() or None,
        }
    return result


def parse_dividends(twse_rows, tpex_rows) -> list[dict]:
    """除權除息預告表：只留 ETF 的除息資料。現金配息尚未公告時為空值。"""
    result = []
    for rows, code_key, date_key, kind_key in (
        (twse_rows, "Code", "Date", "Exdividend"),
        (tpex_rows, "SecuritiesCompanyCode", "ExRrightsExDividendDate", "ExRrightsExDividend"),
    ):
        for row in rows if isinstance(rows, list) else []:
            code = str(row.get(code_key, "")).strip()
            ex_date = roc_date(row.get(date_key))
            if not is_etf(code) or not ex_date or "息" not in str(row.get(kind_key, "")):
                continue
            cash = num(row.get("CashDividend"))
            result.append({"code": code, "ex_date": ex_date, "cash": cash or None})
    return result


# ---------- 分類 ----------

OVERSEAS = re.compile(r"美|S&P|標普|NASDAQ|那斯達克|費城|FANG|道瓊|日本|日經|印度|中國|上証|滬深|恒生|香港|全球|歐洲|越南|韓國|新興")
DIVIDEND = re.compile(r"高息|高股息|股息|收益|息")
THEME = re.compile(r"半導體|科技|5G|電動車|AI|智慧|網通|生技|醫療|電池|綠能|機器人|雲端|元宇宙|IC|晶片|航太|軍工")


def classify(code: str, name: str) -> str:
    if code.endswith("B"):
        return "債券"
    if code.endswith(("L", "R")) or re.search(r"正2|反1|反向|槓桿", name):
        return "槓桿反向"
    if code.endswith("U") or re.search(r"黃金|石油|原油|白銀|期貨|商品", name):
        return "商品"
    if code.endswith("A"):
        return "主動式"
    if OVERSEAS.search(name):
        return "海外股票"
    if DIVIDEND.search(name):
        return "高股息"
    if THEME.search(name):
        return "主題型"
    return "市值型"


ISSUERS = ["富蘭克林華美", "華南永昌", "第一金", "大華銀", "中國信託", "元大", "富邦", "國泰", "群益", "中信", "復華",
           "凱基", "統一", "新光", "永豐", "兆豐", "野村", "台新", "街口", "日盛", "摩根", "貝萊德", "安聯", "聯博",
           "柏瑞", "路博邁", "宏利", "鋒裕匯理", "合庫", "玉山", "富達", "保德信", "施羅德", "瀚亞", "大華", "台灣人壽", "聯邦", "FT"]


def issuer_from_name(name: str) -> str | None:
    """台股 ETF 名稱多半以投信品牌開頭，例如「元大台灣50」；主動式與期貨型前面多了「主動」「期」。"""
    name = re.sub(r"^(主動|期|平衡)", "", name)
    for brand in ISSUERS:
        if name.startswith(brand):
            return {"中國信託": "中信", "FT": "第一金", "大華": "大華銀"}.get(brand, brand)
    return None


def load_meta(path: Path = META_PATH) -> dict[str, dict]:
    """人工維護的補充資料：類型修正、總費用率、規模、發行商。空白欄位不覆蓋自動結果。"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {r["code"].strip(): r for r in csv.DictReader(f) if r.get("code")}


# ---------- 合併 ----------

ISSUER_FIELDS = ("mgmt_fee", "mgmt_fee_text", "custodian_fee_text", "manager", "risk_class",
                 "beneficiaries", "beneficiaries_month", "stats")


def apply_issuer(items: list[dict], issuer: dict[str, dict], date: str) -> None:
    """投信官網的資料優先；沒有接入的投信維持交易所資料，並標示每個欄位的出處。"""
    for it in items:
        it["nav_source"] = "證交所（投信預估淨值）" if it["nav"] is not None else None
        it["aum_source"] = "交易所資料推算（發行單位數×淨值）" if it["aum"] is not None else None
        for f in ISSUER_FIELDS:
            it.setdefault(f, None)
        it["holdings_top"] = []
        it["units_change"] = None
        r = issuer.get(it["code"])
        if not r:
            continue
        src = r["source"]
        for f in ISSUER_FIELDS:
            it[f] = r.get(f)
        if r.get("beneficiaries") is not None:
            it["beneficiaries_source"], it["beneficiaries_source_url"] = src, r.get("source_url")
        for field, key in (("listed_date", "list_date"), ("index_name", "index_name"), ("units", "units")):
            if r.get(key):
                it[field] = r[key]
        if r.get("aum"):
            it["aum"], it["aum_source"] = r["aum"], src
        # 淨值優先順序：投信正式淨值（當天）→ 投信預估淨值（當天）→ 證交所轉載的預估淨值
        nav = None
        if r.get("nav_official") and r.get("nav_date") == date:
            nav, label = r["nav_official"], "正式淨值"
        elif r.get("nav_estimate") and r.get("nav_estimate_date") == date:
            nav, label = r["nav_estimate"], "預估淨值"
        if nav:
            it["nav"], it["nav_source"] = nav, f"{src}（{label}）"
            it["nav_date"] = date
            it["nav_source_url"] = r.get("source_url") if label == "正式淨值" else None
            if it["price"]:
                it["premium"] = round((it["price"] - nav) / nav * 100, 4)
        # 申購買回清單的資料日期可能和最新淨值不同（例如凱基晚一天），有提供就以清單日期為準
        if (r.get("pcf_date") or r.get("nav_date")) == date:
            it["units_change"] = r.get("units_change")
        it["holdings_top"] = sorted((h for h in r.get("holdings") or [] if h.get("weight") is not None),
                                    key=lambda h: -h["weight"])[:10]


def trade_date(nav: dict, quotes: dict, now: datetime) -> str:
    """以收盤行情的日期為準（取最多檔的那天）；沒有行情時用淨值資料的日期。"""
    for source, key in ((quotes, "date"), (nav, "date")):
        dates = [v[key] for v in source.values() if v.get(key)]
        if dates:
            return max(set(dates), key=dates.count)
    return now.strftime("%Y-%m-%d")


def merge(nav: dict, quotes: dict, meta: dict, date: str, fund: dict | None = None) -> list[dict]:
    items = []
    empty = {"name": "", "price": None, "nav": None, "premium": None, "nav_prev": None, "source_date": "", "source_time": ""}
    for code in sorted(set(nav) | set(quotes)):
        n = nav.get(code, empty)
        if n.get("date") and n["date"] != date:
            # 早上開盤前：收盤行情還是昨天，淨值資料可能已換成今天，兩者不能混算
            n = {**empty, "name": n["name"]}
        q = quotes.get(code, {})
        m = meta.get(code, {})
        name = n["name"] or q.get("name", "")
        fd = (fund or {}).get(code, {})
        price = q.get("close") if q.get("close") is not None else n["price"]
        premium = n["premium"]
        if premium is None and price and n["nav"]:
            premium = (price - n["nav"]) / n["nav"] * 100
        units = fd.get("units") or q.get("units")
        value = n["nav"] or n["nav_prev"] or price
        # 規模（億元）＝ 發行單位數 × 淨值；人工資料優先
        aum = num(m.get("aum"))
        if aum is None and units and value:
            aum = round(units * value / 1e8, 2)
        items.append({
            "code": code,
            "name": name,
            "issuer": (m.get("issuer") or "").strip() or issuer_from_name(name),
            "category": (m.get("category") or "").strip() or classify(code, name),
            "market": q.get("market"),
            "price": price,
            "change_pct": q.get("change_pct"),
            "nav": n["nav"],
            "nav_prev": n["nav_prev"],
            "premium": round(premium, 4) if premium is not None else None,
            "volume_lots": q.get("volume_lots"),
            "price_check": q.get("check"),
            "fee": num(m.get("fee")),
            "aum": aum,
            "units": units,
            "index_name": fd.get("index_name"),
            "listed_date": fd.get("listed_date"),
            "source_time": f"{n['source_date']} {n['source_time']}".strip(),
        })
    items.sort(key=lambda x: x["code"])
    return items


def known_dividend_codes(db_path: Path, source: str) -> set[str]:
    """資料庫裡已有某投信配息紀錄的代號；投信模組用來決定要完整回補還是只補最近的。"""
    if not db_path.exists():
        return set()
    con = sqlite3.connect(db_path)
    try:
        return {c for (c,) in con.execute("SELECT DISTINCT code FROM dividend WHERE source = ?", (source,))}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def known_markets(db_path: Path) -> dict[str, str]:
    """資料庫記得的上市 / 上櫃別，交易所資料缺漏時用來決定 Yahoo 代號。"""
    if not db_path.exists():
        return {}
    con = sqlite3.connect(db_path)
    try:
        return {c: m for c, m in con.execute("SELECT code, market FROM etf WHERE market IS NOT NULL")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


# ---------- 儲存 ----------

def save_db(items: list[dict], date: str, db_path: Path = DB_PATH, nav: dict | None = None,
            dividends: list[dict] | None = None, diffs: list[dict] | None = None,
            issuer: dict[str, dict] | None = None) -> None:
    con = store.open_db(db_path, None)
    try:
        now = datetime.now(TW).isoformat(timespec="seconds")
        con.executemany(
            """INSERT INTO etf(code, name, issuer, category, market, fee, aum, units, index_name, listed_date,
                               mgmt_fee, mgmt_fee_text, custodian_fee_text, manager, risk_class,
                               beneficiaries, beneficiaries_month, stats_json, updated_at)
               VALUES(:code, :name, :issuer, :category, :market, :fee, :aum, :units, :index_name, :listed_date,
                      :mgmt_fee, :mgmt_fee_text, :custodian_fee_text, :manager, :risk_class,
                      :beneficiaries, :beneficiaries_month, :stats_json, :now)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                 issuer=COALESCE(excluded.issuer, etf.issuer), category=excluded.category,
                 market=COALESCE(excluded.market, etf.market), fee=COALESCE(excluded.fee, etf.fee),
                 aum=COALESCE(excluded.aum, etf.aum), units=COALESCE(excluded.units, etf.units),
                 index_name=COALESCE(excluded.index_name, etf.index_name),
                 listed_date=COALESCE(excluded.listed_date, etf.listed_date),
                 mgmt_fee=COALESCE(excluded.mgmt_fee, etf.mgmt_fee),
                 mgmt_fee_text=COALESCE(excluded.mgmt_fee_text, etf.mgmt_fee_text),
                 custodian_fee_text=COALESCE(excluded.custodian_fee_text, etf.custodian_fee_text),
                 manager=COALESCE(excluded.manager, etf.manager), risk_class=COALESCE(excluded.risk_class, etf.risk_class),
                 beneficiaries=COALESCE(excluded.beneficiaries, etf.beneficiaries),
                 beneficiaries_month=COALESCE(excluded.beneficiaries_month, etf.beneficiaries_month),
                 stats_json=COALESCE(excluded.stats_json, etf.stats_json), updated_at=excluded.updated_at""",
            [{**it, "now": now, "stats_json": json.dumps(it["stats"], ensure_ascii=False) if it.get("stats") else None}
             for it in items],
        )
        # 同一天重跑會更新當天資料；這次沒抓到的欄位（例如 MIS 暫時連不上時的淨值）保留原值
        con.executemany(
            """INSERT INTO daily(date, code, price, change_pct, nav, nav_prev, premium, volume_lots, source_time, aum,
                                 price_check, nav_source, aum_source, beneficiaries, units_change)
               VALUES(:date, :code, :price, :change_pct, :nav, :nav_prev, :premium, :volume_lots, :source_time, :aum,
                      :price_check, :nav_source, :aum_source, :beneficiaries, :units_change)
               ON CONFLICT(date, code) DO UPDATE SET
                 units_change=COALESCE(excluded.units_change, daily.units_change),
                 nav_source=COALESCE(excluded.nav_source, daily.nav_source),
                 aum_source=COALESCE(excluded.aum_source, daily.aum_source),
                 beneficiaries=COALESCE(excluded.beneficiaries, daily.beneficiaries),
                 price_check=COALESCE(excluded.price_check, daily.price_check),
                 aum=COALESCE(excluded.aum, daily.aum),
                 price=COALESCE(excluded.price, daily.price),
                 change_pct=COALESCE(excluded.change_pct, daily.change_pct),
                 nav=COALESCE(excluded.nav, daily.nav),
                 nav_prev=COALESCE(excluded.nav_prev, daily.nav_prev),
                 premium=COALESCE(excluded.premium, daily.premium),
                 volume_lots=COALESCE(excluded.volume_lots, daily.volume_lots),
                 source_time=COALESCE(NULLIF(excluded.source_time, ''), daily.source_time)""",
            [{**it, "date": date} for it in items],
        )
        save_official_nav(con, nav or {})
        save_issuer_detail(con, issuer or {})
        con.executemany(
            """INSERT OR REPLACE INTO price_check(date, code, field, yahoo, exchange, checked_at)
               VALUES(:date, :code, :field, :yahoo, :exchange, :now)""",
            [{**d, "date": date, "now": now} for d in diffs or []],
        )
        # 交易所除權息預告：只補投信官網還沒有的資料，不覆蓋投信公告
        con.executemany(
            """INSERT INTO dividend(code, ex_date, cash, source, updated_at)
               VALUES(:code, :ex_date, :cash, '交易所除權息預告', :now)
               ON CONFLICT(code, ex_date) DO UPDATE SET
                 cash=CASE WHEN dividend.source LIKE '%官網' THEN dividend.cash ELSE COALESCE(excluded.cash, dividend.cash) END,
                 source=CASE WHEN dividend.source LIKE '%官網' THEN dividend.source ELSE excluded.source END,
                 updated_at=excluded.updated_at""",
            [{**d, "now": now} for d in dividends or []],
        )
        # 投信官網的配息紀錄（含發放日）優先
        con.executemany(
            """INSERT INTO dividend(code, ex_date, cash, pay_date, source, updated_at)
               VALUES(:code, :ex_date, :cash, :pay_date, :source, :now)
               ON CONFLICT(code, ex_date) DO UPDATE SET cash=COALESCE(excluded.cash, dividend.cash),
                 pay_date=COALESCE(excluded.pay_date, dividend.pay_date), source=excluded.source,
                 updated_at=excluded.updated_at""",
            [{**d, "source": r["source"], "now": now} for r in (issuer or {}).values() for d in r.get("dividends") or []],
        )
        con.commit()
        attach_dividends(con, items, date)
        # 讓輸出的 JSON 與資料庫一致
        cols = ["price", "change_pct", "nav", "nav_prev", "premium", "volume_lots", "source_time", "nav_source", "aum_source",
                "units_change"]
        stored = {r[0]: r[1:] for r in con.execute(f"SELECT code, {', '.join(cols)} FROM daily WHERE date=?", (date,))}
        for it in items:
            if it["code"] in stored:
                it.update(zip(cols, stored[it["code"]]))
    finally:
        con.close()


def attach_dividends(con: sqlite3.Connection, items: list[dict], date: str) -> None:
    """每檔附上下一次除息（交易日之後）與最近一次除息（含當天）的日期、金額、發放日。"""
    for it in items:
        cols = ("ex_date", "cash", "pay_date", "source")
        nxt = con.execute("SELECT ex_date, cash, pay_date, source FROM dividend WHERE code=? AND ex_date>? ORDER BY ex_date LIMIT 1",
                          (it["code"], date)).fetchone()
        last = con.execute("SELECT ex_date, cash, pay_date, source FROM dividend WHERE code=? AND ex_date<=? ORDER BY ex_date DESC LIMIT 1",
                           (it["code"], date)).fetchone()
        it["next_ex"] = dict(zip(cols, nxt)) if nxt else None
        it["last_ex"] = dict(zip(cols, last)) if last else None


def save_issuer_detail(con: sqlite3.Connection, issuer: dict[str, dict]) -> None:
    """投信公告的正式淨值與申購買回增減單位數寫回對應日期；成分股依清單日期保存，同一天重抓會覆蓋當天那份。"""
    con.executemany(
        """UPDATE daily SET nav_official = :nav,
             premium_official = CASE WHEN price IS NOT NULL THEN (price - :nav) / :nav * 100 END
           WHERE date = :date AND code = :code""",
        [{"nav": r["nav_official"], "date": r["nav_date"], "code": c}
         for c, r in issuer.items() if r.get("nav_official") and r.get("nav_date")],
    )
    con.executemany(
        "UPDATE daily SET units_change = :chg WHERE date = :date AND code = :code",
        [{"chg": r["units_change"], "date": r.get("pcf_date") or r["nav_date"], "code": c}
         for c, r in issuer.items() if r.get("units_change") is not None and (r.get("pcf_date") or r.get("nav_date"))],
    )
    for code, r in issuer.items():
        if not r.get("holdings"):
            continue
        day = r.get("pcf_date") or r.get("nav_date")
        con.execute("DELETE FROM holding WHERE code = ? AND date IS ?", (code, day))
        con.executemany(
            """INSERT OR REPLACE INTO holding(code, date, asset_code, asset_name, kind, weight, qty, source, source_url)
               VALUES(:code, :date, :asset_code, :asset_name, :kind, :weight, :qty, :source, :source_url)""",
            [{**h, "code": code, "date": day, "source": r["source"], "source_url": r.get("holdings_url") or r.get("source_url")} for h in r["holdings"]],
        )


def save_official_nav(con: sqlite3.Connection, nav: dict) -> None:
    """MIS 的「前一營業日淨值」是投信公告的正式淨值，寫回資料庫中前一個交易日那一列，
    並用當天收盤價算出正式折溢價。早上 08:50 那次排程主要就是為了補這個。"""
    by_date: dict[str, list] = {}
    for n in nav.values():
        if n.get("date") and n.get("nav_prev"):
            by_date.setdefault(n["date"], []).append(n)
    for mis_date, rows in by_date.items():
        prev = con.execute("SELECT MAX(date) FROM daily WHERE date < ?", (mis_date,)).fetchone()[0]
        if not prev:
            continue
        con.executemany(
            # 只補空白：投信官網公告的正式淨值優先，不被證交所轉載的數字覆蓋
            """UPDATE daily SET nav_official = :nav,
                 premium_official = CASE WHEN price IS NOT NULL THEN (price - :nav) / :nav * 100 END
               WHERE date = :date AND code = :code AND nav_official IS NULL""",
            [{"nav": n["nav_prev"], "date": prev, "code": n["code"]} for n in rows],
        )


def attach_returns(con: sqlite3.Connection, items: list[dict], date: str, reused: dict, history: dict) -> None:
    """每檔附上區間報酬。含息報酬要和資料庫的配息紀錄核對，所以在投信資料寫入資料庫之後才算。"""
    known: dict[str, list] = {}
    for code, ex_date, cash in con.execute("SELECT code, ex_date, cash FROM dividend"):
        known.setdefault(code, []).append((ex_date, cash))
    for it in items:
        it["returns"] = reused.get(it["code"]) if reused else returns.compute(
            history.get(it["code"]), date, it["price"], known.get(it["code"], []))


def save_json(items: list[dict], date: str, source: str, out: Path = OUT_PATH, has_nav: bool = True) -> None:
    """網站讀的唯一檔案。區間報酬的起始日與出處放在最上層 returns_info，各檔數字在 items[].returns。"""
    checks: dict[str, int] = {}
    for it in items:
        if it.get("price_check"):
            checks[it["price_check"]] = checks.get(it["price_check"], 0) + 1
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(TW).isoformat(timespec="seconds"),
        "trade_date": date,
        "source": source,
        "has_nav": has_nav,
        "price_check": checks,
        "returns_info": {"source": returns.SOURCE, "starts": returns.start_dates(date)},
        "count": len(items),
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", type=Path, help="改用此資料夾內的 nav.json / twse.json / tpex.json")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--history", type=Path, help="歷史 CSV 資料夾，預設為資料庫旁的 history/")
    args = ap.parse_args(argv)
    history = args.history or args.db.parent / "history"
    store.open_db(args.db, history).close()  # 資料庫檔不存在時，先從歷史 CSV 重建

    raw = load_sources(args.fixtures)
    nav = parse_nav(raw["nav"])
    exchange = {**parse_quotes(raw["tpex"], "上櫃"), **parse_quotes(raw["twse"], "上市")}

    # 市價與成交量：Yahoo 股市為主，交易所為查核。
    # 區間報酬：這個交易日還沒算過時抓五年日線（同一份資料也拿來比對收盤價），算過就沿用上次的結果
    date = trade_date(nav, exchange, datetime.now(TW))
    source = "fixture" if args.fixtures else "live"
    reused = returns.previous(args.out, date, source)
    markets = {**known_markets(args.db), **{c: q["market"] for c, q in exchange.items()}}
    symbols = [f"{c}.TWO" if markets.get(c) == "上櫃" else f"{c}.TW" for c in sorted(set(nav) | set(exchange) | set(markets))]
    history_raw = {}
    if args.fixtures:
        load = lambda name: json.loads((args.fixtures / name).read_text("utf-8")) if (args.fixtures / name).exists() else {}
        yahoo_raw = load("yahoo.json")
        history_raw = {} if reused else load("yahoo_history.json")
    elif reused:
        yahoo_raw = fetch_yahoo(symbols)
    else:
        since = datetime.fromisoformat(date).replace(tzinfo=TW) - timedelta(days=returns.HISTORY_DAYS)
        yahoo_raw = history_raw = fetch_yahoo(symbols, since)
    yahoo = parse_yahoo(yahoo_raw)

    if not nav and not exchange and not yahoo:
        print("錯誤：所有來源都沒有資料，可能是網路被擋或來源格式改變。", file=sys.stderr)
        return 1
    if not nav:
        print("警告：沒有淨值資料，本次只更新價格與成交量。", file=sys.stderr)
    quotes, diffs = reconcile(yahoo, exchange, date)
    items = merge(nav, quotes, load_meta(), date, parse_fund(raw["fund"]))
    dividends = parse_dividends(raw["div_twse"], raw["div_tpex"])

    # 各投信是不同網站，同時抓；同一個網站內仍逐筆、放慢速度
    def fetch_issuer(item):
        name, mod = item
        return mod, mod.fetch(http_get_json, fixtures=args.fixtures / name if args.fixtures else None, context=SSL_CONTEXT,
                              dividend_codes=known_dividend_codes(args.db, mod.SOURCE))

    issuer: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(ISSUER_MODULES)) as pool:
        for mod, records in pool.map(fetch_issuer, ISSUER_MODULES.items()):
            print(f"{mod.SOURCE}：{sum(1 for r in records.values() if not r.get('delisted'))} 檔")
            issuer.update(records)
    apply_issuer(items, issuer, date)
    monthly = sitca.fetch(args.fixtures / "sitca" if args.fixtures else None, context=SSL_CONTEXT)

    save_db(items, date, args.db, nav, dividends, diffs, issuer)
    con = sqlite3.connect(args.db)
    try:
        attach_returns(con, items, date, reused, returns.parse_history(history_raw))
        fund_flows.save(con, issuer, monthly)
        fund_flows.attach(con, items, date)
        con.commit()
        store.dump(con, history)
    finally:
        con.close()
    save_json(items, date, source, args.out, has_nav=any(it["nav"] is not None for it in items))
    flagged = sum(1 for it in items if it["premium"] is not None and abs(it["premium"]) >= 1)
    checks = {k: sum(1 for it in items if it["price_check"] == k) for k in ("一致", "已修正", "未查核", "交易所")}
    print(f"{date}：共 {len(items)} 檔 ETF，折溢價超過 ±1% 有 {flagged} 檔，除息預告 {len(dividends)} 筆 → {args.out}")
    print("市價查核：" + "、".join(f"{k} {v} 檔" for k, v in checks.items()))
    print(f"區間報酬：{sum(1 for it in items if it.get('returns'))} 檔" + ("（沿用本交易日先前的計算）" if reused else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
