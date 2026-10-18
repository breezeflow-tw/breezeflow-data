"""資料庫的長期保存：以文字檔（CSV）存在 data/history/，SQLite 只在執行時重建。

為什麼不直接提交 SQLite：二進位檔每次變動，Git 都要另存一份完整副本，一天兩次排程，
一年後 repo 會超過 GitHub 建議的大小。CSV 依日期分檔，Git 只記錄新增與變動的部分。

目錄結構：
    data/history/etf.csv                 ETF 基本資料（每檔一列）
    data/history/dividend.csv            配息紀錄
    data/history/price_check.csv         市價查核差異
    data/history/daily/YYYY-MM-DD.csv    每日快照（每天一檔）
    data/history/holdings/YYYY-MM-DD.csv 成分股（依清單日期分檔）
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# 資料表 → (分檔欄位或 None, 排序欄位)
TABLES = {
    "etf": (None, "code"),
    "dividend": (None, "code, ex_date"),
    "price_check": (None, "date, code, field"),
    "daily": ("date", "code"),
    "holding": ("date", "code, kind, asset_code"),
}
SPLIT_DIRS = {"daily": "daily", "holding": "holdings"}

# 舊資料庫補上新欄位
MIGRATIONS = {
    "daily": {"nav_official": "REAL", "premium_official": "REAL", "aum": "REAL", "price_check": "TEXT",
              "nav_source": "TEXT", "aum_source": "TEXT", "beneficiaries": "INTEGER", "units_change": "REAL"},
    "etf": {"units": "REAL", "index_name": "TEXT", "listed_date": "TEXT", "mgmt_fee": "REAL",
            "mgmt_fee_text": "TEXT", "custodian_fee_text": "TEXT", "manager": "TEXT", "risk_class": "TEXT",
            "beneficiaries": "INTEGER", "beneficiaries_month": "TEXT", "stats_json": "TEXT"},
    "dividend": {"source": "TEXT"},
}


def ensure_schema(con: sqlite3.Connection) -> None:
    # 舊版成分股表只保留最新一份（主鍵不含日期），改為保留歷史前先移除重建
    pk = [r[1] for r in con.execute("PRAGMA table_info(holding)") if r[5]]
    if pk and "date" not in pk:
        con.execute("DROP TABLE holding")
    con.executescript(SCHEMA_PATH.read_text("utf-8"))
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _files(history: Path, table: str) -> list[Path]:
    if table in SPLIT_DIRS:
        return sorted((history / SPLIT_DIRS[table]).glob("*.csv"))
    path = history / f"{table}.csv"
    return [path] if path.exists() else []


def load(con: sqlite3.Connection, history: Path) -> int:
    """把 CSV 匯入資料庫。空字串視為空值；數字欄位由 SQLite 的欄位型別自動轉換。"""
    count = 0
    for table in TABLES:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for path in _files(history, table):
            with path.open(encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                use = [c for c in reader.fieldnames or [] if c in cols]  # 已移除的欄位略過
                rows = [[row[c] if row[c] != "" else None for c in use] for row in reader]
            if rows:
                con.executemany(f"INSERT OR REPLACE INTO {table}({', '.join(use)}) VALUES({', '.join('?' * len(use))})", rows)
                count += len(rows)
    con.commit()
    return count


def dump(con: sqlite3.Connection, history: Path) -> None:
    """把資料庫寫回 CSV。排序固定、內容沒變的檔案寫出來也完全相同，Git 不會有差異。"""
    for table, (split, order) in TABLES.items():
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}").fetchall()
        if split is None:
            _write(history / f"{table}.csv", cols, rows)
            continue
        idx = cols.index(split)
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row[idx] or "undated", []).append(row)
        for key, group in groups.items():
            _write(history / SPLIT_DIRS[table] / f"{key}.csv", cols, group)


def _write(path: Path, cols: list[str], rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        w.writerows(["" if v is None else v for v in row] for row in rows)


def open_db(db_path: Path, history: Path | None) -> sqlite3.Connection:
    """開啟資料庫；資料庫檔不存在但有歷史 CSV 時，先從 CSV 重建。"""
    fresh = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    ensure_schema(con)
    if fresh and history and history.exists():
        load(con, history)
    return con
