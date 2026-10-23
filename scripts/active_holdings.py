"""主動式 ETF 持股資金動向（latest.json 的 active_holdings，第 1 版）。

估算口徑（使用者已接受）：股數增減 × 當日官方收盤價。這不是經理人的實際成交金額，也不是扣除申購買回後的
主動買賣；每萬單位持股（unit_qty）只是配置密度，不代表實際買賣。

- 範圍：主動式 ETF（代號結尾 A、D）在投信官網每天公告的完整持股清單（申購買回清單）裡的台股股票（代號 4 碼）。
  債券、期貨、基金、外國股票不算；投信投顧公會的月前十大不是完整清單，不用，也不能拿來推斷退出
- 日值：相鄰兩個交易日（依證交所開休市日期）的完整清單逐檔比較股數，股數增減 × 當日官方收盤價
  （stock_close，資料集 11549、11370），單位億元。清單沒列出的股票是未持有（股數 0）；清單列了卻缺股數的不估
- 期間：1d 是 as_of 當天；1w、1m 的期初（baseline）是 as_of 往前 7 天、1 個月那天或之前最近的交易日（與區間報酬相同），
  逐日加總 baseline 之後到 as_of 的日值。每檔 ETF 要有 baseline 到 as_of 每個交易日的清單，否則該期間整檔排除，
  不把相隔數日的兩份清單當成一天
- as_of：最近一個已保存上市官方收盤價的交易日，三個期間共用。官方股價從開始保存那天起累積、不回補；
  期間內有交易日沒有保存股價時，該期間為「資料不足」，不出排行（stocks 為空陣列）
- 金額：buy_amount、sell_amount 是逐日日值的正值加總、負值絕對值加總，net_amount＝buy_amount−sell_amount。
  有股數增減卻缺有效股價、或有 ETF 的部分被排除時，該股票 complete 為 false、金額為 null，不當成 0；
  檔數與股數仍保留可靠的部分。buy_etfs、sell_etfs 是期間淨股數增加、減少的 ETF 檔數
- 疑似公司行動：同一天持有同一檔股票的 ETF（至少兩檔）全部以相同比例增減（除權、分割、減資；有持有者股數沒變就不算），
  或股數剛好照除權預告的配股率增加（配股入帳），該股票在該期間整檔排除；同一檔 ETF 同一天刪除一檔、新增一檔且股數相同，視為疑似代碼變更，
  排除這檔 ETF 的這兩筆。排除的都列在 excluded_stocks。另排除單一 ETF 的重大股數跳變及股數／股價反向調整；
  異常偵測不是完整公司行動清冊，仍可能漏掉小幅、延後入帳的事件
- 證據：每個期間附上列入 ETF 期初與期末清單的出處與完整度（列數、台股列數、缺股數列數、權重合計、取得時間），
  每檔股票附上用到的股價日期、收盤價、市場與資料集
"""
from __future__ import annotations

import math
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import official_quotes
import returns
from active_changes import is_active
from official_quotes import STOCK

VERSION = 1
PERIODS = ("1d", "1w", "1m")
SOURCE = "各投信官網申購買回清單（完整持股）；證交所、櫃買中心收盤行情（政府資料開放平臺資料集 11549、11370）"
METHOD = ("逐日比對相鄰兩個交易日的完整持股清單，台股股票的股數增減 × 當日官方收盤價為日值（億元），"
          "近一週、近一月為逐日日值加總。這是估算，不是經理人的實際成交金額，也未扣除申購買回的影響；"
          "每萬單位持股的變化只是配置密度。")
