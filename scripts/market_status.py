"""提供網站可直接呈現的日期與行情品質狀態；不由前端猜測休市。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import market_calendar

TW = timezone(timedelta(hours=8))


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def build(items: list[dict], trade_date: str, now: datetime, calendar: market_calendar.Calendar) -> dict:
    local = now.astimezone(TW)
    day = local.date().isoformat()
    opened = calendar.is_open(day)
    entry = calendar.entries.get(day, {})
    reason_code = (entry.get("reason_code") or "scheduled_closed") if opened is False else "scheduled_open" if opened else "calendar_unknown"
    if opened is False and not entry:
        reason_code = "weekend"
    expected = day if opened is True and local.hour >= 14 else calendar.previous_trading_day(day)
    closed_price_day = calendar.is_open(trade_date) is False
    no_volume = len(items) >= 10 and not any(positive(it.get("volume_lots")) for it in items)
    usable = not closed_price_day and not no_volume
    return {
        "version": 1, "as_of": day, "trade_date": trade_date,
        "session": "closed" if opened is False else "scheduled_open" if opened else "unknown",
        "reason_code": reason_code,
        "reason": entry.get("name") or ("週末休市" if reason_code == "weekend" else "依官方日曆預定開市" if opened else "尚未確認當日交易安排"),
        "source_url": entry.get("source_url") or market_calendar.URL,
        "expected_trade_date": expected,
        "data_status": "invalid" if not usable else "unconfirmed" if expected is None else "delayed" if trade_date < expected else "current",
        "daily_returns_status": "available" if usable else "unavailable",
        "daily_returns_reason": "行情日期為官方休市日" if closed_price_day else "全市場樣本未取得有效成交量，請先核對資料" if no_volume else None,
        "note": "狀態截至資料產生日期；行情、淨值、規模與持股分別依各自日期閱讀。"
    }
