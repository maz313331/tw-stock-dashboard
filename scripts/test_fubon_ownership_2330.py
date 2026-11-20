import json
from fubon_neo.sdk import FubonSDK
from fubon_neo.fugle_marketdata.rest.base_rest import FugleAPIError

sdk = FubonSDK()
# 依實際環境填入帳號、密碼、憑證路徑與憑證密碼
login_res = sdk.login("YOUR_ID", "YOUR_PASSWORD", "PATH_TO_CERT", "CERT_PASSWORD")
if not login_res.is_success:
    raise RuntimeError(f"登入失敗: {login_res.message}")

sdk.init_realtime()
rest_stock = sdk.marketdata.rest_client.stock

try:
    tdcc = rest_stock.ownership.tdcc_distribution(symbol="2330")
    inst = rest_stock.ownership.institutional_trades(
        symbol="2330",
        **{"from": "2026-10-01", "to": "2026-11-19"},
    )
    print("=== TDCC 集保分佈 ===")
    print(json.dumps(tdcc, ensure_ascii=False, indent=2)[:1500])
    print("=== 三大法人買賣超（近幾筆） ===")
    print(json.dumps(inst, ensure_ascii=False, indent=2)[:1500])
except FugleAPIError as e:
    print(f"API 錯誤: {e.status_code} - {e.response_text}")