RATIO_TOL = 0.0005  # 多檔 ETF 同日同比例變動的容許誤差
DIVIDEND_TOL = 0.002  # 股數比例與 1＋配股率的容許誤差
DIVIDEND_DAYS = 90  # 除權日之後多久內的股數跳增可能是配股入帳
# 依既有投信解析器的明示資料欄位核對，不靠價格／權重吻合程度推定日期。
DATE_BASIS = {
    "野村投信官網": "清單另列 CPcfdate；持股依淨值日期 CNavDt 保存",
    "安聯投信官網": "清單另列 CPcfdate；持股依淨值日期 CNavDt 保存",
    "中信投信官網": "申購買回清單明示「淨值日期」",
    "富邦投信官網": "基金資產頁明示「資料日期」",
    "凱基投信官網": "淨值欄名明示資料日期",
    "摩根投信官網": "持股列 navDate／effectiveDate",
    "復華投信官網": "資產明細回傳日期與查詢的正式淨值日期一致",
}
# 只收實際官方清單與交易所名稱的已核對別名；不以字串模糊比對認定市場身分。
NAME_ALIASES = {
    "1101": {"台泥", "台灣水泥"}, "1301": {"台塑", "台灣塑膠工業"},
    "1326": {"台化", "台灣化學纖維"}, "1560": {"中砂", "中國砂輪企業"},
    "2303": {"聯電", "聯華電子"}, "2330": {"台積電", "台灣積體", "台灣積體電路製造"},
    "2610": {"華航", "中華航空"}, "2851": {"中再保", "中央再保"},
    "2891": {"中信金", "中國信託", "中國信託金融控股"},
    "3533": {"嘉澤", "lotescoltd"}, "6442": {"光聖", "光紅建聖"},
    "6510": {"精測", "中華精測科技"}, "8046": {"南電", "南亞電路", "南亞電路板"},
}
NAME_REFERENCE = json.loads((Path(__file__).resolve().parents[1] / "data/stock_name_reference.json").read_text("utf-8"))
EVENT_SOURCES = {"上市": ("證交所除權除息預告", "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL",
                          {"code": "Code", "date": "Date", "kind": "Exdividend", "stock": "StockDividendRatio",
                           "sub": "SubscriptionRatio"}),
                 "上櫃": ("櫃買中心除權除息預告", "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost",
                          {"code": "SecuritiesCompanyCode", "date": "ExRrightsExDividendDate", "kind": "ExRrightsExDividend",
                           "stock": "StockDividendRatio", "sub": "SubscriptionRatioToNewSharesIssued"})}


def num(value) -> float | None:
    try:
        x = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def roc_date(value) -> str | None:
    s = str(value or "").strip()
    if len(s) not in (6, 7) or not s.isdigit():
        return None
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"


def shares(x):
    """股數輸出成整數（整數股時），其他維持小數。"""
    x = num(x)
    return int(x) if x is not None and float(x).is_integer() else x


def money(x):
    return round(x, 6) if x is not None and math.isfinite(x) else None


def normalized_name(value):
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", str(value or "")).lower().replace("臺", "台"))


def stock_identity(asset, name, official_names):
    n = normalized_name(name)
    known = {normalized_name(v) for v in official_names.get(asset, ()) if v}
    if not n or not known:
        return False
    aliases = {normalized_name(v) for v in NAME_ALIASES.get(asset, ())}
    if n in known or (n in aliases and bool(known & aliases)):
        return True
    # 逐代號核對的全名／簡稱對照，仍要求交易所行情名稱作為錨點。
    # 不做前綴、包含或相似度比對，以免海外同碼、子公司及名稱相近股票混入。
    reference = NAME_REFERENCE["items"].get(asset)
    return bool(reference and normalized_name(reference["short_name"]) in known and
                n in {normalized_name(v) for v in [reference["official_name"], *reference["aliases"]]})


# ---------- 保存 ----------

def held_stocks(con: sqlite3.Connection, days: int = 40) -> set[str]:
    """最近 days 天內，主動式 ETF 官網完整清單持有過的台股股票代號（要保存收盤價的範圍）。"""
    latest = con.execute("SELECT MAX(date) FROM holding WHERE source LIKE '%官網'").fetchone()[0]
    if not latest:
        return set()
    since = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()
    rows = con.execute("SELECT DISTINCT code, asset_code FROM holding WHERE date >= ? AND kind = '股票' AND source LIKE '%官網'",
                       (since,))
    return {a for c, a in rows if is_active(c) and STOCK.fullmatch(a or "")}


