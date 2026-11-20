import datetime as dt
import io
import pandas as pd
import requests
import yfinance as yf


def fetch_yf_history(ticker: str, period: str = "6mo") -> pd.DataFrame:
    """主軌：透過 yfinance 抓取歷史日線"""
    df = yf.download(ticker, period=period, interval="1d", auto_adjust=False, progress=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df["date"] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume"
    })
    return df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).tail(120)


def fetch_stooq_us_history(stooq_symbol: str) -> pd.DataFrame:
    """備軌：透過 Stooq 下載美股指數日線 CSV"""
    url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    if df.empty or "Close" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume"
    })
    return df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).tail(120)


def fetch_finmind_tw_stock(stock_id: str, days_back: int = 180) -> pd.DataFrame:
    """備軌：透過 FinMind 抓取台股日線（如 2330）"""
    start_date = (dt.date.today() - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume"
    })
    return df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).tail(120)


def summarize_series(label: str, df: pd.DataFrame):
    if df.empty or len(df) < 2:
        print(f"[失敗] {label}: 抓不到足夠的歷史資料")
        return
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    last_c = float(last_row["close"])
    prev_c = float(prev_row["close"])
    chg_pts = last_c - prev_c
    chg_pct = (chg_pts / prev_c) * 100 if prev_c else 0.0
    print(
        f"[成功] {label} | 筆數: {len(df)} | 最新日: {last_row['date']} | "
        f"收盤: {last_c:.2f} | 漲跌: {chg_pts:+.2f} ({chg_pct:+.2f}%)"
    )


def main():
    print("=== 1. 測試台股大盤與 2330（主軌 yfinance + 備軌雙確認）===")
    twii_df = fetch_yf_history("^TWII")
    summarize_series("加權指數 (^TWII) [yfinance]", twii_df)

    s2330_yf = fetch_yf_history("2330.TW")
    summarize_series("台積電 (2330.TW) [yfinance]", s2330_yf)

    try:
        s2330_fm = fetch_finmind_tw_stock("2330")
        summarize_series("台積電 (2330) [FinMind 備軌]", s2330_fm)
        if not s2330_yf.empty and not s2330_fm.empty:
            yf_last = s2330_yf.iloc[-1]
            fm_last = s2330_fm.iloc[-1]
            print(
                f"   -> 雙軌比對: yf({yf_last['date']}={float(yf_last['close']):.2f}) vs "
                f"finmind({fm_last['date']}={float(fm_last['close']):.2f})"
            )
    except Exception as e:
        print(f"   -> FinMind 備軌測試略過/異常: {e}")

    print("\n=== 2. 測試美股四大指數（主軌 yfinance + 備軌 Stooq）===")
    us_targets = [
        ("道瓊工業", "^DJI", "^dji"),
        ("標普 500", "^GSPC", "^spx"),
        ("那斯達克", "^IXIC", "^ndq"),
        ("費城半導體", "^SOX", "^sox"),
    ]
    for name, yf_sym, stooq_sym in us_targets:
        df_us = fetch_yf_history(yf_sym)
        if df_us.empty:
            try:
                df_us = fetch_stooq_us_history(stooq_sym)
                summarize_series(f"{name} ({yf_sym}) [Stooq 備軌]", df_us)
            except Exception as e:
                print(f"[失敗] {name} ({yf_sym}): {e}")
        else:
            summarize_series(f"{name} ({yf_sym}) [yfinance]", df_us)


if __name__ == "__main__":
    main()