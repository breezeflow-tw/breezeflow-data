# Codex 任務：主動式 ETF 持股異動

資料端已完成（Claude，`scripts/active_changes.py`），每天的排程會產生資料。本任務只改 `web/index.html`，欄位格式以 `AGENTS.md`「網站資料格式」為準。

## 資料欄位

### 最上層 `active_changes`：全部主動式 ETF 的台股彙總

- 沒有可比對的資料時為 `null`，此時整個區塊顯示「資料累積中」。
- 主要欄位：
  - `date`：異動日期。
  - `etf_count`、`etfs`：列入彙總的主動式 ETF。
  - `not_included`：因為缺發行單位數而沒列入的 ETF。
- `buys`、`sells`：最多 30 檔股票，已經排好順序（依加碼或減碼的 ETF 檔數，再依金額）。每檔欄位：
  - `asset_code`、`asset_name`
  - `buy_etfs`、`sell_etfs`：加碼（含新增）、減碼（含刪除）的 ETF 檔數
  - `buyers`、`sellers`：ETF 代號清單，可連到該檔的詳細面板
  - `buy_shares`、`sell_shares`、`net_shares`：股數
  - `buy_amount`、`sell_amount`：億元，缺收盤價時為 `null`，顯示「—」
- `method`、`source`：放在說明提示裡。

### 各檔 `items[].holdings_changes`

只有主動式 ETF（代號結尾 A、D）且有兩份官網清單時才有，其他是 `null`。

- `date`、`prev_date`：比對的兩份清單日期。
- `flow_checked`：有沒有發行單位數可以判斷申購買回的影響。
- `counts`：`{新增, 加碼, 減碼, 刪除}` 的全部筆數。
- `changes`：最多 50 筆，每筆欄位：
  - `action`：新增／加碼／減碼／刪除
  - `qty_prev` → `qty`、`qty_change`：股數
  - `weight_prev` → `weight`、`weight_change`：%
  - `excess`：超出申購買回比例的百分點
  - `with_flow`：`true` 表示 |excess| 不到 1 個百分點，也就是大致隨申購買回同比例
  - `amount`：億元

## 介面需求

1. **主動式 ETF 持股異動區塊**：表格上方或另一個分頁，標題例如「主動式 ETF 持股異動（2026-10-19）」。
   - 左右（手機上下）兩張表：「多檔同時加碼」「多檔同時減碼」。
   - 欄位：股票、檔數、金額（億元）、ETF 代號。
   - 點 ETF 代號打開該檔詳細面板。
2. **詳細面板**新增「持股異動」區塊，依 `action` 分組。
   - `with_flow: true` 的那幾列淡化，並加註「隨申購買回同比例」。
   - 顯示 `excess`，例如「超出申贖比例 +12.4 個百分點」。
   - `flow_checked: false` 時，在區塊上方註明「缺發行單位數，無法判斷申購買回影響」。
3. **說明提示**放 `method` 的文字，並說明涵蓋範圍：已接入官網的投信才有每日清單；`not_included` 列出未列入的 ETF。
4. **顏色**：加碼、新增用 `--up`，減碼、刪除用 `--down`（台股慣例：紅漲綠跌）。
5. **文字中立**：只描述「加碼／減碼／新增／刪除」與數字，不寫「看好」「主力進場」「值得布局」等評價或建議。

## 驗收

- [ ] `active_changes` 為 `null`、`changes` 為空陣列、`amount` 為 `null` 時都正常顯示
- [ ] 桌機、手機、淺色、深色模式都正常；瀏覽器主控台沒有錯誤
- [ ] `python -m unittest discover -s tests -v` 全部通過