def parse_events(twse_rows, tpex_rows) -> list[dict]:
    """除權預告裡台股股票標示「權」的列（含權息）：除權交易日、無償配股率、現金增資認購率。"""
    out = []
    for market, rows in (("上市", twse_rows), ("上櫃", tpex_rows)):
        source, url, k = EVENT_SOURCES[market]
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            code, day, kind = str(r.get(k["code"]) or "").strip(), roc_date(r.get(k["date"])), str(r.get(k["kind"]) or "").strip()
            if STOCK.fullmatch(code) and day and "權" in kind:
                out.append({"code": code, "date": day, "kind": kind, "stock_dividend_ratio": num(r.get(k["stock"])) or None,
                            "subscription_ratio": num(r.get(k["sub"])) or None, "source": source, "source_url": url})
    return out


def save_events(con: sqlite3.Connection, events: list[dict], now: str) -> None:
    con.executemany(
        "INSERT INTO stock_event(code, date, kind, stock_dividend_ratio, subscription_ratio, source, source_url, first_observed_at, "
        "observed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(code, date, kind) DO UPDATE SET "
        "stock_dividend_ratio=COALESCE(excluded.stock_dividend_ratio, stock_event.stock_dividend_ratio), "
        "subscription_ratio=COALESCE(excluded.subscription_ratio, stock_event.subscription_ratio), observed_at=excluded.observed_at",
        [(e["code"], e["date"], e["kind"], e["stock_dividend_ratio"], e["subscription_ratio"], e["source"], e["source_url"], now, now)
         for e in events])


def save_list_evidence(con: sqlite3.Connection, code: str, day: str, source: str, url: str | None, holdings: list[dict],
                       now: str) -> None:
    """投信官網完整清單的清單層級證據；同一天重抓時更新內容，第一次取得時間保留。"""
    stocks = [h for h in holdings if h.get("kind") == "股票" and STOCK.fullmatch(h.get("asset_code") or "")]
    weights = [num(h.get("weight")) for h in holdings if num(h.get("weight")) is not None]
    con.execute(
        "INSERT INTO holding_list(code, date, source, source_url, rows, stock_rows, missing_qty, weight_sum, complete, "
        "first_observed_at, observed_at, date_basis) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) ON CONFLICT(code, date) DO UPDATE SET "
        "source=excluded.source, source_url=excluded.source_url, rows=excluded.rows, stock_rows=excluded.stock_rows, "
        "missing_qty=excluded.missing_qty, weight_sum=excluded.weight_sum, observed_at=excluded.observed_at, date_basis=excluded.date_basis",
        (code, day, source, url, len(holdings), len(stocks), sum(1 for h in stocks if h.get("qty") is None),
         round(sum(weights), 4) if weights and num(sum(weights)) is not None else None, now, now, DATE_BASIS.get(source)))


# ---------- 讀取 ----------

