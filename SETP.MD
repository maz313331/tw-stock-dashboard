# 台股 Dashboard 執行與驗證步驟手冊（SETP.md）

本手冊說明本機操作、全市場分批驗證、資料健康檢查與 GitHub Actions 上版執行的標準步驟。

## 一、 本機單批與全市場驗證步驟

### 1. 更新大盤概況（`data/market.json`）
```powershell
python .\scripts\update_market.py