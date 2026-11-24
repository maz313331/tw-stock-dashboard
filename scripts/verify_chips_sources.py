import argparse
import datetime as dt
import os
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def check_finmind(symbol: str, start_date: str, token: str):
    datasets = [
        "TaiwanStockInstitutionalInvestorsBuySell",
        "TaiwanStockMarginPurchaseShortSale",
    ]
    results = {}
    for ds in datasets:
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": ds,
            "data_id": symbol,
            "start_date": start_date,
        }
        if token:
            params["token"] = token
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            status = r.status_code
            try:
                body = r.json()
            except Exception:
                body = {}
            rows = body.get("data", []) if isinstance(body, dict) else []
            msg = body.get("msg", "") if isinstance(body, dict) else ""
            results[ds] = {
                "http_status": status,
                "count": len(rows) if isinstance(rows, list) else 0,
                "msg": msg,
            }
        except Exception as e:
            results[ds] = {"http_status": "ERR", "count": 0, "msg": str(e)}
    return results


def check_twse_t86(symbol: str, yyyymmdd: str):
    """TWSE 三大法人買賣超日報（T86），指定日期一次回傳全市場"""
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    params = {"date": yyyymmdd, "selectType": "ALL", "response": "json"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = r.json()
        stat = data.get("stat", "")
        rows = data.get("data", []) or []
        matched = [row for row in rows if row and str(row[0]).strip() == symbol]
        return {
            "http_status": r.status_code,
            "stat": stat,
            "total_market_rows": len(rows),
            "matched_rows": len(matched),
            "sample": matched[0][:6] if matched else None,
        }
    except Exception as e:
        return {"http_status": "ERR", "stat": "", "total_market_rows": 0, "matched_rows": 0, "msg": str(e)}


def check_twse_margin(symbol: str, yyyymmdd: str):
    """TWSE 融資融券日報（MI_MARGN），指定日期一次回傳全市場"""
    url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
    params = {"date": yyyymmdd, "selectType": "ALL", "response": "json"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = r.json()
        stat = data.get("stat", "")
        tables = data.get("tables", []) or []
        matched = []
        for tbl in tables:
            for row in tbl.get("data", []) or []:
                if row and str(row[0]).strip() == symbol:
                    matched.append(row[:6])
        return {
            "http_status": r.status_code,
            "stat": stat,
            "matched_rows": len(matched),
            "sample": matched[0] if matched else None,
        }
    except Exception as e:
        return {"http_status": "ERR", "stat": "", "matched_rows": 0, "msg": str(e)}


def check_tpex_institutional(symbol: str, roc_date: str):
    """TPEx 櫃買三大法人買賣超，roc_date 格式如 115/11/20"""
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {"l": "zh-tw", "d": roc_date, "se": "EW", "t": "D", "o": "json"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = r.json()
        rows = data.get("aaData", []) or []
        matched = [row for row in rows if row and str(row[0]).strip() == symbol]
        return {
            "http_status": r.status_code,
            "total_market_rows": len(rows),
            "matched_rows": len(matched),
            "sample": matched[0][:6] if matched else None,
        }
    except Exception as e:
        return {"http_status": "ERR", "total_market_rows": 0, "matched_rows": 0, "msg": str(e)}


def main():
    parser = argparse.ArgumentParser(description="驗證 FinMind 與 TWSE/TPEx 籌碼替代來源")
    parser.add_argument("--symbols", nargs="+", default=["9951", "9958"])
    parser.add_argument("--start-date", default="2026-11-16", help="FinMind 起始日 YYYY-MM-DD")
    parser.add_argument("--twse-date", default="20261120", help="TWSE 交易日 YYYYMMDD")
    parser.add_argument("--tpex-date", default="115/11/20", help="TPEx 民國日 YYY/MM/DD")
    parser.add_argument("--token", default=os.environ.get("FINMIND_TOKEN", ""))
    args = parser.parse_args()

    for sym in args.symbols:
        print(f"================ 驗證標的: {sym} ================")
        print("[1] FinMind:", check_finmind(sym, args.start_date, args.token))
        print("[2] TWSE T86 (法人):", check_twse_t86(sym, args.twse_date))
        print("[3] TWSE MI_MARGN (資券):", check_twse_margin(sym, args.twse_date))
        print("[4] TPEx 3itrade (櫃買法人):", check_tpex_institutional(sym, args.tpex_date))


if __name__ == "__main__":
    main()