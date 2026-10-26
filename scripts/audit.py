"""資料查核：交叉核對網站上的數字和原始來源（唯讀，不改資料、不連網路）。

    python scripts/audit.py                    # 查 latest.json 的交易日
    python scripts/audit.py --all              # 查資料庫裡的全部日期
    python scripts/audit.py --json audit.json  # 另存機器可讀的結果

- 「阻擋」：任何資料合約下都不該發生的錯誤（收盤價和交易所官方收盤價不同、資料日期晚於交易日、
  瀏覽器無法解析）。有任何一項不符時結束代碼為 1，可當發布前檢查
- 「提醒」：可能是來源之間的正常差異或已知的歷史問題，列出例子交給人工確認，不影響結束代碼
- 資料庫檔不存在時，從 data/history 重建到暫存檔，不改 data/
"""
from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import market_calendar
import store
import market_status

ROOT = Path(__file__).resolve().parent.parent
TW = timezone(timedelta(hours=8))
TICK = 0.01  # MIS 的淨值多半只到小數兩位、四位小數的淨值無條件捨去，相差不到一分可能是同一個數字
BLOCK, WARN = "阻擋", "提醒"
CHECKS = []


def check(key: str, title: str, level: str = WARN, note: str = ""):
    def register(fn):
        CHECKS.append({"key": key, "title": title, "level": level, "note": note, "fn": fn})
        return fn
    return register


class Context:
    def __init__(self, con: sqlite3.Connection, payload: dict, text: str, dates: list[str] | None):
        """dates 是要查的日期；None 代表資料庫裡的全部日期。"""
        self.con, self.payload, self.text, self.dates = con, payload, text, dates
        self.items = payload.get("items") or []
        self.trade_date = payload.get("trade_date") or ""
        self.cal = market_calendar.load(con)
        self.days = [d for (d,) in con.execute("SELECT DISTINCT date FROM daily ORDER BY date")]
        self.scan = self.days if dates is None else dates

    def rows(self, sql: str, column: str = "d.date") -> list[tuple]:
        """sql 裡的 {scope} 換成「column 在查核日期內」。"""
        if self.dates is None:
            return self.con.execute(sql.format(scope="1 = 1")).fetchall()
        marks = ", ".join("?" * len(self.dates)) or "NULL"
        return self.con.execute(sql.format(scope=f"{column} IN ({marks})"), self.dates).fetchall()

    def previous(self, day: str) -> str | None:
        """資料庫裡 day 的前一個營業日；和官方開休市日期對不上（缺資料或臨時休市）時回傳 None，不拿更早的日子比。"""
        earlier = [d for d in self.days if d < day]
        if not earlier:
            return None
        official = self.cal.previous_trading_day(day)
        return earlier[-1] if official in (None, earlier[-1]) else None

    def value(self, sql: str, *args):
        row = self.con.execute(sql, args).fetchone()
        return row[0] if row else None


# ---------- 阻擋：任何資料合約下都不該發生 ----------

@check("J1", "latest.json 沒有 NaN、Infinity（瀏覽器無法解析）", BLOCK)
def json_constants(ctx):
    found = []
    json.loads(ctx.text, parse_constant=found.append)
    return 1, found


@check("J2", "資料日期不晚於網站交易日", BLOCK)
def no_future_dates(ctx):
    checked, bad = 0, []
    for it in ctx.items:
        for field in ("price_date", "nav_date", "aum_date", "units_date", "holdings_date"):
            value = it.get(field)
            if isinstance(value, str) and value:
                checked += 1
                if value[:10] > ctx.trade_date:  # 公會的月底前十大只有年月（YYYY-MM），字串比較同樣成立
                    bad.append((it["code"], field, value))
        month = it.get("beneficiaries_month")
        if isinstance(month, str) and month:
            checked += 1
            if month > ctx.trade_date[:7]:
                bad.append((it["code"], "beneficiaries_month", month))
    return checked, bad


@check("J3", "網站交易日真的有交易（官方日曆不是休市、成交量不是全部 0）", BLOCK)
def trade_date_traded(ctx):
    """2026-10-23 沒有交易，Yahoo 卻有一根成交量 0 的日線，網站把那天當成交易日、358 檔全部顯示持平。"""
    volumes = [it.get("volume_lots") for it in ctx.items if it.get("volume_lots") is not None]
    bad = []
    if ctx.cal.is_open(ctx.trade_date) is False:
        bad.append((ctx.trade_date, "官方日曆休市"))
    if len(ctx.items) >= 10 and not any(market_status.positive(v) for v in volumes):
        bad.append((ctx.trade_date, "全市場樣本沒有可驗證的正成交量（零或缺值）"))
    return 1, bad


