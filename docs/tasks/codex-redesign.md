# Codex 任務：網站重新美編

| 項目 | 內容 |
|---|---|
| 分支 | `codex/redesign`（已從最新 `main` 開好，本檔就在這個分支） |
| 修改範圍 | 只改 `web/index.html`。不要動 `scripts/`、`tests/`、`data/`、`web/data/latest.json` |
| 完成方式 | 開 Pull Request 到 `main`，由擁有者確認後合併（Claude 可協助檢查） |
| 開工前 | 先讀 `AGENTS.md`（協作規則、網站資料格式），再 `git pull origin main` |

## 1. 背景

微風流動是免費、公開的台股全市場 ETF 研究網站（約 360 檔），每個交易日 08:50、18:00 自動更新。網站只有一個檔案 `web/index.html`，資料只讀 `web/data/latest.json`。

資料庫最近大幅擴充，網頁還沒跟上：

- 已接入 6 家投信官網：元大、統一、群益、復華、凱基、富邦，約 180 檔 ETF 有官網公告的淨值、規模、申購買回單位增減、成分股、配息（含發放日）。
- 新欄位（格式見 `AGENTS.md`「網站資料格式」）目前網頁大多沒有顯示：
  `nav_source`、`aum_source`、`units_change`、`beneficiaries`、`beneficiaries_month`、`holdings_top`、`stats`、`risk_class`、`listed_date`、`index_name`、`manager`、`mgmt_fee_text`、`custodian_fee_text`、`price_check`。
- 「資料來源」段落的文字已過時（還寫「元大 ETF 的正式淨值……取自元大投信官網；其他投信陸續接入」）。

### 目前的版面

品牌列 → 簡介 → 市場摘要（4 格）→ 篩選列（類型標籤、發行投信下拉、「只看即將除息」、「只看折溢價超過 ±1%」、搜尋）→ 全市場 13 欄表格（代號、名稱／發行商、類型、市價、漲跌、淨值、折溢價、成交量、規模、經理費、除息日、每單位配息、發放日）→ 說明（折溢價怎麼算、淨值與市價、資料來源）→ 頁尾聲明。

### 尚未合併的相關分支

`codex/etf-detail`（ETF 詳細資訊面板＋網址代號直連，例如 `#0050`）還沒合併進 `main`，和本任務改的是同一個檔案。請先把它併進本分支再開始美編（實測可乾淨合併），把詳細面板納入新設計：

```bash
git fetch origin && git merge origin/codex/etf-detail
```

## 2. 目標

1. **整體視覺重新設計**：清爽、專業、資訊密度高但好讀的金融資料網站。品牌名「微風流動」可延伸出輕盈、流動的視覺語言；主色可沿用現在的青綠，也可重新定義整組色票。
2. **市場摘要**：做成清楚的摘要卡，至少包含 ETF 檔數、資料交易日與更新時間（`trade_date`、`generated_at`）、折溢價超過 ±1% 的檔數、即將除息的檔數、市價查核結果（`price_check` 統計）。
3. **全市場表格**：數字用等寬字、右對齊、千分位；表頭固定、代號欄固定；漲跌與折溢價的顏色語意清楚；排序與篩選保留。淨值欄用小標籤標示出處：正式淨值或預估淨值、來自投信官網或證交所（看 `nav_source`）。
4. **ETF 詳細面板**（以 `codex/etf-detail` 為基礎）：點任一檔展開，顯示該檔所有欄位，例如：
   - 淨值與出處、前一營業日淨值、折溢價、市價查核結果
   - 規模（`aum`）與出處、發行單位數、當日單位增減（`units_change`，正數是淨申購、負數是淨買回）
   - 受益人數與資料年月
   - 前 10 大成分（`holdings_top`，可用純 CSS／SVG 長條表示權重，不要引入圖表套件）
   - 風險等級、上市日、追蹤指數、經理人、經理費與保管費原文
   - 下一次與最近一次除息（除息日、每單位配息、發放日、出處）
   - 投資組合統計 `stats`（有資料才顯示）
