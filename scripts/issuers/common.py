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
