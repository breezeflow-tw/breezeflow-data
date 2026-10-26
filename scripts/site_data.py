"""發布用的網站資料：把 latest.json 拆成列表用的精簡檔與單檔完整檔。

網站先讀 data/summary.json（列表、篩選、排行、週資金流、資料查核用得到的欄位），
打開單檔詳情時才讀 data/etf/<代號>.json（該檔完整資料）。兩者都在發布時由同一份
latest.json 產生、不提交；latest.json 仍是資料合約與完整下載檔。

本機預覽：python scripts/site_data.py（讀 web/data/latest.json，寫在同一個資料夾）。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

VERSION = 1

# 只有單檔詳情用到、體積又大的欄位，精簡檔不放；其餘欄位（含日後新增的）都保留。
# 網站列表要用到這裡的欄位時，先把它從清單移除。
DETAIL_ONLY = (
    "holdings_top", "holdings_changes", "sitca_monthly", "monthly", "stats", "fee", "fee_source",
    "nav_source_url", "aum_source_url", "units_source_url", "holdings_source_url",
    "beneficiaries", "beneficiaries_month", "beneficiaries_source", "beneficiaries_source_url",
    "index_name", "manager", "risk_class", "listed_date", "units_change", "nav_prev", "source_time",
)
DETAIL_ONLY_NESTED = {
    # 週資金流：列表只用本週與近 4 週數字；逐週明細、逐日來源與方法說明留給詳情。
    "flows": ("weeks", "sources", "events_status", "method", "trading_days", "closed_days", "calendar",
              "latest_official_date"),
    # 商品資料：列表只用分類與幣別；出處連結、判斷依據與公告原文留給詳情。
    "product": ("official_name", "source", "source_url", "checked_at", "rules_url", "currency_basis",
                "currency_source_url", "region_scope", "region_basis", "strategy_basis", "classification_code",
                "classification_month", "classification_source_url", "reference_code", "data_month",
                "trading_unit", "fund_currency", "listing_source", "listing_source_url", "listing_checked_at"),
    # 區間報酬：排行榜只用市價報酬。
    "returns": ("total",),
}
# 代號直接當檔名，只接受數字與大寫英文字母。
CODE = re.compile(r"[0-9A-Z]{4,8}")


def summary_item(item):
    out = {key: value for key, value in item.items() if key not in DETAIL_ONLY}
    for key, drop in DETAIL_ONLY_NESTED.items():
        if isinstance(out.get(key), dict):
            out[key] = {k: v for k, v in out[key].items() if k not in drop}
    # 資料查核要統計「有前十大持股」的檔數；精簡檔只留筆數，不放成分本身。
    top = item.get("holdings_top")
    out["holdings_count"] = len(top) if isinstance(top, list) else 0
    return out


def build(payload):
    """回傳（精簡檔, {代號: 單檔完整檔}）；單檔帶產生時間，網站據此確認與列表是同一次發布。"""
    stamp = {"version": VERSION, "generated_at": payload.get("generated_at"), "trade_date": payload.get("trade_date")}
    details = {}
    for item in payload.get("items") or []:
        code = item.get("code")
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise ValueError(f"ETF 代號不能當成檔名：{code!r}，停止產生網站資料。")
        if code in details:
            raise ValueError(f"ETF 代號重複：{code}，停止產生網站資料。")
        details[code] = {**stamp, "item": item}
    summary = {**payload, "split": {**stamp, "detail": "etf/{code}.json"},
               "items": [summary_item(item) for item in payload.get("items") or []]}
    return summary, details


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write(payload, out_dir):
    """寫入 summary.json 與 etf/*.json；舊代號的單檔先刪除，精簡檔最後寫。"""
    out_dir = Path(out_dir)
    summary, details = build(payload)
    folder = out_dir / "etf"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    for code, data in details.items():
        (folder / f"{code}.json").write_text(dump(data), "utf-8")
    (out_dir / "summary.json").write_text(dump(summary), "utf-8")
    return summary, details


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", default="web/data/latest.json", help="完整資料檔（預設 web/data/latest.json）")
    args = parser.parse_args()
    source = Path(args.source)
    summary, details = write(json.loads(source.read_text("utf-8")), source.parent)
    print(f"已寫入 {source.parent / 'summary.json'} 與 {len(details)} 個單檔（{source.parent / 'etf'}）")
