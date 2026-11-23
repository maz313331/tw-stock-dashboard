import datetime as dt
import json
import math
from pathlib import Path
import pandas as pd
from fetch_sources import fetch_tw_history_with_fallback, fetch_yf_history, get_us_index_quote


def _safe_float(val, default: float = 0.0) -> float:
    try:
        num = float(val)
    except (ValueError, TypeError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def build_ma_status(df: pd.DataFrame) -> tuple[str, dict]:
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    last_c = _safe_float(closes.iloc[-1])
    ma5 = _safe_float(closes.tail(5).mean(), last_c) if len(closes) >= 5 else last_c
    ma20 = _safe_float(closes.tail(20).mean(), last_c) if len(closes) >= 20 else last_c
    ma60 = _safe_float(closes.tail(60).mean(), last_c) if len(closes) >= 60 else last_c

    above_5 = last_c >= ma5
    above_20 = last_c >= ma20
    above_60 = last_c >= ma60

    parts = []
    if above_5:
        parts.append("5MA")
    if above_20:
        parts.append("20MA")
    if above_60:
        parts.append("60MA")

    status = f"站上 {'/'.join(parts)}" if parts else "均線下方震盪"
    return status, {"above_5": above_5, "above_20": above_20, "above_60": above_60}


def build_regime(ma_flags: dict, chg_pct: float) -> dict:
    score = int(ma_flags["above_5"]) + int(ma_flags["above_20"]) + int(ma_flags["above_60"])
    if score == 3 and chg_pct >= -1.5:
        return {
            "signal": "多頭偏強",
            "suggested_exposure": "70% ~ 80%",
            "summary": "加權站穩短中長均線上方，高分強勢股可順勢分批布局，嚴守停損。",
        }
    if score >= 2:
        return {
            "signal": "中性偏多",
            "suggested_exposure": "50% ~ 65%",
            "summary": "大盤處於多空交錯或短線震盪，建議篩選高分標的並控制單筆追價部位。",
        }
    return {
        "signal": "保守防禦",
        "suggested_exposure": "30% ~ 45%",
        "summary": "大盤跌破關鍵均線或波動放大，宜降低持股水位、重防守與嚴設停損。",
    }


def main():
    root = Path(__file__).resolve().parent.parent
    market_path = root / "data" / "market.json"

    prev_market = {}
    if market_path.exists():
        try:
            prev_market = json.loads(market_path.read_text(encoding="utf-8"))
        except Exception:
            prev_market = {}

    twii_df = fetch_tw_history_with_fallback("^TWII", yf_tickers=["^TWII"], period="6mo", limit=120)
    if twii_df.empty or len(twii_df) < 2:
        raise RuntimeError("無法取得加權指數 (^TWII) 歷史行情")

    last_tw = twii_df.iloc[-1]
    prev_tw = twii_df.iloc[-2]
    tw_close = round(_safe_float(last_tw["close"]), 2)
    tw_prev_close = round(_safe_float(prev_tw["close"]), 2)
    tw_chg_pct = round(((tw_close - tw_prev_close) / tw_prev_close) * 100, 2) if tw_prev_close else 0.0

    ma_status, ma_flags = build_ma_status(twii_df)
    regime = build_regime(ma_flags, tw_chg_pct)

    otc_df = fetch_yf_history("^TWOII", period="6mo", limit=120)
    if not otc_df.empty and len(otc_df) >= 2:
        otc_close = round(_safe_float(otc_df.iloc[-1]["close"]), 2)
        otc_prev = round(_safe_float(otc_df.iloc[-2]["close"]), 2)
        otc_chg_pct = round(((otc_close - otc_prev) / otc_prev) * 100, 2) if otc_prev else 0.0
    else:
        prev_otc = prev_market.get("otc_index", {})
        otc_close = round(_safe_float(prev_otc.get("close", 0.0)), 2)
        otc_chg_pct = round(_safe_float(prev_otc.get("change_pct", 0.0)), 2)

    us_indices = [
        get_us_index_quote("道瓊工業", "^DJI", "^dji", "美股參考"),
        get_us_index_quote("標普 500", "^GSPC", "^spx", "美股參考"),
        get_us_index_quote("那斯達克", "^IXIC", "^ndq", "科技風向"),
        get_us_index_quote("費城半導體", "^SOX", "^sox", "2330 連動"),
    ]

    history = [
        {
            "date": str(row["date"]),
            "close": round(_safe_float(row["close"]), 2),
            "institutional_net": 0.0,
        }
        for _, row in twii_df.tail(120).iterrows()
    ]

    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {
        "updated_at": f"{now_str}（行情日 {last_tw['date']}）",
        "regime": regime,
        "tw_index": {
            "close": tw_close,
            "change_pct": tw_chg_pct,
            "ma_status": ma_status,
        },
        "otc_index": {
            "close": otc_close,
            "change_pct": otc_chg_pct,
        },
        "institutional_flow": prev_market.get(
            "institutional_flow",
            {"foreign_net": 0.0, "trust_net": 0.0, "dealer_net": 0.0},
        ),
        "market_breadth": prev_market.get(
            "market_breadth",
            {"advancing": 0, "declining": 0, "high_score_count": 0, "excluded_count": 0},
        ),
        "us_indices": us_indices,
        "history": history,
    }

    market_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = market_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(market_path)
    print(f"[完成] 已更新 {market_path} | 加權 {last_tw['date']} 收盤 {tw_close} ({tw_chg_pct:+.2f}%) | 燈號: {regime['signal']}")


if __name__ == "__main__":
    main()