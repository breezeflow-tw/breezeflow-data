"""投信投顧公會 ETF 月明細；月資料不拆成週資料。只用公開查詢頁。"""
from __future__ import annotations

import math
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.sitca.org.tw/ROC/SITCA_ETF/"
SOURCE = "投信投顧公會（SITCA）月統計"


class Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("th", "td"):
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, value):
        if self.cell is not None:
            self.cell.append(value)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []


def number(value):
    try:
        value = float(value.replace(",", "").strip())
        return value if math.isfinite(value) else None
    except (ValueError, AttributeError):
        return None


def latest_month(page):
    m = re.search(r'<select[^>]*id="[^"]*ddlYM"[^>]*>(.*?)</select>', page, re.S)
    values = re.findall(r'value="(\d{6})"', m[1]) if m else []
    return max(values) if values else None


def parse(page, month):
    if not re.fullmatch(r"\d{4}(?:0[1-9]|1[0-2])", month):
        raise ValueError("公會資料年月無效")
    p = Rows()
    p.feed(page)
    fields = {"aum": "基金規模(台幣)", "nav": "單位淨值(台幣)", "units": "受益權單位數",
              "subscriptions": "本月申購總金額(台幣)", "redemptions": "本月買回總金額(台幣)",
              "beneficiaries": "總受益人數"}
    header, result = None, {}
    updated = re.search(r'lblMRdate"[^>]*>\s*(\d{4})\.(\d{2})\.(\d{2})', page)
    for row in p.rows:
        if "證券代號" in row:
            header = row
            if not all(k in header for k in (*fields.values(), "計價幣別")):
                raise ValueError("公會月統計欄位已改變")
            continue
        if not header or len(row) != len(header):
            continue
        d = dict(zip(header, row))
        code = d["證券代號"]
        if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
            continue
        if code in result:
            raise ValueError(f"公會證券代號重複：{code}，不猜測級別")
        result[code] = {"code": code, "month": f"{month[:4]}-{month[4:]}", "currency": d["計價幣別"],
                        **{k: number(d[v]) for k, v in fields.items()}, "source": SOURCE,
                        "source_url": f"{BASE}etf_statement2.aspx?txtYM={month}&txtR1=0",
                        "published_at": "-".join(updated.groups()) if updated else None}
        # 公會金額原欄位以新臺幣元列示，統一以億元保存；非新臺幣級別的淨值不混用。
        for key in ("aum", "subscriptions", "redemptions"):
            if result[code][key] is not None:
                result[code][key] /= 1e8
        if d["計價幣別"] != "TWD":
            result[code]["nav"] = None
    if not header:
        raise ValueError("公會頁面沒有月統計表")
    return result


def fetch(fixtures: Path | None = None, context=None):
    def load(name, path):
        if fixtures:
            file = fixtures / (name + ".html")
            return file.read_text("utf-8") if file.exists() else ""
        request = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0 (breezeflow-data)"})
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            return response.read().decode("utf-8-sig")
    try:
        month = latest_month(load("index", "etf_statement.aspx"))
        if not month:
            return {}
        if not fixtures:
            time.sleep(1)
        return parse(load(month, f"etf_statement2.aspx?txtYM={month}&txtR1=0"), month)
    except (OSError, ValueError) as exc:
        print(f"警告：公會月統計本次未取得（{exc}），不補猜、不重試被擋請求。", file=sys.stderr)
        return {}