@check("P1", "收盤價＝交易所官方收盤價", BLOCK)
def close_matches(ctx):
    rows = ctx.rows("""SELECT d.date, d.code, d.price, e.close FROM daily d
                       JOIN exchange_daily e ON e.code = d.code AND e.date = d.date
                       WHERE d.price IS NOT NULL AND e.close IS NOT NULL AND {scope}""")
    return len(rows), [r for r in rows if abs(r[2] - r[3]) > 0.005 + 1e-9]


# ---------- 提醒：來源之間的差異與已知問題，交給人工確認 ----------

@check("V1", "成交量＝交易所官方成交股數 ÷ 1000", note="改用 Yahoo 或其他來源補值時會不同")
def volume_matches(ctx):
    rows = ctx.rows("""SELECT d.date, d.code, d.volume_lots, e.volume_shares FROM daily d
                       JOIN exchange_daily e ON e.code = d.code AND e.date = d.date
                       WHERE d.volume_lots IS NOT NULL AND e.volume_shares IS NOT NULL AND {scope}""")
    return len(rows), [r for r in rows if abs(r[2] - r[3] / 1000) > 0.5 + 1e-9]


@check("C1", "漲跌幅＝交易所漲跌價差 ÷ 參考價",
       note="除息、除權日交易所沒有提供相對參考價的漲跌（證交所記 0、櫃買中心空白），網站應留空")
def change_matches(ctx):
    paid = {(c, d) for c, d in ctx.con.execute("SELECT code, ex_date FROM dividend")}
    rows = ctx.rows("""SELECT d.date, d.code, d.change_pct, e.close, e.change FROM daily d
                       JOIN exchange_daily e ON e.code = d.code AND e.date = d.date
                       WHERE d.change_pct IS NOT NULL AND e.close IS NOT NULL AND {scope}""")
    bad = []
    for day, code, pct, close, change in rows:
        if change is None or close - change <= 0 or ((code, day) in paid and change == 0):
            bad.append((day, code, round(pct, 4), "交易所沒有漲跌"))
        elif abs(pct - change / (close - change) * 100) > 0.01:
            bad.append((day, code, round(pct, 4), round(change / (close - change) * 100, 4)))
    return len(rows), bad


@check("PR1", "折溢價＝（收盤價 − 淨值）÷ 淨值", note="用證交所 MIS 的預估折溢價（MIS 市價而非收盤價）時會不同")
def premium_matches(ctx):
    rows = ctx.rows("""SELECT d.date, d.code, d.price, d.nav, d.premium FROM daily d
                       WHERE d.price > 0 AND d.nav > 0 AND d.premium IS NOT NULL AND {scope}""")
    bad = [(day, code, price, nav, premium) for day, code, price, nav, premium in rows
           if abs((price - nav) / nav * 100 - premium) > 0.01]
    return len(rows), bad


@check("N1", "標示投信正式淨值的數字＝投信公告同一天的淨值", note="投信事後更正或不同頁面數字不同時會發生")
def issuer_nav_matches(ctx):
    rows = ctx.rows("""SELECT d.date, d.code, d.nav, f.nav FROM daily d JOIN fund_daily f ON f.code = d.code AND f.date = d.date
                       WHERE d.nav IS NOT NULL AND f.nav IS NOT NULL AND d.nav_source LIKE '%官網（正式淨值）' AND {scope}""")
    return len(rows), [r for r in rows if abs(r[2] - r[3]) > 0.00005]


@check("N2", "前一營業日淨值＝前一營業日的正式淨值",
       note="容許 MIS 小數兩位無條件捨去的差距；MIS 隔天早上會先換成當天的淨值，2026-10-23 前的舊資料有錯一天的列")
def nav_prev_matches(ctx):
    checked, bad = 0, []
    for day in ctx.scan:
        prev = ctx.previous(day)
        if not prev:
            continue
        official = dict(ctx.con.execute("SELECT code, nav_official FROM daily WHERE date = ? AND nav_official IS NOT NULL", (prev,)))
        same = dict(ctx.con.execute("SELECT code, nav_official FROM daily WHERE date = ? AND nav_official IS NOT NULL", (day,)))
        for code, nav_prev in ctx.con.execute("SELECT code, nav_prev FROM daily WHERE date = ? AND nav_prev IS NOT NULL", (day,)):
            if code in official:
                checked += 1
                if abs(official[code] - nav_prev) >= TICK - 1e-9:
                    bad.append((day, code, nav_prev, official[code], "等於當天的淨值" if code in same and abs(same[code] - nav_prev) < TICK else ""))
    return checked, bad


