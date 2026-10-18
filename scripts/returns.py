"""區間報酬率：用 Yahoo 股市日線（收盤價已做分割還原）計算各檔 ETF 的市價報酬與含息報酬。

- 市價報酬＝期末收盤價 ÷ 期初收盤價 − 1
- 含息報酬＝期末收盤價 ÷ 期初還原收盤價 − 1（Yahoo 的還原收盤價已把期間的配息再投入）
- 期初是起始日當天或之前最近的交易日；ETF 上市還不到該區間就不算，留空
- 期末收盤價用查核後的數字（Yahoo 與交易所不一致時已改用交易所）
- 含息報酬會和資料庫的配息紀錄（投信官網、交易所）核對：區間內有 Yahoo 沒列到或金額不符的配息，
  該區間的含息報酬留空，不用可能漏算配息的數字
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TW = timezone(timedelta(hours=8))
PERIODS = ("1w", "1m", "3m", "6m", "ytd", "1y", "3y", "5y")
MONTHS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}
HISTORY_DAYS = 5 * 366 + 30  # 五年再多抓一個月，確保五年前那天之前有交易日
SOURCE = "Yahoo 股市日線（含息報酬以還原收盤價計算，配息與投信官網、交易所紀錄核對）"


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


def parse_history(payloads: dict) -> dict[str, dict]:
    """Yahoo 日線 → {代號: {"days": [(日期, 收盤價, 還原收盤價)], "dividends": [(除息日, 每單位金額)]}}。"""
    def day(ts):
        return datetime.fromtimestamp(int(ts), TW).strftime("%Y-%m-%d")

    result = {}
    for symbol, payload in (payloads or {}).items():
        try:
            chart = payload["chart"]["result"][0]
            stamps = chart.get("timestamp") or []
            close = chart["indicators"]["quote"][0].get("close") or []
        except (KeyError, IndexError, TypeError):
            continue
        adj = ((chart["indicators"].get("adjclose") or [{}])[0].get("adjclose") or [None] * len(close))
        events = (chart.get("events") or {}).get("dividends") or {}
        result[symbol.split(".")[0]] = {
            "days": [(day(t), c, a) for t, c, a in zip(stamps, close, adj) if c],
            "dividends": sorted((day(v.get("date") or t), v.get("amount")) for t, v in events.items()),
        }
    return result


def listed_in_yahoo(yahoo_divs: list, ex_date: str, cash: float | None) -> bool:
    """資料庫的一筆配息在 Yahoo 找得到：除息日相差 3 天內，金額相差 1% 或 0.002 元以內。
    ETF 分割後，Yahoo 會把分割前的配息除以分割比例（例如 1 拆 7 時 10.19 元變 1.4557 元），
    所以金額剛好是整數倍（誤差 1% 內）也算同一筆。"""
    d0 = date.fromisoformat(ex_date)
    for d, amount in yahoo_divs:
        if abs((date.fromisoformat(d) - d0).days) <= 3:
            if cash is None or amount is None or abs(amount - cash) <= max(0.002, abs(cash) * 0.01):
                return True
            ratio = cash / amount if amount > 0 else 0
            return round(ratio) >= 2 and abs(ratio - round(ratio)) <= round(ratio) * 0.01
    return False


def compute(series: dict | None, end: str, end_price: float | None = None,
            known_dividends: list[tuple[str, float | None]] = ()) -> dict | None:
    """單檔的各區間報酬（%，四捨五入到小數兩位）。Yahoo 沒有期末那天的資料就整檔不算。"""
    days = (series or {}).get("days") or []
    idx = next((i for i in range(len(days) - 1, -1, -1) if days[i][0] == end), None)
    if idx is None:
        return None
    last = end_price if end_price else days[idx][1]
    price, total = {}, {}
    for period, start in start_dates(end).items():
        base = next((days[i] for i in range(idx, -1, -1) if days[i][0] <= start), None)
        price[period] = round((last / base[1] - 1) * 100, 2) if base else None
        complete = base and base[2] and all(listed_in_yahoo(series.get("dividends") or [], d, cash)
                                            for d, cash in known_dividends if base[0] < d <= end)
        total[period] = round((last / base[2] - 1) * 100, 2) if complete else None
    return {"price": price, "total": total}


def previous(out: Path, trade_date: str, source: str) -> dict[str, dict]:
    """同一個交易日再跑（例如早上 08:50）時，沿用上一次輸出的報酬率，不必重抓五年日線。"""
    try:
        payload = json.loads(Path(out).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("trade_date") != trade_date or payload.get("source") != source:
        return {}
    return {it["code"]: it["returns"] for it in payload.get("items") or [] if it.get("code") and it.get("returns")}
