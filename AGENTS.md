# 微風流動 · 台股 ETF 資料庫：協作規則

本檔是所有 AI 助手（Codex、Claude 等）與人類協作者的共同規則。修改程式前請先讀完。

## 專案是什麼

免費、公開的台股全市場 ETF 研究網站。每個交易日自動抓資料、存進 SQLite 累積歷史，並發布成一頁式 ETF 總覽（GitHub Pages）。目前不收費，目標是把資料庫做大、做正確。

## 檔案

| 路徑 | 內容 |
|---|---|
| `scripts/fetch_etf.py` | 抓資料、查核、合併、寫入資料庫與 JSON |
| `scripts/schema.sql` | 資料庫結構（`etf`、`daily`、`dividend`、`price_check`、`holding`） |
| `scripts/store.py` | 資料保存：歷史資料存成 `data/history/` 的 CSV，執行時重建 SQLite |
| `scripts/issuers/` | 各投信官網的抓取模組（一家一個檔案） |
| `data/history/` | **資料庫本體**（CSV，依日期分檔）。`data/etf.db` 是執行時產生的，不提交 |
| `web/index.html` | 網站（單一檔案，讀 `web/data/latest.json`） |
| `tests/` | 離線測試；`tests/fixtures/` 是測試用假資料 |
| `data/etf_meta.csv` | 人工補充資料（類型修正、費用率、規模） |
| `.github/workflows/daily.yml` | 週一到週五台灣時間 08:50、18:00 自動更新並部署 |

## 資料來源規則（最重要）

| 資料 | 主要來源 | 查核／備援 |
|---|---|---|
| 淨值、規模、費用率、除息日、發放日、配息金額、成分股 | **各投信官網公告**（逐家接入中） | 證交所、櫃買中心開放資料 |
| 收盤價 | **Yahoo 股市** | 證交所、櫃買中心逐檔查核；不一致時採用交易所並記錄在 `price_check` |
| 成交量 | 證交所、櫃買中心官方總量 | Yahoo（實測系統性偏低 1～8%，只作備援） |

- **絕對不可編造或估計數字。** 抓不到就留空（`None` / `—`），並在輸出中說明。
- 新增來源時，資料庫要記錄來源，讓網站能標示出處。
- 抓取要有禮貌：限制並行數、遇到被擋（HTTP 403 / 代理拒絕）就停止，不要狂重試。

## 開發規則

- 只用 **Python 標準函式庫**（GitHub Actions 上不安裝套件）。
- 改程式就要補測試。執行測試：`python -m unittest discover -s tests -v`
- 測試只用 `tests/fixtures/` 的假資料，不連網路。
- 網站維持單一 `web/index.html`，不引入建置工具；顏色用 CSS 變數，深淺色模式都要能看。
- 程式註解、文件、網頁文字使用**繁體中文**。

## Git 規則（避免互相覆蓋）

- **不要直接推 `main`。** 每項工作開新分支（例如 `codex/改善手機版表格`、`claude/接元大投信`），完成後開 Pull Request。
- **不要提交 `data/history/` 和 `web/data/latest.json` 的變更。** 這些只由每日排程機器人更新；本機測試產生的變更請還原：`git checkout -- data/history web/data/latest.json && git clean -fd data/history`
- `data/etf.db` 已列入 `.gitignore`，不會也不應提交。資料庫結構有變動時，同步更新 `scripts/store.py` 的 `MIGRATIONS`。
- 開工前先 `git pull origin main`，PR 合併前確認測試通過。
- 一個 PR 只做一件事，說明寫清楚改了什麼、怎麼驗證。
- **合併權限（擁有者授權）**：Claude 的分支在全部測試通過後，可由 Claude 自行合併進 `main`；Codex 與其他協作者的修改，須由擁有者確認後合併（可請 Claude 先檢查）。

## 分工（避免同時改同一個檔案）

| 負責範圍 | 主要檔案 |
|---|---|
| 資料抓取、投信接入、資料庫 | `scripts/`、`tests/` |
| 網站介面、圖表、手機版 | `web/index.html` |

要跨範圍修改時，先在 PR 說明中註明。

## 合規

本站是投資研究與教育用途：不提供個別有價證券的買賣建議、不推薦標的、不預測漲跌。網頁文字請維持中立描述。
