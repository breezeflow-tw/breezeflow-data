"""各投信模組共用的小工具。"""
from __future__ import annotations

import re


def ymd(value) -> str | None:
    """西元日期 → YYYY-MM-DD。接受 20261016、2026-10-16、2026/5/27、2026-10-16T00:00:00。"""
    s = str(value or "").strip()
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s) or re.match(r"(\d{4})(\d{2})(\d{2})", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def roc_ymd(value) -> str | None:
    """民國日期 115/10/05 → 2026-10-05。"""
    m = re.match(r"(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})", str(value or "").strip())
    return f"{int(m.group(1)) + 1911}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def flat_fee(text) -> float | None:
    """只接受單一費率（例如「0.99%」）。分段級距的說明不換算，避免算錯。"""
    m = re.fullmatch(r"\s*(?:年費率)?\s*([\d.]+)\s*[%％]\s*", str(text or ""))
    return float(m.group(1)) if m else None


def num(value) -> float | None:
    s = str(value if value is not None else "").replace(",", "").strip()
    try:
        return float(s) if s not in ("", "-", "--") else None
    except ValueError:
        return None


HISTORY_WINDOW = 25  # 回補最近幾份申購買回清單（約五週，夠算近 4 週的週申贖）
HISTORY_BUDGET = 60  # 每個投信每次執行最多回補幾份


def backfill_pcfs(candidates: dict[str, list[str]], attempted: set, fetch_one, budget: int = HISTORY_BUDGET) -> dict[str, dict]:
    """回補歷史申購買回清單：清單日期由新到舊、各檔輪流，查過的（attempted 裡的 (代號, 清單日期)）不再查。
    candidates 是 {代號: [清單日期]}；fetch_one(代號, 清單日期) 回傳解析後的清單，查無資料回傳 {}。
    回傳 {代號: {"history": [清單], "attempts": [{pcf_date, data_date, status}]}}；status 是 ok 或 empty。
    連線失敗或被擋就停止，那一筆不記為已查，下次再試。"""
    queue = sorted(((day, code) for code, days in candidates.items() for day in days[:HISTORY_WINDOW]
                    if (code, day) not in attempted), key=lambda x: (x[0], x[1]), reverse=True)
    out: dict[str, dict] = {}
    for day, code in queue[:budget]:
        try:
            pcf = fetch_one(code, day)
        except RuntimeError:
            break
        got = out.setdefault(code, {"history": [], "attempts": []})
        if pcf.get("pcf_date"):
            got["history"].append({k: v for k, v in pcf.items() if k != "holdings"})
        got["attempts"].append({"pcf_date": day, "data_date": pcf.get("pcf_date"), "status": "ok" if pcf.get("pcf_date") else "empty"})
    return out
