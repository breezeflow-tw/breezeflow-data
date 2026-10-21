"""官方基金快照與保守的週申贖估算；不以規模差額冒充淨流入。"""
from __future__ import annotations

import calendar
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

METHOD = "每日流通單位數增減 × 同日投信正式淨值，再逐日加總／1億；屬申贖對價估算，含實物申贖，不等於實際現金流。"


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def upsert(con, table, keys, record, keep=()):
    """新值優先、沒有新值時保留原值；keep 裡的欄位反過來以原值優先（例如第一次取得時間）。"""
    columns = list(record)
    update = [k for k in columns if k not in keys]
    con.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT({','.join(keys)}) DO UPDATE SET " +
                ",".join(f"{k}=COALESCE({table}.{k},excluded.{k})" if k in keep else f"{k}=COALESCE(excluded.{k},{table}.{k})"
                         for k in update), list(record.values()))


def save(con, issuer, monthly):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in monthly.values():
        upsert(con, "fund_monthly", ("code", "month"), r)
    for code, r in issuer.items():
        if r.get("delisted") or not r.get("source_url"):
            continue
        # 每個數值依其資料日期保存，公告日期／更新時間不拿來充當交易日。
        nav_day = r.get("nav_date")
        asset_day = r.get("asset_date") or r.get("pcf_date") or nav_day
        currency = r.get("currency") or monthly.get(code, {}).get("currency")
        base = {"code": code, "currency": currency, "source": r["source"],
                "source_url": r["source_url"], "observed_at": now, "first_observed_at": now}
        keep = ("first_observed_at",)
        if nav_day and numeric(r.get("nav_official")) and r["nav_official"] > 0:
            upsert(con, "fund_daily", ("code", "date"), {**base, "date": nav_day, "nav": r["nav_official"]}, keep)
        if asset_day:
            values = {key: r.get(key) for key in ("aum", "units", "units_change")}
            if r.get("pcf_date") and r["pcf_date"] != asset_day:
                values["units_change"] = None
                if numeric(r.get("units_change")):
                    upsert(con, "fund_daily", ("code", "date"), {**base, "date": r["pcf_date"], "units_change": r["units_change"]}, keep)
            if any(numeric(v) for v in values.values()):
                # 公告日期：投信標示的申購買回清單日期（清單適用的交易日），只屬於清單裡的規模與單位數
                upsert(con, "fund_daily", ("code", "date"), {**base, "date": asset_day, **values,
                                                            "announced": r.get("pcf_announced")}, keep)
            # 凱基清單可能比最新淨值早一天；清單自身的正式淨值跟清單日期配對。
            if numeric(r.get("pcf_nav")) and r["pcf_nav"] > 0 and currency == "TWD":
                upsert(con, "fund_daily", ("code", "date"), {**base, "date": asset_day, "nav": r["pcf_nav"]}, keep)
        for e in r.get("events") or []:
            if e.get("date") and e.get("kind") and e.get("source_url"):
                upsert(con, "fund_event", ("code", "date", "kind"), {"code": code, **e})


