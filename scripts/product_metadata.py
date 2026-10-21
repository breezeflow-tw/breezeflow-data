"""保留公會申報欄位，將明示地域及商品定位整理成篩選標籤。"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

SOURCE_URL = "https://www.sitca.org.tw/ROC/Industry/IN2105.aspx?pid=IN2212_02"
MAIN_LINES = {"00625K":"006205", "00636K":"00636", "00643K":"00643", "00657K":"00657", "00668K":"00668", "00687C":"00687B"}
CLASSES = {"AH11":"股票", "AH13":"股票", "AH21":"股票", "AH23":"股票", "AL11":"股票", "AL21":"股票",
           "AH22":"債券", "AH24":"債券", "AL22":"債券", "AH25":"不動產", "AH26":"多重資產"}


@lru_cache(maxsize=1)
def reference():
    return json.loads((Path(__file__).resolve().parent.parent / "data/product_reference.json").read_text("utf-8"))


def official_fund(code, funds):
    """同基金外幣線僅共用投資定位；數值及其幣別不可由主交易線繼承。"""
    key = code if code in funds or code in reference()["funds"] else MAIN_LINES.get(code, code)
    fresh = funds.get(key) or {}
    saved = reference()["funds"].get(key) or {}
    # 代碼相同但基金統編不同，不可沿用舊商品資料。
    if fresh.get("fund_id") and saved.get("fund_id") != fresh["fund_id"]:
        saved = {}
    if fresh.get("class_code") or fresh.get("investment_region"):
        return fresh, key, fresh.get("data_month"), fresh.get("fetched")
    return ({**fresh, **saved} if saved else fresh), key, reference()["data_month"] if saved else fresh.get("data_month"), reference()["checked_at"] if saved else fresh.get("fetched")


def product_name(name):
    # 不讓發行公司「中國信託」或母傘型名稱「美日」成為投資地域。
    name = re.split(r"\(\s*(?:本)?(?:子)?基金", unicodedata.normalize("NFKC", name or ""))[0]
    name = name.rsplit("之", 1)[-1]
    return re.sub(r"^(?:富蘭克林華美|中國信託|華南永昌|貝萊德|大華銀|元大|國泰|富邦|永豐|群益|野村|兆豐|復華|統一|凱基|台新|第一金|安聯|摩根|聯博|玉山|聯邦)", "", name).strip()


def region_label(name, scope, cls):
    """只辨認官方全名明示的地域；否則清楚標成公告可投資範圍。"""
    if cls in ("AH11", "AH13", "AL11"):
        return "臺灣", "公會國內投資分類"
    for words, label in (("台日韓|臺日韓", "臺灣、日本、韓國"), ("台美|臺美", "臺灣、美國"),
                         ("臺韓|台韓", "臺灣、韓國"), ("全球|環球|世界", "全球"), ("新興市場", "新興市場"),
                         ("已開發市場|成熟市場", "已開發市場"), ("亞太", "亞太"), ("北美", "北美"),
                         ("亞洲", "亞洲"), ("美國", "美國"), ("日本|日經|東證|東証", "日本"),
                         ("韓國", "韓國"), ("越南", "越南"), ("印度", "印度"), ("澳洲", "澳洲"),
                         ("歐洲", "歐洲"), ("中國|上證|上証|滬深|深証|深證|中証|中證", "中國大陸"),
                         ("台灣|臺灣|台股", "臺灣")):
        if re.search(words, name):
            return label, "公會基金全名明示；不代表全部持倉或發行人國籍"
    scope = (scope or "").strip()
    if scope in ("台灣", "臺灣", "中華民國", "國內", "中華民國境內"):
        return "臺灣", "公會申報投資範圍"
    if scope in ("美國", "美國市場", "日本", "日本市場", "香港", "全球", "全球市場", "全球區域", "新興市場", "大陸", "大陸地區"):
        return {"大陸":"中國大陸", "大陸地區":"中國大陸", "美國市場":"美國", "日本市場":"日本", "全球市場":"全球", "全球區域":"全球"}.get(scope, scope), "公會申報投資範圍"
    if scope and scope != "詳公開說明書":
        return "跨國（公告範圍）", "公會申報可投資範圍；主要市場請核對追蹤指數／公開說明書"
    return None, None


def strategy_label(name, asset, management):
    """本站整理官方全名的明示定位，不宣稱為投信的標準分類。"""
    if not name:
        return None
    if asset == "債券":
        for words, label in (("非投資等級", "非投資等級債"), ("政府|公債", "政府公債"), ("主權", "主權／類主權債"),
                             ("金融|銀行", "金融債"), ("電信", "電信業公司債"), ("科技", "科技業公司債"),
                             ("能源", "能源業公司債"), ("製藥", "製藥業公司債"), ("公用事業", "公用事業公司債"),
                             ("投資級|投資等級|A級|A等級|BBB|高評", "投資等級債"), ("公司債|企業債", "公司債")):
            if re.search(words, name):
                return label
        return "主動債券配置" if management == "主動式" else "債券指數追蹤"
    if asset == "多重資產":
        return "股債平衡"
    if asset == "不動產":
        return "不動產證券化"
    for words, label in (("半導體|晶圓|IC設計", "半導體主題"), ("高股息.*低波|低波.*高股息|低波動.*股利", "高股息低波動"),
                         ("ESG.*高股息|ESG.*高息", "ESG高股息"), ("高股息|高息|股利|優息|股息", "股息策略"),
                         ("ESG|永續|低碳|公司治理", "ESG／永續"), ("人工智慧|AI|科技|網路資安|PCB|通訊|算力|5G|元宇宙", "科技主題"),
                         ("金融|銀行", "金融主題"), ("生技|醫療|基因", "生技醫療"), ("不動產|地產", "不動產主題"),
                         ("電動車|未來車|智能車|電池|儲能|綠色電力|潔淨能源", "能源／電動車"),
                         ("太空|航太|防衛", "航太國防"), ("特別股", "特別股"), ("航運", "航運主題"),
                         ("稀土|資源", "資源主題"), ("商社", "商社主題"), ("電力|基建|基礎建設", "基礎建設"),
                         ("等權重", "等權重"), ("動能", "動能策略"), ("成長|增長", "成長策略"), ("價值", "價值策略")):
        if re.search(words, name, re.I):
            return label
    return "主動選股" if management == "主動式" else "指數追蹤"


def enrich(profile, code, funds):
    fund, key, month, checked = official_fund(code, funds or {})
    if not fund:
        return
    name, cls = product_name(fund.get("name")), fund.get("class_code")
    asset = CLASSES.get(cls) or profile.get("asset_class")
    region, basis = region_label(name, fund.get("investment_region"), cls)
    profile.update(asset_class=asset, region=region, region_basis=basis, region_scope=fund.get("investment_region"),
                   strategy=strategy_label(name, asset, profile.get("management")), strategy_basis="本站依官方基金全名整理；詳細方法以投信公告為準",
                   official_name=fund.get("name"), classification_code=cls, source="投信投顧公會基金基本資料",
                   source_url=SOURCE_URL, data_month=month, checked_at=checked, reference_code=key,
                   classification_source_url=SOURCE_URL, classification_month=month)
    if code in MAIN_LINES:
        profile["main_code"] = MAIN_LINES[code]
