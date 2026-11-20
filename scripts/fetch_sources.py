import datetime as dt
import io
import time
import pandas as pd
import requests
import yfinance as yf


def fetch_yf_history(
    ticker: str,
    period: str = "6mo",
    limit: int = 120,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> pd.DataFrame:
    for attempt in range(1, retries + 1):
        try:
            df = yf.download(
                ticker,
                period=period,
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if df is not None and not df.empty:
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
                    "Volume": "volume",
                })
                out = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).copy()
                for col in ["open", "high", "low", "close", "volume"]:
                    out[col] = pd.to_numeric(out[col], errors="coerce")
                out = out.dropna(subset=["close"]).drop_duplicates(subset=["date"]).sort_values("date")
                if not out.empty:
                    return out.tail(limit).reset_index(drop=True)
        except Exception:
            pass

        if attempt < retries:
            time.sleep(retry_delay * attempt)

    return pd.DataFrame()


def fetch_stooq_us_history(stooq_symbol: str, limit: int = 120) -> pd.DataFrame:
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
        "Volume": "volume",
    })
    out = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).copy()
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"]).drop_duplicates(subset=["date"]).sort_values("date")
    return out.tail(limit).reset_index(drop=True)


def fetch_finmind_tw_stock(stock_id: str, days_back: int = 210, limit: int = 120) -> pd.DataFrame:
    start_date = (dt.date.today() - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume",
    })
    out = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).copy()
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"]).drop_duplicates(subset=["date"]).sort_values("date")
    return out.tail(limit).reset_index(drop=True)


def get_us_index_quote(name: str, yf_sym: str, stooq_sym: str, note: str) -> dict:
    df = fetch_yf_history(yf_sym, period="6mo", limit=120)
    source = "yfinance"
    if df.empty or len(df) < 2:
        df = fetch_stooq_us_history(stooq_sym, limit=120)
        source = "stooq"
    if df.empty or len(df) < 2:
        return {
            "name": name,
            "symbol": yf_sym,
            "close": None,
            "change_pts": None,
            "change_pct": None,
            "date": None,
            "source": "missing",
            "note": note,
        }
    last_c = float(df.iloc[-1]["close"])
    prev_c = float(df.iloc[-2]["close"])
    chg_pts = round(last_c - prev_c, 2)
    chg_pct = round((chg_pts / prev_c) * 100, 2) if prev_c else 0.0
    return {
        "name": name,
        "symbol": yf_sym,
        "close": round(last_c, 2),
        "change_pts": chg_pts,
        "change_pct": chg_pct,
        "date": str(df.iloc[-1]["date"]),
        "source": source,
        "note": note,
    }