@check("N3", "疑似沿用前一天的正式淨值",
       note="連續兩個營業日正式淨值完全相同、投信官網沒有當天的公告，而且收盤價變動超過 1%（海外市場休市時可能是正常的）")
def stale_official_nav(ctx):
    checked, bad = 0, []
    for day in ctx.scan:
        prev = ctx.previous(day)
        if not prev:
            continue
        for code, nav, price, prev_nav, prev_price in ctx.con.execute(
                """SELECT d.code, d.nav_official, d.price, p.nav_official, p.price FROM daily d
                   JOIN daily p ON p.code = d.code AND p.date = ?
                   WHERE d.date = ? AND d.nav_official IS NOT NULL AND p.nav_official IS NOT NULL
                   AND NOT EXISTS(SELECT 1 FROM fund_daily f WHERE f.code = d.code AND f.date = d.date AND f.nav IS NOT NULL)""",
                (prev, day)):
            checked += 1
            if abs(nav - prev_nav) < 1e-9 and price and prev_price and abs(price / prev_price - 1) > 0.01:
                bad.append((day, code, nav, f"收盤價 {prev_price} → {price}"))
    return checked, bad


@check("N4", "除息前一天的正式淨值疑似已先扣掉配息",
       note="正式淨值加上配息剛好等於證交所轉載的前一營業日淨值（2026-10-21 元大 0056：56.97 ＋ 1.72 ＝ 58.69）")
def pre_distribution_nav(ctx):
    checked, bad = 0, []
    for day in ctx.scan:
        prev = ctx.previous(day)
        if not prev:
            continue
        for code, cash, nav_prev, official in ctx.con.execute(
                """SELECT v.code, v.cash, d.nav_prev, p.nav_official FROM dividend v
                   JOIN daily d ON d.code = v.code AND d.date = v.ex_date
                   JOIN daily p ON p.code = v.code AND p.date = ?
                   WHERE v.ex_date = ? AND v.cash > 0 AND d.nav_prev IS NOT NULL AND p.nav_official IS NOT NULL""", (prev, day)):
            checked += 1
            if abs(official + cash - nav_prev) < TICK and abs(official - nav_prev) >= TICK:
                bad.append((prev, code, official, f"配息 {cash}", f"證交所轉載 {nav_prev}"))
    return checked, bad


@check("PO1", "台股股票型 ETF 的正式折溢價超過 ±3%", note="台股股票型通常在 1% 以內，超過多半是淨值日期或配息處理的問題")
def premium_outliers(ctx):
    domestic = {it["code"] for it in ctx.items
                if (it.get("product") or {}).get("region") == "臺灣" and (it.get("product") or {}).get("asset_class") == "股票"}
    rows = [r for r in ctx.rows("""SELECT d.date, d.code, d.price, d.nav_official, d.premium_official FROM daily d
                                   WHERE d.premium_official IS NOT NULL AND {scope}""") if r[1] in domestic]
    return len(rows), [(day, code, price, nav, round(p, 2)) for day, code, price, nav, p in rows if abs(p) > 3]


@check("A4", "每日行情表的規模＝投信公告同一天的規模", note="2026-10-23 前的舊資料會把別天的規模寫進來")
def aum_matches(ctx):
    rows = ctx.rows("""SELECT d.date, d.code, d.aum, f.aum FROM daily d JOIN fund_daily f ON f.code = d.code AND f.date = d.date
                       WHERE d.aum IS NOT NULL AND f.aum IS NOT NULL AND {scope}""")
    return len(rows), [r for r in rows if abs(r[2] - r[3]) > 0.011]


@check("A5", "每日行情表沒有推算的規模（只放官方數字）", note="舊版把「發行單位數 × 淨值」的推算值存進資料庫，網站沒有顯示")
def no_estimated_aum(ctx):
    rows = ctx.rows("SELECT d.date, d.code, d.aum, d.aum_source FROM daily d WHERE d.aum IS NOT NULL AND {scope}")
    return len(rows), [r for r in rows if "推算" in (r[3] or "")]


