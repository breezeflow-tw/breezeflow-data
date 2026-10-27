"""區間報酬率：只用交易所開放資料逐日保存的官方收盤行情（exchange_daily）與資料庫的配息紀錄計算。

- 市價報酬＝期末收盤價 ÷ 期初收盤價 − 1
- 含息報酬：配息在除息日以當天收盤價再投入，＝期末收盤價 ÷ 期初收盤價 × Π（1＋每單位配息 ÷ 除息日收盤價）− 1
- 期初是起始日當天或之前最近的交易日（依證交所開休市日期），期末是網站的交易日
- 官方收盤行情從開始保存那天起累積、不回補。期初早於保存起點、期初或期末沒有官方收盤價、
  期間內有交易日沒有保存到這檔的收盤行情，或開休市日期沒有涵蓋時，該區間為 null
- 交易所的漲跌價差是相對於當天參考價的差。參考價（收盤價 − 漲跌價差）比前一個收盤價低的部分要能用當天的配息解釋：
  解釋不了的小幅調整（疑似漏列配息）只讓含息報酬為 null；大幅調整（疑似分割、合併）時，市價與含息報酬都為 null
- 除息、除權日交易所不提供相對參考價的漲跌（證交所記 0、櫃買中心空白，2026-10 實測）：有配息紀錄就以配息解釋；
  沒有紀錄時，單日變動超過 25% 疑似分割、合併，兩種報酬都為 null，否則只讓含息報酬為 null
"""
from __future__ import annotations

import calendar
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

PERIODS = ("1w", "1m", "3m", "6m", "ytd", "1y", "3y", "5y")
MONTHS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}
SOURCE = ("證交所、櫃買中心開放資料逐日保存的官方收盤價（政府資料開放平臺資料集 11549、11370）；"
          "含息報酬依資料庫的配息紀錄，在除息日以當天收盤價再投入")
TICK = 0.05  # 參考價的容許誤差（元）：ETF 的升降單位最大 0.05 元
DIVIDEND_MAX = 0.25  # 參考價比前一個收盤價低 25% 以內才可能是配息；更多、或參考價反而較高時疑似分割、合併


def add_months(d: date, n: int) -> date:
    """往前或往後 n 個月；目標月份沒有那一天時用該月最後一天（3/31 往前一個月是 2/28 或 2/29）。"""
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def start_dates(end: str) -> dict[str, str]:
    """各區間的起始日。今年以來的基準是去年最後一個交易日。"""
    e = date.fromisoformat(end)
    starts = {"1w": e - timedelta(days=7), "ytd": date(e.year - 1, 12, 31)}
    starts.update({p: add_months(e, -n) for p, n in MONTHS.items()})
    return {p: starts[p].isoformat() for p in PERIODS}


def load(con: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """官方收盤行情 {代號: {日期: (收盤價, 漲跌價差)}}（收盤價不是正數時為 None）、{代號: 市場}，
    以及各市場保存到收盤行情的日期 {市場: {日期}}。"""
    series, markets, captured = defaultdict(dict), {}, defaultdict(set)
    try:
        rows = con.execute("SELECT code, date, market, close, change FROM exchange_daily ORDER BY date").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for code, day, market, close, change in rows:
        series[code][day] = (close if close is not None and close > 0 else None, change)
        markets[code] = market
        captured[market].add(day)
    return series, markets, captured


def load_dividends(con: sqlite3.Connection) -> dict[str, dict[str, float | None]]:
    """資料庫的配息紀錄 {代號: {除息日: 每單位配息}}；金額尚未公告時為 None。"""
    out = defaultdict(dict)
    for code, ex_date, cash in con.execute("SELECT code, ex_date, cash FROM dividend"):
        out[code][ex_date] = cash
    return out


def history_from(captured: dict) -> dict[str, str]:
    """各市場官方收盤行情的保存起點。"""
    return {market: min(days) for market, days in sorted(captured.items()) if days}


def window(cal, start: str, end: str) -> tuple[str, list[str]] | None:
    """期初（起始日當天或之前最近的交易日）與期初之後到期末的交易日；開休市日期沒涵蓋時回傳 None。"""
    is_open = cal.is_open(start) if cal else None
    if is_open is None:
        return None
    base = start if is_open else cal.previous_trading_day(start)
    if not base or base >= end:
        return None
    days = cal.trading_days((date.fromisoformat(base) + timedelta(days=1)).isoformat(), end)
    return (base, days) if days else None


def compute(series: dict, captured: set, end: str, cal, dividends: dict | None = None) -> dict | None:
    """單檔的各區間報酬（%，四捨五入到小數兩位）。series 是 {日期: (收盤價, 漲跌價差)}，captured 是這檔所屬市場
    保存到收盤行情的日期，dividends 是 {除息日: 每單位配息}。期末沒有官方收盤價時回傳 None。"""
    last = (series.get(end) or (None, None))[0]
    if last is None:
        return None
    dividends = dividends or {}
    price, total = {}, {}
    for period, start in start_dates(end).items():
        price[period] = total[period] = None
        span = window(cal, start, end)
        if span is None:
            continue
        base_day, days = span
        base = (series.get(base_day) or (None, None))[0]
        if base is None or any(d not in captured or d not in series for d in days):
            continue
        price_ok = total_ok = True
        factor, prev = 1.0, base
        for d in days:
            close, change = series[d]
            paid = d in dividends
            cash = dividends.get(d)
            if paid and cash is None:
                total_ok = False  # 配息金額還沒公告
            if close is None:
                total_ok = total_ok and not paid  # 除息日沒有成交，沒有再投入的價格
                continue  # 沒有成交：下一個有成交的日子再核對參考價
            if change is None or (change == 0 and (paid or abs(close - prev) > 1e-9)):
                # 交易所沒有提供相對參考價的漲跌（除息、除權日）：有配息紀錄就由配息解釋
                if not paid:
                    if abs(close / prev - 1) > DIVIDEND_MAX:
                        price_ok = total_ok = False  # 疑似分割、合併
                    else:
                        total_ok = False  # 疑似漏列配息：市價報酬照算
            else:
                gap = prev - (close - change)  # 前一個收盤價比參考價高的部分，應該等於當天配息
                if abs(gap - (cash or 0)) > TICK:
                    if abs(gap) <= TICK or 0 < gap <= prev * DIVIDEND_MAX:
                        total_ok = False  # 配息紀錄和參考價對不上（漏列、金額或日期不符）：市價報酬照算
                    else:
                        price_ok = total_ok = False  # 疑似分割、合併
            if paid and cash:
                factor *= 1 + cash / close
            prev = close
        if price_ok:
            price[period] = round((last / base - 1) * 100, 2)
            if total_ok:
                total[period] = round((last / base * factor - 1) * 100, 2)
    return {"price": price, "total": total}


def attach(con: sqlite3.Connection, items: list[dict], end: str, cal) -> dict:
    """每檔附上區間報酬（items[].returns），回傳 latest.json 的 returns_info。"""
    series, markets, captured = load(con)
    dividends = load_dividends(con)
    for it in items:
        code = it["code"]
        it["returns"] = compute(series.get(code) or {}, captured.get(markets.get(code), set()), end, cal, dividends.get(code))
    return {"source": SOURCE, "starts": start_dates(end), "history_from": history_from(captured)}
