# 主動式 ETF 持股資金動向：資料合約（active_holdings 第 1 版）

資料端初版由 Claude 完成，Codex 接手資料防護、整合測試與網站介面。既有的 `active_changes`、`holdings_changes` 保留不變。

## 估算口徑

使用者已接受的估算口徑是**股數增減 × 當日官方收盤價**，有三點限制：

- 這**不是**經理人的實際成交金額，也**不是**扣除申購買回後的主動買賣。
- 每萬單位持股（`unit_qty`）只代表配置密度，不得標成實際買賣。
- 基金單位增加、但股票股數沒動，不算賣出。

**範圍：** 只算主動式 ETF（代號結尾 A、D）在投信官網每天公告的完整持股清單裡的**台股股票**。代號還須與交易所名稱相符，或符合按代號核對的明確別名；不能因海外股票也有四位數代號而套用台股行情。

- 不算：債券、期貨、基金、外國股票。
- 不用投信投顧公會的月前十大：它不是完整清單，也不能拿來推斷退出。

**排行：** 第一版以每日為主。週、月要等官方歷史累積到期間內每個交易日都有資料，才會是「可計算」；資料不足時 `stocks` 是空陣列，不出榜。資料不回補、不推猜。

## 頂層

| 欄位 | 說明 |
|---|---|
| `version` | 合約版本，目前是 `1` |
| `as_of` | 三個期間共用的期末日。定義是最近一個已保存上市官方收盤價的交易日。傍晚證交所還沒公布當天資料，所以可能比 `trade_date` 早一天；從來沒保存過股價時為 `null` |
| `source`、`method` | 資料來源與計算方法的說明文字（放在說明提示） |
| `price_history_from` | 官方股價開始保存的日期 |
| `attributions` | `as_of` 用到的股價資料集顯名資訊，格式同最上層的 `attributions`：提供機關、資料名稱、資料集頁、端點、資料日期、授權條款與連結。連結可以直接點 |
| `periods` | `{"1d": period, "1w": period, "1m": period}` |

## period

| 欄位 | 說明 |
|---|---|
| `start`、`end`、`baseline` | 第一個加總日、期末（＝`as_of`）、比較基準日。`1d` 的 `baseline` 是前一個交易日。`1w`、`1m` 的 `baseline` 是 `as_of` 往前 7 天、1 個月那天或之前最近的交易日，與 `returns_info.starts` 相同 |
| `days` | 逐日加總的交易日清單，依證交所官方開休市日期 |
| `status` | 只有 `可計算` 或 `資料不足` 兩種。資料不足時 `stocks` 是空陣列 |
| `reason`、`reason_code` | 說明文字與機器可讀的原因：`no_prices` 期間內有交易日沒有保存官方股價、`no_calendar` 官方開休市日期沒涵蓋、`no_etfs` 沒有 ETF 具備每天的完整清單。可計算時 `reason_code` 是 `null` |
| `included_etfs` | 列入的 ETF 代號：有 `baseline` 到 `end` 每個交易日官網完整清單的主動式 ETF |
| `excluded_etfs` | `[{code, reason}]`：沒有官網完整清單（例如只有公會前十大），或缺某些日期的清單 |
| `excluded_stocks` | `[{asset_code, asset_name, date, reason, etfs}]`：因疑似公司行動、缺股數而排除的股票與日期。`etfs` 是受影響的 ETF；股票層級的事件（除權、分割、減資、配股入帳）整檔股票不出現在 `stocks` |
| `lists` | 列入 ETF 的期初與期末清單證據 `[{code, date, source, source_url, rows, stock_rows, missing_qty, weight_sum, first_observed_at, observed_at}]`：全部列數、台股股票列數、缺股數列數、權重合計（%）、本站第一次與最近一次取得時間（舊清單沒有取得時間時為 `null`）。逐日的證據存在資料庫 `holding_list` 表 |
| `stocks` | 全部有股數增減的台股股票，不截斷。依 `net_amount` 由大到小排序，空值放最後；前端請自己依金額、同步檔數各取前 10 |

## stock

| 欄位 | 說明 |
|---|---|
| `asset_code`、`asset_name` | 股票代號、名稱 |
| `net_amount`、`buy_amount`、`sell_amount` | 億元。`buy_amount` 是逐日日值的正值加總，`sell_amount` 是負值的絕對值加總，`net_amount = buy_amount − sell_amount`。`complete` 為 `false` 時三者都是 `null` |
| `buy_etfs`、`sell_etfs` | 期間淨股數增加、減少的 ETF 檔數，只算可靠的部分 |
| `net_shares` | 期間淨股數增減的加總 |
| `complete` | `true` 代表每一筆都有官方股價、沒有任何 ETF 的部分被排除 |
| `counts_complete` | 股數是否完整；缺股價但股數完整可列檔數榜，有 ETF 部分被排除則不可排名 |
| `missing_price_dates` | 有股數增減、卻缺有效官方收盤價的日期（沒有成交等）。不是空陣列時，金額為 `null` |
| `excluded_etfs` | `[{code, date, reason}]`：這檔股票被排除的 ETF 部分，例如疑似代碼變更、清單缺股數。不是空陣列時，金額為 `null`，不會把排除的部分當成 0 |
| `prices` | 用到的官方股價 `[{date, close, market, dataset}]`。`dataset` 是政府資料開放平臺資料集代號：`11549` 上市、`11370` 上櫃 |
| `etfs` | 各 ETF 的 contribution，依金額絕對值排序 |

