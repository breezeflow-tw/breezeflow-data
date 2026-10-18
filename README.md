# 微風流動 · 台股 ETF 資料庫

免費、公開的台股全市場 ETF 研究資料庫。每個交易日收盤後自動抓取市價、預估淨值、折溢價與成交量，存進 SQLite 累積成歷史資料，並發布成一頁式 ETF 總覽網站。

## 資料夾

| 路徑 | 內容 |
|---|---|
| `scripts/fetch_etf.py` | 抓資料、分類、合併、寫入資料庫與 JSON（只用 Python 標準函式庫） |
| `scripts/schema.sql` | 資料庫結構：`etf` 基本資料、`daily` 每日快照、`dividend` 配息、`holding` 成分股、`price_check` 查核紀錄 |
| `scripts/store.py` | 資料保存：`data/history/` 的 CSV ↔ SQLite |
| `data/history/` | 資料庫本體，CSV 依日期分檔（`daily/`、`holdings/`），每日自動更新並提交 |
| `data/etf.db` | 執行時從 `data/history/` 重建的 SQLite，不提交；網站上提供下載 `data/etf.db` |
| `data/etf_meta.csv` | 人工補充資料：發行商、類型修正、總費用率、規模 |
| `web/index.html` | ETF 總覽網頁，讀取 `web/data/latest.json` |
| `tests/` | 離線測試與測試用假資料 |
| `.github/workflows/daily.yml` | 週一到週五台灣時間 08:50 與 18:00 自動執行並部署到 GitHub Pages |

## 資料來源

| 資料 | 來源 |
|---|---|
| 已接入投信的 ETF：正式／預估淨值、規模、發行單位數、申購買回增減、經理費、保管費、成分股、配息紀錄 | 各投信官網（`scripts/issuers/`）：元大 `etfapi.yuantaetfs.com`、統一 `www.ezmoney.com.tw`、群益 `www.capitalfund.com.tw`、復華 `www.fhtrust.com.tw`、凱基 `www.kgifund.com.tw`、富邦 `websys.fsit.com.tw/FubonETF`、國泰 `cwapi.cathaysite.com.tw` |
| 尚未接入投信的 ETF：預估淨值、前日淨值、折溢價 | 臺灣證券交易所 ETF 預估淨值資訊 `mis.twse.com.tw/stock/data/all_etf.txt`（投信提供、證交所轉載） |
| 收盤價、成交量（主要） | Yahoo 股市日線 `query1.finance.yahoo.com/v8/finance/chart/{代號}.TW`（上櫃為 `.TWO`） |
| 收盤價、成交量（查核） | 證交所 OpenAPI `exchangeReport/STOCK_DAY_ALL`、櫃買 `tpex_mainboard_daily_close_quotes`。與 Yahoo 不一致時採用交易所數字，差異記錄在 `price_check` 表 |
| 規模 | 發行單位數 × 淨值。上市用證交所 OpenAPI `opendata/t187ap47_L`（基金基本資料），上櫃用櫃買行情的 `Capitals` |
| 除息日、每單位配息、發放日 | 投信官網配息紀錄為主（統一、群益、復華、凱基、富邦、國泰，含發放日）；證交所 `exchangeReport/TWT48U_ALL`、櫃買 `tpex_exright_prepost` 除權息預告補充尚未公布的部分 |
| 總費用率 | 尚未接上，需要投信投顧公會 `www.sitca.org.tw`；目前可在 `data/etf_meta.csv` 人工填寫 |

## 資料下載

- 最新總覽：`https://htc20025-oss.github.io/breezeflow-data/data/latest.json`
- 完整 SQLite 資料庫：`https://htc20025-oss.github.io/breezeflow-data/data/etf.db`
- 歷史 CSV：本 repo 的 `data/history/`

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
- 已接入投信：元大（54 檔）、統一（11 檔）、群益（29 檔）、復華（21 檔）、凱基（18 檔）、富邦（50 檔）、國泰（41 檔）。各投信網站同時抓取。其餘投信陸續接入，接入前沿用交易所資料。
- 元大的配息歷史在 `api.yuantafunds.com`，尚未接上；配息暫用交易所除權息預告表。
- 經理費多為依規模分段收費，程式不自行換算，只在投信公布單一費率或實際費率時記錄數字，其餘保留原文。
- 債券型、海外型 ETF 的正式淨值通常晚一天公布，當天折溢價使用預估淨值。
- Yahoo 股市使用條款限制自動抓取與轉載，介面也可能變更或封鎖雲端主機；連不上時自動改用交易所資料。
- 類型由代號與名稱自動判斷，判斷錯誤時在 `data/etf_meta.csv` 的 `category` 欄修正。
- 總費用率尚無自動來源。元大與尚未接入投信的 ETF 沒有配息發放日。
- 除息預告表只列近期資料，配息歷史要靠每天累積；越早開始跑，歷史越完整。

## 聲明

本專案為投資研究與教育用途，不提供個別有價證券的買賣建議，亦非證券投資顧問服務。
