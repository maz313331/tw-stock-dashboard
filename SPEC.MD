# 台股 Dashboard 批次抓取與評分系統規格書（SPEC.md）

## 一、 專案定位與核心架構
本專案（`tw-stock-dashboard`）用於自動化蒐集台股市場概況、個股／ETF 行情、籌碼、基本面與技術指標並產出評分明細與彙整資料，支援本機分批執行與 GitHub Actions 排程更新。

### 主要腳本與職責分工
- **批次主控與全市場排程**：
  - `scripts/run_dashboard_batch.py`：核心單批／分批執行器，支援 `--phase`（`auto`、`close_tech`、`chips`、`fundamental`）、`--offset`、`--limit`、`--sleep`、`--max-hourly-calls`、`--only-missing`、`--force-check`、`--priority-file`、`--full-market`、`--no-merge`，並透過 `ApiQuotaGuard` 控制每小時請求上限（預設 `580` 次/hr，低於 `600` 次/hr 緩衝）與防空值安全合併（`merge_summary_stocks`）。
  - `scripts/run_all_batches.py`：全市場分批排程器，讀取 `data/stock_list_common.json`（約 `1,990` 檔），依 `--batch-size`、`--min-wait`、`--max-wait`、`--sleep` 分批呼叫 `scripts/run_dashboard_batch.py`（預設未帶 `--full-market`，全市場手動／自動化需確保傳入 `--full-market`）。
  - `scripts/run_etf_batch.py`：ETF 批次更新與指標計算。
- **清單與大盤／附屬更新**：
  - `scripts/build_priority_stocks.py`：產生與更新 `data/priority_stocks.json`（核心／熱門動態清單，實測約 `110` 檔，可依 `--top-amount`、`--top-volume`、`--max-total` 調整）。
  - `scripts/update_market.py`：更新 `data/market.json`（加權指數、漲跌幅、均線與市場燈號；遇 `^TWOII` 查無資料時走防呆不中斷）。
  - `scripts/fetch_stock_list.py`、`scripts/fetch_tpex.py`：證券清單與上櫃／上市輔助資料抓取。
- **各維度抓取與算分**：
  - `scripts/calc_scores.py`：KD、RSI、MACD、技術面評分（`evaluate_technical_10`）與個股明細組裝（`build_stock_detail`）。
  - `scripts/fetch_chips.py`、`scripts/chips_fallback_helper.py`、`scripts/fetch_tdcc.py`、`scripts/verify_chips_sources.py`：三大法人、融資融券、集保股權分散與籌碼評分（`fetch_chip_data`、`evaluate_chip_10`）。
  - `scripts/fetch_fundamental.py`、`scripts/fetch_sources.py`：基本面、財報、月營收、估值與歷史行情抓取（`fetch_fundamental_data`、`evaluate_fundamental_10`、`fetch_yf_history`）。
- **資料健康檢查**：
  - `check_data_health.py`：遞迴掃描 `data/` 底下所有 `.json` 檔案語法與 `NaN`／`Infinity` 非標準浮點數，並抽查指定個股明細檔；其 `check_zeros_and_missing()` 僅統計頂層 `list` 或 `dict["items"]`。

---

## 二、 關鍵資料檔結構與對齊注意事項
1. **`data/priority_stocks.json`（白名單優先清單）**：
   - 作為核心／動態熱門股清單（約 `110` 檔），避免日常排程對過期或非核心標的做無效重複查詢。
2. **`data/stock_list_common.json`（全市場普通股清單）**：
   - 實測普通股總數約 `1,990` 檔；搭配 `--full-market` 可跑全市場區間。
3. **`data/summary.json`（彙整輸出檔）**：
   - 實際結構為 `dict`，包含 `["updated_at", "stocks"]`（`stocks` 為個股/標的列清單，全市場批次合併後已達 `2,012` 檔）。
   - 注意：`check_data_health.py` 讀取 `summary.json` 時僅檢查頂層 `list` 或 `dict["items"]`，因此 `check_data_health.py` 顯示 `summary.json 摘要：總筆數=0` 屬鍵名差異，不代表 `stocks` 無資料；驗證 `summary.json` 應直接讀取 `d.get("stocks", [])` 與 `d.get("updated_at")`。
