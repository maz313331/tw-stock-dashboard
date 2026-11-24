import argparse
import datetime as dt
import json
import math
from pathlib import Path
import time
import pandas as pd
import requests
from calc_scores import write_json
from fetch_sources import fetch_yf_history

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}
DEFAULT_TIMEOUT = 10
MAX_HISTORY_ROWS = 120
TZ_CST = dt.timezone(dt.timedelta(hours=8))


def now_cst() -> dt.datetime:
    return dt.datetime.now(TZ_CST)


def _safe_num(val, default: float = 0.0) -> float:
    try:
        num = float(val)
    except (ValueError, TypeError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _sanitize_split_discontinuity(df: pd.DataFrame) -> pd.DataFrame:
    """
    偵測未還原的 ETF 股票分割/反分割跳空（單日漲跌幅超過 ±35%，台股 ETF 無此單日幅度），
    僅保留分割發生後的連續價格序列，避免出現如 00662 分割後 MA20/MA60 嚴重失真。
    """
    if df is None or len(df) < 2 or "close" not in df.columns:
        return df
    work = df.copy().reset_index(drop=True)
    closes = pd.to_numeric(work["close"], errors="coerce")
    ratios = closes / closes.shift(1)
    jump_indices = work.index[(ratios > 1.35) | (ratios < 0.65)].tolist()
    if jump_indices:
        last_jump = jump_indices[-1]
        work = work.iloc[last_jump:].reset_index(drop=True)
    return work


def calc_rsi(series: pd.Series, period: int = 14) -> float:
    clean = pd.to_numeric(series, errors="coerce").replace([float("inf"), float("-inf")], float("nan")).dropna()
    if len(clean) <= period:
        return 50.0

    delta = clean.diff()
    gain = delta.clip(lower=0).rolling(window=period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(_safe_num(val, 50.0), 1) if pd.notna(val) else 50.0


def classify_trend(last_close: float, ma5: float, ma20: float, ma60: float) -> str:
    if last_close > ma5 > ma20 > ma60:
        return "多頭排列"
    if last_close > ma20 and last_close > ma60:
        return "均線之上偏多"
    if last_close < ma5 < ma20 < ma60:
        return "空頭排列"
    if last_close < ma20 and last_close < ma60:
        return "均線下方偏弱"
    return "均線糾結震盪"


def calc_period_return(close_s: pd.Series, lookback: int) -> float:
    if len(close_s) <= lookback:
        return 0.0
    base = _safe_num(close_s.iloc[-(lookback + 1)], 0.0)
    last = _safe_num(close_s.iloc[-1], 0.0)
    if not base:
        return 0.0
    return round(((last - base) / base) * 100.0, 2)


def evaluate_etf_metrics(
    sym: str,
    name: str,
    updated_at: str,
    df: pd.DataFrame,
    chip_data: dict | None = None,
) -> tuple[dict, dict]:
    chip = chip_data or {}
    clean_df = _sanitize_split_discontinuity(df)
    close_s = pd.to_numeric(clean_df["close"], errors="coerce").replace([float("inf"), float("-inf")], float("nan")).dropna()
    if len(close_s) < 2:
        raise ValueError(f"{sym} 有效收盤價資料不足（筆數={len(close_s)}）")

    last_close = round(_safe_num(close_s.iloc[-1]), 2)
    prev_close = round(_safe_num(close_s.iloc[-2]), 2)
    chg_pct = round(((last_close - prev_close) / prev_close) * 100.0, 2) if prev_close else 0.0
    chg_5d = calc_period_return(close_s, 5)
    chg_20d = calc_period_return(close_s, 20)

    ma5 = round(_safe_num(close_s.tail(5).mean()), 2)
    ma20 = round(_safe_num(close_s.tail(20).mean()), 2)
    ma60 = round(_safe_num(close_s.tail(60).mean()), 2) if len(close_s) >= 60 else ma20
    rsi14 = calc_rsi(close_s, 14)
    trend_status = classify_trend(last_close, ma5, ma20, ma60)

    inst_5d_net = round(_safe_num(chip.get("inst_5d_net", 0.0)), 1)
    concentration_5d = round(_safe_num(chip.get("concentration_5d", 0.0)), 2)

    row_obj = {
        "symbol": sym,
        "name": name,
        "asset_type": "ETF",
        "close": last_close,
        "change_pct": chg_pct,
        "change_5d_pct": chg_5d,
        "change_20d_pct": chg_20d,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "rsi14": rsi14,
        "trend_status": trend_status,
        "inst_5d_net": inst_5d_net,
        "concentration_5d": concentration_5d,
        "updated_at": updated_at,
    }

    detail_obj = {
        "symbol": sym,
        "name": name,
        "asset_type": "ETF",
        "updated_at": updated_at,
        "excluded": True,
        "exclude_reason": "ETF 獨立追蹤模式（不適用個股財報與估值評分）",
        "notice": (
            f"ETF 獨立追蹤模式：目前技術架構為「{trend_status}」，"
            f"RSI(14)={rsi14}，近 20 日報酬 {chg_20d:+.2f}%。"
            "ETF 不適用個股財報(EPS/ROE)與本益比評分。"
        ),
        "quote": row_obj,
    }
    return detail_obj, row_obj


def load_etf_targets(root: Path, offset: int = 0, limit: int = 100) -> list[tuple[str, str, str]]:
    etf_path = root / "data" / "stock_list_etf.json"
    pool: list[tuple[str, str, str]] = []
    if etf_path.exists():
        raw = json.loads(etf_path.read_text(encoding="utf-8"))
        for item in raw:
            sym = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or sym).strip()
            mkt = str(item.get("market") or ("TWO" if sym.endswith("B") else "TW")).strip()
            if sym:
                pool.append((sym, name, mkt))

    start = max(0, offset)
    return pool[start : start + limit] if (limit and limit > 0) else pool[start:]


def _extract_market_date(label: str) -> str:
    if "行情日 " in label:
        return label.split("行情日 ", 1)[1].rstrip("）)").strip()
    return ""


def merge_etf_summary(etf_summary_path: Path, new_rows: list[dict]) -> tuple[list[dict], str]:
    by_sym: dict[str, dict] = {}
    prev_latest_market_date = ""
    if etf_summary_path.exists():
        try:
            prev = json.loads(etf_summary_path.read_text(encoding="utf-8"))
            prev_latest_market_date = _extract_market_date(str(prev.get("updated_at") or ""))
            for r in prev.get("etfs", []):
                s = str(r.get("symbol") or "").strip()
                if s:
                    by_sym[s] = r
        except Exception:
            pass

    for r in new_rows:
        by_sym[r["symbol"]] = r

    merged = sorted(by_sym.values(), key=lambda x: x["symbol"])
    all_dates = [
        _extract_market_date(str(r.get("updated_at") or ""))
        for r in merged
        if _extract_market_date(str(r.get("updated_at") or ""))
    ]
    if prev_latest_market_date:
        all_dates.append(prev_latest_market_date)
    best_market_date = max(all_dates) if all_dates else ""
    return merged, best_market_date


def _parse_roc_row(row: list, close_idx: int) -> dict | None:
    if len(row) <= close_idx:
        return None
    parts = str(row[0]).strip().split("/")
    if len(parts) != 3:
        return None
    try:
        roc_year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        iso_date = f"{roc_year + 1911:04d}-{month:02d}-{day:02d}"
        close_str = str(row[close_idx]).replace(",", "").strip()
        if not close_str or close_str in {"--", "-"}:
            return None
        val = float(close_str)
        if math.isnan(val) or math.isinf(val) or val <= 0:
            return None
        return {"date": iso_date, "close": val}
    except ValueError:
        return None


def fetch_twse_stock_day_fallback(
    sym: str,
    months: int = 6,
    session: requests.Session | None = None,
) -> pd.DataFrame | None:
    """當 Yahoo Finance 查無資料時，從證交所 STOCK_DAY 逐月抓近幾個月日線。"""
    today = now_cst().date()
    frames: list[dict] = []
    client = session or requests.Session()

    for m in range(months):
        year = today.year
        month = today.month - m
        while month <= 0:
            month += 12
            year -= 1
        date_str = f"{year}{month:02d}01"
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?date={date_str}&stockNo={sym}&response=json"
        )
        try:
            resp = client.get(url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("stat") == "OK" and data.get("data"):
                for row in data["data"]:
                    parsed = _parse_roc_row(row, close_idx=6)
                    if parsed:
                        frames.append(parsed)
            time.sleep(0.25)
        except Exception:
            continue

    if not frames:
        return None

    df = (
        pd.DataFrame(frames)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    return df.tail(MAX_HISTORY_ROWS).reset_index(drop=True) if len(df) >= 2 else None


def fetch_tpex_stock_day_fallback(
    sym: str,
    months: int = 6,
    session: requests.Session | None = None,
) -> pd.DataFrame | None:
    """針對上櫃債券/股票 ETF（.TWO），從櫃買中心逐月抓近幾個月日線。"""
    today = now_cst().date()
    frames: list[dict] = []
    client = session or requests.Session()

    for m in range(months):
        year = today.year
        month = today.month - m
        while month <= 0:
            month += 12
            year -= 1
        roc_year = year - 1911
        date_str = f"{roc_year}/{month:02d}"
        url = (
            "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
            f"?l=zh-tw&d={date_str}&stkno={sym}&o=json"
        )
        try:
            resp = client.get(url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("aaData") or []
            if not rows and isinstance(data.get("tables"), list) and data["tables"]:
                rows = data["tables"][0].get("data") or []
            for row in rows:
                parsed = _parse_roc_row(row, close_idx=6)
                if parsed:
                    frames.append(parsed)
            time.sleep(0.25)
        except Exception:
            continue

    if not frames:
        return None

    df = (
        pd.DataFrame(frames)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    return df.tail(MAX_HISTORY_ROWS).reset_index(drop=True) if len(df) >= 2 else None


def fetch_etf_history_with_fallback(
    sym: str,
    mkt: str = "TW",
    session: requests.Session | None = None,
) -> pd.DataFrame | None:
    is_otc = str(mkt).upper() in {"TWO", "OTC", "TPEX"} or sym.endswith("B")
    suffixes = [".TWO", ".TW"] if is_otc else [".TW", ".TWO"]
    for sfx in suffixes:
        df = fetch_yf_history(
            f"{sym}{sfx}",
            period="6mo",
            limit=MAX_HISTORY_ROWS,
            retries=2,
            retry_delay=0.8,
        )
        if df is not None and not df.empty and len(df) >= 2:
            return df

    if is_otc:
        tpex_df = fetch_tpex_stock_day_fallback(sym, months=6, session=session)
        if tpex_df is not None and len(tpex_df) >= 2:
            return tpex_df

    twse_df = fetch_twse_stock_day_fallback(sym, months=6, session=session)
    if twse_df is not None and len(twse_df) >= 2:
        return twse_df

    if not is_otc:
        tpex_df = fetch_tpex_stock_day_fallback(sym, months=6, session=session)
        if tpex_df is not None and len(tpex_df) >= 2:
            return tpex_df

    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="台股 ETF 獨立趨勢與動能批次更新")
    parser.add_argument("--offset", type=int, default=0, help="從 ETF 清單第幾筆開始（預設 0）")
    parser.add_argument("--limit", type=int, default=100, help="單次處理幾檔 ETF（預設 100；設 0 代表全量）")
    parser.add_argument("--sleep", type=float, default=0.25, help="每檔間隔秒數")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    etf_summary_path = root / "data" / "etf_summary.json"
    targets = load_etf_targets(root, offset=args.offset, limit=args.limit)

    if not targets:
        print("[提示] 找不到 data/stock_list_etf.json 或目標清單為空，請先執行 fetch_stock_list.py")
        return

    now_str = now_cst().strftime("%Y-%m-%d %H:%M")
    batch_rows: list[dict] = []
    skipped_syms: list[str] = []
    latest_date = ""
    default_chip = {"inst_5d_net": 0.0, "concentration_5d": 0.0}

    with requests.Session() as session:
        for idx, (sym, name, mkt) in enumerate(targets):
            if (idx + 1) % 25 == 0 or idx == 0:
                print(f"[ETF 進度] {idx + 1}/{len(targets)} -> {sym} {name}")

            df = fetch_etf_history_with_fallback(sym, mkt=mkt, session=session)
            if df is None:
                skipped_syms.append(sym)
                print(f"[略過] {sym} {name} 暫時無法取得日線")
                continue

            row_date = str(df.iloc[-1]["date"])
            if row_date > latest_date:
                latest_date = row_date
            updated_label = f"{now_str}（行情日 {row_date}）"

            try:
                detail_obj, row_obj = evaluate_etf_metrics(sym, name, updated_label, df, default_chip)
            except Exception as exc:
                skipped_syms.append(sym)
                print(f"[略過] {sym} {name} 指標計算失敗：{exc}")
                continue

            write_json(root / "data" / "details" / f"{sym}.json", detail_obj)
            batch_rows.append(row_obj)

            if args.sleep > 0 and idx < len(targets) - 1:
                time.sleep(args.sleep)

    merged, best_market_date = merge_etf_summary(etf_summary_path, batch_rows)
    effective_date = max(latest_date, best_market_date)
    final_label = f"{now_str}（行情日 {effective_date}）" if effective_date else now_str
    write_json(
        etf_summary_path,
        {
            "updated_at": final_label,
            "count": len(merged),
            "etfs": merged,
        },
    )
    print(
        f"[完成] ETF 批次更新 | 本批成功={len(batch_rows)} (略過={len(skipped_syms)}) "
        f"| 累計 ETF 總數={len(merged)}"
    )
    if skipped_syms:
        print(f"[略過清單] {', '.join(skipped_syms)}")


if __name__ == "__main__":
    main()