@check("C2", "投信公告：規模 ≈ 單位數 × 淨值（±1%）", note="外幣計價、規模含其他級別或除息前一天時可能不同")
def aum_consistent(ctx):
    rows = ctx.rows("""SELECT f.date, f.code, f.currency, f.aum, f.units, f.nav FROM fund_daily f
                       WHERE f.nav > 0 AND f.aum > 0 AND f.units > 0 AND {scope}""", "f.date")
    return len(rows), [(day, code, cur, round(aum * 1e8 / (units * nav), 4)) for day, code, cur, aum, units, nav in rows
                       if abs(aum * 1e8 / (units * nav) - 1) > 0.01]


@check("C3", "完整持股清單的權重合計在 90%～110%", note="期貨、反向型或只列部分資產的清單會超出")
def holding_weights(ctx):
    rows = ctx.rows("SELECT h.date, h.code, h.weight_sum, h.rows FROM holding_list h WHERE h.weight_sum IS NOT NULL AND {scope}", "h.date")
    return len(rows), sorted((r for r in rows if not 90 <= r[2] <= 110), key=lambda r: r[2])


@check("F1", "週申贖估算＝逐日（單位數增減 × 同日淨值）加總", note="網站 flows 與投信逐日資料重算的結果不同")
def flows_recompute(ctx):
    checked, bad = 0, []
    for it in ctx.items:
        flows = it.get("flows") or {}
        net, base, end = flows.get("net_amount"), flows.get("baseline"), flows.get("end")
        if not isinstance(net, (int, float)) or not base or not end:
            continue
        days = dict(((d, (n, u)) for d, n, u in ctx.con.execute(
            "SELECT date, nav, units FROM fund_daily WHERE code = ? AND date BETWEEN ? AND ? AND nav > 0 AND units > 0",
            (it["code"], base, end))))
        trading = ctx.cal.trading_days(base, end) or sorted(days)
        if not trading or trading[0] != base or any(d not in days for d in trading):
            continue
        total = sum((days[b][1] - days[a][1]) * days[b][0] for a, b in zip(trading, trading[1:])) / 1e8
        checked += 1
        if abs(total - net) > 0.01:
            bad.append((it["code"], round(net, 4), round(total, 4)))
    return checked, bad