5. **手機版**：375px 寬也好用。表格可在框內橫向捲動（代號欄固定），或改成卡片列表；篩選列可收合成一個「篩選」按鈕。整頁不可出現橫向捲軸。
6. **資料來源段落改成自動產生**：已接入的投信名單從資料推導（`nav_source` 或 `aum_source` 含「官網」的發行投信），不要寫死在文字裡。

## 3. 必須遵守（摘自 AGENTS.md）

- 維持單一 `web/index.html`，原生 HTML／CSS／JavaScript，不引入建置工具或前端框架。
- 顏色一律用 CSS 變數，淺色、深色模式都要清楚。保留 `prefers-color-scheme` 自動切換和 `data-theme` 手動切換。
- 台股慣例：漲紅、跌綠。折價、溢價維持各自的顏色語意。
- 欄位是 `null` 就顯示「—」。**不可自行估算或補值**，也不要顯示 0、`NaN`、`undefined`。
- 網頁文字用繁體中文。
- 合規：本站是研究與教育用途，文字維持中立描述。不提供買賣建議、不推薦標的、不預測漲跌（避免「推薦」「必買」「看好」「飆漲」之類字眼）。頁尾聲明保留。
- 不要改資料格式。如果需要新欄位，在 PR 說明寫出需求，由負責資料的 Claude 在 `scripts/` 補上。
- 不要提交 `web/data/latest.json` 和 `data/history/` 的變更。

### 現有功能都要保留

類型篩選、發行投信篩選、只看即將除息、只看折溢價超過 ±1%、搜尋、欄位排序（滑鼠與鍵盤）、淨值出處標示、市價查核說明（交易所修正過的價格要能看出來）、除息日、每單位配息、發放日、下載連結（`data/etf.db`、`data/latest.json`）、測試資料提示（`source` 不是 `live` 時）、載入中與載入失敗的提示。

## 4. 本機測試

**真實資料（建議）**：從線上網站下載最新 JSON。週一 08:50 排程跑完後就會是含 6 家投信資料的新格式；擁有者也可以先到 GitHub Actions 手動執行一次「每日更新 ETF 資料」。

```bash
curl -o web/data/latest.json https://breezeflow-tw.github.io/breezeflow-data/data/latest.json
python -m http.server -d web 8000      # 開 http://localhost:8000
git checkout -- web/data/latest.json   # 用完一定要還原
```

**測試用假資料**（檔數少，但有各種欄位）：

```bash
python scripts/fetch_etf.py --fixtures tests/fixtures --db /tmp/test.db --out web/data/latest.json
python -m http.server -d web 8000
git checkout -- data/history web/data/latest.json && git clean -fd data/history   # 用完還原
```

## 5. 驗收清單

- [ ] 桌機（1280px）和手機（375px）版面都正常，整頁沒有橫向捲軸
- [ ] 淺色、深色模式都清楚，文字對比符合 WCAG AA
- [ ] 第 3 節列出的現有功能都還在，而且能正常使用
- [ ] 詳細面板能顯示第 2 節列出的欄位；沒資料的欄位顯示「—」或整區隱藏
- [ ] 網址代號直連可用（例如 `#0050`）
- [ ] 鍵盤可以操作排序、篩選、開關詳細面板，焦點看得見
- [ ] 360 檔資料下操作順暢（搜尋輸入不卡頓），瀏覽器主控台沒有錯誤
- [ ] `python -m unittest discover -s tests -v` 全部通過
- [ ] PR 說明附上桌機／手機、淺色／深色截圖，並寫清楚改了什麼、怎麼驗證

## 6. 之後可以另開分支的點子（不要放進這次的 PR）

- 「ETF 資金流向」排行：依當日發行單位數增減占總單位數的比例（`units_change ÷ units`）排序，看哪些 ETF 淨申購或淨買回最多。
- 單檔 ETF 的折溢價、規模歷史走勢（需要 Claude 先在資料端輸出歷史資料）。
