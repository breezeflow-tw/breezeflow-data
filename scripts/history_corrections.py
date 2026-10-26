"""隔離已確認錯誤的行情；只接受指定內容，不刪除投信公告或任意日期。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

DAY = "2026-10-23"
BAD_SHA256 = "f283ae1c96a6d5120115d7847ba8bf73aacc30fa038b986d365c14b857d33814"
SOURCE = "https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d6a0fa351b01a1638507940202.pdf"


def apply(history: Path, db: Path) -> bool:
    """每日流程提交隔離副本及刪除差異；內容改變時停止，不能擴大刪除範圍。"""
    root = history.resolve()
    source = (root / "daily" / f"{DAY}.csv").resolve()
    backup = (root / "quarantine" / "daily" / f"{DAY}.csv").resolve()
    if not source.is_relative_to(root) or not backup.is_relative_to(root):
        raise ValueError("隔離路徑超出歷史資料目錄")
    raw = source.read_bytes().replace(b"\r\n", b"\n") if source.exists() else None
    if raw is not None and hashlib.sha256(raw).hexdigest() != BAD_SHA256:
        raise ValueError("10/23 行情內容與已確認錯誤版本不同，停止自動隔離")
    if backup.exists() and hashlib.sha256(backup.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != BAD_SHA256:
        raise ValueError("隔離副本內容不同，停止覆寫")
    removed = 0
    if db.exists():
        with closing(sqlite3.connect(db)) as con, con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily'").fetchone():
                rows = con.execute("SELECT volume_lots, change_pct FROM daily WHERE date=?", (DAY,)).fetchall()
                if rows and (len(rows) != 360 or any(v not in (0, None) or c not in (0, None) for v, c in rows)):
                    raise ValueError("10/23 資料庫行情與錯誤樣本不同，停止清理")
                if rows and raw is None and not backup.exists():
                    raise ValueError("缺少可追溯的原始 CSV，不清理資料庫")
                removed = con.execute("DELETE FROM daily WHERE date=?", (DAY,)).rowcount
    if raw is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(raw)
        backup.with_suffix(".json").write_text(json.dumps({
            "date": DAY, "reason": "官方全日休市；原始行情將無交易日誤列為 360 筆持平資料",
            "source_url": SOURCE, "original_sha256": BAD_SHA256, "rows": 360,
            "note": "只隔離 daily 行情，不移除 holdings、holding_lists 或 fund_daily 公告。"
        }, ensure_ascii=False, indent=2) + "\n", "utf-8")
        source.unlink()
    return raw is not None or removed > 0
