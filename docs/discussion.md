# 免註冊公開討論板（試作，尚未啟用）

目前僅完成程式與本機驗收。尚未建立 Cloudflare 帳號、正式 D1、Workers 或 Turnstile，也未完成雲端整合驗收。`web/discussion-config.json` 預設停用，不能把本機測試通過當成正式服務可用。

## 行為與範圍

- 訪客只填暱稱、主題、分類與內容；同意公開規則並通過驗證後即公開，不經事前審核。
- 暱稱沒有身分驗證，可被其他人重複使用。保留管理員相關名稱只是減少明顯冒名，不構成身分保證。所有公開發言均標「訪客」。
- 發文成功立即顯示。其他訪客在本區可見、分頁在前景、沒有編輯或焦點停留時每 30 秒更新；可手動更新。載入較早頁後停止對該列表自動更新，避免打斷閱讀。這是定期讀取，並非 WebSocket 推播。
- 站長可隱藏／恢復留言、鎖定／開放主題；檢舉與移除申請沿用既有私人 Google 表單。
- 不要求 Email、不提供註冊、附件、私訊、自行編輯或身分驗證。本版沒有新留言 Email／手機推播；站長須查看討論板管理清單。原有私人表單通知不變。
- 不執行 AI 呼叫；一般發文與閱讀消耗 Cloudflare 額度，不使用 Codex Token。日後請 AI 修改功能仍會使用相應額度。
- 網站仍在 GitHub Pages，討論資料與 ETF 資料庫分離。討論失敗時不阻斷行情介面。

## 組成與公開／私密資料

| 元件 | 用途 | 可否公開 |
| --- | --- | --- |
| `web/index.html` | 顯示、發文、閱讀、站長管理介面 | 可公開；無管理密鑰 |
| `web/discussion-config.json` | 啟用旗標、Worker URL、Turnstile site key | 可公開；site key 不是 secret |
| `discussion/worker.mjs` | 驗證、限速、讀寫、管理 API | 程式可公開，密鑰另設 |
| `discussion/schema.sql` | 獨立 D1 的 `posts`、`rate_limits` | 結構可公開，資料不進 Git |
| Worker Secrets | `TURNSTILE_SECRET`、`RATE_SECRET`、`ADMIN_TOKEN` | 絕不可放前端、PR、截圖或 Git |
| `discussion/local-db.mjs`、`preview.mjs` | Node 24 離線測試／本機預覽 | 只在本機執行，不部署 |

公開 API 僅回傳暱稱、主題、分類、內容、時間、公開編號、回覆數及鎖定狀態。請求 UUID、限速識別碼與隱藏內容不出現在公開列表。D1 不保存原始 IP，但 Cloudflare 的傳輸、防機器人與平台服務仍會處理連線資料，不能宣稱完全匿名或沒有個資處理。

## 防濫用與限制

- 後端驗證 Turnstile 的成功旗標、hostname 與 `action=discussion`；本機的模擬驗證不在正式 `fetch` 入口啟用。
- Origin 限定本站；CORS 是瀏覽器限制，不能代替驗證，也不能阻止非瀏覽器用戶端。
- 使用每日變動的 IP HMAC 做限速：每個 IP 每分鐘最多 6 次、每日最多 40 次合格格式的嘗試；未通過驗證也會消耗次數。不同裝置共用對外 IP 時會共享限制。
- 全站每日最多 1,000 次通過驗證後的發文嘗試；主題已鎖等後續失敗也可能計入。日界線採 UTC。這是應用層控制，不是 Cloudflare 費用上限。
- 限速紀錄 48 小時後失效，在下次發文時刪除過期資料；沒有流量時可能仍留存在資料庫，不承諾精確 48 小時永久刪除。
- 暱稱最多 24、主題最多 80、內文最多 2,000 個 UTF-16 單位；整個 JSON 上限 14,000 bytes。無附件、不呈現 HTML、不把訪客網址轉為可點連結。
- SQL 使用參數；管理密鑰經摘要後定長比較；錯誤訊息不回傳 SQL 或密鑰。同一 UUID 重試不重複發文，修改已成功內容後重試會拒絕。
- 每次列表最多 20 個主題、每次內文最多 50 則回覆，採游標分頁。讀取仍可能遭大量請求耗盡免費額度；目前沒有宣稱能防住所有濫用。
- 管理密鑰僅放頁面記憶體，輸入框登入後清空，登出、離頁、重新整理清除；不存 localStorage。這仍是高權限憑證，應使用隨機長密鑰並在可信裝置操作。

## 免費額度與風險

依 2026-10-23 查閱的官方文件，Workers Free 每日 100,000 requests；D1 Free 每日 5,000,000 rows read、100,000 rows written，帳戶總儲存 5 GB。索引維護也可能增加計算的寫入量；一次讀取不等於一個訪客，一則留言也會執行多次資料庫操作。Turnstile 有免費方案。

