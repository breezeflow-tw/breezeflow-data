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
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "etf.db"
META_PATH = ROOT / "data" / "etf_meta.csv"
OUT_PATH = ROOT / "web" / "data" / "latest.json"
SCHEMA_PATH = ROOT / "scripts" / "schema.sql"

TW = timezone(timedelta(hours=8))

SOURCES = {
    # 證交所「ETF 預估淨值」頁面背後的資料，上市與上櫃 ETF 都在裡面
    "nav": "https://mis.twse.com.tw/stock/data/all_etf.txt",
    # 上市個股與 ETF 當日收盤行情
    "twse": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    # 上櫃當日收盤行情
    "tpex": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}


# ---------- 下載 ----------

def http_get_json(url: str, retries: int = 4) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (breezeflow-data)"})
    delay = 2
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8-sig")
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - 網路錯誤一律重試
            if attempt == retries:
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
                # 淨值資料是核心，抓不到就停；行情資料缺一個市場仍可繼續
                if key == "nav":
                    raise
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
                "name": str(row.get("b", "")).strip(),
                "price": price,
                "nav": nav,
                "premium": premium,
                "nav_prev": num(row.get("h")),
                "source_date": str(row.get("i", "")).strip(),
                "source_time": str(row.get("j", "")).strip(),
            }
    return result


def pick(row: dict, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def parse_quotes(rows, market: str) -> dict[str, dict]:
    """證交所與櫃買的收盤行情欄位名稱不同，這裡統一成 close / change / volume_lots。"""
    result = {}
    for row in rows if isinstance(rows, list) else []:
        code = str(pick(row, "Code", "SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        close = num(pick(row, "ClosingPrice", "Close"))
        change = num(pick(row, "Change"))
        shares = num(pick(row, "TradeVolume", "TradingShares"))
        prev = close - change if close is not None and change is not None else None
        result[code] = {
            "market": market,
            "close": close,
            "change_pct": (change / prev * 100) if prev else None,
            "volume_lots": round(shares / 1000) if shares is not None else None,
        }
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
           "柏瑞", "路博邁", "宏利", "鋒裕匯理", "合庫", "玉山", "富達", "保德信", "施羅德", "瀚亞", "大華", "台灣人壽"]


def issuer_from_name(name: str) -> str | None:
    """台股 ETF 名稱多半以投信品牌開頭，例如「元大台灣50」。"""
    for brand in ISSUERS:
        if name.startswith(brand):
            return "中信" if brand == "中國信託" else brand
    return None


def load_meta(path: Path = META_PATH) -> dict[str, dict]:
    """人工維護的補充資料：類型修正、總費用率、規模、發行商。空白欄位不覆蓋自動結果。"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {r["code"].strip(): r for r in csv.DictReader(f) if r.get("code")}


# ---------- 合併 ----------

def merge(nav: dict, quotes: dict, meta: dict) -> list[dict]:
    items = []
    for code, n in nav.items():
        q = quotes.get(code, {})
        m = meta.get(code, {})
        price = q.get("close") if q.get("close") is not None else n["price"]
        premium = n["premium"]
        if premium is None and price and n["nav"]:
            premium = (price - n["nav"]) / n["nav"] * 100
        items.append({
            "code": code,
            "name": n["name"],
            "issuer": (m.get("issuer") or "").strip() or issuer_from_name(n["name"]),
            "category": (m.get("category") or "").strip() or classify(code, n["name"]),
            "market": q.get("market"),
            "price": price,
            "change_pct": q.get("change_pct"),
            "nav": n["nav"],
            "nav_prev": n["nav_prev"],
            "premium": round(premium, 4) if premium is not None else None,
            "volume_lots": q.get("volume_lots"),
            "fee": num(m.get("fee")),
            "aum": num(m.get("aum")),
            "source_time": f"{n['source_date']} {n['source_time']}".strip(),
        })
    items.sort(key=lambda x: x["code"])
    return items


def trade_date(items: list[dict], now: datetime) -> str:
    for it in items:
        m = re.match(r"(\d{4})(\d{2})(\d{2})", it["source_time"].replace("/", "").replace("-", ""))
        if m:
            return "-".join(m.groups())
    return now.strftime("%Y-%m-%d")


# ---------- 儲存 ----------

def save_db(items: list[dict], date: str, db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA_PATH.read_text("utf-8"))
        now = datetime.now(TW).isoformat(timespec="seconds")
        con.executemany(
            """INSERT INTO etf(code, name, issuer, category, market, fee, aum, updated_at)
               VALUES(:code, :name, :issuer, :category, :market, :fee, :aum, :now)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                 issuer=COALESCE(excluded.issuer, etf.issuer), category=excluded.category,
                 market=COALESCE(excluded.market, etf.market), fee=COALESCE(excluded.fee, etf.fee),
                 aum=COALESCE(excluded.aum, etf.aum), updated_at=excluded.updated_at""",
            [{**it, "now": now} for it in items],
        )
        # 同一天重跑會覆蓋當天資料，歷史不重複
        con.executemany(
            """INSERT OR REPLACE INTO daily(date, code, price, change_pct, nav, nav_prev, premium, volume_lots, source_time)
               VALUES(:date, :code, :price, :change_pct, :nav, :nav_prev, :premium, :volume_lots, :source_time)""",
            [{**it, "date": date} for it in items],
        )
        con.commit()
    finally:
        con.close()


def save_json(items: list[dict], date: str, source: str, out: Path = OUT_PATH) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(TW).isoformat(timespec="seconds"),
        "trade_date": date,
        "source": source,
        "count": len(items),
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", type=Path, help="改用此資料夾內的 nav.json / twse.json / tpex.json")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)

    raw = load_sources(args.fixtures)
    nav = parse_nav(raw["nav"])
    if not nav:
        print("錯誤：淨值資料是空的，可能是非交易時段或來源格式改變。", file=sys.stderr)
        return 1
    quotes = {**parse_quotes(raw["tpex"], "上櫃"), **parse_quotes(raw["twse"], "上市")}
    items = merge(nav, quotes, load_meta())
    date = trade_date(items, datetime.now(TW))

    save_db(items, date, args.db)
    save_json(items, date, "fixture" if args.fixtures else "live", args.out)
    flagged = sum(1 for it in items if it["premium"] is not None and abs(it["premium"]) >= 1)
    print(f"{date}：共 {len(items)} 檔 ETF，折溢價超過 ±1% 有 {flagged} 檔 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