@check("X1", "投信月底淨值、單位數＝投信投顧公會月底資料", note="公會彙總表的單位數或淨值和投信公告不同")
def month_end_matches(ctx):
    checked, bad = 0, []
    for month, in ctx.con.execute("SELECT DISTINCT month FROM fund_month"):
        year, mo = map(int, month.split("-"))
        nxt = date(year + mo // 12, mo % 12 + 1, 1).isoformat()
        last = ctx.cal.previous_trading_day(nxt) or f"{month}-{calendar.monthrange(year, mo)[1]:02d}"
        for code, nav, units, f_nav, f_units in ctx.con.execute(
                """SELECT m.code, m.nav, m.units, f.nav, f.units FROM fund_month m JOIN fund_daily f ON f.code = m.code AND f.date = ?
                   WHERE m.month = ?""", (last, month)):
            if nav and f_nav:
                checked += 1
                if abs(nav - f_nav) > 0.005:
                    bad.append((last, code, "淨值", f_nav, nav))
            if units and f_units:
                checked += 1
                if abs(units - f_units) > 0.5:
                    bad.append((last, code, "單位數", f_units, units))
    return checked, bad


@check("E1", "交易所參考價＝前一個收盤價（扣除已知配息）", note="不符代表有沒列入資料庫的配息、分割，或交易所資料問題")
def reference_price(ctx):
    paid = {(c, d) for c, d in ctx.con.execute("SELECT code, ex_date FROM dividend")}
    rows = ctx.rows("""SELECT e.date, e.code, e.close, e.change, p.close FROM exchange_daily e
                       JOIN exchange_daily p ON p.code = e.code
                        AND p.date = (SELECT MAX(x.date) FROM exchange_daily x WHERE x.code = e.code AND x.date < e.date)
                       WHERE e.close IS NOT NULL AND e.change IS NOT NULL AND p.close IS NOT NULL AND {scope}""", "e.date")
    return len(rows), [r for r in rows if abs(r[4] - (r[2] - r[3])) > 0.051 and (r[1], r[0]) not in paid]


@check("D1", "官方收盤價單日漲跌超過 10%（待人工確認）", note="分割、合併、除息或交易所資料問題")
def close_jumps(ctx):
    rows = ctx.rows("""SELECT e.date, e.code, p.close, e.close FROM exchange_daily e
                       JOIN exchange_daily p ON p.code = e.code
                        AND p.date = (SELECT MAX(x.date) FROM exchange_daily x WHERE x.code = e.code AND x.date < e.date)
                       WHERE e.close > 0 AND p.close > 0 AND {scope}""", "e.date")
    return len(rows), [r for r in rows if abs(r[3] / r[2] - 1) > 0.10]


@check("D2", "投信淨值單日變動超過 10% 或單位數變動超過 50%（待人工確認）", note="大額配息、分割或投信資料問題")
def fund_jumps(ctx):
    rows = ctx.rows("""SELECT f.date, f.code, p.nav, f.nav, p.units, f.units FROM fund_daily f
                       JOIN fund_daily p ON p.code = f.code
                        AND p.date = (SELECT MAX(x.date) FROM fund_daily x WHERE x.code = f.code AND x.date < f.date AND x.nav > 0)
                       WHERE f.nav > 0 AND p.nav > 0 AND f.units > 0 AND p.units > 0 AND {scope}""", "f.date")
    return len(rows), [r for r in rows if abs(r[3] / r[2] - 1) > 0.10 or abs(r[5] / r[4] - 1) > 0.5]


# ---------- 執行與輸出 ----------

def run(ctx: Context, examples: int = 5) -> list[dict]:
    results = []
    for c in CHECKS:
        try:
            checked, bad = c["fn"](ctx)
            error = None
        except sqlite3.Error as exc:  # 舊資料庫缺表：記下來，不讓整份查核中斷
            checked, bad, error = 0, [], str(exc)
        results.append({"key": c["key"], "title": c["title"], "level": c["level"], "note": c["note"], "checked": checked,
                        "failed": len(bad), "error": error, "examples": [list(map(str, b)) if isinstance(b, tuple) else [str(b)]
                                                                           for b in bad[:examples]]})
    return results


def report(results: list[dict], trade_date: str, scope: str) -> str:
    lines = [f"微風流動資料查核：網站交易日 {trade_date}，範圍 {scope}"]
    for r in results:
        if r["error"]:
            status = f"無法查核（{r['error']}）"
        elif r["failed"]:
            status = f"檢查 {r['checked']} 筆，不符 {r['failed']} 筆"
        else:
            status = f"檢查 {r['checked']} 筆，通過"
        lines.append(f"[{r['level']}] {r['key']} {r['title']}：{status}")
        if r["failed"] and r["note"]:
            lines.append(f"      說明：{r['note']}")
        lines += [f"      例：{'、'.join(e)}" for e in r["examples"]]
    blocked = sum(1 for r in results if r["level"] == BLOCK and (r["failed"] or r["error"]))
    warned = sum(1 for r in results if r["level"] == WARN and r["failed"])
    lines.append(f"結果：阻擋項目 {blocked} 項不符；提醒項目 {warned} 項需人工確認（共 {len(results)} 項）")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "etf.db")
    ap.add_argument("--history", type=Path, default=ROOT / "data" / "history", help="資料庫檔不存在時用來重建的歷史 CSV")
    ap.add_argument("--latest", type=Path, default=ROOT / "web" / "data" / "latest.json")
    ap.add_argument("--all", action="store_true", help="查資料庫裡的全部日期（預設只查網站交易日）")
    ap.add_argument("--json", type=Path, help="另存查核結果")
    ap.add_argument("--examples", type=int, default=5, help="每項列出的例子數")
    args = ap.parse_args(argv)
    text = args.latest.read_text("utf-8")
    payload = json.loads(text)
    with tempfile.TemporaryDirectory() as tmp:
        if args.db.exists():
            con = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            con = store.open_db(Path(tmp) / "audit.db", args.history)
        try:
            ctx = Context(con, payload, text, None if args.all else [payload.get("trade_date")])
            results = run(ctx, args.examples)
        finally:
            con.close()
    scope = f"全部日期（每日行情 {len(ctx.days)} 天）" if args.all else "網站交易日"
    print(report(results, payload.get("trade_date") or "—", scope))
    if args.json:
        args.json.write_text(json.dumps({"generated_at": datetime.now(TW).isoformat(timespec="seconds"),
                                         "trade_date": payload.get("trade_date"), "scope": scope, "checks": results},
                                        ensure_ascii=False, indent=1), "utf-8")
    return 1 if any(r["level"] == BLOCK and (r["failed"] or r["error"]) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