免費額度耗盡可能使討論讀寫失敗；D1 免費版不提供超額付費使用，付費版另依方案計費。先維持免費方案，不升級、不訂閱付費產品。實際條款與帳戶方案需在建立時再核對。網站大流量或惡意讀取仍可能讓討論暫停，不能承諾永久免費且不中斷。

官方來源：

- [Workers 價格](https://developers.cloudflare.com/workers/platform/pricing/)
- [D1 價格與超額行為](https://developers.cloudflare.com/d1/platform/pricing/)
- [Turnstile 方案](https://developers.cloudflare.com/turnstile/plans/)
- [Turnstile 伺服器驗證](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)
- [Cloudflare 隱私政策](https://www.cloudflare.com/privacypolicy/)

免註冊即時公開會有垃圾留言、冒名、騷擾、詐騙及個資被貼上的風險。機器驗證與限速只能減少風險，站長仍須查看檢舉並處理。公開內容可能被搜尋、截圖或備份，隱藏無法保證外部副本消失。

## 本機驗收

需 Node.js 24；不需下載依賴，不連 Cloudflare。從專案根目錄執行：

```powershell
node --test tests/*.mjs
node discussion/preview.mjs
```

開啟 `http://127.0.0.1:8769/#discussion`。只監聽回環位址。測試資料存在被 Git 忽略的 `discussion/preview.db`，每則種子內容均標示本機測試。試用管理密鑰為 `local-preview-admin-only-not-for-production`，僅供本機；絕不可用於正式服務。

## 正式建立順序（尚待執行）

1. 擁有者建立／登入 Cloudflare 免費帳號，確認服務條款、方案與帳戶安全設定。不能把「可以考慮」視為已同意所有條款或付費。
2. 建立獨立 D1 `breezeflow-discussion`，執行 `discussion/schema.sql`。不要匯入本機測試資料或 ETF 資料庫。
3. 建立 Worker，部署 `discussion/worker.mjs`；把 D1 綁定名稱設為 `DB`。可依 `discussion/wrangler.example.toml` 設定，實際 `wrangler.toml` 不進 Git。初始保持 `POSTING_ENABLED=false`。
4. 設定一般變數 `ALLOWED_ORIGIN=https://breezeflow-tw.github.io`，沒有結尾斜線或路徑。需保留現有 GitHub Pages 網址，無須搬站或轉移 DNS。
5. 建立 Turnstile widget，允許 hostname `breezeflow-tw.github.io`。公開 site key 填前端設定；secret 只放 Worker Secret `TURNSTILE_SECRET`。
6. 產生兩組各至少 32 字元、獨立且高隨機性的值，分別設為 Worker Secrets `RATE_SECRET`、`ADMIN_TOKEN`。管理密鑰交由擁有者存於密碼管理器；不是 Cloudflare 帳號密碼。
7. 在隔離測試設定驗證真實 D1、Turnstile、跨來源請求、手機、額度耗盡錯誤、管理及備份還原。勿將 localhost 加入正式驗證白名單或使用 Turnstile 的測試通行密鑰上線。
8. 本機與雲端驗收通過、擁有者確認合併後，才在前端填真實 Worker HTTPS URL、site key 並設 `enabled=true`，把 Worker `POSTING_ENABLED` 設為字串 `true`。GitHub 每日流程只發布網站，不會自行部署 Worker。
9. 發布後再用實際網站做一則明確標記的上線測試，確認管理可隱藏，並確認原有行情與私人留言表單仍可用。

本版 PR 應保留 draft，直到帳號、部署、營運意願與雲端整合驗收完成。

## 管理、停用與資料處理

- 日常：開啟討論板 → 展開「站長管理」→ 輸入管理密鑰 → 讀取清單。不要在公共電腦操作；用完按「清除密鑰」。
- 疑似垃圾或不當內容：先隱藏；主題隱藏後所有回覆亦不對外讀取。可恢復。鎖定主題只阻止新回覆，不隱藏既有內容。
- 緊急暫停發文：Worker `POSTING_ENABLED=false`，既有公開內容仍可讀。若需要連讀取都關閉，停用 Worker 路由／服務，並將網站 `enabled=false`。只改前端旗標不能阻止直接 API 請求。
- 密鑰疑似外洩：在 Cloudflare 更換 `ADMIN_TOKEN`，舊密鑰立刻失效。更換 `RATE_SECRET` 會重置現行限速識別；不要因此認為可移除平台紀錄。
- 備份：上線前確認 Cloudflare D1 當前備份／還原能力，另安排受控的資料匯出與還原演練；本版未建立自動備份或保留期限承諾。匯出含隱藏內容，不要放公開 GitHub、`web/` 或公開雲端連結。
- 移除請求：前台隱藏只是限制公開讀取，資料仍在 D1。若需永久刪除，由擁有者核對留言編號及範圍後，針對主題與回覆在 D1 刪除；不可把「隱藏」宣稱為永久刪除。同步核對備份保留、法律保存需求與第三方副本的限制。
