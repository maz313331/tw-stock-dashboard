# 本機標準執行與校正步驟手冊 (SETP.md)

**最後校正時間：** 2026-11-25  
**適用腳本：** `scripts\fetch_priority_stocks.py`  
**工作目錄：** `C:\Users\Chen Chia\Desktop\股市Test\tw-stock-dashboard`

---

## Step 0：環境與檔案位置校正（執行前檢查）

每次開始作業前，請先在 PowerShell 確認目前路徑與唯一主力檔案狀態，避免誤改或誤跑舊檔：

```powershell
# 1. 切換至專案根目錄
cd "C:\Users\Chen Chia\Desktop\股市Test\tw-stock-dashboard"

# 2. 確認輸入名單與主力腳本皆存在，且根目錄無重複腳本
Test-Path .\data\priority_stocks.json
Test-Path .\scripts\fetch_priority_stocks.py
Test-Path .\fetch_priority_stocks.py   # 應回傳 False
```

---

## Step 1：串接實際抓股與算分函式（僅首次或改版時執行）

目前 `scripts\fetch_priority_stocks.py` 已完成全自動分段與落盤骨架，正式抓取前需確認 `fetch_symbol_data(item)` 已接上實際抓取與算分邏輯：

1. 開啟 `scripts\fetch_priority_stocks.py`。
2. 在檔案頂部 `import` 專案內負責抓取單檔行情與計算分數的模組。
3. 改寫 `def fetch_symbol_data(item: dict) -> dict:`：
   * 以 `item["symbol"]`、`item["ticker"]`、`item["market"]` 呼叫抓股與算分函式。
   * 回傳標準化字典，包含 `status`（`"ok"` 或 `"partial"`）與 `missing_fields`（缺漏欄位清單）。
4. 若先前曾跑過純骨架測試，建議先清空或備份舊的 `local_output\progress.json`，避免測試假資料混入正式進度。

---

## Step 2：第一輪全自動分段抓取（主要股票白名單）

啟動後程式會自動讀取 `.\data\priority_stocks.json`（確保只抓當期名單內標的，不會查詢已移出的舊股票），並依設定自動分段、批間休息與大段冷卻直到全部跑完：

### 2.1 一般規模執行（約 100 ~ 200 檔，如目前 110 檔）
```powershell
python .\scripts\fetch_priority_stocks.py `
  --input .\data\priority_stocks.json `
  --out-dir .\local_output `
  --batch-size 25 `
  --min-wait 0.8 `
  --max-wait 2.0 `
  --batch-pause 5 `
  --stage-batches 4 `
  --stage-pause 30
```

### 2.2 大量規模執行（擴充至 2,000 檔時）
```powershell
python .\scripts\fetch_priority_stocks.py `
  --input .\data\priority_stocks.json `
  --out-dir .\local_output `
  --batch-size 200 `
  --min-wait 0.8 `
  --max-wait 1.8 `
  --batch-pause 10 `
  --stage-batches 5 `
  --stage-pause 45
```

> **中斷恢復說明：** 若執行期間因網路異常或手動按 `Ctrl+C` 中斷，已完成批次皆已寫入 `local_output\progress.json`，直接進行 Step 3 即可無縫接續。

---

## Step 3：第二輪增量補漏（`--only-missing`）

第一輪結束後，針對白名單內「尚未完成」、「網路逾時失敗」或「關鍵欄位缺漏」的標的執行補抓（已完成的標的會自動跳過，不重複發送請求）：

```powershell
python .\scripts\fetch_priority_stocks.py `
  --input .\data\priority_stocks.json `
  --out-dir .\local_output `
  --only-missing `
  --batch-size 25 `
  --min-wait 1.2 `
  --max-wait 2.5 `
  --batch-pause 8
```

---

## Step 4：附屬市場資料更新（獨立於個股批次之外）

主要股票名單處理完畢後，再獨立檢查並更新 `data\market.json` 中的非個股項目（避免混入個股迴圈）：

1. **美股指數更新（`us_indices`）：** 確認 `^DJI`、`^GSPC`、`^IXIC`、`^SOX` 已更新至最新美股收盤日。
2. **三大法人歷史序列回補（`history[*].institutional_net`）：** 針對尚未填入數值（目前為 `0.0`）的歷史交易日回補法人淨買超金額。
3. **當日盤勢與廣度檢核：** 確認 `tw_index`、`otc_index`、`institutional_flow` 與 `market_breadth` 日期與最新交易日一致。

---

## Step 5：結果驗收、整理與推送至 GitHub

1. **檢查本機產出完整性：**
   * 確認 `local_output\summary_local.json` 筆數與 `data\priority_stocks.json` 總數一致（例如 `110/110`）。
   * 檢視 `local_output\progress.json` 中的 `errors` 區塊是否已清空或僅剩來源端尚未公布的欄位缺漏。
2. **更新正式資料檔：**
   * 確認無誤後，將驗證後的結果同步至 `data\summary.json`（及更新後的 `data\market.json`）。
3. **統一版控推送（避免中途頻繁 Push 衝突）：**
   ```powershell
   git status
   git add data/
   git commit -m "Update daily priority stocks summary and market data"
   git push origin main
   ```
   *(注意：請勿將 `local_output/` 內的暫存批次檔與 `progress.json` 推上遠端倉庫。)*