"""證交所有價證券集中交易市場開（休）市日期（OpenAPI holidaySchedule/holidaySchedule），上市、上櫃共用。

- 官方表列出當年度的休市日（含補假、僅辦理結算交割的日子），以及「開始交易日」「最後交易日」等說明；
  後兩種當天有交易
- 表上沒有列出的平日是交易日，週六、週日休市
- 只認得官方表涵蓋的年度（每次抓到的年度都存進資料庫）；其他年份一律「未知」，
  不能把沒有資料的日子當成休市
- 颱風等臨時休市不在年度表內：表上是交易日、但交易所沒有資料的日子算「缺資料」，不是休市
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

SOURCE = "證交所有價證券集中交易市場開（休）市日期"
URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
OPEN_MARKS = ("開始交易", "最後交易")  # 名稱有這些字的日子當天有交易
LOOKBACK = 31  # 往前找前一個交易日最多幾天


def roc_date(value) -> str | None:
    """民國日期 '1151026' → '2026-10-26'。"""
    m = re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", str(value or "").strip())
    return f"{int(m.group(1)) + 1911}-{m.group(2)}-{m.group(3)}" if m else None


def parse(rows) -> dict[str, dict]:
    """官方表 → {日期: {is_open, name, description}}。"""
    out = {}
    for r in rows if isinstance(rows, list) else []:
        day = roc_date(r.get("Date")) if isinstance(r, dict) else None
        if not day:
            continue
        name = re.sub(r"\s+", " ", str(r.get("Name") or "")).strip()
        text = re.sub(r"<[^>]+>", "", str(r.get("Description") or "")).strip()
        out[day] = {"is_open": "無交易" not in name and any(k in name for k in OPEN_MARKS), "name": name, "description": text}
    return out


def save(con: sqlite3.Connection, entries: dict[str, dict], fetched_at: str) -> None:
    """依年度整批取代：官方表一次列出整年，同一年重抓時以新表為準；沒抓到的年度保留原資料。"""
    years = sorted({int(d[:4]) for d in entries})
    for year in years:
        con.execute("DELETE FROM market_calendar WHERE date LIKE ?", (f"{year}-%",))
        rows = [(d, int(e["is_open"]), e["name"], e["description"], URL, fetched_at)
                for d, e in sorted(entries.items()) if d.startswith(f"{year}-")]
        con.executemany("INSERT INTO market_calendar(date, is_open, name, description, source_url, fetched_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)", rows)
        con.execute("INSERT OR REPLACE INTO market_calendar_year(year, entries, source_url, fetched_at) VALUES(?, ?, ?, ?)",
                    (year, len(rows), URL, fetched_at))


class Calendar:
    def __init__(self, entries: dict[str, dict], years: set[int]):
        self.entries, self.years = entries, set(years)

    def is_open(self, day: str) -> bool | None:
        """有交易 True、休市 False；官方表沒涵蓋那一年時 None。"""
        d = date.fromisoformat(day)
        if d.year not in self.years:
            return None
        if day in self.entries:
            return self.entries[day]["is_open"]
        return d.weekday() < 5

    def trading_days(self, start: str, end: str) -> list[str] | None:
        """start 到 end（含）的交易日；區間內有任何一天在官方表涵蓋範圍外時回傳 None。"""
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        out = []
        for i in range((e - s).days + 1):
            day = (s + timedelta(days=i)).isoformat()
            status = self.is_open(day)
            if status is None:
                return None
            if status:
                out.append(day)
        return out

    def previous_trading_day(self, day: str) -> str | None:
        """day 之前（不含當天）最近的交易日；往前碰到官方表涵蓋範圍外的日子時回傳 None。"""
        d = date.fromisoformat(day)
        for i in range(1, LOOKBACK + 1):
            prev = (d - timedelta(days=i)).isoformat()
            status = self.is_open(prev)
            if status is None:
                return None
            if status:
                return prev
        return None


def load(con: sqlite3.Connection) -> Calendar:
    try:
        entries = {d: {"is_open": bool(o), "name": n, "description": t}
                   for d, o, n, t in con.execute("SELECT date, is_open, name, description FROM market_calendar")}
        years = {y for (y,) in con.execute("SELECT year FROM market_calendar_year")}
    except sqlite3.OperationalError:
        return Calendar({}, set())
    return Calendar(entries, years)
