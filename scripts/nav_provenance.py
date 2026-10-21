"""淨值數字、日期與來源一併保存；只從可驗證的原始快照恢復日期。"""
from datetime import date, datetime
import re

MIS_URL = "https://mis.twse.com.tw/stock/data/all_etf.txt"
SITCA_URL = "https://www.sitca.org.tw/ROC/Industry/IN2106.aspx?pid=IN2213_02"


def valid_day(value):
    try:
        return date.fromisoformat(value).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "") else None
    except (ValueError, TypeError):
        return None


def timestamp_day(value):
    try:
        if not re.fullmatch(r"\d{8} \d{2}:\d{2}:\d{2}", value or ""):
            return None
        return datetime.strptime(value or "", "%Y%m%d %H:%M:%S").date().isoformat()
    except (ValueError, TypeError):
        return None


def restore(con, items, day):
    """舊 CSV 缺日期時，公會正式值須與該交易日已存正式值完全相符。"""
    for item in items:
        if item.get("nav") is None or valid_day(item.get("nav_date")):
            continue
        source = item.get("nav_source") or ""
        resolved, url = None, None
        if source == "證交所（投信預估淨值）":
            resolved, url = timestamp_day(item.get("source_time")), MIS_URL
        elif source == "投信投顧公會（正式淨值）":
            r = con.execute("SELECT nav_official FROM daily WHERE code=? AND date=?", (item["code"], day)).fetchone()
            if r and r[0] == item["nav"]:
                resolved, url = valid_day(day), SITCA_URL
        if resolved:
            item["nav_date"], item["nav_source_url"] = resolved, url
            con.execute("UPDATE daily SET nav_date=?, nav_source_url=? WHERE code=? AND date=?", (resolved, url, item["code"], day))
