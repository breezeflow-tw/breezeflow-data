"""主動式 ETF 持股異動：比對投信官網每天公告的完整持股清單（申購買回清單）。

- 主動式 ETF：代號結尾 A（股票型）或 D（債券型），投信投顧公會分類為主動式交易所交易基金
- 每檔：最新一份清單和前一份清單逐一比對成分，分成新增、刪除、加碼、減碼（看實際股數增減）
- 標記隨申購買回同比例的增減：股數增減的比例和發行單位數（投信公告）的增減比例相同時，多半是申購買回
  帶動的照比例買賣，不是經理人主動調整。台灣的主動式 ETF 多為現金申購，經理人可以自己決定新資金買哪些股票，
  所以只在比例相符時標記，不推算「應該買多少」
- 全部 ETF 彙總（台股）：同一檔股票有幾檔主動式 ETF 加碼（含新增）、幾檔減碼（含刪除），以及股數與金額合計；
  隨申購買回同比例的增減、以及股數變動不到 1% 的加碼減碼不算。金額＝增減股數 × 該日交易所收盤價，沒有收盤價時留空。
  需要投信公告的發行單位數才能判斷是否同比例，沒有單位數的 ETF 不列入彙總
"""
from __future__ import annotations

import re
import sqlite3

TOLERANCE = 0.001  # 股數和「前一份股數 × 單位數比例」差在 0.1% 以內，視為隨申購買回同比例增減
LOT = 1000  # 台股一張；同比例增減的進位誤差在一張以內
MATERIAL = 0.01  # 彙總時加碼、減碼的股數至少變動 1%（新增、刪除不受限），太小的多半是分配申購資金
TOP = 30  # 彙總表每邊列幾檔
PER_ETF = 50  # 每檔 ETF 最多列幾筆異動（依權重變化排序；筆數統計仍是全部）
SOURCE = "各投信官網申購買回清單"
METHOD = ("比對投信官網每天公告的完整持股清單，看實際股數增減。股數增減比例和發行單位數增減比例相同（差在 0.1% 或一張以內）"
          "標記為隨申購買回同比例增減，彙總時不算，股數變動不到 1% 的加碼減碼也不算；金額＝增減股數 × 該日交易所收盤價。")


def is_active(code: str) -> bool:
    return code[-1:] in ("A", "D")


def taiwan_stock(code: str | None) -> bool:
    return bool(re.fullmatch(r"\d{4,6}[A-Z]?", code or ""))


def latest_lists(con: sqlite3.Connection, code: str, as_of: str) -> tuple[str, str] | None:
    """截至交易日最新兩份投信官網清單的日期（新、舊）；不到兩份時回傳 None。"""
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM holding WHERE code = ? AND date <= ? AND source LIKE '%官網' ORDER BY date DESC LIMIT 2",
        (code, as_of))]
    return (days[0], days[1]) if len(days) == 2 else None


def load_list(con: sqlite3.Connection, code: str, day: str) -> dict[tuple[str, str], dict]:
    return {(r[0], r[2]): {"asset_code": r[0], "asset_name": r[1], "kind": r[2], "qty": r[3], "weight": r[4]}
            for r in con.execute("SELECT asset_code, asset_name, kind, qty, weight FROM holding WHERE code = ? AND date = ?",
                                 (code, day))}


