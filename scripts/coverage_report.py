"""資料完整度盤點：依投信與欄位統計缺值、日期缺漏與來源，輸出可核對的 Markdown 報告（唯讀，不改任何資料）。

- 當天（latest.json）：各投信的 ETF 檔數，以及各欄位有值的檔數；正式淨值與預估淨值分開算，
  規模、單位數、持股依出處分成投信官網、投信投顧公會月資料等
- 日資料（fund_daily）：各來源涵蓋的 ETF、日期範圍，以及依官方開休市日期應有、實際缺少的交易日
- 月資料（fund_monthly、fund_month）：各月份的 ETF 檔數
- 沒有接入官網的投信：列出還只能用交易所或公會資料的 ETF

用法：python scripts/coverage_report.py [--db data/etf.db] [--json web/data/latest.json] [--out 報告.md]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import market_calendar  # noqa: E402
import store  # noqa: E402


def has(value) -> bool:
    return value not in (None, "", [], {})


def origin(source: str | None) -> str:
    """出處分類：投信官網、投信投顧公會、交易所、其他；沒有出處時為「—」。"""
    s = source or ""
    if not s:
        return "—"
    if "官網" in s:
        return "投信官網"
    if "投信投顧公會" in s:
        return "投信投顧公會"
    if "證交所" in s or "交易所" in s or "櫃買" in s:
        return "交易所"
    return "其他"


FIELDS = [  # (欄位標題, 判斷函式)
    ("市價", lambda it: has(it.get("price"))),
    ("正式淨值", lambda it: has(it.get("nav")) and "正式淨值" in (it.get("nav_source") or "")),
    ("預估淨值", lambda it: has(it.get("nav")) and "正式淨值" not in (it.get("nav_source") or "")),
    ("規模（官網日資料）", lambda it: has(it.get("aum")) and origin(it.get("aum_source")) == "投信官網"),
    ("規模（公會月資料）", lambda it: has(it.get("aum")) and origin(it.get("aum_source")) == "投信投顧公會"),
    ("單位數", lambda it: has(it.get("units"))),
    ("單位增減", lambda it: it.get("units_change") is not None),
    ("持股（官網每日）", lambda it: has(it.get("holdings_top")) and origin(it.get("holdings_source")) == "投信官網"),
    ("持股（公會月前十大）", lambda it: has(it.get("holdings_top")) and origin(it.get("holdings_source")) == "投信投顧公會"),
    ("受益人數", lambda it: it.get("beneficiaries") is not None),
    ("費率", lambda it: has(it.get("mgmt_fee_text")) or it.get("mgmt_fee") is not None),
    ("配息紀錄", lambda it: has(it.get("last_ex")) or has(it.get("next_ex"))),
    ("公會月資料", lambda it: has(it.get("monthly"))),
    ("週申贖可算週數 ≥1", lambda it: ((it.get("flows") or {}).get("available_weeks") or 0) >= 1),
]


def today_table(items: list[dict]) -> list[str]:
    by_issuer: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_issuer[it.get("issuer") or "（未知）"].append(it)
    head = ["投信", "檔數"] + [f for f, _ in FIELDS]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    rows = sorted(by_issuer.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for issuer, group in rows + [("合計", items)]:
        cells = [issuer, str(len(group))] + [str(sum(1 for it in group if test(it))) for _, test in FIELDS]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def daily_table(con: sqlite3.Connection, cal: market_calendar.Calendar, as_of: str) -> list[str]:
    rows = con.execute("SELECT source, code, date, nav, units, aum FROM fund_daily WHERE date <= ? ORDER BY source, code, date",
                       (as_of,)).fetchall()
    per: dict[str, dict] = defaultdict(lambda: {"codes": set(), "dates": set(), "nav": 0, "units": 0, "aum": 0, "rows": 0,
                                                 "missing": 0, "expected": 0, "unknown": False})
    by_code: dict[tuple, list[str]] = defaultdict(list)
    for source, code, day, nav, units, aum in rows:
        p = per[source]
        p["codes"].add(code)
        p["dates"].add(day)
        p["rows"] += 1
        p["nav"] += nav is not None
        p["units"] += units is not None
        p["aum"] += aum is not None
        by_code[(source, code)].append(day)
    for (source, code), days in by_code.items():
        expected = cal.trading_days(min(days), max(days))
        if expected is None:
            per[source]["unknown"] = True
            continue
        per[source]["expected"] += len(expected)
        per[source]["missing"] += len(set(expected) - set(days))
    lines = ["| 來源 | ETF 檔數 | 最早 | 最新 | 筆數 | 有淨值 | 有單位數 | 有規模 | 涵蓋期間應有交易日 | 缺少的交易日 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for source, p in sorted(per.items()):
        missing = "日曆未涵蓋" if p["unknown"] else str(p["missing"])
        lines.append(f"| {source} | {len(p['codes'])} | {min(p['dates'])} | {max(p['dates'])} | {p['rows']} | {p['nav']} | "
                     f"{p['units']} | {p['aum']} | {p['expected']} | {missing} |")
    return lines


def monthly_table(con: sqlite3.Connection) -> list[str]:
    lines = ["| 資料 | 月份 | ETF 檔數 |", "|---|---|---|"]
    for label, sql in (("投信投顧公會 ETF 月統計（fund_monthly）", "SELECT month, COUNT(*) FROM fund_monthly GROUP BY month ORDER BY month DESC"),
                       ("投信投顧公會基金資料彙總表（fund_month）", "SELECT month, COUNT(*) FROM fund_month GROUP BY month ORDER BY month DESC")):
        try:
            got = con.execute(sql).fetchall()
        except sqlite3.OperationalError:
            got = []
        if not got:
            lines.append(f"| {label} | — | 0 |")
        for month, n in got[:6]:
            lines.append(f"| {label} | {month} | {n} |")
        if len(got) > 6:
            lines.append(f"| {label} | 更早 {len(got) - 6} 個月（最早 {got[-1][0]}） | — |")
    return lines


def report(con: sqlite3.Connection, payload: dict) -> str:
    items = payload.get("items") or []
    as_of = payload.get("trade_date") or ""
    cal = market_calendar.load(con)
    no_site = sorted((it.get("issuer") or "（未知）", it["code"]) for it in items
                     if origin(it.get("nav_source")) != "投信官網" and origin(it.get("aum_source")) != "投信官網"
                     and origin(it.get("holdings_source")) != "投信官網")
    groups: dict[str, list[str]] = defaultdict(list)
    for issuer, code in no_site:
        groups[issuer].append(code)
    out = [f"# 資料完整度盤點（交易日 {as_of}）", "",
           f"- 產生自 `latest.json`（{payload.get('generated_at') or '—'}，{len(items)} 檔）與資料庫；程式：`scripts/coverage_report.py`。",
           "- 只統計有沒有值與出處，不判斷數字對錯；空值代表來源沒有提供或尚未公布，不代表 0。",
           f"- 官方開休市日期涵蓋年度：{'、'.join(map(str, sorted(cal.years))) or '無'}。", "",
           "## 當天各欄位有值的檔數", ""] + today_table(items) + [
           "", "## 官方日資料（fund_daily）", "",
           "依官方開休市日期，計算每檔從最早到最新日期之間應有的交易日，再數缺少幾天。", ""] + daily_table(con, cal, as_of) + [
           "", "## 月資料", ""] + monthly_table(con) + [
           "", "## 沒有任何官網日資料的 ETF", "",
           "淨值、規模、持股都不是來自投信官網的 ETF，依投信列出。", "",
           "| 投信 | 檔數 | 代號 |", "|---|---|---|"]
    for issuer, codes in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(f"| {issuer} | {len(codes)} | {'、'.join(codes)} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "etf.db")
    ap.add_argument("--json", type=Path, default=ROOT / "web" / "data" / "latest.json")
    ap.add_argument("--history", type=Path, help="歷史 CSV 資料夾，預設為資料庫旁的 history/")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    con = store.open_db(args.db, args.history or args.db.parent / "history")
    try:
        text = report(con, json.loads(args.json.read_text("utf-8")))
    finally:
        con.close()
    if args.out:
        args.out.write_text(text, "utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