## contribution

| 欄位 | 說明 |
|---|---|
| `code`、`name` | ETF 代號、名稱 |
| `qty_prev`、`qty`、`qty_change` | 期初（`prev_date`）、期末（`date`）股數，以及兩者的差。完整清單沒列出就是未持有，股數記 0 |
| `amount` | 逐日「股數增減 × 當日官方收盤價」的加總（億元）；缺任何一天的有效股價時為 `null` |
| `action` | 依期間淨股數判斷，共五種：`新增`、`刪除`、`加碼`、`減碼`、`不變`。`不變` 是淨股數 0、但期間內有增減，例如先加碼後減碼 |
| `weight_prev`、`weight` | 期初、期末權重（%）；沒列在清單上時為 `null` |
| `unit_qty_prev`、`unit_qty`、`unit_qty_change` | 每萬單位 ETF 持有的股數＝股數 ÷ 當日官方發行單位數 × 10,000。缺單位數時為 `null`。這只是配置密度，不是實際買賣 |
| `source`、`source_url` | 期末清單的出處與連結 |
| `prev_date`、`date` | 比較的兩份清單日期，等於 period 的 `baseline` 與 `end` |
| `reason` | 有缺值時的說明，例如缺某天的收盤價 |

所有金額都是有限數字或 `null`，股數為整數。

## 資料查核規則

- **同一比較區間：** 每檔 ETF 都用同一個 `baseline` 到 `end`，必須有期間內每個交易日的官網完整清單。缺任何一天就整段排除，不把相隔數日的兩份清單當成一天。
- **日期依據：** `DATE_BASIS` 明列可核對的既有解析欄位：野村／安聯 CNavDt、中信「淨值日期」、富邦「資料日期」、凱基淨值欄名日期、摩根持股 navDate、復華回傳資產日期。其餘暫不排行；不以價格或權重吻合來推定日期。證據的 `date_basis` 另保存於 `holding_list`，舊清單的來源可依相同解析規則核對，取得時間仍不補造。
- **數值：** 股價只接受有限正數，股數接受有限非負數；負價格、Infinity、NaN 都不能估值。
- **缺股數：** 清單列了某檔股票卻缺股數時，不當成 0 或清倉，這筆排除並列在 `excluded_stocks`。
- **疑似公司行動：**
  - 同一天持有同一檔股票的 ETF（至少兩檔）全部以相同比例增減，判為疑似除權、分割或減資；只要有持有者股數沒變，就不算。
  - 股數剛好照除權預告的配股率增加，判為疑似配股入帳。
  - 這兩種情況，該股票在期間內整檔排除。
  - 同一檔 ETF 同一天刪除一檔、新增一檔且股數相同，判為疑似代碼變更，排除這檔 ETF 的這兩筆。
- **已知限制：**
  - 單一 ETF 股數扣除發行單位增減後達 1.5 倍或 2/3 以下，先排除該分項。股數與同日股價反向調整（股數變化至少 5%，兩者乘積距 1 不超過 2%）時，整檔股票保守排除。這些是疑似事件檢查，也可能排除真實交易。
  - 事件偵測不是完整公司行動清冊，仍可能漏掉小幅、延後入帳等事件，不能保證毫無誤差。
  - 現金增資認購的新股會在入帳那天算成增加，金額用當日收盤價，不是認購價。

## 資料保存

- **`stock_close` 表：** 主動式 ETF 持有的股票代號對應的台股官方名稱與收盤價。來自資料集 11549 上市、11370 上櫃，存在 `data/history/stock_close/`。名稱用於核對市場身分；價格依交易所標示日期增量保存，從開始保存那天起累積，不回補。
- **`stock_event` 表：** 台股除權預告，取自證交所 `TWT48U_ALL`、櫃買中心 `tpex_exright_prepost` 裡標示「權」的列。預告表只列即將除權的股票，每次執行時保存下來。
- **`holding_list` 表：** 每份官網完整清單的出處與完整度證據，存在 `data/history/holding_lists/`。

## 前端注意事項

- 每日排行優先。週、月只在 `status` 為「可計算」時出榜；資料不足時顯示 `reason`。
- 揭示 `excluded_stocks`、`days` 與 `baseline`（比較期間），以及 `included_etfs`、`excluded_etfs`（ETF 樣本範圍）。
- `complete` 為 `false` 的股票，金額顯示「—」並註明原因；只有 `counts_complete` 為 `true` 才可列入檔數榜，不完整分項可在缺漏明細檢視。
- 文字維持中立，只描述股數增減與估值，不寫成買賣建議。
