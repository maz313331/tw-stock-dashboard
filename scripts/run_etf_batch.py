import argparse
import datetime as dt
import json
from pathlib import Path
import time
import pandas as pd
from calc_scores import write_json
from fetch_chips import fetch_chip_data
from fetch_sources import fetch_yf_history


def calc_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 1) if pd.notna(val) else 50.0


def evaluate_etf_metrics(sym: str, name: str, updated_at: str, df: pd.DataFrame, chip_data: dict) -> tuple[dict, dict]:
    close_s = df["close"].astype(float)
    last_close = round(float(close_s.iloc[-1]), 2)
    prev_close = round(float(close_s.iloc[-2]), 2)
    chg_pct = round(((last_close - prev_close) / prev_close) * 100.0, 2) if prev_close else 0.0

    chg_5d = round(((last_close - float(close_s.iloc[-6])) / float(close_s.iloc[-6])) * 100.0, 2) if len(close_s) >= 6 else 0.0
    chg_20d = round(((last_close - float(close_s.iloc[-21])) / float(close_s.iloc[-21])) * 100.0, 2) if len(close_s) >= 21 else 0.0

    ma5 = round(float(close_s.tail(5).mean()), 2)
    ma20 = round(float(close_s.tail(20).mean()), 2)
    ma60 = round(float(close_s.tail(60).mean()), 2) if len(close_s) >= 60 else ma20
    rsi14 = calc_rsi(close_s, 14)

    if last_close > ma5 > ma20 > ma60:
        trend_status = "多頭排列"
    elif last_close > ma20 and last_close > ma60:
        trend_status = "均線之上偏多"
    elif last_close < ma5 < ma20 < ma60:
        trend_status = "空頭排列"
    elif last_close < ma20 and last_close < ma60:
        trend_status = "均線下方偏弱"
    else:
        trend_status = "均線糾結震盪"

    inst_5d_net = round(float(chip_data.get("inst_5d_net", 0.0) or 0.0), 1)
    concentration_5d = round(float(chip_data.get("concentration_5d", 0.0) or 0.0), 2)

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
        "notice": f"ETF 獨立追蹤模式：目前技術架構為「{trend_status}」，RSI(14)={rsi14}，近 20 日報酬 {chg_20d:+.2f}%。ETF 不適用個股財報(EPS/ROE)與本益比評分。",
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
            mkt = str(item.get("market") or "TW").strip()
            if sym:
                pool.append((sym, name, mkt))
    start = max(0, offset)
    return pool[start : start + limit] if (limit and limit > 0) else pool[start:]


def merge_etf_summary(etf_summary_path: Path, new_rows: list[dict]) -> list[dict]:
    by_sym: dict[str, dict] = {}
    if etf_summary_path.exists():
        try:
            prev = json.loads(etf_summary_path.read_text(encoding="utf-8"))
            for r in prev.get("etfs", []):
                s = str(r.get("symbol") or "").strip()
                if s:
                    by_sym[s] = r
        except Exception:
            pass
    for r in new_rows:
        by_sym[r["symbol"]] = r
    return sorted(by_sym.values(), key=lambda x: x["symbol"])


def main():
    parser = argparse.ArgumentParser(description="台股 ETF 獨立趨勢與動能批次更新")
    parser.add_argument("--offset", type=int, default=0, help="從 ETF 清單第幾筆開始（預設 0）")
    parser.add_argument("--limit", type=int, default=100, help="單次處理幾檔 ETF（預設 100；設 0 代表全量 247 檔）")
    parser.add_argument("--sleep", type=float, default=0.15, help="每檔間隔秒數")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    etf_summary_path = root / "data" / "etf_summary.json"
    targets = load_etf_targets(root, offset=args.offset, limit=args.limit)

    if not targets:
        print("[提示] 找不到 data/stock_list_etf.json 或目標清單為空，請先執行 fetch_stock_list.py")
        return

    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    batch_rows = []
    skipped = 0
    latest_date = ""

    for idx, (sym, name, mkt) in enumerate(targets):
        if (idx + 1) % 25 == 0 or idx == 0:
            print(f"[ETF 進度] {idx + 1}/{len(targets)} -> {sym} {name}")

        df = fetch_yf_history(f"{sym}.TW", period="6mo", limit=120, retries=1, retry_delay=0.5)
        if df is None or df.empty or len(df) < 2:
            df = fetch_yf_history(f"{sym}.TWO", period="6mo", limit=120, retries=1, retry_delay=0.5)

        if df is None or df.empty or len(df) < 2:
            skipped += 1
            print(f"[略過] {sym} {name} 暫時無法取得日線")
            continue

        latest_date = str(df.iloc[-1]["date"])
        updated_label = f"{now_str}（行情日 {latest_date}）"
        chip_data = fetch_chip_data(sym, stock_df=df)

        detail_obj, row_obj = evaluate_etf_metrics(sym, name, updated_label, df, chip_data)
        write_json(root / "data" / "details" / f"{sym}.json", detail_obj)
        batch_rows.append(row_obj)

        if args.sleep > 0 and idx < len(targets) - 1:
            time.sleep(args.sleep)

    merged = merge_etf_summary(etf_summary_path, batch_rows)
    final_label = f"{now_str}（行情日 {latest_date}）" if latest_date else now_str
    write_json(
        etf_summary_path,
        {
            "updated_at": final_label,
            "count": len(merged),
            "etfs": merged,
        },
    )
    print(f"[完成] ETF 批次更新 | 本批成功={len(batch_rows)} (略過={skipped}) | 累計 ETF 總數={len(merged)}")


if __name__ == "__main__":
    main()