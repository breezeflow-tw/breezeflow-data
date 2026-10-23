#!/usr/bin/env python3
"""抓取台股全市場 ETF 的官方收盤行情、淨值、折溢價與成交量，寫入 SQLite 並輸出網頁用的 JSON。

收盤價、漲跌幅、成交量只用證交所、櫃買中心 OpenAPI 的收盤行情（政府資料開放平臺資料集 11549、11370），
先逐筆保存到 exchange_daily（記錄端點與取得時間），網站交易日那天沒有官方收盤價就留空、寫明原因。
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
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path

import active_changes
import active_holdings
import returns
import sitca
import store
import fund_flows
import market_calendar
import market_status
import history_corrections
import official_quotes
import product_profile
import nav_provenance
import sitca_etf
from issuers import allianz, blackrock, capital, cathay, ctbc, fubon, fuhhwa, jpmorgan, kgi, nomura, uni, yuanta
from issuers.common import flat_fee

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
    # 有價證券集中交易市場開（休）市日期：區分休市與缺資料
    "calendar": market_calendar.URL,
}

# 擁有者指示不收錄的代號：網站不顯示、資料庫不保存，資料來源出現時一律略過
EXCLUDED = {"00847B"}  # 中信美國市政債：2026-10 起交易所沒有行情、中信官網也不再列出
# 收盤價的出處標示，以及當天還沒有官方收盤價時的說明（證交所的開放資料隔天清晨才更新）
PRICE_SOURCES = {"上市": "證交所收盤行情", "上櫃": "櫃買中心收盤行情"}
PRICE_PENDING = {"上市": "證交所的官方收盤行情還沒公布（通常隔天清晨更新）", "上櫃": "櫃買中心的官方收盤行情還沒公布"}

# 已接入的投信官網；淨值、規模、費用、成分股以這裡的資料為主
ISSUER_MODULES = {"yuanta": yuanta, "uni": uni, "capital": capital, "fuhhwa": fuhhwa, "kgi": kgi, "fubon": fubon,
                  "cathay": cathay, "ctbc": ctbc, "nomura": nomura, "jpmorgan": jpmorgan, "blackrock": blackrock,
                  "allianz": allianz}


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
            # 沒有成交時市價是 0、尚未計算時淨值是 0，都不是真的數字
            price, nav = num(row.get("e")) or None, num(row.get("f")) or None
            premium = num(row.get("g")) if price and nav else None
            if premium is None and price and nav:
                premium = (price - nav) / nav * 100
            result[code] = {
                "code": code,
                "name": clean_name(row.get("b", "")),
                "price": price,
                "nav": nav,
                "premium": premium,
                "nav_prev": num(row.get("h")) or None,
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


def official_prices(con: sqlite3.Connection, exchange: dict, date: str) -> dict[str, dict]:
    """網站交易日那天的官方收盤行情（exchange_daily 逐筆保存的交易所原值）：收盤價、漲跌幅、成交量（張）與出處。
    名稱、市場、發行單位數沿用這次抓到的交易所資料（日期不同也可以用）。沒有官方收盤價時 close 為 None，note 寫明原因：
    交易所還沒公布那天的資料（證交所隔天清晨才更新）、當天沒有成交，或交易所資料沒有列出這檔。"""
    rows, captured = {}, set()
    # 除息、除權日交易所的漲跌價差不是相對參考價的漲跌（證交所記 0、櫃買中心空白）：用前一個官方收盤價與配息紀錄辨認
    prev_close = dict(con.execute(
        "SELECT e.code, e.close FROM exchange_daily e WHERE e.close > 0 AND e.date = "
        "(SELECT MAX(x.date) FROM exchange_daily x WHERE x.code = e.code AND x.date < ? AND x.close > 0)", (date,)))
    paid = {c for (c,) in con.execute("SELECT code FROM dividend WHERE ex_date = ?", (date,))}
    for code, market, name, close, change, volume in con.execute(
            "SELECT code, market, name, close, change, volume_shares FROM exchange_daily WHERE date = ?", (date,)):
        rows[code] = {"market": market, "name": name, "close": close if close is not None and close > 0 else None,
                      "change": change, "volume": volume}
        captured.add(market)
    quotes = {}
    for code in sorted(set(exchange) | set(rows)):
        e, o = exchange.get(code, {}), rows.get(code)
        market = (o or {}).get("market") or e.get("market")
        q = {"name": e.get("name") or (o or {}).get("name"), "market": market, "units": e.get("units"), "date": date,
             "close": None, "change_pct": None, "volume_lots": None, "check": None, "source": None, "note": None,
             "change_note": None}
        if o:
            q["volume_lots"] = round(o["volume"] / 1000) if o["volume"] is not None else None
        if o and o["close"] is not None:
            change, before = o["change"], prev_close.get(code)
            if change is None or (change == 0 and (code in paid or (before and abs(o["close"] - before) > 1e-9))):
                change = None
                q["change_note"] = "除息、除權日交易所沒有提供相對參考價的漲跌，不用前一天收盤價自行推算"
            base = o["close"] - change if change is not None else None
            q.update(close=o["close"], change_pct=change / base * 100 if base else None, check="交易所",
                     source=PRICE_SOURCES.get(market))
        elif o:
            q["note"] = "當天沒有成交"
        elif market in PRICE_PENDING and market not in captured:
            q["note"] = PRICE_PENDING[market]
        else:
            q["note"] = "交易所收盤行情沒有列出這檔"
        quotes[code] = q
    return quotes


def parse_closes(twse_rows, tpex_rows) -> dict[str, tuple[str, float]]:
    """交易所收盤行情的全部證券（含個股）：{代號: (日期, 收盤價)}，用來算主動式 ETF 持股異動的金額。"""
    closes = {}
    for rows, code_key, close_key in ((twse_rows, "Code", "ClosingPrice"), (tpex_rows, "SecuritiesCompanyCode", "Close")):
        for row in rows if isinstance(rows, list) else []:
            code, close, day = str(row.get(code_key) or "").strip(), num(row.get(close_key)), roc_date(row.get("Date"))
            if code and close and day:
                closes[code] = (day, close)
    return closes


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
    verified = product_profile.overrides().get(code, {}).get("category")
    if verified:
        return verified
    if code.endswith(("A", "D")):
        return "主動式"
    if code.endswith(("B", "C")):
        return "債券"
    if code.endswith("T"):
        return "多重資產"
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
    return "待分類"


ISSUERS = ["富蘭克林華美", "華南永昌", "第一金", "大華銀", "中國信託", "元大", "富邦", "國泰", "群益", "中信", "復華",
           "凱基", "統一", "新光", "永豐", "兆豐", "野村", "台新", "街口", "日盛", "摩根", "貝萊德", "安聯", "聯博",
           "柏瑞", "路博邁", "宏利", "鋒裕匯理", "合庫", "玉山", "富達", "保德信", "施羅德", "瀚亞", "大華", "台灣人壽", "聯邦", "FT"]


def issuer_from_name(name: str) -> str | None:
    """台股 ETF 名稱多半以投信品牌開頭，例如「元大台灣50」；主動式與期貨型前面多了「主動」「期」。"""
    name = re.sub(r"^(主動|期|平衡)", "", name)
    for brand in ISSUERS:
        if name.startswith(brand):
            return {"中國信託": "中信", "FT": "富蘭克林華美", "大華": "大華銀"}.get(brand, brand)
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
        it["nav_date"] = nav_provenance.timestamp_day(it.get("source_time")) if it["nav"] is not None else None
        it["nav_source_url"] = SOURCES["nav"] if it["nav_date"] else None
        it["aum_source"] = "交易所資料推算（發行單位數×淨值）" if it["aum"] is not None else None
        for f in ISSUER_FIELDS:
            it.setdefault(f, None)
        it["holdings_top"] = []
        it["holdings_source"] = it["holdings_date"] = None
        it["units_change"] = None
        it["fee_source"] = None
        it["beneficiaries_source"] = None
        r = issuer.get(it["code"])
        if not r:
            continue
        src = r["source"]
        for f in ISSUER_FIELDS:
            it[f] = r.get(f)
        if it["mgmt_fee_text"] or it["custodian_fee_text"] or it["mgmt_fee"] is not None:
            it["fee_source"] = src
        if it["beneficiaries"] is not None:
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
        if it["holdings_top"]:
            it["holdings_source"] = src
            it["holdings_date"] = r.get("holdings_date") or r.get("pcf_date") or r.get("nav_date")


def without_excluded(data: dict) -> dict:
    return {code: v for code, v in data.items() if code not in EXCLUDED}


def purge_excluded(con: sqlite3.Connection) -> None:
    """不收錄的代號從資料庫各表移除（公會等來源仍會列出，在存檔前統一清掉）。"""
    marks = ", ".join("?" * len(EXCLUDED))
    for table in store.TABLES:
        if "code" in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"DELETE FROM {table} WHERE code IN ({marks})", sorted(EXCLUDED))
    con.commit()


def trade_date(nav: dict, quotes: dict, now: datetime, calendar: market_calendar.Calendar | None = None) -> str:
    """取交易所收盤行情及 MIS 市價中最新的已收盤日期。
    交易所與 MIS 各採最多檔的日期，避免單一異常值推進整站日期。
    MIS 時間戳須通過官方開休市日曆，交易所資料的日期也不能是官方日曆的休市日（例如 2026-10-23 颱風臨時休市）；
    所有來源排除未來及當日 14:00 前的資料。"""
    local = now.astimezone(TW)
    today = local.strftime("%Y-%m-%d")

    def completed(day):
        return bool(nav_provenance.valid_day(day) and (day < today or (day == today and local.hour >= 14)))

    def majority(days):
        return max(set(days), key=lambda day: (days.count(day), day))

    candidates = []
    dates = [v["date"] for v in quotes.values() if completed(v.get("date"))
             and (calendar is None or calendar.is_open(v["date"]) is not False)]
    if dates:
        candidates.append(majority(dates))
    dates = [v["date"] for v in nav.values() if completed(v.get("date")) and v.get("price") is not None
             and calendar is not None and calendar.is_open(v["date"]) is True]
    if dates:
        candidates.append(majority(dates))
    if candidates:
        return max(candidates)
    raise ValueError("無法確認已收盤的交易日期，停止更新。")


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
        # 收盤價只用交易所的官方收盤行情；MIS 的市價不補，折溢價也只用官方收盤價計算
        price = q.get("close")
        premium = (price - n["nav"]) / n["nav"] * 100 if price and n["nav"] else None
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
            "price_source": q.get("source"),
            "price_date": date if price is not None else None,
            "price_note": None if price is not None else q.get("note") or "交易所收盤行情沒有列出這檔",
            "change_note": q.get("change_note") if price is not None else None,
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


# ---------- 儲存 ----------

def save_db(items: list[dict], date: str, db_path: Path = DB_PATH, nav: dict | None = None,
            dividends: list[dict] | None = None, issuer: dict[str, dict] | None = None, fund_dividends: list[dict] | None = None,
            fund_nav: dict[str, float] | None = None) -> None:
    con = store.open_db(db_path, None)
    try:
        now = datetime.now(TW).isoformat(timespec="seconds")
        con.executemany(
            """INSERT INTO etf(code, name, issuer, category, market, fee, aum, units, index_name, listed_date,
                               mgmt_fee, mgmt_fee_text, custodian_fee_text, manager, risk_class,
                               beneficiaries, beneficiaries_month, stats_json, fee_source, expense_json, updated_at)
               VALUES(:code, :name, :issuer, :category, :market, :fee, :aum, :units, :index_name, :listed_date,
                      :mgmt_fee, :mgmt_fee_text, :custodian_fee_text, :manager, :risk_class,
                      :beneficiaries, :beneficiaries_month, :stats_json, :fee_source, :expense_json, :now)
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
                 stats_json=COALESCE(excluded.stats_json, etf.stats_json),
                 fee_source=COALESCE(excluded.fee_source, etf.fee_source),
                 expense_json=COALESCE(excluded.expense_json, etf.expense_json), updated_at=excluded.updated_at""",
            [{**it, "now": now, "stats_json": json.dumps(it["stats"], ensure_ascii=False) if it.get("stats") else None,
              "fee_source": it.get("fee_source"),
              "expense_json": json.dumps(it["expense"], ensure_ascii=False) if it.get("expense") else None}
             for it in items],
        )
        # 同一天重跑會更新當天資料；這次沒抓到的欄位（例如 MIS 暫時連不上時的淨值）保留原值
        con.executemany(
            """INSERT INTO daily(date, code, price, change_pct, nav, nav_prev, premium, volume_lots, source_time, aum,
                                 price_check, nav_source, aum_source, beneficiaries, units_change, nav_date, nav_source_url)
               VALUES(:date, :code, :price, :change_pct, :nav, :nav_prev, :premium, :volume_lots, :source_time, :aum,
                      :price_check, :nav_source, :aum_source, :beneficiaries, :units_change, :nav_date, :nav_source_url)
               ON CONFLICT(date, code) DO UPDATE SET
                 units_change=COALESCE(excluded.units_change, daily.units_change),
                 nav_source=CASE WHEN excluded.nav IS NOT NULL THEN excluded.nav_source ELSE daily.nav_source END,
                 nav_date=CASE WHEN excluded.nav IS NOT NULL THEN excluded.nav_date ELSE daily.nav_date END,
                 nav_source_url=CASE WHEN excluded.nav IS NOT NULL THEN excluded.nav_source_url ELSE daily.nav_source_url END,
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
                 source_time=CASE WHEN excluded.nav IS NOT NULL THEN excluded.source_time ELSE daily.source_time END""",
            [{"nav_date":None, "nav_source_url":None, **it, "date": date} for it in items],
        )
        save_official_nav(con, nav or {})
        save_issuer_detail(con, issuer or {})
        # 投信投顧公會的當日正式淨值：只補空白，投信官網公告的優先
        con.executemany(
            """UPDATE daily SET nav_official = :nav,
                 premium_official = CASE WHEN price IS NOT NULL THEN (price - :nav) / :nav * 100 END
               WHERE date = :date AND code = :code AND nav_official IS NULL""",
            [{"nav": v, "date": date, "code": c} for c, v in (fund_nav or {}).items()],
        )
        # 交易所除權息預告：只補投信官網、投信投顧公會還沒有的資料，不覆蓋投信公告
        con.executemany(
            """INSERT INTO dividend(code, ex_date, cash, source, updated_at)
               VALUES(:code, :ex_date, :cash, '交易所除權息預告', :now)
               ON CONFLICT(code, ex_date) DO UPDATE SET
                 cash=CASE WHEN dividend.source LIKE '%官網' OR dividend.source = :fund THEN dividend.cash
                           ELSE COALESCE(excluded.cash, dividend.cash) END,
                 source=CASE WHEN dividend.source LIKE '%官網' OR dividend.source = :fund THEN dividend.source ELSE excluded.source END,
                 updated_at=excluded.updated_at""",
            [{**d, "now": now, "fund": sitca.SOURCE} for d in dividends or []],
        )
        # 投信投顧公會的配息資料（含發放日）：取代交易所預告，不覆蓋投信官網
        con.executemany(
            """INSERT INTO dividend(code, ex_date, cash, pay_date, source, updated_at)
               VALUES(:code, :ex_date, :cash, :pay_date, :source, :now)
               ON CONFLICT(code, ex_date) DO UPDATE SET
                 cash=CASE WHEN dividend.source LIKE '%官網' THEN dividend.cash ELSE COALESCE(excluded.cash, dividend.cash) END,
                 pay_date=COALESCE(dividend.pay_date, excluded.pay_date),
                 source=CASE WHEN dividend.source LIKE '%官網' THEN dividend.source ELSE excluded.source END,
                 updated_at=excluded.updated_at""",
            [{**d, "source": sitca.SOURCE, "now": now} for d in fund_dividends or []],
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
                "units_change", "nav_date", "nav_source_url"]
        stored = {r[0]: r[1:] for r in con.execute(f"SELECT code, {', '.join(cols)} FROM daily WHERE date=?", (date,))}
        for it in items:
            if it["code"] in stored:
                it.update(zip(cols, stored[it["code"]]))
        nav_provenance.restore(con, items, date)
        con.commit()
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
    now = datetime.now(TW).isoformat(timespec="seconds")
    for code, r in issuer.items():
        if not r.get("holdings"):
            continue
        # 持股有自己的資料日期時（例如海外 ETF 的持股比申購買回單位差異早一天）以持股日期為準
        day = r.get("holdings_date") or r.get("pcf_date") or r.get("nav_date")
        url = r.get("holdings_url") or r.get("source_url")
        con.execute("DELETE FROM holding WHERE code = ? AND date IS ?", (code, day))
        con.executemany(
            """INSERT OR REPLACE INTO holding(code, date, asset_code, asset_name, kind, weight, qty, source, source_url)
               VALUES(:code, :date, :asset_code, :asset_name, :kind, :weight, :qty, :source, :source_url)""",
            [{**h, "code": code, "date": day, "source": r["source"], "source_url": url} for h in r["holdings"]],
        )
        # 完整清單的出處與完整度證據（列數、權重合計、取得時間）
        active_holdings.save_list_evidence(con, code, day, r["source"], url, r["holdings"], now)


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


FUND_INFO_COLS = ("code", "fund_id", "name", "data_month", "inception", "currency", "manager", "risk_class",
                  "mgmt_fee_text", "custodian_fee_text", "expense_year", "exp_mgmt", "exp_custodian", "exp_other",
                  "exp_total", "fetched", "class_code", "investment_region")


def load_fund_info(db_path: Path) -> tuple[dict, str | None]:
    """資料庫裡上一次抓的投信投顧公會資料（格式同 sitca.fetch 的回傳值）與抓取日期。"""
    if not db_path.exists():
        return {}, None
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(f"SELECT {', '.join(FUND_INFO_COLS)} FROM fund_info").fetchall()
    except sqlite3.OperationalError:
        return {}, None
    finally:
        con.close()
    funds, fetched = {}, None
    for row in rows:
        r = dict(zip(FUND_INFO_COLS, row))
        fetched = max(fetched or "", r["fetched"] or "") or None
        expense = ({"year": r["expense_year"], "mgmt": r["exp_mgmt"], "custodian": r["exp_custodian"],
                    "other": r["exp_other"], "total": r["exp_total"]} if r["exp_total"] is not None else None)
        funds[r["code"]] = {k: r[k] for k in ("fund_id", "name", "inception", "currency", "manager", "risk_class",
                                               "mgmt_fee_text", "custodian_fee_text", "class_code", "investment_region", "data_month", "fetched")} | {"expense": expense}
    if not funds:
        return {}, None
    months = [r[3] for r in rows if r[3]]
    years = [r[10] for r in rows if r[10]]
    return {"month": max(months) if months else None, "year": max(years) if years else None, "funds": funds}, fetched


def save_fund_info(con: sqlite3.Connection, info: dict, fetched: str) -> None:
    """重新抓到的公會資料整批取代舊資料。"""
    con.execute("DELETE FROM fund_info")
    con.executemany(
        f"INSERT INTO fund_info({', '.join(FUND_INFO_COLS)}) VALUES({', '.join('?' * len(FUND_INFO_COLS))})",
        [(code, f["fund_id"], f["name"], info.get("month"), f["inception"], f["currency"], f["manager"], f["risk_class"],
          f["mgmt_fee_text"], f["custodian_fee_text"], (f["expense"] or {}).get("year"), (f["expense"] or {}).get("mgmt"),
          (f["expense"] or {}).get("custodian"), (f["expense"] or {}).get("other"), (f["expense"] or {}).get("total"), fetched,
          f.get("class_code"), f.get("investment_region"))
         for code, f in sorted(info["funds"].items())],
    )


# 外幣交易線（代號結尾 K）和主要交易線是同一檔基金；多半是去掉 K 的代號，例外列在這裡
MAIN_LINES = {"00625K": "006205", "00687C": "00687B"}  # 已核對的同基金交易線


def main_line(code: str) -> str | None:
    """外幣交易線對應的主要交易線代號；不是外幣交易線時回傳 None。"""
    return MAIN_LINES.get(code) or (code[:-1] if code.endswith("K") else None)


def apply_fund_info(items: list[dict], info: dict) -> None:
    """投信投顧公會的資料補在投信官網沒有提供的欄位，並附上年度實際費用比率。
    外幣交易線（例如 00636K）和主要交易線是同一檔基金，公會只列主要交易線，沿用其資料。"""
    funds = info.get("funds") or {}
    for it in items:
        code = it["code"]
        f = funds.get(code) or funds.get(main_line(code) or "")
        it["expense"] = None
        if not f:
            continue
        if not it.get("mgmt_fee_text") and not it.get("custodian_fee_text") and it.get("mgmt_fee") is None:
            if f["mgmt_fee_text"] or f["custodian_fee_text"]:
                it["mgmt_fee_text"], it["custodian_fee_text"] = f["mgmt_fee_text"], f["custodian_fee_text"]
                it["mgmt_fee"] = flat_fee(f["mgmt_fee_text"])
                it["fee_source"] = f"{sitca.SOURCE}基金基本資料（{info.get('month') or '最新'}）"
        it["manager"] = it.get("manager") or f["manager"]
        it["risk_class"] = it.get("risk_class") or f["risk_class"]
        if f["expense"]:
            it["expense"] = {**f["expense"], "source": f"{sitca.SOURCE}各項費用比率（{f['expense']['year']} 年度）"}
            if it.get("fee") is None:
                it["fee"] = f["expense"]["total"]


def apply_main_line_holdings(items: list[dict]) -> None:
    """外幣交易線沒有自己的成分股時，沿用主要交易線的（同一檔基金、同一份投資組合）。
    規模與受益人數是整檔基金合計，不沿用，避免重複計算。"""
    by_code = {it["code"]: it for it in items}
    for it in items:
        main = by_code.get(main_line(it["code"]) or "")
        if it.get("holdings_top") or not main or not main.get("holdings_top"):
            continue
        it["holdings_top"] = main["holdings_top"]
        it["holdings_date"] = main.get("holdings_date")
        it["holdings_source"] = f"{main.get('holdings_source')}（與 {main['code']} 同一檔基金）"
        if "holdings_source_url" in main:
            it["holdings_source_url"] = main["holdings_source_url"]


def apply_fund_nav(items: list[dict], navs: dict[str, float], as_of: str | None = None) -> None:
    """投信投顧公會的當日正式淨值：還沒有投信官網正式淨值的 ETF，用它取代預估淨值，折溢價跟著重算。"""
    for it in items:
        nav = navs.get(it["code"])
        if not nav or "正式淨值" in (it.get("nav_source") or ""):
            continue
        it["nav"], it["nav_source"] = nav, f"{sitca.SOURCE}（正式淨值）"
        it["nav_date"], it["nav_source_url"] = as_of, sitca.BASE + sitca.NAV
        it["premium"] = round((it["price"] - nav) / nav * 100, 4) if it.get("price") else None


FUND_MONTH_COLS = ("code", "month", "aum_twd", "nav", "units", "beneficiaries", "beneficiaries_natural",
                   "beneficiaries_legal", "subscribe_twd", "redeem_twd")


def save_fund_month(con: sqlite3.Connection, summary: dict) -> None:
    """公會基金資料彙總表依月份累積；同一個月重抓時以新資料為準。"""
    if not summary.get("month"):
        return
    con.executemany(
        f"INSERT OR REPLACE INTO fund_month({', '.join(FUND_MONTH_COLS)}) VALUES({', '.join('?' * len(FUND_MONTH_COLS))})",
        [(code, summary["month"], *(f[c] for c in FUND_MONTH_COLS[2:])) for code, f in sorted(summary["funds"].items())],
    )


def load_fund_month(con: sqlite3.Connection) -> dict[str, dict]:
    """每檔 ETF 最新一個月的公會彙總表資料。"""
    rows = con.execute(f"SELECT {', '.join(FUND_MONTH_COLS)} FROM fund_month f "
                       "WHERE month = (SELECT MAX(month) FROM fund_month g WHERE g.code = f.code)").fetchall()
    return {r[0]: dict(zip(FUND_MONTH_COLS, r)) for r in rows}


def apply_fund_month(items: list[dict], monthly: dict[str, dict]) -> None:
    """附上投信投顧公會基金資料彙總表（月）：月底規模、受益人數（自然人／法人）與當月申購、買回金額（億元）。
    受益人數以資料月份較新的為準，月份相同時用投信官網的。外幣交易線（代號結尾 K）和主要交易線是同一檔基金，
    公會只列一筆，不重複套用。"""
    def yi(value):
        return round(value / 1e8, 2) if value is not None else None

    for it in items:
        m = monthly.get(it["code"])
        it["monthly"] = None
        if not m:
            continue
        net = m["subscribe_twd"] - m["redeem_twd"] if None not in (m["subscribe_twd"], m["redeem_twd"]) else None
        it["monthly"] = {"month": m["month"], "aum": yi(m["aum_twd"]), "nav": m["nav"], "units": m["units"],
                         "beneficiaries": m["beneficiaries"], "beneficiaries_natural": m["beneficiaries_natural"],
                         "beneficiaries_legal": m["beneficiaries_legal"], "subscribe": yi(m["subscribe_twd"]),
                         "redeem": yi(m["redeem_twd"]), "net": yi(net), "source": f"{sitca.SOURCE}基金資料彙總表"}
        if m["beneficiaries"] and (it.get("beneficiaries") is None or (it.get("beneficiaries_month") or "") < m["month"]):
            it["beneficiaries"], it["beneficiaries_month"] = m["beneficiaries"], m["month"]
            it["beneficiaries_source"] = f"{sitca.SOURCE}基金資料彙總表"
            it["beneficiaries_source_url"] = sitca.BASE + sitca.SUMMARY


def months_between(first: str, last: str) -> list[str]:
    """YYYY-MM 到 YYYY-MM（含）的每個月。"""
    y, m = map(int, first.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def backfill_plan(db_path: Path) -> dict[str, list[str]]:
    """要回補的月份：從 sitca.BACKFILL_FROM 到資料庫最新一個月之間還沒抓過的，由近到遠，每種最多 BACKFILL_MONTHS 個。"""
    con = sqlite3.connect(db_path)
    try:
        latest = con.execute("SELECT MAX(month) FROM fund_month").fetchone()[0]
        done = set(con.execute("SELECT report, month FROM fund_fetch").fetchall())
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    if not latest:
        return {}
    return {report: [m for m in reversed(months_between(first, latest)) if (report, m) not in done][:sitca.BACKFILL_MONTHS]
            for report, first in sitca.BACKFILL_FROM.items()}


def save_fund_top(con: sqlite3.Connection, top10: dict) -> None:
    """公會月前十大依月份累積；同一個月重抓時整批取代。"""
    if not top10.get("month"):
        return
    con.execute("DELETE FROM fund_top WHERE month = ?", (top10["month"],))
    con.executemany(
        "INSERT OR REPLACE INTO fund_top(code, month, rank, asset_code, asset_name, kind, weight) VALUES(?, ?, ?, ?, ?, ?, ?)",
        [(code, top10["month"], h["rank"], h["asset_code"], h["asset_name"], h["kind"], h["weight"])
         for code, rows in sorted(top10["funds"].items()) for h in rows],
    )


def load_fund_top(con: sqlite3.Connection) -> dict[str, tuple[str, list[dict]]]:
    """每檔 ETF 最新一個月的公會月前十大：{代號: (年月, 持股)}。"""
    out: dict[str, tuple[str, list[dict]]] = {}
    for code, month, asset_code, asset_name, kind, weight in con.execute(
            "SELECT code, month, asset_code, asset_name, kind, weight FROM fund_top f "
            "WHERE month = (SELECT MAX(month) FROM fund_top g WHERE g.code = f.code) ORDER BY code, rank"):
        out.setdefault(code, (month, []))[1].append(
            {"asset_code": asset_code, "asset_name": asset_name, "kind": kind, "weight": weight, "qty": None})
    return out


def apply_fund_top(items: list[dict], top: dict[str, tuple[str, list[dict]]], as_of: str | None = None) -> None:
    """投信官網沒有成分股的 ETF，用投信投顧公會的月底前十大（只有權重，沒有股數）。"""
    for it in items:
        if it.get("holdings_top") or it["code"] not in top:
            continue
        month, rows = top[it["code"]]
        if as_of and month >= as_of[:7]:
            continue  # 當月未結束，不能將尚未公布的月末持股套用至截至日
        it["holdings_top"] = sorted(rows, key=lambda h: -(h.get("weight") or 0))[:10]
        it["holdings_source_url"] = sitca.BASE + sitca.TOP10
        it["holdings_source"] = f"{sitca.SOURCE}基金投資明細（月前十大）"
        it["holdings_date"] = month


def save_json(items: list[dict], date: str, source: str, out: Path = OUT_PATH, has_nav: bool = True,
              active: dict | None = None, attributions: list[dict] | None = None, holdings_flow: dict | None = None,
              calendar: market_calendar.Calendar | None = None, returns_info: dict | None = None) -> None:
    """網站讀的唯一檔案。區間報酬的出處、起始日與官方歷史起點放在最上層 returns_info，各檔數字在 items[].returns。"""
    checks: dict[str, int] = {}
    for it in items:
        check = it.get("price_check") or ("狀態未提供" if it.get("price") is not None else "無市價")
        checks[check] = checks.get(check, 0) + 1
    out.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(TW)
    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "trade_date": date,
        "source": source,
        "has_nav": has_nav,
        "price_check": checks,
        "returns_info": returns_info or {"source": returns.SOURCE, "starts": returns.start_dates(date), "history_from": {}},
        "active_changes": active,
        "attributions": attributions or [],
        "active_holdings": holdings_flow,
        "count": len(items),
        "items": items,
    }
    payload["market_status"] = market_status.build(items, date, generated_at, calendar or market_calendar.Calendar({}, set()))
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", type=Path, help="改用此資料夾內的 nav.json / twse.json / tpex.json")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--history", type=Path, help="歷史 CSV 資料夾，預設為資料庫旁的 history/")
    args = ap.parse_args(argv)
    history = args.history or args.db.parent / "history"
    if not args.fixtures:
        history_corrections.apply(history, args.db)
    store.open_db(args.db, history).close()  # 資料庫檔不存在時，先從歷史 CSV 重建

    raw = load_sources(args.fixtures)
    nav = without_excluded(parse_nav(raw["nav"]))
    exchange = without_excluded({**parse_quotes(raw["tpex"], "上櫃"), **parse_quotes(raw["twse"], "上市")})

    # 交易所收盤行情先逐筆保存（端點、資料日期、取得時間），收盤價一律從保存的官方原值取
    official = [q for q in official_quotes.parse(raw["twse"], raw["tpex"]) if q["code"] not in EXCLUDED]
    now = datetime.now(TW).isoformat(timespec="seconds")
    # 判定 MIS 日期與回補歷史清單都使用同一份官方開休市日曆。
    con = sqlite3.connect(args.db)
    try:
        stored = market_calendar.load(con)
        attempted = fund_flows.backfill_done(con)
        official_quotes.save(con, official, now)
        con.commit()
    finally:
        con.close()
    fresh_days = market_calendar.parse(raw["calendar"])
    calendar = market_calendar.Calendar({**stored.entries, **fresh_days}, stored.years | {int(d[:4]) for d in fresh_days})
    backfill = {"calendar": calendar, "attempted": attempted}

    # 交易所收盤行情與 MIS 市價的日期一起比較，避免單一來源延遲使日期倒退；
    # 收盤價、漲跌幅、成交量只用那天的官方收盤行情，還沒公布就留空並寫明原因
    date = trade_date(nav, exchange, datetime.now(TW), calendar)
    source = "fixture" if args.fixtures else "live"
    if not nav and not exchange:
        print("錯誤：所有來源都沒有資料，可能是網路被擋或來源格式改變。", file=sys.stderr)
        return 1
    if not nav:
        print("警告：沒有淨值資料，本次只更新價格與成交量。", file=sys.stderr)
    con = sqlite3.connect(args.db)
    try:
        quotes = official_prices(con, exchange, date)
    finally:
        con.close()
    items = merge(nav, quotes, load_meta(), date, parse_fund(raw["fund"]))
    dividends = [d for d in parse_dividends(raw["div_twse"], raw["div_tpex"]) if d["code"] not in EXCLUDED]

    # 各投信是不同網站，同時抓；同一個網站內仍逐筆、放慢速度
    def fetch_issuer(item):
        name, mod = item
        return mod, mod.fetch(http_get_json, fixtures=args.fixtures / name if args.fixtures else None, context=SSL_CONTEXT,
                              dividend_codes=known_dividend_codes(args.db, mod.SOURCE), history=backfill)

    # 投信投顧公會的資料每月才更新：資料庫裡的資料超過一週才重抓，和各投信同時進行
    fund_info, fetched = load_fund_info(args.db)
    today = datetime.now(TW).date()
    refresh = not fetched or date_type.fromisoformat(fetched) <= today - timedelta(days=sitca.REFRESH_DAYS)

    issuer: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(ISSUER_MODULES) + 1) as pool:
        fresh_info = pool.submit(sitca.fetch, args.fixtures / "sitca" if args.fixtures else None, SSL_CONTEXT, today) if refresh else None
        fund_nav = pool.submit(sitca.fetch_nav, date, args.fixtures / "sitca" if args.fixtures else None, SSL_CONTEXT)
        # 沒有重抓最新資料的那幾次，順便回補公會彙總表與配息資料的歷史月份
        plan = {} if refresh or not fund_info.get("funds") else backfill_plan(args.db)
        old_info = (pool.submit(sitca.backfill, plan, fund_info["funds"], args.fixtures / "sitca" if args.fixtures else None,
                                SSL_CONTEXT) if any(plan.values()) else None)
        for mod, records in pool.map(fetch_issuer, ISSUER_MODULES.items()):
            print(f"{mod.SOURCE}：{sum(1 for r in records.values() if not r.get('delisted'))} 檔")
            issuer.update(without_excluded(records))
        fresh = fresh_info.result() if fresh_info else {}
        old = old_info.result() if old_info else {}
    if fresh:
        fund_info = fresh
        for f in fund_info.get("funds", {}).values():
            f.update(data_month=fund_info.get("month"), fetched=today.isoformat())
    if old.get("done"):
        print(f"{sitca.SOURCE}歷史資料回補：" + "、".join(f"{'彙總表' if r == 'summary' else '配息'} {m}" for r, m in old["done"]))
    summary = fresh.get("summary") or {}
    print(f"{sitca.SOURCE}：{len(fund_info.get('funds') or {})} 檔（基本資料 {fund_info.get('month') or '—'}、"
          f"費用比率 {fund_info.get('year') or '—'} 年度" + (f"、彙總表 {summary['month']} 共 {len(summary['funds'])} 檔" if summary else "")
          + "）" + ("" if fresh else "，沿用資料庫裡的資料"))
    apply_issuer(items, issuer, date)
    monthly = sitca_etf.fetch(args.fixtures / "sitca" if args.fixtures else None, context=SSL_CONTEXT)
    apply_fund_info(items, fund_info)
    navs = fund_nav.result()
    apply_fund_nav(items, navs, date)
    print(f"{sitca.SOURCE}基金淨值：{date} 共 {len(navs)} 檔" if navs else f"{sitca.SOURCE}基金淨值：{date} 尚未公布")

    product_profile.attach(items, issuer, fund_info.get("funds"))
    save_db(items, date, args.db, nav, dividends, issuer, (fresh.get("dividends") or []) + (old.get("dividends") or []), navs)
    con = sqlite3.connect(args.db)
    try:
        returns_info = returns.attach(con, items, date, calendar)
        if fresh:
            save_fund_info(con, fresh, today.isoformat())
            save_fund_month(con, fresh.get("summary") or {})
            save_fund_top(con, fresh.get("top10") or {})
        for month, funds in (old.get("summary") or {}).items():
            save_fund_month(con, {"month": month, "funds": funds})
        done = [("summary", fresh["summary"]["month"])] if (fresh.get("summary") or {}).get("month") else []
        done += [("dividend", m) for m in fresh.get("dividend_months") or []] + list(old.get("done") or [])
        con.executemany("INSERT OR REPLACE INTO fund_fetch(report, month, fetched) VALUES(?, ?, ?)",
                        [(r, m, today.isoformat()) for r, m in done])
        con.commit()
        apply_fund_month(items, load_fund_month(con))
        if fresh_days:
            market_calendar.save(con, fresh_days, datetime.now(TW).isoformat(timespec="seconds"))
        else:
            print(f"警告：{market_calendar.SOURCE}沒有抓到，週申贖沿用資料庫裡的日曆。", file=sys.stderr)
        market_calendar.save_announcements(con)
        fund_flows.save(con, issuer, monthly)
        fund_flows.save_history(con, issuer)
        backfilled = sum(len(r.get("history") or []) for r in issuer.values())
        if backfilled:
            print(f"投信官網歷史申購買回清單回補：{backfilled} 份")
        fund_flows.attach(con, items, date, market_calendar.load(con))
        apply_fund_top(items, load_fund_top(con), date)
        apply_main_line_holdings(items)
        active = active_changes.attach(con, items, date, issuer, parse_closes(raw["twse"], raw["tpex"]))
        # 主動式 ETF 持股資金動向：先增量保存持有股票的官方收盤價與除權預告，再估值
        stocks = official_quotes.parse_stocks(raw["twse"], raw["tpex"], active_holdings.held_stocks(con))
        official_quotes.save_stocks(con, stocks, now)
        active_holdings.save_events(con, active_holdings.parse_events(raw["div_twse"], raw["div_tpex"]), now)
        holdings_flow = active_holdings.build(con, items, date, market_calendar.load(con))
        product_profile.attach(items, issuer, fund_info.get("funds"))
        product_profile.save(con, items)
        for it in items:
            if it.get("premium_reason"):
                con.execute("UPDATE daily SET premium=NULL, premium_official=NULL WHERE code=? AND date=?", (it["code"], date))
        official += stocks
        con.commit()
        purge_excluded(con)
        # 已由 PR 核實取得與公開使用範圍的離線資料包；匯入以內容雜湊去重。
        import research_history
        if not args.fixtures:
            research_history.import_pending(con, ROOT / "data/research-imports", date)
        store.dump(con, history)
    finally:
        con.close()
    # 顯名：這次抓到的原值，加上網站交易日實際採用的收盤行情（可能是先前保存的）
    used = [{"market": it["market"], "date": date} for it in items if it["price"] is not None and it.get("market")]
    save_json(items, date, source, args.out, has_nav=any(it["nav"] is not None for it in items), active=active,
              attributions=official_quotes.attributions(official + used), holdings_flow=holdings_flow,
              calendar=calendar, returns_info=returns_info)
    flagged = sum(1 for it in items if it["premium"] is not None and abs(it["premium"]) >= 1)
    notes: dict[str, int] = {}
    for it in items:
        if it["price"] is None:
            notes[it["price_note"]] = notes.get(it["price_note"], 0) + 1
    print(f"{date}：共 {len(items)} 檔 ETF，折溢價超過 ±1% 有 {flagged} 檔，除息預告 {len(dividends)} 筆 → {args.out}")
    print(f"官方收盤價：{sum(1 for it in items if it['price'] is not None)} 檔" + "".join(f"；{k} {v} 檔" for k, v in notes.items()))
    print(f"區間報酬：{sum(1 for it in items if it.get('returns'))} 檔有期末官方收盤價（官方歷史起點："
          + "、".join(f"{m} {d}" for m, d in (returns_info.get("history_from") or {}).items()) + "）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
