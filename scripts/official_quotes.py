"""交易所 OpenAPI 的 ETF 收盤行情原值，逐筆保存來源與取得時間（exchange_daily 表），累積成官方歷史。

- 上市：證交所 OpenAPI exchangeReport/STOCK_DAY_ALL，政府資料開放平臺資料集 11549「盤後資訊 > 個股日成交資訊」
- 上櫃：櫃買中心 OpenAPI tpex_mainboard_daily_close_quotes，資料集 11370「上櫃股票行情」（含 ETF）
- 兩個端點都只有最新一天，沒有可依日期查的歷史：每次執行保存當下的原值，日期用交易所標示的資料日期
- 價格是 0、空白或「--」代表沒有成交或沒有資料，存成空值；成交股數 0 照存
- 這張表只放交易所原值，不改變網站目前採用的收盤價（daily 表）
- 主動式 ETF 持有的台股股票另存 stock_close（只有收盤價），供持股資金動向估值；同樣從開始保存那天起逐日累積，不回補
- 兩個資料集在政府資料開放平臺列為政府資料開放授權條款第 1 版（docs/data-licensing.md 證據 10、11），
  利用時須標示提供機關、年份、資料名稱與版本、授權聲明連結：latest.json 的 attributions 列出這些資訊
"""
from __future__ import annotations

import re
import sqlite3

LICENSE = "政府資料開放授權條款－第1版"
LICENSE_URL = "https://data.gov.tw/license"
# 市場 → 提供機關、資料集名稱、資料集頁、取得的端點、欄位名稱
DATASETS = {
    "上市": {"provider": "臺灣證券交易所", "dataset": "盤後資訊 > 個股日成交資訊", "dataset_url": "https://data.gov.tw/dataset/11549",
             "dataset_id": "11549",
             "source_url": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
             "fields": {"code": "Code", "name": "Name", "open": "OpeningPrice", "high": "HighestPrice", "low": "LowestPrice",
                        "close": "ClosingPrice", "change": "Change", "volume_shares": "TradeVolume", "trade_value": "TradeValue",
                        "transactions": "Transaction"}},
    "上櫃": {"provider": "證券櫃檯買賣中心", "dataset": "上櫃股票行情", "dataset_url": "https://data.gov.tw/dataset/11370",
             "dataset_id": "11370",
             "source_url": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
             "fields": {"code": "SecuritiesCompanyCode", "name": "CompanyName", "open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "change": "Change", "volume_shares": "TradingShares", "trade_value": "TransactionAmount",
                        "transactions": "TransactionNumber"}},
}
PRICES = ("open", "high", "low", "close")
STOCK = re.compile(r"[1-9]\d{3}[A-Z]?")  # 台股股票代號：4 碼（特別股另加 1 個英文字母）；00 開頭是 ETF
COLUMNS = ("code", "date", "market", "name", "open", "high", "low", "close", "change", "volume_shares", "trade_value",
           "transactions", "dataset", "source_url", "first_observed_at", "observed_at")


def num(value) -> float | None:
    s = re.sub(r"^[+＋]", "", str(value if value is not None else "").replace(",", "").strip())
    try:
        return float(s) if s not in ("", "-", "--", "---", "X", "除息", "除權") else None
    except ValueError:
        return None


def roc_date(value) -> str | None:
    m = re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", str(value or "").strip())
    return f"{int(m.group(1)) + 1911}-{m.group(2)}-{m.group(3)}" if m else None


def parse(twse_rows, tpex_rows) -> list[dict]:
    """兩個市場的 ETF（代號 00 開頭）原值；沒有資料日期的列不收。"""
    out = []
    for market, rows in (("上市", twse_rows), ("上櫃", tpex_rows)):
        spec = DATASETS[market]
        for r in rows if isinstance(rows, list) else []:
            f = {k: r.get(v) for k, v in spec["fields"].items()} if isinstance(r, dict) else {}
            code, day = str(f.get("code") or "").strip(), roc_date(r.get("Date") if isinstance(r, dict) else None)
            if not (code.startswith("00") and len(code) >= 4) or not day:
                continue
            rec = {"code": code, "date": day, "market": market, "name": str(f.get("name") or "").strip() or None,
                   "dataset": spec["dataset"], "source_url": spec["source_url"]}
            for k in ("open", "high", "low", "close", "change", "volume_shares", "trade_value", "transactions"):
                rec[k] = num(f.get(k))
            for k in PRICES:
                rec[k] = rec[k] or None  # 價格 0 是沒有成交
            out.append(rec)
    return out


def parse_stocks(twse_rows, tpex_rows, codes: set[str]) -> list[dict]:
    """台股股票的收盤價，只收 codes 裡的代號（主動式 ETF 持有的股票）；沒有成交（0、空白）存成空值。"""
    out = []
    for market, rows in (("上市", twse_rows), ("上櫃", tpex_rows)):
        spec = DATASETS[market]
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            code, day = str(r.get(spec["fields"]["code"]) or "").strip(), roc_date(r.get("Date"))
            if code in codes and STOCK.fullmatch(code) and day:
                out.append({"code": code, "date": day, "market": market,
                            "close": num(r.get(spec["fields"]["close"])) or None, "dataset": spec["dataset_id"]})
    return out


def save_stocks(con: sqlite3.Connection, rows: list[dict], now: str) -> None:
    """同一天重抓時以新值為準；第一次取得時間保留原值。"""
    con.executemany(
        "INSERT INTO stock_close(code, date, market, close, dataset, first_observed_at, observed_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(code, date) DO UPDATE SET market=excluded.market, close=excluded.close, dataset=excluded.dataset, "
        "observed_at=excluded.observed_at",
        [(r["code"], r["date"], r["market"], r["close"], r["dataset"], now, now) for r in rows])


def save(con: sqlite3.Connection, rows: list[dict], now: str) -> None:
    """同一天重抓時以新值為準；第一次取得時間保留原值。"""
    cols = COLUMNS
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("code", "date", "first_observed_at"))
    con.executemany(
        f"INSERT INTO exchange_daily({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(code, date) DO UPDATE SET {updates}",
        [tuple({**r, "first_observed_at": now, "observed_at": now}[c] for c in cols) for r in rows])


def attributions(rows: list[dict]) -> list[dict]:
    """latest.json 的顯名資訊：這次用到的資料集、提供機關、資料日期（年份）與授權條款連結。"""
    out = []
    for market, spec in DATASETS.items():
        days = sorted({r["date"] for r in rows if r["market"] == market})
        if days:
            out.append({"provider": spec["provider"], "dataset": spec["dataset"], "dataset_url": spec["dataset_url"],
                        "source_url": spec["source_url"], "data_date": days[-1], "license": LICENSE, "license_url": LICENSE_URL})
    return out
