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
  - 例外之一是擁有者要求的週申贖 `flows`：用官方的單位數增減 × 同日正式淨值推算，一律標示「估算」並附公式，資料不齊就留空，不補零。
  - 擁有者另於本次授權 `active_holdings`：完整公告持股的逐日股數增減 × 同日官方收盤價。這是持股變動估值，不是實際成交或扣除申贖後的主動買賣；每日先做，週月待完整歷史累積。缺值、日期或市場身分不明、疑似公司行動不排名。
- 新增來源時，資料庫要記錄來源，讓網站能標示出處。
- 新增或變更資料來源前，先在 `docs/data-licensing.md` 記錄官方條款或授權證據（連結、摘錄、取得時間）。沒有證據就標「待確認」；公開可讀、免費或免責聲明都不等於有再散布授權。
- 抓取要有禮貌：限制並行數、遇到被擋（HTTP 403 / 代理拒絕）就停止，不要狂重試。
- 擁有者指示不收錄的代號列在 `scripts/fetch_etf.py` 的 `EXCLUDED`（目前是 00847B）：網站不顯示、資料庫不保存。
- 市價、淨值是 0 代表沒有成交或尚未計算，一律當成沒有資料（`null`）。

## 網站資料格式（`web/data/latest.json`）

網站只讀這一個檔案。欄位沒有資料時一律是 `null`，畫面請顯示「—」，**不可自行估算或補值**。

最上層：`generated_at`（產生時間）、`trade_date`（交易日）、`source`（`live` 或 `fixture`）、`has_nav`、`price_check`（市價查核統計）、`returns_info`（區間報酬的出處 `source` 與各區間起始日 `starts`）、`active_changes`（主動式 ETF 持股異動的台股彙總，見下）、`active_holdings`（主動式 ETF 持股資金動向，見下）、`attributions`（顯名資訊，見下）、`count`、`items`（每檔 ETF 一筆）。

`attributions`：這次用到、已在政府資料開放平臺列為政府資料開放授權條款第 1 版的資料集，依條款顯名用 `[{provider, dataset, dataset_url, source_url, data_date, license, license_url}]`：提供機關、資料名稱、資料集頁、取得的端點、資料日期（年份取這個日期）、授權條款名稱與連結。目前只有交易所的收盤行情（上市資料集 11549、上櫃資料集 11370）；沒有用到時為空陣列。其他來源的授權狀態見 `docs/data-licensing.md`，不在此列不代表已取得授權。

`active_holdings`：主動式 ETF 持股資金動向（第 1 版），完整合約見 `docs/tasks/active-holdings-contract.md`。`{version, as_of, source, method, price_history_from, attributions, periods: {1d, 1w, 1m}}`。估算口徑是投信官網完整持股清單裡台股股票的股數增減 × 當日官方收盤價（億元），週、月為逐日日值加總；不是實際成交金額，也未扣除申購買回影響，每萬單位持股（`unit_qty`）只是配置密度。每個 period `{start, end, baseline, days, status, reason, reason_code, included_etfs, excluded_etfs, excluded_stocks, lists, stocks}`：`status` 為「可計算」或「資料不足」（不足時 `stocks` 為空陣列，不出排行）；每檔 ETF 必須有 `baseline` 到 `end` 每個交易日的官網完整清單；`stocks` 完整不截斷，每檔 `{asset_code, asset_name, net_amount, buy_amount, sell_amount, buy_etfs, sell_etfs, net_shares, complete, missing_price_dates, excluded_etfs, prices, etfs}`，缺股價或有 ETF 部分被排除時 `complete` 為 `false`、金額為 `null`（不當成 0），檔數保留可靠的部分；疑似除權、分割、減資、配股入帳、代碼變更與缺股數列在 `excluded_stocks`。網站文字維持中立，不寫成買賣建議。

`active_changes`：全部主動式 ETF（代號結尾 A、D）同一天的台股持股異動彙總，沒有可比對的資料時為 `null`。`{date, etf_count, etfs, not_included, buys, sells, source, method}`：`buys`／`sells` 各列最多 30 檔股票 `{asset_code, asset_name, buy_etfs, sell_etfs, buyers, sellers, buy_shares, sell_shares, buy_amount, sell_amount, net_shares}`，依加碼（含新增）或減碼（含刪除）的 ETF 檔數排序，金額為億元（增減股數 × 該日交易所收盤價，缺收盤價時為 `null`）。只列入有投信公告發行單位數、能判斷申購買回影響的 ETF。加碼＝新增，或股數增加且超出申購買回比例至少 1 個百分點；減碼＝刪除，或股數減少且超出比例至少 −1 個百分點；股數沒變的不算減碼。這是持股異動的統計，網站文字維持中立，不寫成買賣建議。