def load_lists(con: sqlite3.Connection, since: str, until: str) -> dict[str, dict[str, dict]]:
    """主動式 ETF 的官網完整清單：{ETF: {日期: {"stocks": {股票: 列}, "missing": {缺股數的股票}, "evidence": {...}}}}。
    沒有台股股票的清單（例如債券型）也要記下日期，才知道那天有公告。完整度證據以清單內容計算，取得時間取自 holding_list。"""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    official_names = defaultdict(set)
    for asset, name in con.execute("SELECT code, name FROM stock_close WHERE date <= ? AND name IS NOT NULL", (until,)):
        official_names[asset].add(name)
    for code, day, asset, name, kind, weight, qty, source, url in con.execute(
            "SELECT code, date, asset_code, asset_name, kind, weight, qty, source, source_url FROM holding "
            "WHERE date >= ? AND date <= ? AND source LIKE '%官網'", (since, until)):
        if not is_active(code):
            continue
        lst = out[code].setdefault(day, {"stocks": {}, "missing": set(), "source": source, "source_url": url, "rows": 0,
                                         "weight_sum": 0.0, "invalid": {}, "basis": DATE_BASIS.get(source)})
        lst["rows"] += 1
        lst["weight_sum"] += num(weight) or 0
        if kind == "股票" and STOCK.fullmatch(asset or ""):
            value = num(qty)
            if not stock_identity(asset, name, official_names):
                lst["missing"].add(asset)
                lst["invalid"][asset] = "代號與名稱尚無法核對台股身分，不以同碼行情估值"
            elif value is None or value < 0:
                lst["missing"].add(asset)
                if qty is not None:
                    lst["invalid"][asset] = "股數不是有限非負數字，不當成 0"
            else:
                lst["stocks"][asset] = {"name": name, "qty": value, "weight": num(weight)}
    seen = {}
    try:
        seen = {(c, d): (f, o) for c, d, f, o in con.execute(
            "SELECT code, date, first_observed_at, observed_at FROM holding_list WHERE date >= ?", (since,))}
    except sqlite3.OperationalError:
        pass
    for code, days in out.items():
        for day, lst in days.items():
            first, last = seen.get((code, day), (None, None))
            lst["evidence"] = {"code": code, "date": day, "source": lst["source"], "source_url": lst["source_url"],
                               "rows": lst["rows"], "stock_rows": len(lst["stocks"]) + len(lst["missing"]),
                               "missing_qty": len(lst["missing"]), "weight_sum": money(lst["weight_sum"]),
                               "date_basis": lst["basis"], "unverified_rows": len(lst["invalid"]),
                               "first_observed_at": first, "observed_at": last}
    return out


def load_prices(con: sqlite3.Connection, since: str) -> tuple[dict, set[str], str | None]:
    """官方收盤價 {(股票, 日期): (收盤價, 市場, 資料集)}、有上市股價的日期，以及股價開始保存的日期。"""
    prices, days = {}, set()
    for code, day, market, close, dataset in con.execute(
            "SELECT code, date, market, close, dataset FROM stock_close WHERE date >= ?", (since,)):
        value = num(close)
        prices[(code, day)] = (value if value is not None and value > 0 else None, market, dataset)
        if market == "上市":
            days.add(day)
    first = con.execute("SELECT MIN(date) FROM stock_close").fetchone()[0]
    return prices, days, first


def load_units(con: sqlite3.Connection, since: str) -> dict[tuple[str, str], float]:
    try:
        return {(c, d): num(u) for c, d, u in con.execute("SELECT code, date, units FROM fund_daily WHERE date >= ? AND units > 0", (since,))
                if num(u) is not None and num(u) > 0}
    except sqlite3.OperationalError:
        return {}


def load_events(con: sqlite3.Connection) -> dict[str, list[tuple[str, float]]]:
    """{股票: [(除權日, 無償配股率)]}，只留有配股率的。"""
    out = defaultdict(list)
    try:
        for code, day, ratio in con.execute("SELECT code, date, stock_dividend_ratio FROM stock_event WHERE stock_dividend_ratio > 0"):
            out[code].append((day, ratio))
    except sqlite3.OperationalError:
        pass
    return out


# ---------- 計算 ----------

def window(cal, as_of: str, period: str) -> tuple[str, list[str]] | None:
    """期間的 baseline 與逐日加總的交易日；開休市日期沒涵蓋時回傳 None。"""
    if period == "1d":
        baseline = cal.previous_trading_day(as_of)
        return (baseline, [as_of]) if baseline else None
    start = returns.start_dates(as_of)[period]
    is_open = cal.is_open(start)
    if is_open is None:
        return None
    baseline = start if is_open else cal.previous_trading_day(start)
    if not baseline:
        return None
    days = cal.trading_days((date.fromisoformat(baseline) + timedelta(days=1)).isoformat(), as_of)
    return (baseline, days) if days else None


