"""各家投信官網的資料抓取模組。

每個模組提供 `fetch(get_json, fixtures=None, context=None, **kwargs) -> dict[代號, dict]`，回傳統一格式的紀錄。
主程式另外傳入 `dividend_codes`（資料庫已有該投信配息紀錄的代號），需要回補歷史的模組可據此決定查詢範圍；
用不到的模組以 `**_` 忽略即可。紀錄欄位：

    code            代號
    issuer          投信簡稱
    source          來源名稱（網站上標示出處用）
    nav_official    投信公告的正式淨值
    nav_date        正式淨值的日期 YYYY-MM-DD
    nav_estimate    投信公布的預估淨值；nav_estimate_date 為其日期（有提供的投信才有）
    units_change    當日申購買回造成的發行單位數增減（對應 nav_date）
    aum             基金規模（億元）
    units           發行單位數
    beneficiaries   受益人數；beneficiaries_month 為資料年月 YYYY-MM
    mgmt_fee        經理費率（%），只有單一費率或官方公布實際費率時才有數字
    mgmt_fee_text   經理費原文；custodian_fee_text 保管費原文
    manager / risk_class / list_date / index_name
    stats           投資組合統計 {date, div_yield, pe, pb, roe, beta, volatility}
    holdings        成分 [{asset_code, asset_name, kind, weight, qty}]
    dividends       配息紀錄 [{code, ex_date, pay_date, cash}]（有提供的投信才有）

抓不到的欄位一律為 None，不可估計或補值。
"""
