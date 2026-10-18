# 微風流動 · 台股 ETF 資料庫

免費、公開的台股全市場 ETF 研究資料庫。每個交易日收盤後自動抓取市價、預估淨值、折溢價與成交量，存進 SQLite 累積成歷史資料，並發布成一頁式 ETF 總覽網站。

## 資料夾

| 路徑 | 內容 |
|---|---|
| `scripts/fetch_etf.py` | 抓資料、分類、合併、寫入資料庫與 JSON（只用 Python 標準函式庫） |
| `scripts/schema.sql` | 資料庫結構：`etf` 基本資料、`daily` 每日快照 |
| `data/etf.db` | SQLite 資料庫（每日自動更新並提交） |
| `data/etf_meta.csv` | 人工補充資料：發行商、類型修正、總費用率、規模 |
| `web/index.html` | ETF 總覽網頁，讀取 `web/data/latest.json` |
| `tests/` | 離線測試與測試用假資料 |
| `.github/workflows/daily.yml` | 週一到週五 14:47（台灣時間）自動執行並部署到 GitHub Pages |

## 資料來源

| 資料 | 來源 |
|---|---|
| 預估淨值、前日淨值、折溢價 | 臺灣證券交易所 ETF 預估淨值資訊 `mis.twse.com.tw/stock/data/all_etf.txt` |
| 上市收盤價、成交量 | 證交所 OpenAPI `exchangeReport/STOCK_DAY_ALL` |
| 上櫃收盤價、成交量 | 櫃買中心 OpenAPI `tpex_mainboard_daily_close_quotes` |
| 總費用率、規模 | 目前由 `data/etf_meta.csv` 人工維護，之後改接投信投顧公會 |

## 本機執行

```bash
python -m unittest discover -s tests -v   # 離線測試
python scripts/fetch_etf.py               # 抓真實資料（需要能連到上面三個網站）
python -m http.server -d web 8000         # 開 http://localhost:8000 看網頁
```

## 上線步驟

1. 把這個資料夾推到 GitHub 新 repo。
2. repo 的 Settings → Pages → Source 選「GitHub Actions」。
3. Actions 頁面手動執行一次「每日更新 ETF 資料」，之後每個交易日會自動跑。

## 已知限制

- 證交所 MIS（淨值來源）偶爾回應 502。抓不到時本次只更新價格與成交量，同一天已存的淨值不會被清掉；重跑一次即可補上。
- 投信網站目前未接。若 MIS 長期不穩，可改從各投信官網補抓淨值。
- 類型由代號與名稱自動判斷，判斷錯誤時在 `data/etf_meta.csv` 的 `category` 欄修正。
- 總費用率與規模尚無自動來源，需在 `data/etf_meta.csv` 填寫。

## 聲明

本專案為投資研究與教育用途，不提供個別有價證券的買賣建議，亦非證券投資顧問服務。
