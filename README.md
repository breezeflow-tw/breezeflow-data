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
| `.github/workflows/daily.yml` | 週一到週五台灣時間 08:50 與 18:00 自動執行並部署到 GitHub Pages |

## 資料來源

| 資料 | 來源 |
|---|---|
| 預估淨值、前日淨值、折溢價 | 臺灣證券交易所 ETF 預估淨值資訊 `mis.twse.com.tw/stock/data/all_etf.txt` |
| 上市收盤價、成交量 | 證交所 OpenAPI `exchangeReport/STOCK_DAY_ALL` |
| 上櫃收盤價、成交量 | 櫃買中心 OpenAPI `tpex_mainboard_daily_close_quotes` |
| 規模 | 發行單位數 × 淨值。上市用證交所 OpenAPI `opendata/t187ap47_L`（基金基本資料），上櫃用櫃買行情的 `Capitals` |
| 除息日、每單位配息 | 證交所 `exchangeReport/TWT48U_ALL`、櫃買 `tpex_exright_prepost`（除權除息預告表），每天累積進 `dividend` 表 |
| 配息發放日 | 尚未接上，需要公開資訊觀測站 `mops.twse.com.tw` |
| 總費用率 | 尚未接上，需要投信投顧公會 `www.sitca.org.tw`；目前可在 `data/etf_meta.csv` 人工填寫 |

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

## 排程

| 時間（台灣） | 做什麼 |
|---|---|
| 週一到週五 18:00 | 抓當天收盤價、成交量、預估淨值與折溢價 |
| 週一到週五 08:50 | 開盤前補上前一交易日投信公告的正式淨值，算出正式折溢價 |

週末不執行。國定假日休市時排程仍會跑，但來源資料停在前一交易日，不會產生新的一天，只會重寫同一天的資料。

## 已知限制

- GitHub 排程在整點時段較忙，實際執行時間可能延後數分鐘到半小時。

- 證交所 MIS（淨值來源）偶爾回應 502。抓不到時本次只更新價格與成交量，同一天已存的淨值不會被清掉；重跑一次即可補上。
- 投信網站目前未接。若 MIS 長期不穩，可改從各投信官網補抓淨值。
- 類型由代號與名稱自動判斷，判斷錯誤時在 `data/etf_meta.csv` 的 `category` 欄修正。
- 總費用率與配息發放日尚無自動來源（網域未開放）。
- 除息預告表只列近期資料，配息歷史要靠每天累積；越早開始跑，歷史越完整。

## 聲明

本專案為投資研究與教育用途，不提供個別有價證券的買賣建議，亦非證券投資顧問服務。