def units_on(con: sqlite3.Connection, code: str, day: str) -> float | None:
    try:
        row = con.execute("SELECT units FROM fund_daily WHERE code = ? AND date = ?", (code, day)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def units_pair(con: sqlite3.Connection, code: str, day: str, prev: str, rec: dict | None) -> tuple[float | None, float | None]:
    """清單那兩天的發行單位數。先用資料庫逐日保存的投信公告值；前一天沒有時，若前一份清單正好是前一個交易日，
    用今天的單位數減掉投信公告的「與前日已發行單位差異數」。"""
    rec = rec or {}
    day_of_rec = rec.get("asset_date") or rec.get("pcf_date") or rec.get("nav_date")
    units = units_on(con, code, day) or (rec.get("units") if day_of_rec == day else None)
    before = units_on(con, code, prev)
    if before is None and units:
        change = rec.get("units_change") if rec.get("pcf_date") == day else None
        if change is None:
            row = con.execute("SELECT units_change FROM daily WHERE code = ? AND date = ?", (code, day)).fetchone()
            change = row[0] if row else None
        previous_day = con.execute("SELECT MAX(date) FROM daily WHERE date < ?", (day,)).fetchone()[0]
        if change is not None and previous_day == prev:
            before = units - change
    return units, (before if before and before > 0 else None)


def compare(cur: dict, old: dict, ratio: float | None) -> list[dict]:
    """逐一比對成分，回傳股數有變動的成分（新增、刪除、加碼、減碼）。ratio 是今天 ÷ 前一份的發行單位數，
    有的話標記 with_flow：股數增減和單位數同比例（多半是申購買回帶動）。"""
    out = []
    for key in sorted(set(cur) | set(old)):
        c, o = cur.get(key) or {}, old.get(key) or {}
        qty, qty_prev = c.get("qty") or 0, o.get("qty") or 0
        if qty == qty_prev:
            continue
        action = "新增" if not qty_prev else "刪除" if not qty else "加碼" if qty > qty_prev else "減碼"
        with_flow = None
        if ratio is not None and qty and qty_prev:
            expected = qty_prev * ratio
            floor = LOT if taiwan_stock(key[0]) and key[1] == "股票" else 1
            with_flow = abs(qty - expected) <= max(floor, expected * TOLERANCE)
        out.append({"asset_code": key[0], "asset_name": c.get("asset_name") or o.get("asset_name"), "kind": key[1],
                    "action": action, "qty_prev": qty_prev or None, "qty": qty or None, "qty_change": qty - qty_prev,
                    "weight_prev": o.get("weight"), "weight": c.get("weight"),
                    "weight_change": round(c["weight"] - o["weight"], 4)
                    if c.get("weight") is not None and o.get("weight") is not None else None,
                    "with_flow": with_flow})
    order = {"新增": 0, "加碼": 1, "減碼": 2, "刪除": 3}
    return sorted(out, key=lambda r: (order[r["action"]], r["with_flow"] is True,
                                      -abs(r["weight_change"] or r["weight"] or r["weight_prev"] or 0)))


def material(row: dict) -> bool:
    """新增、刪除一律算；加碼、減碼的股數至少要變動 MATERIAL。"""
    if row["action"] in ("新增", "刪除"):
        return True
    return abs(row["qty_change"]) >= row["qty_prev"] * MATERIAL


def amount(row: dict, day: str, closes: dict[str, tuple[str, float]]) -> float | None:
    """增減金額（億元）：只算台股、且有該日交易所收盤價的。"""
    price = closes.get(row["asset_code"])
    if row["kind"] != "股票" or not taiwan_stock(row["asset_code"]) or not price or price[0] != day:
        return None
    return round(row["qty_change"] * price[1] / 1e8, 4)


def attach(con: sqlite3.Connection, items: list[dict], as_of: str, issuer: dict[str, dict] | None = None,
           closes: dict[str, tuple[str, float]] | None = None) -> dict | None:
    """每檔主動式 ETF 附上 holdings_changes；回傳全部主動式 ETF 的台股彙總（網站最上層的 active_changes）。"""
    closes = closes or {}
    per_etf = []
    for it in items:
        it["holdings_changes"] = None
        if not is_active(it["code"]):
            continue
        days = latest_lists(con, it["code"], as_of)
        if not days:
            continue
        day, prev = days
        units, before = units_pair(con, it["code"], day, prev, (issuer or {}).get(it["code"]))
        ratio = units / before if units and before else None
        rows = compare(load_list(con, it["code"], day), load_list(con, it["code"], prev), ratio)
        for r in rows:
            r["amount"] = amount(r, day, closes)
        source = con.execute("SELECT source FROM holding WHERE code = ? AND date = ? LIMIT 1", (it["code"], day)).fetchone()[0]
        counts = {a: sum(1 for r in rows if r["action"] == a) for a in ("新增", "加碼", "減碼", "刪除")}
        it["holdings_changes"] = {"date": day, "prev_date": prev, "units": units, "units_prev": before,
                                  "flow_checked": ratio is not None, "counts": counts, "changes": rows[:PER_ETF],
                                  "source": source}
        per_etf.append((it, {**it["holdings_changes"], "changes": rows}))
    return summarize(per_etf)


def summarize(per_etf: list[tuple[dict, dict]]) -> dict | None:
    """全部主動式 ETF 的台股彙總：只用能判斷申購買回影響（有發行單位數）、且清單日期是最多檔那天的 ETF；
    隨申購買回同比例的增減、以及股數變動不到 MATERIAL 的加碼減碼不算。"""
    usable = [(it, ch) for it, ch in per_etf if ch["flow_checked"]]
    if not usable:
        return None
    dates = [ch["date"] for _, ch in usable]
    day = max(set(dates), key=dates.count)
    stocks: dict[str, dict] = {}
    for it, ch in usable:
        if ch["date"] != day:
            continue
        for r in ch["changes"]:
            if r["kind"] != "股票" or not taiwan_stock(r["asset_code"]) or r["with_flow"] or not material(r):
                continue
            s = stocks.setdefault(r["asset_code"], {"asset_code": r["asset_code"], "asset_name": r["asset_name"],
                                                     "buy_etfs": 0, "sell_etfs": 0, "buyers": [], "sellers": [],
                                                     "buy_shares": 0, "sell_shares": 0, "buy_amount": 0.0, "sell_amount": 0.0,
                                                     "complete": {"buy": True, "sell": True}})
            side = "buy" if r["qty_change"] > 0 else "sell"
            s[f"{side}_etfs"] += 1
            s[f"{side}ers"].append(it["code"])
            s[f"{side}_shares"] += abs(r["qty_change"])
            if r["amount"] is None:
                s["complete"][side] = False
            else:
                s[f"{side}_amount"] += abs(r["amount"])
    for s in stocks.values():
        done = s.pop("complete")
        for side in ("buy", "sell"):
            s[f"{side}_amount"] = round(s[f"{side}_amount"], 4) if done[side] and s[f"{side}_etfs"] else None
        s["net_shares"] = s["buy_shares"] - s["sell_shares"]

    def rank(side):
        return sorted((s for s in stocks.values() if s[f"{side}_etfs"]),
                      key=lambda s: (-s[f"{side}_etfs"], -(s[f"{side}_amount"] or 0), s["asset_code"]))[:TOP]

    included = sorted(it["code"] for it, ch in usable if ch["date"] == day)
    return {"date": day, "etf_count": len(included), "etfs": included,
            "not_included": sorted(it["code"] for it, ch in per_etf if it["code"] not in included),
            "buys": rank("buy"), "sells": rank("sell"), "source": SOURCE, "method": METHOD}
