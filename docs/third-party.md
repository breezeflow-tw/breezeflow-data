# 網站使用的第三方程式與素材

網站主程式是 `web/index.html`。這裡列出它用到的第三方程式、圖示與字型，以及授權與核對紀錄。新增或升級時同步更新本檔，規則見 `AGENTS.md` 的開發規則。

## 程式與圖示

| 名稱 | 版本 | 用途 | 授權 | 放在哪裡、怎麼載入 |
|---|---|---|---|---|
| Fuse.js（basic 版） | 7.0.0 | 搜尋：沒有完全相符時，列出名稱相近的 ETF | Apache-2.0 | `web/vendor/fuse-7.0.0.basic.min.js`（約 15 KB），以 `defer` 載入 |
| Lucide 圖示 | 1.61.0 | 搜尋、關閉／清除、閱讀設定、篩選 4 個介面圖示 | ISC | 只把用到的圖示內嵌在 `web/index.html`，不載入整套 |

授權全文：`web/vendor/licenses/`。

### 來源與核對

| 名稱 | 取得來源 | 檔案 SHA-256 | 核對日期 |
|---|---|---|---|
| Fuse.js 7.0.0 | npm 套件 `fuse.js@7.0.0`（`https://registry.npmjs.org/fuse.js/-/fuse.js-7.0.0.tgz`，完整性 `sha512-14F4hBIxqKvD4Zz/XjDc3y94mNZN6pRv3U13Udo0lNLCWRBUsrMv2xwcF/y/Z5sV6+FQW+/ow68cHpm4sunt8Q==`）中的 `dist/fuse.basic.min.js`，未修改 | `d42fa2fe333d0ce3d9369962cba1da9b205e4fb3406d80fc89199c6766e8a666` | 2026-10-26：重新下載比對，檔案與授權全文一致 |
| Lucide 1.61.0 | npm 套件 `lucide-static@1.61.0`（`https://registry.npmjs.org/lucide-static/-/lucide-static-1.61.0.tgz`）的 `search`、`x`、`settings-2`、`sliders-horizontal` 圖示 | 內嵌於網頁，不另存檔 | 2026-10-26 |

### 沒有載入時

- Fuse.js：搜尋只做「代號、名稱、投信完全包含」與「字依序出現」兩步，不列相近名稱；其他功能不受影響。
- Lucide：圖示內嵌在網頁裡，不會載入失敗。

## 字型

| 名稱 | 用途 | 授權 | 載入方式 |
|---|---|---|---|
| Noto Sans TC | 中文字 | SIL Open Font License 1.1 | Google Fonts 線上載入 |
| Inter | 數字與英文（等寬數字） | SIL Open Font License 1.1 | Google Fonts 線上載入 |

- 載入失敗時改用系統字型，內容照常顯示。
- 從 Google Fonts 載入時，瀏覽者的 IP 位址等連線資訊會傳給 Google。
- 分享預覽圖 `web/og.png` 也是用這兩套字型產生的；字型授權允許用來製作圖片。

## 本站自製素材

網站圖示（`web/icons/`、`web/favicon.svg`）與分享預覽圖（`web/og.png`）是本站自製的，原稿在 `docs/brand/`。修改原稿後用下面的指令重新產生（需要 Playwright，只在更新素材時使用）：

```bash
NODE_PATH="$(npm root -g)" node docs/brand/render.mjs
```