def qty(lst: dict, asset: str) -> float:
    return lst["stocks"].get(asset, {}).get("qty", 0.0)


def diff(prev: dict, cur: dict) -> dict[str, tuple[float, float]]:
    """兩份完整清單股數有變動的台股股票：{股票: (前一份股數, 這一份股數)}；沒列出的是 0。"""
    return {a: (qty(prev, a), qty(cur, a)) for a in set(prev["stocks"]) | set(cur["stocks"])
            if a not in prev["missing"] and a not in cur["missing"] and qty(prev, a) != qty(cur, a)}


def corporate_actions(changes: dict[str, dict[str, dict]], events: dict, holders: dict | None = None) -> tuple[dict, dict]:
    """疑似公司行動。changes 是 {日期: {ETF: {股票: (前股數, 後股數)}}}；holders 是 {日期: {股票: 前後都持有且股數沒變的 ETF 數}}。
    回傳 (股票層級 {(股票, 日期): (原因, [ETF])}, ETF 層級 {(ETF, 股票, 日期): 原因})。"""
    stock_level, etf_level, holders = {}, {}, holders or {}
    for day, per_etf in changes.items():
        ratios, steady = defaultdict(list), holders.get(day, {})
        for etf, d in per_etf.items():
            for asset, (q0, q1) in d.items():
                if q0 > 0 and q1 > 0:
                    ratios[asset].append((etf, q1 / q0))
            dels = {a: q0 for a, (q0, q1) in d.items() if q0 > 0 and q1 == 0}
            adds = {a: q1 for a, (q0, q1) in d.items() if q0 == 0 and q1 > 0}
            for a1, q in dels.items():
                for a2, q2 in adds.items():
                    if q == q2:
                        why = f"疑似代碼變更：{a1} 刪除、{a2} 新增，同為 {shares(q)} 股"
                        etf_level[(etf, a1, day)] = etf_level[(etf, a2, day)] = why
        for asset, lst in ratios.items():
            rs = [r for _, r in lst]
            # 公司行動會讓所有持有者同一天變動：有持有者股數沒變就不是
            if len(lst) >= 2 and not steady.get(asset) and max(rs) / min(rs) - 1 <= RATIO_TOL:
                stock_level[(asset, day)] = (f"疑似除權、分割或減資：{len(lst)} 檔 ETF 同日同比例變動（×{rs[0]:.4f}）",
                                             sorted(e for e, _ in lst))
                continue
            d0 = date.fromisoformat(day)
            for ex, ratio in events.get(asset, []):
                if -1 <= (d0 - date.fromisoformat(ex)).days <= DIVIDEND_DAYS and any(abs(r - (1 + ratio)) <= DIVIDEND_TOL for r in rs):
                    stock_level[(asset, day)] = (f"疑似配股入帳：除權日 {ex}，配股率 {ratio:g}", sorted(e for e, _ in lst))
                    break
    return stock_level, etf_level


