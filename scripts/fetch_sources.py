import datetime as dt
import io
import os
from pathlib import Path
import time
import pandas as pd
import requests
import yfinance as yf

TZ_CST = dt.timezone(dt.timedelta(hours=8))


def _today_cst() -> dt.date:
    return dt.datetime.now(TZ_CST).date()


def _load_finmind_token() -> str:
    token = (os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_TOKEN") or "").strip()
    if token:
        return token
    for env_file in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    if k.strip() in {"FINMIND_TOKEN", "FINMIND_API_TOKEN"}:
                        val = v.strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
    return ""


def _sanitize_split_jump(df: pd.DataFrame) -> pd.DataFrame:
    """若收盤價出現單日 >35% 或 <-35% 的跳空（常見於未還原分割/反分割），保留最新連續區段。"""
    if df is None or len(df) < 2 or "close" not in df.columns:
        return df
    work = df.copy().reset_index(drop=True)
    closes = pd.to_numeric(work["close"], errors="coerce")
    ratios = closes / closes.shift(1)
    jump_idx = work.index[(ratios > 1.35) | (ratios < 0.65)].tolist()
    if jump_idx:
        work = work.iloc[jump_idx[-1]:].reset_index(drop=True)
    return work


def _normalize_ohlcv_df(df: pd.DataFrame, limit: int = 120) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()

    if isinstance(work.columns, pd.MultiIndex):
        work.columns = [
            col[0] if isinstance(col, tuple) and len(col) > 0 else col
            for col in work.columns
        ]

    rename_map = {
        "Date": "date",
        "Datetime": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume",
    }
    work = work.rename(columns={k: v for k, v in rename_map.items() if k in work.columns})

    if "date" not in work.columns:
        work = work.reset_index()
        first_col = work.columns[0]
        work["date"] = pd.to_datetime(work[first_col], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in work.columns:
            work[col] = pd.NA
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = (
        work[["date", "open", "high", "low", "close", "volume"]]
        .dropna(subset=["date", "close"])
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    work = work[work["close"] > 0].reset_index(drop=True)
    if work.empty:
        return pd.DataFrame()

    work = _sanitize_split_jump(work)
    return work.tail(limit).reset_index(drop=True)


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
            out = _normalize_ohlcv_df(df, limit=limit)
            if not out.empty and len(out) >= 2:
                return out
        except Exception:
            pass

        if attempt < retries:
            time.sleep(retry_delay * attempt)

    return pd.DataFrame()


def fetch_stooq_us_history(stooq_symbol: str, limit: int = 120) -> pd.DataFrame:
    try:
        url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        return _normalize_ohlcv_df(df, limit=limit)
    except Exception:
        return pd.DataFrame()


def fetch_finmind_tw_stock(stock_id: str, days_back: int = 210, limit: int = 120) -> pd.DataFrame:
    start_date = (_today_cst() - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": str(stock_id).strip(),
        "start_date": start_date,
    }
    token = _load_finmind_token()
    if token:
        params["token"] = token

    for attempt in range(1, 3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("msg") not in (None, "", "success"):
                if attempt < 2:
                    time.sleep(1.0)
                    continue
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if not rows:
                return pd.DataFrame()
            out = _normalize_ohlcv_df(pd.DataFrame(rows), limit=limit)
            if not out.empty:
                return out
        except Exception:
            if attempt < 2:
                time.sleep(1.0)
    return pd.DataFrame()


def fetch_tw_history_with_fallback(
    stock_id: str,
    yf_tickers: list[str] | None = None,
    period: str = "6mo",
    days_back: int = 210,
    limit: int = 120,
) -> pd.DataFrame:
    """先嘗試 yfinance 代號清單，抓不到時退回 FinMind TaiwanStockPrice。"""
    sym = str(stock_id).strip()
    candidates = yf_tickers if yf_tickers else ([sym] if sym.startswith("^") else [f"{sym}.TW", f"{sym}.TWO"])
    for tkr in candidates:
        df = fetch_yf_history(tkr, period=period, limit=limit, retries=1, retry_delay=0.5)
        if not df.empty and len(df) >= 2:
            return df

    if not sym.startswith("^"):
        fm_df = fetch_finmind_tw_stock(sym, days_back=days_back, limit=limit)
        if not fm_df.empty and len(fm_df) >= 2:
            return fm_df

    return pd.DataFrame()


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