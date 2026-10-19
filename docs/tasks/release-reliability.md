# 發布穩定與驗收

## 問題與處理

每日抓取約數分鐘；期間若另一位協作者更新 main，原本直接 `git push` 會因非快轉遭拒，整個 Pages 部署也會跳過。2026-10-19 的工作 39268703092 即發生此狀況。

本次維持單一發布工作與既有排程，加入以下處理：

- main 的介面、資料程式、測試及發布設定改動會自動啟動更新。文件或純資料提交不觸發；排程機器人使用 GITHUB_TOKEN 推送也不會遞迴觸發 push 工作。
- 工作只允許 main，開始時取最新 main 與完整歷史；同一發布群組依序執行，不取消正在抓資料的工作。
- 自動提交僅限歷史 CSV 與 latest.json。推送前同步遠端，介面／文件等可合併修改以正常 rebase 保留，並重跑測試。
- 若資料程式或人工 metadata 更新，先驗證新版，再重新抓取後提交。若發布腳本／流程本身更新，停止舊工作，由最新 main 觸發的新工作接手。
- 真正資料衝突停止，不用 force push、不以 ours/theirs 覆蓋。推送競爭最多重試三次；權限、網路或驗證失敗不當成競爭重試。
- 下載用 SQLite 從最後提交的 CSV 重建，避免 rebase 後仍提供舊資料庫。
- 部署產物加上 release.json，列出 commit、建置時間、資料交易日、產生時間、筆數及 HTML／JSON／SQLite 的 SHA-256。這份檔案隨網站部署，不提交回 Git。

前端仍依各欄位資料日期呈現，建置時間不代表行情更新時間。release.json 表示發布包版本；真正部署成功仍須由工作結果及正式網址讀取確認。

## 驗證

`python -m unittest discover -s tests -p test_release.py -v` 使用本機暫存 bare Git 遠端，涵蓋正常提交、遠端前進、資料程式更新後重抓、真實資料衝突、流程更新、非資料修改拒絕、推送瞬間競爭、發布雜湊與資料庫重建、拒絕測試／空資料。

完整專案測試另執行 `python -m unittest discover -s tests -v`。這是發布工具範圍，新增 scripts/release.py、tests/test_release.py，不修改 Claude 的來源解析與週申贖計算檔案。

## 發布後核對

1. GitHub 工作 update 與 deploy 都成功。
2. 正式站 release.json 的 commit 對應這次發布包；HTML、latest.json 與下載 SQLite 雜湊吻合。
3. latest.json 為 live、筆數非零且代號不重複；核對資料交易日與產生時間。
4. 瀏覽器核對排行／週資金流入口、0050 等樣本的欄位日期、來源與免責聲明。

官方參考：[工作觸發與 GITHUB_TOKEN 限制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[工作語法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)。
