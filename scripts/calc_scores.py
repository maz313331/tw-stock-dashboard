# scripts/calc_scores.py
import json
from pathlib import Path
import numpy as np
import pandas as pd
from fetch_chips import CHIP_LABELS, evaluate_chip_10
from fetch_fundamental import FUNDAMENTAL_LABELS, evaluate_fundamental_10


def calc_kd(df: pd.DataFrame, n: int = 9) -> tuple[pd.Series, pd.Series]:
    low_n = df["low"].rolling(n, min_periods=n).min()
    high_n = df["high"].rolling(n, min_periods=n).max()
    denom = (high_n - low_n).replace(0, np.nan)
    rsv = ((df["close"] - low_n) / denom) * 100.0
    rsv = rsv.fillna(50.0)

    k_vals, d_vals = [], []
    k_prev, d_prev = 50.0, 50.0
    for val in rsv:
        k_curr = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * float(val)
        d_curr = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_curr
        k_vals.append(k_curr)
        d_vals.append(d_curr)
        k_prev, d_prev = k_curr, d_curr
    return pd.Series(k_vals, index=df.index), pd.Series(d_vals, index=df.index)


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series]:
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def evaluate_technical_10(stock_df: pd.DataFrame, index_df: pd.DataFrame) -> tuple[list[dict], int]:
    s = stock_df.copy().reset_index(drop=True)
    closes = s["close"].astype(float)

    ma5 = closes.rolling(5).mean()
    ma10 = closes.rolling(10).mean()
    ma20 = closes.rolling(20).mean()
    ma60 = closes.rolling(60).mean()

    k, d = calc_kd(s, 9)
    rsi14 = calc_rsi(s, 14)
    dif, dea = calc_macd(s, 12, 26, 9)

    c0, c1, c2, c3 = float(closes.iloc[-1]), float(closes.iloc[-2]), float(closes.iloc[-3]), float(closes.iloc[-4])
    up_3d = bool(c0 > c1 > c2 > c3)
    ret_3d_pct = ((c0 - c3) / c3) * 100.0 if c3 else 0.0
    ret_3d_over_5 = bool(ret_3d_pct > 5.0)

    beat_3d = False
    if index_df is not None and not index_df.empty:
        merged = pd.merge(
            s[["date", "close"]].rename(columns={"close": "s_close"}),
            index_df[["date", "close"]].rename(columns={"close": "m_close"}),
            on="date",
            how="inner",
        )
        if len(merged) >= 4:
            merged["s_ret"] = merged["s_close"].pct_change()
            merged["m_ret"] = merged["m_close"].pct_change()
            last3 = merged.tail(3)
            beat_3d = bool((last3["s_ret"] > last3["m_ret"]).all())

    kd_cross = bool(k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2])
    rsi_bull = bool(rsi14.iloc[-1] > 50.0 and rsi14.iloc[-1] > rsi14.iloc[-2])
    macd_bull = bool(dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-1] > 0.0)

    above_5ma = bool(c0 >= float(ma5.iloc[-1]))
    above_20ma = bool(c0 >= float(ma20.iloc[-1]))
    ma20_gt_ma60 = bool(float(ma20.iloc[-1]) > float(ma60.iloc[-1]))
    ma_bull_align = bool(float(ma5.iloc[-1]) > float(ma10.iloc[-1]) > float(ma20.iloc[-1]))

    checks = [
        {"label": "收盤價連 3 日漲", "passed": up_3d, "value": "符合" if up_3d else "未連3漲"},
        {"label": "3 日漲幅大於 5%", "passed": ret_3d_over_5, "value": f"{ret_3d_pct:+.2f}%"},
        {"label": "連 3 日打敗大盤", "passed": beat_3d, "value": "符合" if beat_3d else "未連3日勝出"},
        {"label": "KD 黃金交叉", "passed": kd_cross, "value": f"K:{k.iloc[-1]:.1f} / D:{d.iloc[-1]:.1f}"},
        {"label": "RSI 多頭趨勢", "passed": rsi_bull, "value": f"RSI14:{rsi14.iloc[-1]:.1f}"},
        {"label": "MACD 多頭趨勢", "passed": macd_bull, "value": f"DIF:{dif.iloc[-1]:.2f}"},
        {"label": "收盤價大於週線", "passed": above_5ma, "value": f"5MA:{ma5.iloc[-1]:.2f}"},
        {"label": "收盤價大於月線", "passed": above_20ma, "value": f"20MA:{ma20.iloc[-1]:.2f}"},
        {"label": "月線大於季線", "passed": ma20_gt_ma60, "value": f"20MA/60MA:{ma20.iloc[-1]:.1f}/{ma60.iloc[-1]:.1f}"},
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

    # 僅在核心日線不足，或基本面＋籌碼完全無任何資料時才排除
    no_any_ext_data = (fundamental_data is None and chip_data is None)
    excluded = len(critical_reasons) > 0 or no_any_ext_data

    all_notes = list(dict.fromkeys(critical_reasons + partial_notes))
    note_text = "；".join(all_notes)

    last_c = round(float(stock_df.iloc[-1]["close"]), 2)
    prev_c = round(float(stock_df.iloc[-2]["close"]), 2)
    chg_pct = round(((last_c - prev_c) / prev_c) * 100.0, 2) if prev_c else 0.0

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

    detail = {
        "symbol": symbol,
        "name": name,
        "updated_at": updated_at,
        "excluded": excluded,
        "exclude_reason": note_text if excluded else "",
        "partial_missing": note_text if not excluded else "",
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
    }
    return detail, summary_row


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")