`items[]` 每筆：

| 欄位 | 說明 |
|---|---|
| `code`、`name`、`issuer`、`category`、`market` | 代號、名稱、發行投信、類型、上市／上櫃 |
| `price`、`change_pct`、`volume_lots`、`price_check` | 收盤價、漲跌幅 %、成交量（張）、市價查核結果 |
| `nav`、`nav_source` | 淨值與出處；出處含「正式淨值」代表投信公告的正式淨值（投信官網，或投信投顧公會的每日基金淨值），其餘為預估淨值（投信官網公布或證交所轉載）。優先順序：投信官網正式淨值 → 投信投顧公會正式淨值 → 投信官網預估淨值 → 證交所轉載的預估淨值 |
| `nav_date`、`nav_source_url` | 淨值的資料日期與原文連結（正式淨值才有連結） |
| `source_time` | 證交所轉載的預估淨值資料時間（YYYYMMDD HH:MM:SS），沒有用到時為空字串 |
| `nav_prev`、`premium` | 前一營業日淨值、折溢價 %（正數為溢價） |
| `aum`、`aum_source`、`aum_date`、`aum_source_url` | 規模（億元）、出處、資料日期與原文連結。只放官方數字：投信官網公告或投信投顧公會 ETF 月統計（月底），取資料日期較新的；都沒有時為 `null` |
| `units`、`units_source`、`units_date`、`units_source_url` | 發行（受益權）單位數，來源規則同規模 |
| `units_change` | 當天申購買回造成的發行單位數增減，正數為淨申購（資金流入）、負數為淨買回；只有部分投信提供 |
| `mgmt_fee`、`mgmt_fee_text`、`custodian_fee_text`、`fee_source` | 經理費率 %（只有單一費率才有數字）、經理費與保管費原文（多為依規模分段）、原文出處。投信官網優先，沒有接入官網的 ETF 用投信投顧公會基金基本資料 |
| `manager`、`risk_class`、`listed_date`、`index_name` | 經理人、風險等級（RR1～RR5）、上市日、追蹤指數 |
| `beneficiaries`、`beneficiaries_month`、`beneficiaries_source`、`beneficiaries_source_url` | 受益人數、資料年月、出處與原文連結。投信官網與投信投顧公會（基金資料彙總表、ETF 月統計）取資料月份較新的，同月份用投信官網的 |
| `stats` | 投資組合統計 `{date, div_yield, pe, pb, roe, beta, volatility}`，可能為 `null` |
| `holdings_top`、`holdings_source`、`holdings_date`、`holdings_source_url` | 前 10 大成分 `[{asset_code, asset_name, kind, weight, qty}]`、出處、資料日期與原文連結。`weight` 為 %，`kind` 為股票／債券／期貨／基金／其他。投信官網的申購買回清單優先（`holdings_date` 為 YYYY-MM-DD）；沒有接入官網的 ETF 用投信投顧公會的月底前十大（`holdings_date` 為 YYYY-MM，`qty` 為 `null`）。外幣交易線（代號結尾 K，例如 00625K、00636K）沿用主要交易線的成分股，出處註明「與 XXXX 同一檔基金」 |
| `holdings_changes` | 主動式 ETF 的持股異動（其他 ETF 為 `null`）：比對投信官網最新兩份完整持股清單 `{date, prev_date, units, units_prev, flow_checked, counts, changes, source}`。`changes` 最多 50 筆 `{asset_code, asset_name, kind, action, qty_prev, qty, qty_change, weight_prev, weight, weight_change, excess, with_flow, amount}`，`action` 為新增／加碼／減碼／刪除（看實際股數）。`excess` 是超出申購買回比例的百分點（股數增減比例 − 發行單位數增減比例），`with_flow` 為 `true` 表示 |excess| 不到 1 個百分點、大致隨申購買回同比例；沒有單位數可判斷、或新增刪除時兩者為 `null`。`counts` 是全部筆數。`amount` 為億元（台股才有） |
| `next_ex`、`last_ex` | 下一次／最近一次除息 `{ex_date, cash, pay_date, source}`，`pay_date` 為配息發放日。`source` 依優先順序為投信官網、投信投顧公會（每月公布上個月的除息紀錄，含發放日）、交易所除權息預告 |
| `fee` | 總費用率 %：人工填寫（`data/etf_meta.csv`）優先，否則用投信投顧公會年度費用比率的合計（含交易成本） |
| `monthly` | 投信投顧公會基金資料彙總表（月）`{month, aum, nav, units, beneficiaries, beneficiaries_natural, beneficiaries_legal, subscribe, redeem, net, source}`：月底規模（億元）、單位淨值、受益權單位數、總／自然人／法人受益人數，以及當月申購、買回總金額與兩者相減的淨申購（億元，負數為淨買回）。每月第 10 個營業日公布上個月的資料；公會沒有列出的 ETF（例如外幣交易線、期貨信託基金）為 `null` |
| `sitca_monthly` | 投信投顧公會 ETF 月統計（`SITCA_ETF/etf_statement2.aspx`，以證券代號對應）`{code, month, currency, aum, nav, units, subscriptions, redemptions, beneficiaries, net_amount, source, source_url, published_at}`：金額為億元，`net_amount` 為申購減買回，`published_at` 為公會公布日；非新台幣級別的 `nav` 為 `null`。只用截至交易日已經公布的月份 |
| `flows` | 週申贖**估算**（擁有者要求的推算值，見資料來源規則的例外）：本週 `{start, end, baseline, net_amount, net_rate, units_rate, aum_change, aum_rate, status, reason, method, sources}`，加上 `as_of`、`latest_official_date`、近 4 週合計 `four_week_amount`、淨申購週數 `positive_weeks`、可計算週數 `available_weeks`、各週明細 `weeks[]`、已取得的特殊事件 `events[]` 與 `events_status`。`net_amount` 單位億元，為逐日「流通單位數增減 × 同日投信正式淨值」加總；期初到本週任一個交易日缺官方資料、幣別未確認或遇分割合併等事件時為 `null`，`reason` 說明原因。各週另有選用欄位（舊資料可能沒有）：`calendar`（`官方開休市日期`／`未涵蓋`／`未使用`）、`trading_days`（本週交易日）、`closed_days`（本週休市的平日）與機器可讀的原因 `reason_code`（`calendar_unknown` 日曆未涵蓋、`market_closed` 整週休市、`missing_official` 缺官方日資料、`currency_unconfirmed`、`special_event`、`incomplete`、`units_mismatch`、`split_suspected`、`source_changed`；可估算時為 `null`）。有官方開休市日期時，`baseline` 是本週第一天之前最近的交易日（例如前週五休市就是前週四），只比較交易日；官方表沒涵蓋的年度不判定休市，該週為 `null`。整週休市時 `status` 為「休市」、`net_amount` 為 `null`。各週 `sources[]` 是逐日官方快照 `{date, source, url}`，另有選用的 `announced`（投信標示的公告日期，即申購買回清單的清單日期；沒有標示時為 `null`）、`first_observed_at`、`observed_at`（本站第一次與最近一次取得的時間） |
| `expense` | 投信投顧公會公布的年度實際費用比率 `{year, mgmt, custodian, other, total, source}`，單位 %：經理費、保管費、其他費用與合計（含交易成本）。年中才成立的 ETF 沒有整年資料，為 `null` |
| `returns` | 區間報酬 `{price: {...}, total: {...}}`，單位 %。`price` 是市價報酬，`total` 是含息報酬（配息再投入）。區間代號：`1w` 近一週、`1m` 近一月、`3m` 近三月、`6m` 近六月、`ytd` 今年以來、`1y` 近一年、`3y` 近三年、`5y` 近五年。期初是 `returns_info.starts` 那天或之前最近的交易日，期末是 `trade_date`。上市不到該區間、或配息紀錄核對不符時為 `null`；整檔沒有資料時 `returns` 為 `null` |

本機產生測試資料（假資料，用完請還原）：

```bash
python scripts/fetch_etf.py --fixtures tests/fixtures --db /tmp/test.db --out web/data/latest.json
python -m http.server -d web 8000   # 開 http://localhost:8000
git checkout -- web/data/latest.json
```

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