def contribution(etf: str, name: str | None, asset: str, have: dict, prices: dict, units: dict, chain: list[str]) -> dict | None:
    """一檔 ETF 在期間內對一檔股票的增減；期間內股數都沒變時回傳 None。
    _buy、_sell 是逐日日值的正值與負值絕對值加總（缺股價那一邊為 None），_days 是有變動的日期。"""
    baseline, end, days = chain[0], chain[-1], chain[1:]
    buy = sell = 0.0
    buy_ok = sell_ok = True
    moved, missing = [], []
    for prev, day in zip(chain, days):
        q0, q1 = qty(have[prev], asset), qty(have[day], asset)
        if q0 == q1:
            continue
        moved.append(day)
        close = (prices.get((asset, day)) or (None,))[0]
        if num(close) is None or close <= 0:
            missing.append(day)
            buy_ok, sell_ok = (False, sell_ok) if q1 > q0 else (buy_ok, False)
            continue
        value = (q1 - q0) * close / 1e8
        buy, sell = (buy + value, sell) if value > 0 else (buy, sell - value)
    if not moved:
        return None
    q_prev, q_end = qty(have[baseline], asset), qty(have[end], asset)
    change = q_end - q_prev
    action = "不變" if change == 0 else "新增" if q_prev == 0 else "刪除" if q_end == 0 else "加碼" if change > 0 else "減碼"

    def per_unit(q, day):
        u = units.get((etf, day))
        value = num(q / u * 1e4) if u else None
        return round(value, 4) if value is not None else None

    unit_prev, unit_end = per_unit(q_prev, baseline), per_unit(q_end, end)
    row = have[end]["stocks"].get(asset) or have[baseline]["stocks"].get(asset) or {}
    return {"code": etf, "name": name, "qty_prev": shares(q_prev), "qty": shares(q_end), "qty_change": shares(change),
            "amount": money(buy - sell) if buy_ok and sell_ok else None, "action": action,
            "weight_prev": have[baseline]["stocks"].get(asset, {}).get("weight"), "weight": have[end]["stocks"].get(asset, {}).get("weight"),
            "unit_qty_prev": unit_prev, "unit_qty": unit_end,
            "unit_qty_change": money(unit_end - unit_prev) if unit_prev is not None and unit_end is not None else None,
            "source": have[end]["source"], "source_url": have[end]["source_url"], "prev_date": baseline, "date": end,
            "reason": f"缺 {'、'.join(missing)} 的官方收盤價，金額無法估算" if missing else None,
            "_buy": buy if buy_ok else None, "_sell": sell if sell_ok else None, "_days": moved, "_missing": missing,
            "_asset_name": row.get("name")}