def save_history(con, issuer):
    """投信官網回補的歷史申購買回清單：依資料日期寫入 fund_daily（公告日期是清單日期），並記下查過的清單日期。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for code, r in issuer.items():
        for h in r.get("history") or []:
            save(con, {code: {**h, "source": r["source"], "source_url": r.get("source_url"), "currency": r.get("currency")}}, {})
        for a in r.get("history_attempts") or []:
            upsert(con, "fund_backfill", ("code", "pcf_date"),
                   {"code": code, **a, "source": r["source"], "fetched_at": now})


def backfill_done(con) -> set:
    """已經有的清單：查過的清單日期，加上 fund_daily 已記下公告日期的。"""
    try:
        done = set(con.execute("SELECT code, pcf_date FROM fund_backfill"))
        return done | set(con.execute("SELECT code, announced FROM fund_daily WHERE announced IS NOT NULL"))
    except sqlite3.OperationalError:
        return set()


def weekdays(start, end):
    return [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)
            if (start + timedelta(days=i)).weekday() < 5]


def week(rows, start, end, events=(), cal=None):
    """一週的申贖估算。有官方開休市日期（cal）時，期初是本週第一天之前最近的交易日，只比較本週的交易日；
    日曆沒涵蓋的日期保守留空。沒有日曆時沿用前週五與週一到週五，休市日會被當成缺資料。
    不把缺漏抓取日當休市；四週與本週均不跨缺口加總。reason_code 是機器可讀的原因。"""
    result = {"start": start.isoformat(), "end": end.isoformat(), "baseline": None,
              "net_amount": None, "net_rate": None, "units_rate": None, "aum_change": None,
              "aum_rate": None, "status": "待確認", "reason": None, "method": "估算", "sources": [],
              "reason_code": None, "calendar": "未使用", "trading_days": None, "closed_days": None}

    def stop(code, reason):
        result.update(reason_code=code, reason=reason)
        return result

    if cal is None:
        baseline = (start - timedelta(days=3)).isoformat()  # 前週五；若無官方快照，期初未確認
        trading = weekdays(start, end)
        holiday_note = "；休市日亦須核對"
    else:
        trading = cal.trading_days(start.isoformat(), end.isoformat())
        baseline = cal.previous_trading_day(start.isoformat())
        if trading is None or baseline is None:
            result["calendar"] = "未涵蓋"
            return stop("calendar_unknown", "官方開休市日期未涵蓋本週或前一個交易日，無法區分休市與缺資料。")
        result.update(calendar="官方開休市日期", trading_days=trading,
                      closed_days=[d for d in weekdays(start, end) if d not in trading])
        holiday_note = ""
    result["baseline"] = baseline
    if not trading:
        result["status"] = "休市"
        return stop("market_closed", "本週全部休市（官方開休市日期），沒有可比較的交易日。")
    days = [baseline, *trading]
    missing = [d for d in days if d not in rows]
    if missing:
        return stop("missing_official", "官方日資料不足（缺 " + "、".join(missing) + f"）{holiday_note}，不補零。")
    data = [rows[d] for d in days]
    if any(r.get("currency") != "TWD" for r in data):
        return stop("currency_unconfirmed", "尚未確認全期為新臺幣同一受益權級別。")
    if any(e["kind"] in ("分割", "反分割", "合併", "級別轉換", "清算") and baseline < e["date"] <= end.isoformat() for e in events):
        return stop("special_event", "區間包含分割、合併或級別等特殊事件，暫停比較。")
    if any(not numeric(r.get("units")) or r["units"] <= 0 or not numeric(r.get("nav")) or r["nav"] <= 0 for r in data):
        return stop("incomplete", "流通單位數或同日正式淨值不完整。")
    amount = 0
    for previous, current in zip(data, data[1:]):
        delta = current["units"] - previous["units"]
        stated = current.get("units_change")
        if numeric(stated) and not math.isclose(delta, stated, rel_tol=1e-8, abs_tol=1):
            return stop("units_mismatch", "相鄰快照與投信公告單位增減不一致，須核對日期或特殊事件。")
        ratio = current["units"] / previous["units"]
        if (ratio >= 1.5 or ratio <= 2/3) and abs(ratio * current["nav"] / previous["nav"] - 1) < .15:
            return stop("split_suspected", "單位數與淨值出現疑似分割／反分割變動，暫停估算。")
        if previous.get("source") != current.get("source"):
            return stop("source_changed", "日資料來源不同，尚未確認相同口徑。")
        amount += delta * current["nav"] / 1e8
    first, last = data[0], data[-1]
    result.update(net_amount=round(amount, 6), units_rate=round((last["units"] / first["units"] - 1)*100, 4),
                  status="可估算", reason="特殊事件公告未保證完整；請併同原公告核對。")
    if numeric(first.get("aum")) and first["aum"] > 0:
        result["net_rate"] = round(amount / first["aum"] * 100, 4)
        if numeric(last.get("aum")):
            result["aum_change"] = round(last["aum"] - first["aum"], 6)
            result["aum_rate"] = round((last["aum"] / first["aum"] - 1)*100, 4)
    result["sources"] = [{"date": r["date"], "source": r["source"], "url": r["source_url"], "announced": r.get("announced"),
                          "first_observed_at": r.get("first_observed_at"), "observed_at": r.get("observed_at")} for r in data]
    return result


def attach(con, items, as_of, cal=None):
    """cal 是官方開休市日期（market_calendar.Calendar）；沒有時各週沿用前週五、週一到週五。"""
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    end = date.fromisoformat(as_of)
    monday = end - timedelta(days=end.weekday())
    for it in items:
        code = it["code"]
        snapshots = [dict(r) for r in con.execute("SELECT * FROM fund_daily WHERE code=? AND date<=? ORDER BY date", (code, as_of))]
        rows = {r["date"]: r for r in snapshots}
        events = [dict(r) for r in con.execute("SELECT * FROM fund_event WHERE code=? AND date<=? ORDER BY date DESC", (code, as_of))]
        periods = [week(rows, monday-timedelta(weeks=i), min(end, monday-timedelta(weeks=i)+timedelta(days=4)), events, cal)
                   for i in range(4)]
        complete = all(numeric(p["net_amount"]) for p in periods)
        it["flows"] = {**periods[0], "as_of": as_of, "latest_official_date": snapshots[-1]["date"] if snapshots else None,
                       "four_week_amount": round(sum(p["net_amount"] for p in periods), 6) if complete else None,
                       "positive_weeks": sum(p["net_amount"] > 0 for p in periods) if complete else None,
                       "available_weeks": sum(numeric(p["net_amount"]) for p in periods), "weeks": periods,
                       "events": events, "events_status": "僅列已取得公告；未列不代表沒有特殊事件。"}
        month = con.execute("SELECT * FROM fund_monthly WHERE code=? AND date(month || '-01','+1 month','-1 day')<=? "
                            "AND (published_at IS NULL OR published_at<=?) ORDER BY month DESC LIMIT 1", (code, as_of, as_of)).fetchone()
        it["sitca_monthly"] = dict(month) if month else None
        if month:
            it["sitca_monthly"]["net_amount"] = (round(month["subscriptions"]-month["redemptions"], 6)
                if numeric(month["subscriptions"]) and numeric(month["redemptions"]) else None)
        month_end = None
        if month:
            year, mo = map(int, month["month"].split("-"))
            month_end = date(year, mo, calendar.monthrange(year, mo)[1]).isoformat()
            if numeric(month["beneficiaries"]) and (not it.get("beneficiaries_month") or it["beneficiaries_month"] < month["month"]):
                it["beneficiaries"], it["beneficiaries_month"] = month["beneficiaries"], month["month"]
                it["beneficiaries_source"], it["beneficiaries_source_url"] = month["source"], month["source_url"]
        # 規模與流通單位沿用既有欄位，只提供官方值，並加上各自日期與來源。
        for key in ("aum", "units"):
            candidates = [r for r in snapshots if numeric(r.get(key))]
            chosen = candidates[-1] if candidates else None
            if chosen and (not month or not numeric(month[key]) or chosen["date"] >= month_end):
                it[key], it[key+"_date"], it[key+"_source"], it[key+"_source_url"] = chosen[key], chosen["date"], chosen["source"], chosen["source_url"]
                if key == "aum":
                    it["aum_currency"] = chosen.get("currency")
            elif month and numeric(month[key]):
                it[key], it[key+"_date"], it[key+"_source"], it[key+"_source_url"] = month[key], month_end, month["source"], month["source_url"]
                if key == "aum":
                    it["aum_currency"] = month["currency"]
            else:
                it[key] = it[key+"_date"] = it[key+"_source"] = it[key+"_source_url"] = None
        held = con.execute("SELECT MAX(date) FROM holding WHERE code=? AND date<=? AND source LIKE '%官網%'", (code, as_of)).fetchone()[0]
        holdings = [dict(r) for r in con.execute("SELECT asset_code,asset_name,kind,weight,qty,source,source_url FROM holding WHERE code=? AND date=? AND weight IS NOT NULL ORDER BY weight DESC,asset_code LIMIT 10", (code, held))] if held else []
        it["holdings_top"] = holdings
        it["holdings_date"] = held
        it["holdings_source"] = holdings[0]["source"] if holdings else None
        it["holdings_source_url"] = holdings[0]["source_url"] if holdings else None
    con.row_factory = old_factory
