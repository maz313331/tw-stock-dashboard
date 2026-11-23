# scripts/calc_scores.py
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd
from fetch_chips import CHIP_LABELS, evaluate_chip_10
from fetch_fundamental import FUNDAMENTAL_LABELS, evaluate_fundamental_10


def _clean_series(s: pd.Series) -> pd.Series:
    num = pd.to_numeric(s, errors="coerce")
    return num.replace([np.inf, -np.inf], np.nan)


def _safe_float(val, default: float = 0.0) -> float:
    if isinstance(val, pd.Series):
        if val.empty:
            return default
        val = val.iloc[-1]
    elif isinstance(val, (list, tuple, np.ndarray)):
        if len(val) == 0:
            return default
        val = val[-1]
    try:
        f = float(val)
    except (ValueError, TypeError):
        return default
    if np.isnan(f) or np.isinf(f) or math.isnan(f) or math.isinf(f):
        return default
    return f


def _sanitize_for_json(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, pd.Series):
        if value.empty:
            return []
        return [_sanitize_for_json(v) for v in value.tolist()]
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return []
        return _sanitize_for_json(value.to_dict(orient="records"))
    if isinstance(value, np.ndarray):
        return [_sanitize_for_json(v) for v in value.tolist()]
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        num = float(value)
        return None if (math.isnan(num) or math.isinf(num)) else num
    if isinstance(value, dict):
        return {str(k): _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_for_json(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def calc_kd(df: pd.DataFrame, n: int = 9) -> tuple[pd.Series, pd.Series]:
    low = _clean_series(df["low"])
    high = _clean_series(df["high"])
    close = _clean_series(df["close"])

    low_n = low.rolling(n, min_periods=n).min()
    high_n = high.rolling(n, min_periods=n).max()
    denom = (high_n - low_n).replace(0, np.nan)
    rsv = ((close - low_n) / denom) * 100.0
    rsv = rsv.replace([np.inf, -np.inf], np.nan).fillna(50.0)

    k_vals, d_vals = [], []
    k_prev, d_prev = 50.0, 50.0
    for val in rsv:
        v = _safe_float(val, 50.0)
        k_curr = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * v
        d_curr = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_curr
        k_vals.append(k_curr)
        d_vals.append(d_curr)
        k_prev, d_prev = k_curr, d_curr
    return pd.Series(k_vals, index=df.index), pd.Series(d_vals, index=df.index)


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    close = _clean_series(df["close"])
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.replace([np.inf, -np.inf], np.nan).fillna(50.0)


def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series]:
    close = _clean_series(df["close"]).ffill().bfill().fillna(0.0)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def evaluate_technical_10(stock_df: pd.DataFrame, index_df: pd.DataFrame) -> tuple[list[dict], int]:
    s = stock_df.copy().reset_index(drop=True)
    closes = _clean_series(s["close"]).ffill().bfill().fillna(0.0)

    ma5 = closes.rolling(5).mean()
    ma10 = closes.rolling(10).mean()
    ma20 = closes.rolling(20).mean()
    ma60 = closes.rolling(60).mean()

    k, d = calc_kd(s, 9)
    rsi14 = calc_rsi(s, 14)
    dif, dea = calc_macd(s, 12, 26, 9)

    c0 = _safe_float(closes.iloc[-1])
    c1 = _safe_float(closes.iloc[-2])
    c2 = _safe_float(closes.iloc[-3])
    c3 = _safe_float(closes.iloc[-4])

    up_3d = bool(c0 > c1 > c2 > c3)
    ret_3d_pct = ((c0 - c3) / c3) * 100.0 if c3 != 0 else 0.0
    ret_3d_over_5 = bool(ret_3d_pct > 5.0)

    beat_3d = False
    if index_df is not None and not index_df.empty and "date" in s.columns and "date" in index_df.columns:
        idx_clean = index_df.copy()
        idx_clean["close"] = _clean_series(idx_clean["close"])
        merged = pd.merge(
            s[["date", "close"]].assign(close=closes).rename(columns={"close": "s_close"}),
            idx_clean[["date", "close"]].rename(columns={"close": "m_close"}),
            on="date",
            how="inner",
        )
        if len(merged) >= 4:
            merged["s_ret"] = merged["s_close"].pct_change()
            merged["m_ret"] = merged["m_close"].pct_change()
            last3 = merged.tail(3)
            beat_3d = bool((last3["s_ret"] > last3["m_ret"]).all())

    k0, d0 = _safe_float(k.iloc[-1], 50.0), _safe_float(d.iloc[-1], 50.0)
    k1, d1 = _safe_float(k.iloc[-2], 50.0), _safe_float(d.iloc[-2], 50.0)
    kd_cross = bool(k0 > d0 and k1 <= d1)

    r0 = _safe_float(rsi14.iloc[-1], 50.0)
    r1 = _safe_float(rsi14.iloc[-2], 50.0)
    rsi_bull = bool(r0 > 50.0 and r0 > r1)

    dif0 = _safe_float(dif.iloc[-1])
    dea0 = _safe_float(dea.iloc[-1])
    macd_bull = bool(dif0 > dea0 and dif0 > 0.0)

    m5_0 = _safe_float(ma5.iloc[-1])
    m10_0 = _safe_float(ma10.iloc[-1])
    m20_0 = _safe_float(ma20.iloc[-1])
    m60_0 = _safe_float(ma60.iloc[-1])

    above_5ma = bool(c0 >= m5_0)
    above_20ma = bool(c0 >= m20_0)
    ma20_gt_ma60 = bool(m20_0 > m60_0)
    ma_bull_align = bool(m5_0 > m10_0 > m20_0)

    checks = [
        {"label": "收盤價連 3 日漲", "passed": up_3d, "value": "符合" if up_3d else "未連3漲"},
        {"label": "3 日漲幅大於 5%", "passed": ret_3d_over_5, "value": f"{ret_3d_pct:+.2f}%"},
        {"label": "連 3 日打敗大盤", "passed": beat_3d, "value": "符合" if beat_3d else "未連3日勝出"},
        {"label": "KD 黃金交叉", "passed": kd_cross, "value": f"K:{k0:.1f} / D:{d0:.1f}"},
        {"label": "RSI 多頭趨勢", "passed": rsi_bull, "value": f"RSI14:{r0:.1f}"},
        {"label": "MACD 多頭趨勢", "passed": macd_bull, "value": f"DIF:{dif0:.2f}"},
        {"label": "收盤價大於週線", "passed": above_5ma, "value": f"5MA:{m5_0:.2f}"},
        {"label": "收盤價大於月線", "passed": above_20ma, "value": f"20MA:{m20_0:.2f}"},
        {"label": "月線大於季線", "passed": ma20_gt_ma60, "value": f"20MA/60MA:{m20_0:.1f}/{m60_0:.1f}"},
        {"label": "均線多頭排列 (5>10>20)", "passed": ma_bull_align, "value": "多頭排列" if ma_bull_align else "非多頭排列"},
    ]
    score = sum(1 for item in checks if item["passed"])
    return checks, score


def _fallback_checks(labels: list[str], reason: str) -> list[dict]:
    return [{"label": lbl, "passed": False, "value": reason} for lbl in labels]


def build_stock_detail(
    symbol: str,
    name: str,
    updated_at: str,
    stock_df: pd.DataFrame,
    index_df: pd.DataFrame,
    fundamental_data: dict | None = None,
    chip_data: dict | None = None,
) -> tuple[dict, dict]:
    critical_reasons: list[str] = []
    partial_notes: list[str] = []

    if stock_df is None or len(stock_df) < 60:
        critical_reasons.append("歷史日線不足 60 日")
        tech_checks = [{"label": f"技術指標 {i+1}", "passed": False, "value": "日線不足"} for i in range(10)]
        tech_score = 0
    else:
        tech_checks, tech_score = evaluate_technical_10(stock_df, index_df)

    if fundamental_data is None:
        partial_notes.append("基本面尚未取得")
        fund_checks = _fallback_checks(FUNDAMENTAL_LABELS, "缺漏")
        fund_score = 0
    else:
        fund_checks, fund_score = evaluate_fundamental_10(fundamental_data)
        if not fundamental_data.get("complete", False):
            partial_notes.extend(fundamental_data.get("missing_reasons", ["基本面部分欄位缺漏"]))

    if chip_data is None:
        partial_notes.append("籌碼面尚未取得")
        chip_checks = _fallback_checks(CHIP_LABELS, "缺漏")
        chip_score = 0
        metrics_6 = {
            "main_force_net": 0,
            "retail_net": 0,
            "margin_net": 0,
            "chip_concentration_5d": 0.0,
            "major_holder_pct": 0.0,
            "retail_holder_pct": 0.0,
        }
    else:
        chip_checks, chip_score = evaluate_chip_10(chip_data)
        metrics_6 = chip_data.get("metrics_6", {
            "main_force_net": 0,
            "retail_net": 0,
            "margin_net": 0,
            "chip_concentration_5d": 0.0,
            "major_holder_pct": 0.0,
            "retail_holder_pct": 0.0,
        })
        if not chip_data.get("complete", False):
            partial_notes.extend(chip_data.get("missing_reasons", ["籌碼部分欄位缺漏"]))

    no_any_ext_data = (fundamental_data is None and chip_data is None)
    excluded = len(critical_reasons) > 0 or no_any_ext_data

    all_notes = list(dict.fromkeys(critical_reasons + partial_notes))
    note_text = "；".join(all_notes)

    if stock_df is not None and len(stock_df) >= 2:
        last_c = round(_safe_float(stock_df.iloc[-1]["close"]), 2)
        prev_c = round(_safe_float(stock_df.iloc[-2]["close"]), 2)
        chg_pct = round(((last_c - prev_c) / prev_c) * 100.0, 2) if prev_c else 0.0
    elif stock_df is not None and len(stock_df) == 1:
        last_c = round(_safe_float(stock_df.iloc[-1]["close"]), 2)
        chg_pct = 0.0
    else:
        last_c = 0.0
        chg_pct = 0.0

    total_score = fund_score + tech_score + chip_score
    if excluded:
        badge = "資料未齊全（已排除）"
        trading_note = f"今日已依核心規則排除評分排行：{note_text}。請搭配大盤水位、量價結構與停損紀律綜合判斷。"
    elif total_score >= 22:
        badge = "面面領漲強勢主攻"
        trading_note = (
            f"部分欄位暫缺（{note_text}），已依現有資料計分；請結合進場位置、量價結構、大盤環境與停損紀律綜合判斷。"
            if note_text
            else "資料齊全，請結合進場位置、量價結構、大盤環境與停損紀律綜合判斷。"
        )
    elif total_score >= 18:
        badge = "多頭偏強觀察"
        trading_note = (
            f"部分欄位暫缺（{note_text}），已依現有資料計分；請結合進場位置、量價結構、大盤環境與停損紀律綜合判斷。"
            if note_text
            else "資料齊全，請結合進場位置、量價結構、大盤環境與停損紀律綜合判斷。"
        )
    else:
        badge = "中性觀望"
        trading_note = (
            f"部分欄位暫缺（{note_text}），已依現有資料計分；請結合進場位置、量價結構、大盤環境與停損紀律綜合判斷。"
            if note_text
            else "資料齊全，請結合進場位置、量價結構、大盤環境與停損紀律綜合判斷。"
        )

    partial_missing_str = note_text if not excluded else ""

    detail = {
        "symbol": symbol,
        "name": name,
        "updated_at": updated_at,
        "excluded": excluded,
        "exclude_reason": note_text if excluded else "",
        "partial_missing": partial_missing_str,
        "total_score": total_score,
        "max_score": 30,
        "badge": badge,
        "trading_note": trading_note,
        "scores": {
            "fundamental": fund_score,
            "technical": tech_score,
            "chip": chip_score,
        },
        "metrics_6": metrics_6,
        "checks": {
            "fundamental": fund_checks,
            "technical": tech_checks,
            "chip": chip_checks,
        },
    }

    summary_row = {
        "symbol": symbol,
        "name": name,
        "close": last_c,
        "change_pct": chg_pct,
        "total_score": total_score,
        "fundamental_score": fund_score,
        "technical_score": tech_score,
        "chip_score": chip_score,
        "badge": badge,
        "excluded": excluded,
        "exclude_reason": note_text if excluded else "",
        "partial_missing": partial_missing_str,
    }
    return detail, summary_row


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = _sanitize_for_json(obj)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)