4. **`data/market.json` 與 `data/` 全量 JSON**：
   - 存放加權指數、市場燈號與各類快取／明細 JSON（實測共 `2,411` 個 JSON 檔，語法與數值合法性異常 `0` 個）。

---

## 三、 GitHub Actions（`.github/workflows/update-stock-dashboard.yml`）抓取執行條件規格表

| 條件類別 | 參數 / 判斷規則 | 預設值 / 條件邏輯 | 實際影響與行為說明 |
| :--- | :--- | :--- | :--- |
| **排程時間（CST / UTC）** | `on.schedule` (`cron`) | • `0 7 * * 1-5`（週一～五 15:00 CST）<br>• `0 8 * * 1-5`（週一～五 16:00 CST）<br>• `0 13 * * *`（每日 21:00 CST） | 依盤後收盤、籌碼公布與晚間補齊節奏自動觸發；支援 `workflow_dispatch` 手動覆蓋 |
| **抓取範圍** | `market_scope` (`--full-market`) | • 預設 `priority`（`data/priority_stocks.json`）<br>• 手動可選 `full_market`（`data/stock_list_common.json`，約 `1,990` 檔） | `schedule` 固定走 `priority`；僅 `workflow_dispatch` 選 `full_market` 時切換全市場並加 `--full-market` |
| **執行模式** | `run_mode` | • `single_batch`（單批）<br>• `full_auto_all`（自動連續分批） | `schedule` 與 `single_batch` 跑單批；`full_auto_all` 依 `offset` 與 `limit` 迴圈推進直到清單結尾（無總數時上限 `25` 批） |
| **更新分段** | `phase` (`auto` 分流) | • 指定非 `auto` 時直接採用<br>• `auto` 且 CST `>= 20`：`fundamental`<br>• `auto` 且 CST `16~19`：`chips`<br>• 其餘時段：`close_tech` | 決定 `run_dashboard_batch.py` 優先更新哪個維度（收盤/技術、籌碼、基本面） |
| **缺漏補抓** | `only_missing` (`auto` 分流) | • `"true"` / `"false"` 依指定值<br>• `"auto"`：僅 `schedule` 且 CST `>= 20` 時為 `True`，其餘為 `False` | 避免盤中或剛收盤時把尚未公布的盤後籌碼誤判成大量缺漏硬補 |
| **分批與節流** | `offset`、`limit`、`sleep`、`max_hourly_calls` | • `offset`：預設 `0`<br>• `limit`：預設 `100`（全市場建議 `150~200`）<br>• `sleep`：預設 `0.5` 秒（建議 `0.45~0.6`）<br>• `max_hourly_calls`：固定 `580` | 控制單批處理量、單檔間隔與每小時 API 請求安全緩衝（低於 `600` 次/hr 上限） |
| **批間休息** | `full_auto_all` 迴圈間隔 | • `full_market`：每批休息 `20` 秒<br>• `priority`：每批休息 `8` 秒 | 降低連續分批對外部行情與籌碼端造成的瞬間請求壓力 |
| **重點股刷新** | `top_amount`、`top_volume`、`max_total` | • 預設 `60`、`40`、`150`<br>• 條件：非 `full_market`，且（`phase == "close_tech"` 或 `workflow_dispatch` 或清單檔不存在） | 符合條件時先執行 `scripts/build_priority_stocks.py` 更新動態熱門與核心清單 |
| **前後置作業** | 大盤更新、健檢與 Git 推送 | • 前置：先跑 `scripts/update_market.py`（失敗僅警告不中斷）<br>• 後置：跑 `check_data_health.py`，再 `git add data/` 並 `rebase` 衝突保護推送 `main` | 確保 `data/market.json` 優先更新、JSON 語法／數值合法，且多批次推送不覆蓋既有資料 |

---

## 四、 實測關鍵結論與風險防範
1. **盤中／盤後時間差造成的籌碼缺漏**：當日三大法人與融資融券籌碼多於盤後陸續公布；若在上午盤中執行 `--only-missing`，容易出現大量待補抓標的（例如上午盤中實測曾出現 `1,716` 檔待補），屬資料公布時序現象，建議安排在盤後或晚間時段補抓。
2. **全市場分批節奏**：單批 `100~200` 檔搭配 `--sleep 0.4~0.5` 秒與批間休息，可在維持 `0` 限流、未觸發配額熔斷的前提下穩定推進。