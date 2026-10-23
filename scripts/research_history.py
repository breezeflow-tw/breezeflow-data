"""十年研究資料庫：離線匯入核實的資料包，發布時輸出每檔 ETF 的歷史。

不自動抓歷史查詢頁、不把 Yahoo 或淨值冒充官方市價；缺漏保留，回測需另有完整事件證據。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import market_calendar
import official_quotes
import store

YEARS = 10
EXCLUDED = {"00847B"}
CODE = re.compile(r"00[0-9A-Z]{2,6}")


def iso(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期必須為 YYYY-MM-DD")
    return value


def years_before(day, years=YEARS):
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:
        return d.replace(year=d.year - years, day=28).isoformat()


def days(start, end):
    d, stop = date.fromisoformat(start), date.fromisoformat(end)
    while d <= stop:
        yield d.isoformat()
        d += timedelta(days=1)


def number(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("數值不得缺漏、非數字或無限大")
    if value < 0 or (not zero and value == 0):
        raise ValueError("價格／比例須大於零；金額與成交量不得為負")
    return value


def candle(row):
    vals = [number(row[k]) for k in ("open", "high", "low", "close")]
    o, h, low, c = vals
    if not low <= min(o, c) <= max(o, c) <= h:
        raise ValueError("開高低收互相矛盾")
    number(row["volume"], zero=True)
    return row


def https(value):
    p = urlparse(value if isinstance(value, str) else "")
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("出處及授權證據必須是完整 HTTPS 網址")
    return value


def import_package(con, path, as_of, *, allow_test=False):
    """整包驗證後才寫入；相同檔案冪等，不接受無證據的資料包或靜默覆蓋歷史。"""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if con.execute("SELECT 1 FROM research_import WHERE sha256=?", (digest,)).fetchone():
        return False
    obj = json.loads(raw)
    source = obj.get("source", {})
    if source.get("test_only") and not allow_test:
        raise ValueError("測試資料禁止匯入正式研究資料庫")
    if obj.get("version") != 1 or source.get("status") != "verified":
        raise ValueError("資料包版本不符或來源尚未核實")
    for key in ("id", "provider", "dataset", "license", "reviewed_by"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            raise ValueError(f"來源缺少 {key}")
    for key in ("url", "acquisition_evidence", "redistribution_evidence"):
        https(source.get(key))
    iso(as_of)
    # 查核可晚於行情截止日（例如週末審查週五資料），只禁止真正的未來日期。
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    if iso(source.get("checked_at")) > today:
        raise ValueError("來源查核日期在未來")
    sid = source["id"]
    if sid in {s["dataset_id"] for s in official_quotes.DATASETS.values()}:
        raise ValueError("不得使用保留的每日官方來源 ID")
    tables = {"prices": [], "actions": [], "coverage": []}
    seen = set()
    for kind in tables:
        if not isinstance(obj.get(kind, []), list):
            raise ValueError("資料區段必須為陣列")
        for row in obj.get(kind, []):
            code = row.get("code", "")
            if not CODE.fullmatch(code) or code in EXCLUDED:
                raise ValueError("不接受此證券代號")
            if kind == "prices":
                if source.get("currency") not in ("TWD", "USD", "CNY", "HKD", "JPY", "EUR"):
                    raise ValueError("價量資料須明示交易幣別")
                candle(row)
                day = iso(row["date"])
                if row.get("market") not in ("上市", "上櫃"):
                    raise ValueError("未知市場")
                record = (code, day, row["market"], row["open"], row["high"], row["low"], row["close"], row["volume"], sid)
                key = (kind, code, day)
            elif kind == "actions":
                day = iso(row["date"])
                action = row.get("kind")
                if action == "dividend":
                    number(row.get("cash"), zero=True)
                    if iso(row.get("pay_date")) < day:
                        raise ValueError("發放日不得早於除息日")
                    if source.get("currency") not in ("TWD", "USD", "CNY", "HKD", "JPY", "EUR"):
                        raise ValueError("配息須明示幣別")
                elif action == "split":
                    number(row.get("ratio"))
                    if row["ratio"] == 1:
                        raise ValueError("分割比例不能為一")
                else:
                    raise ValueError("只接受已支援的配息與分割事件；其他事件須另行處理")
                record = (code, day, action, row.get("cash") if action == "dividend" else None,
                          row.get("pay_date") if action == "dividend" else None,
                          row.get("ratio") if action == "split" else None, sid)
                key = (kind, code, day, action)
            else:
                start, day = iso(row["start"]), iso(row["end"])
                if start > day or row.get("kind") not in ("calendar", "actions"):
                    raise ValueError("完整度區間不合法")
                if row.get("complete") is not True:
                    raise ValueError("完整度證據必須明確核實；空事件清單不等於沒有事件")
                dates = row.get("trading_days")
                if row["kind"] == "calendar":
                    if not isinstance(dates, list) or dates != sorted(set(dates)):
                        raise ValueError("交易日必須排序且不重複")
                    if any(not start <= iso(d) <= day for d in dates):
                        raise ValueError("交易日超出核實區間")
                record = (code, row["kind"], start, day, json.dumps(dates) if row["kind"] == "calendar" else None, sid)
                key = (kind, code, row["kind"], start, day)
            if day > as_of or key in seen:
                raise ValueError("資料在未來或同包主鍵重複")
            seen.add(key)
            tables[kind].append(record)
    if not any(tables.values()):
        raise ValueError("資料包沒有紀錄")
    mapping = {"prices": ("research_price", (0, 1)), "actions": ("research_action", (0, 1, 2)),
               "coverage": ("research_coverage", (0, 1, 2, 3))}
    with con:
        metadata = json.dumps(source, ensure_ascii=False, sort_keys=True)
        old = con.execute("SELECT metadata_json FROM research_source WHERE id=?", (sid,)).fetchone()
        if old and old[0] != metadata:
            raise ValueError("來源 ID 已存在且內容不同；更正須另案檢查")
        con.execute("INSERT OR IGNORE INTO research_source VALUES (?,?)", (sid, metadata))
        for kind, records in tables.items():
            table, indices = mapping[kind]
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            for record in records:
                where = " AND ".join(cols[i] + "=?" for i in indices)
                old = con.execute(f"SELECT * FROM {table} WHERE {where}", tuple(record[i] for i in indices)).fetchone()
                if old and tuple(old) != record:
                    raise ValueError(f"{kind} 與既有資料衝突，整包撤回")
                con.execute(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' for _ in record)})", record)
        con.execute("INSERT INTO research_import VALUES (?,?,?)", (digest, sid, datetime.now(timezone.utc).isoformat()))
    return True


def import_pending(con, folder, as_of):
    return sum(import_package(con, path, as_of) for path in sorted(Path(folder).glob("*.json")))


def rows(con, sql, params=()):
    cur = con.execute(sql, params)
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur]


def build(con, item, as_of, years=YEARS):
    code = item["code"]
    target = years_before(as_of, years)
    listed = item.get("listed_date")
    try:
        listed = iso(listed)
    except (ValueError, TypeError):
        listed = None
    start = max(target, listed or target)
    currency = (item.get("product") or {}).get("trading_currency")
    sources = {sid: json.loads(meta) for sid, meta in con.execute("SELECT id, metadata_json FROM research_source")}
    prices, issues, used = {}, [], set()
    for r in rows(con, "SELECT * FROM research_price WHERE code=? AND date BETWEEN ? AND ? ORDER BY date", (code, start, as_of)):
        src = sources[r["source_id"]]
        if src.get("currency") != currency or r["market"] != item.get("market"):
            issues.append({"date": r["date"], "reason": "匯入行情市場／幣別不符"})
            continue
        r["source"] = r.pop("source_id")
        prices[r["date"]] = r
        used.add(r["source"])
    for r in rows(con, "SELECT * FROM exchange_daily WHERE code=? AND date BETWEEN ? AND ? ORDER BY date", (code, start, as_of)):
        spec = official_quotes.DATASETS.get(r["market"])
        if not spec or r["source_url"] != spec["source_url"] or r["dataset"] != spec["dataset"]:
            issues.append({"date": r["date"], "reason": "價量來源待確認"})
            continue
        bar = {k: r[k] for k in ("date", "open", "high", "low", "close")}
        bar.update(volume=r["volume_shares"], source=spec["dataset_id"])
        try:
            candle(bar)
        except ValueError:
            issues.append({"date": r["date"], "reason": "缺少完整開高低收／成交量"})
            prices.pop(r["date"], None)
            continue
        old = prices.get(r["date"])
        if old and any(old[k] != bar[k] for k in ("open", "high", "low", "close", "volume")):
            issues.append({"date": r["date"], "reason": "官方日資料與匯入資料矛盾"})
            prices.pop(r["date"], None)
            continue
        prices[r["date"]] = bar
        used.add(bar["source"])
        sources[bar["source"]] = {**{k: spec[k] for k in ("provider", "dataset", "dataset_url", "source_url")},
                                  "license": official_quotes.LICENSE, "license_url": official_quotes.LICENSE_URL}
    coverage = rows(con, "SELECT * FROM research_coverage WHERE code=? AND end>=? AND start<=?", (code, start, as_of))
    for c in coverage:
        c["dates"] = set(json.loads(c["trading_days_json"])) if c["kind"] == "calendar" else set()
    actions = rows(con, "SELECT * FROM research_action WHERE code=? AND date BETWEEN ? AND ? ORDER BY date,kind", (code, start, as_of))
    for action in actions:
        used.add(action["source_id"])
        if action["kind"] == "dividend" and sources[action["source_id"]].get("currency") != currency:
            issues.append({"date": action["date"], "reason": "配息幣別不符"})
    cal = market_calendar.load(con)
    status = {}
    expected, unknown, event_missing = [], [], []
    for day in days(start, as_of):
        claims = [day in c["dates"] for c in coverage
                  if c["kind"] == "calendar" and c["start"] <= day <= c["end"]]
        opened = claims[0] if claims and len(set(claims)) == 1 else cal.is_open(day) if not claims else None
        if len(set(claims)) > 1:
            issues.append({"date": day, "reason": "交易日曆證據矛盾"})
        if day in prices and opened is False:
            issues.append({"date": day, "reason": "行情與交易日曆矛盾"})
        status[day] = opened
        if opened is None:
            unknown.append(day)
        elif opened:
            expected.append(day)
        if not any(c["kind"] == "actions" and c["start"] <= day <= c["end"] for c in coverage):
            event_missing.append(day)
    for c in coverage:
        used.add(c["source_id"])
    missing = [d for d in expected if d not in prices]
    bars = [{k: r[k] for k in ("date", "open", "high", "low", "close", "volume", "source")} for _, r in sorted(prices.items())]
    # 原始價格跳動只作攔截；絕不推算分割比例。完整事件仍須有證據。
    for a, b in zip(bars, bars[1:]):
        if abs(b["close"] / a["close"] - 1) > .35 and not any(x["date"] == b["date"] for x in actions):
            issues.append({"date": b["date"], "reason": "大幅價差需核對公司行動"})
    ready = []
    run = None
    issue_dates = {i["date"] for i in issues}
    event_missing_set = set(event_missing)
    for day in days(start, as_of):
        ok = listed is not None and bool(currency) and status[day] is not None and day not in event_missing_set and day not in issue_dates and (not status[day] or day in prices)
        if ok:
            run = run or {"start": day, "end": day}
            run["end"] = day
        elif run:
            ready.append(run)
            run = None
    if run:
        ready.append(run)
    for sid in used:
        source_days = [b["date"] for b in bars if b["source"] == sid]
        source_days += [a["date"] for a in actions if a["source_id"] == sid]
        if source_days:
            sources[sid].update(data_start=min(source_days), data_end=max(source_days))
    return {"version": 1, "code": code, "name": item.get("name"), "as_of": as_of, "currency": currency,
            "target_years": years, "target_start": target, "required_start": start, "listed_date": listed,
            "price_basis": "交易所原始市價，未還原", "bars": bars, "actions": actions,
            "trading_days": expected, "unknown_calendar_dates": unknown, "missing_price_dates": missing,
            "missing_action_dates": event_missing, "issues": issues, "ready_ranges": ready,
            "available_start": bars[0]["date"] if bars else None, "available_end": bars[-1]["date"] if bars else None,
            "sources": [{"id": sid, **sources[sid]} for sid in sorted(used)],
            "ten_year_complete": bool(ready and ready[0]["start"] == start and ready[0]["end"] == as_of and listed and listed <= target)}


def export(con, items, as_of, folder, years=YEARS, *, allow_test=False):
    """依內容雜湊命名，避免瀏覽器混用不同發布批次；只有點開的 ETF 需要下載歷史。"""
    folder = Path(folder)
    if not allow_test and any(json.loads(meta).get("test_only") for (meta,) in con.execute("SELECT metadata_json FROM research_source")):
        raise ValueError("測試來源不得發布為正式歷史資料")
    folder.mkdir(parents=True, exist_ok=True)
    index = {"version": 1, "target_years": years, "as_of": as_of, "items": {}}
    for item in items:
        code = item["code"]
        if not CODE.fullmatch(code) or code in EXCLUDED:
            continue
        record = build(con, item, as_of, years)
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:16]
        filename = f"{code}-{digest}.json"
        (folder / filename).write_bytes(raw)
        index["items"][code] = {"file": filename, "bars": len(record["bars"]), "available_start": record["available_start"],
                                "available_end": record["available_end"], "ten_year_complete": record["ten_year_complete"]}
    (folder / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return index


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--history", type=Path, required=True)
    p.add_argument("--as-of", type=iso, required=True)
    a = p.parse_args()
    with store.open_db(a.db, a.history) as db:
        imported = import_package(db, a.package, a.as_of)
        store.dump(db, a.history)
    print("已匯入" if imported else "相同資料包已處理")