def build_period(period: str, cal, as_of: str | None, lists: dict, names: dict, prices: dict, price_days: set,
                 first_price: str | None, units: dict, events: dict) -> dict:
    out = {"start": None, "end": as_of, "baseline": None, "status": "資料不足", "reason": None, "reason_code": None, "days": [],
           "included_etfs": [], "excluded_etfs": [], "excluded_stocks": [], "lists": [], "stocks": []}
    if not as_of:
        out.update(reason="尚未保存官方收盤價。", reason_code="no_prices")
        return out
    w = window(cal, as_of, period) if cal else None
    if not w:
        out.update(reason="官方開休市日期沒有涵蓋這段期間，無法確認交易日。", reason_code="no_calendar")
        return out
    baseline, days = w
    chain = [baseline] + days
    out.update(start=days[0], baseline=baseline, days=days)
    for etf in sorted(c for c in names if is_active(c)):
        have = lists.get(etf) or {}
        missing = [d for d in chain if d not in have]
        if not have:
            out["excluded_etfs"].append({"code": etf, "reason": "沒有投信官網的完整持股清單（投信投顧公會的前十大不是完整清單）"})
        elif missing:
            more = f" 等 {len(missing)} 天" if len(missing) > 3 else ""
            out["excluded_etfs"].append({"code": etf, "reason": f"缺 {'、'.join(missing[:3])}{more}的完整持股清單"})
        elif any(not have[d].get("basis") for d in chain):
            out["excluded_etfs"].append({"code": etf, "reason": "尚未核實此投信清單日期是持股基準日，暫不比較"})
        elif len({have[d]["source"] for d in chain}) > 1:
            out["excluded_etfs"].append({"code": etf, "reason": "期間內持股來源改變，暫不比較"})
        else:
            out["included_etfs"].append(etf)
            out["lists"] += [have[baseline]["evidence"], have[as_of]["evidence"]]
    no_price = [d for d in days if d not in price_days]
    if no_price:
        since = f"官方收盤價自 {first_price} 起保存，" if first_price else "尚未保存官方收盤價，"
        out.update(reason=f"{since}缺 {'、'.join(no_price[:3])}{' 等' if len(no_price) > 3 else ''}的股價，期間不估值、不排行。",
                   reason_code="no_prices")
        return out
    if not out["included_etfs"]:
        out.update(reason="沒有 ETF 具備期間內每個交易日的完整持股清單，期間不排行。", reason_code="no_etfs")
        return out

    # 判斷公司行動用所有有連續清單的主動式 ETF，不只列入本期間的
    changes: dict[str, dict[str, dict]] = defaultdict(dict)
    steady: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for etf, have in lists.items():
        for prev, day in zip(chain, days):
            if prev in have and day in have and have[prev].get("basis") and have[day].get("basis"):
                changes[day][etf] = diff(have[prev], have[day])
                for asset in set(have[prev]["stocks"]) & set(have[day]["stocks"]):
                    if asset not in changes[day][etf]:
                        steady[day][asset] += 1
    stock_level, etf_level = corporate_actions(changes, events, steady)
    # 單一持有者也保守處理；重大股數跳變可能是真實交易，但未核對前不放進排行。
    for day, per_etf in changes.items():
        prev = chain[chain.index(day) - 1]
        for etf, assets in per_etf.items():
            for asset, (q0, q1) in assets.items():
                if q0 <= 0 or q1 <= 0:
                    continue
                ratio = q1 / q0
                p0 = (prices.get((asset, prev)) or (None,))[0]
                p1 = (prices.get((asset, day)) or (None,))[0]
                if (p0 and p1 and abs(ratio - 1) >= .05 and (ratio - 1) * (p1 / p0 - 1) < 0
                        and abs(ratio * p1 / p0 - 1) <= .02):
                    affected = sorted(e for e, moves in per_etf.items() if asset in moves)
                    stock_level[(asset, day)] = ("疑似分割、減資或配股：股數與股價反向調整，待核對", affected)
                u0, u1 = units.get((etf, prev)), units.get((etf, day))
                density_ratio = ratio / (u1 / u0) if u0 and u1 else ratio
                if density_ratio >= 1.5 or density_ratio <= 2 / 3:
                    etf_level[(etf, asset, day)] = "持股股數重大跳變，無法排除分割、減資或資料異常，暫不排名"

    excluded: dict[tuple, dict] = {}
    parts: dict[str, list[dict]] = defaultdict(list)
    dropped: dict[str, list[dict]] = defaultdict(list)  # 股票: 被排除的 ETF 部分

    def drop(asset, day, why, etf):
        e = excluded.setdefault((asset, day, why), {"asset_code": asset, "date": day, "reason": why, "etfs": set()})
        e["etfs"].add(etf)

    for etf in out["included_etfs"]:
        have = lists[etf]
        assets = set().union(*(set(have[d]["stocks"]) | have[d]["missing"] for d in chain))
        for asset in sorted(assets):
            hits = [(day, *stock_level[(asset, day)]) for day in days if (asset, day) in stock_level]
            if hits:
                for day, why, etfs in hits:
                    for e in etfs:
                        drop(asset, day, why, e)
                continue
            gap = [d for d in chain if asset in have[d]["missing"]]
            odd = [(day, etf_level[(etf, asset, day)]) for day in days if (etf, asset, day) in etf_level]
            if gap or odd:
                for day, why in [(gap[0], have[gap[0]].get("invalid", {}).get(asset) or "清單列了這檔股票卻缺股數，不當成 0")] if gap else odd:
                    drop(asset, day, why, etf)
                    dropped[asset].append({"code": etf, "date": day, "reason": why})
                continue
            c = contribution(etf, names.get(etf), asset, have, prices, units, chain)
            if c:
                parts[asset].append(c)

    def stock_name(asset):
        for etf_lists in lists.values():
            for lst in etf_lists.values():
                row = lst["stocks"].get(asset)
                if row and row.get("name"):
                    return row["name"]
        return None

    whole = {a for (a, _d, _w), e in excluded.items() if (a, _d) in stock_level}
    out["excluded_stocks"] = sorted(({"asset_code": e["asset_code"], "asset_name": stock_name(e["asset_code"]), "date": e["date"],
                                      "reason": e["reason"], "etfs": sorted(e["etfs"])} for e in excluded.values()),
                                    key=lambda e: (e["date"], e["asset_code"], e["reason"]))
    stocks = []
    for asset, cs in parts.items():
        if asset in whole:
            continue
        missing = sorted({d for c in cs for d in c["_missing"]})
        complete = not missing and not dropped.get(asset)
        buy = sum(c["_buy"] for c in cs) if complete else None
        sell = sum(c["_sell"] for c in cs) if complete else None
        moved = sorted({d for c in cs for d in c["_days"]})
        price_rows = []
        for d in moved:
            close, market, dataset = prices.get((asset, d)) or (None, None, None)
            if close:
                price_rows.append({"date": d, "close": close, "market": market, "dataset": dataset})
        name = next((c["_asset_name"] for c in cs if c["_asset_name"]), None) or stock_name(asset)
        for c in cs:
            for k in ("_buy", "_sell", "_days", "_missing", "_asset_name"):
                c.pop(k)
        cs.sort(key=lambda c: (c["amount"] is None, -abs(c["amount"] or 0), c["code"]))
        stocks.append({"asset_code": asset, "asset_name": name,
                       "net_amount": money(buy - sell) if complete else None, "buy_amount": money(buy), "sell_amount": money(sell),
                       "buy_etfs": sum(1 for c in cs if c["qty_change"] > 0), "sell_etfs": sum(1 for c in cs if c["qty_change"] < 0),
                       "net_shares": shares(sum(c["qty_change"] for c in cs)), "complete": complete,
                       "counts_complete": not bool(dropped.get(asset)),
                       "missing_price_dates": missing, "excluded_etfs": sorted(dropped.get(asset, []), key=lambda x: (x["date"], x["code"])),
                       "prices": price_rows, "etfs": cs})
    stocks.sort(key=lambda s: (s["net_amount"] is None, -(s["net_amount"] or 0), s["asset_code"]))
    incomplete = sum(1 for s in stocks if not s["complete"])
    out.update(status="可計算", stocks=stocks,
               reason="股數增減 × 當日官方收盤價的估算，不是實際成交金額，也未扣除申購買回影響。"
                      + (f"其中 {incomplete} 檔股票因缺股價或部分 ETF 被排除，金額為空值。" if incomplete else ""))
    return out


def build(con: sqlite3.Connection, items: list[dict], trade_date: str, cal) -> dict:
    """latest.json 的 active_holdings。cal 是官方開休市日期（market_calendar.Calendar）。"""
    row = con.execute("SELECT MAX(date) FROM stock_close WHERE market = '上市' AND date <= ?", (trade_date,)).fetchone()
    as_of = row[0] if row else None
    since = (date.fromisoformat(as_of or trade_date) - timedelta(days=45)).isoformat()
    lists = load_lists(con, since, as_of or trade_date)
    prices, price_days, first_price = load_prices(con, since)
    units, events = load_units(con, since), load_events(con)
    names = {it["code"]: it.get("name") for it in items}
    periods = {p: build_period(p, cal, as_of, lists, names, prices, price_days, first_price, units, events) for p in PERIODS}
    markets = sorted({market for (_c, d), (_close, market, _ds) in prices.items() if market and d == as_of})
    return {"version": VERSION, "as_of": as_of, "source": SOURCE, "method": METHOD, "price_history_from": first_price,
            "attributions": official_quotes.attributions([{"market": m, "date": as_of} for m in markets])
                            + NAME_REFERENCE["attributions"],
            "periods": periods}
