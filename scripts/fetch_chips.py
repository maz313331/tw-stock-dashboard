import datetime as dt
import math
import os
import re
import time
from typing import Optional
import pandas as pd
import requests

try:
    from fetch_tdcc import fetch_tdcc_holding_shares
except Exception:
    try:
        from scripts.fetch_tdcc import fetch_tdcc_holding_shares
    except Exception:
        fetch_tdcc_holding_shares = None

CHIP_LABELS = [
    "近 3 日主力單日買超 > 1,000 張",
    "近 1 日、5 日籌碼集中度為正",
    "外資、投信同時連買 2 天",
    "近 2 日中長線買盤連續買超",
    "近 20 日短線強勢買盤曾連買",
    "近 20 日波段買盤曾連買",
    "近 1 日法人或主力買超力道放大",
    "近 20 日前十大買盤淨額為正",
    "近 20 日前十大買盤曾連日買超",
    "近 1 週大戶加碼且散戶減碼",
]


def _to_finite_float(val) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, pd.Series):
        if val.empty:
            return None
        val = val.iloc[-1]
    try:
        num = float(val)
    except (ValueError, TypeError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _clean_float_series(s: pd.Series) -> pd.Series:
    if s is None or s.empty:
        return pd.Series(dtype=float)
    clean = pd.to_numeric(s, errors="coerce").replace([float("inf"), float("-inf")], float("nan"))
    return clean.dropna()


def _finmind_get(
    dataset: str,
    stock_id: str,
    start_date: str,
    retries: int = 3,
    quota_guard=None,
) -> pd.DataFrame:
    if quota_guard is not None:
        allowed, _ = quota_guard.can_call()
        if not allowed:
            return pd.DataFrame()

    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
    }
    token = os.getenv("FINMIND_TOKEN", "").strip()
    if token:
        params["token"] = token

    if quota_guard is not None and hasattr(quota_guard, "execute_with_guard"):
        def _do_req():
            resp = requests.get(url, params=params, timeout=12)
            try:
                payload = resp.json()
            except Exception:
                payload = None
            return resp.status_code, payload

        ok, status_code, payload, _ = quota_guard.execute_with_guard(_do_req)
        if not ok or not isinstance(payload, dict):
            return pd.DataFrame()
        rows = payload.get("data", [])
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=12)
            if resp.status_code == 429:
                wait_sec = 1.5 * (attempt + 1)
                if attempt < retries:
                    print(f"[限流] {stock_id} {dataset} 觸發 429，等待 {wait_sec:.1f}s 後重試 ({attempt + 1}/{retries})")
                    time.sleep(wait_sec)
                    continue
                print(f"[警告] {stock_id} {dataset} 多次觸發 429 限流，略過本次抓取")
                return pd.DataFrame()
            if resp.status_code >= 500:
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return pd.DataFrame()
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if rows:
                return pd.DataFrame(rows)
            return pd.DataFrame()
        except Exception as exc:
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
            else:
                print(f"[警告] {stock_id} {dataset} 請求異常: {exc}")
                return pd.DataFrame()
    return pd.DataFrame()


def _max_consecutive_true(bool_list: list[bool]) -> int:
    best = 0
    curr = 0
    for b in bool_list:
        if b:
            curr += 1
            best = max(best, curr)
        else:
            curr = 0
    return best


def _extract_min_shares(level_str: str) -> int | None:
    """從 HoldingSharesLevel 抽出最小股數（例如 '400,001-600,000' -> 400001）"""
    s = str(level_str).replace(",", "").strip()
    if "more than" in s.lower() or "以上" in s or "over" in s.lower():
        nums = re.findall(r"\d+", s)
        return int(nums[0]) if nums else 1_000_001
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else None


def fetch_chip_data(
    stock_id: str,
    stock_df: pd.DataFrame | None = None,
    quota_guard=None,
) -> dict:
    today = dt.date.today()
    chip_start = (today - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    hold_start = (today - dt.timedelta(days=75)).strftime("%Y-%m-%d")

    missing = []

    foreign_daily = pd.Series(dtype=float)
    trust_daily = pd.Series(dtype=float)
    inst_total_daily = pd.Series(dtype=float)
    margin_net_last = None

    major_holder_pct = None
    retail_holder_pct = None
    major_increased_1w = None
    retail_decreased_1w = None

    # 1. 三大法人買賣超（單位：股 -> 轉張 / 1000）
    try:
        df_inst = _finmind_get(
            "TaiwanStockInstitutionalInvestorsBuySell",
            stock_id,
            chip_start,
            quota_guard=quota_guard,
        )
        if not df_inst.empty and {"date", "name", "buy", "sell"}.issubset(df_inst.columns):
            df_inst["net_lots"] = (
                pd.to_numeric(df_inst["buy"], errors="coerce").fillna(0.0)
                - pd.to_numeric(df_inst["sell"], errors="coerce").fillna(0.0)
            ) / 1000.0
            piv = df_inst.pivot_table(
                index="date", columns="name", values="net_lots", aggfunc="sum"
            ).fillna(0.0).sort_index()

            if not piv.empty:
                inst_total_daily = _clean_float_series(piv.sum(axis=1))
                for col in ["Foreign_Investor", "Foreign_Dealer_Self"]:
                    if col in piv.columns:
                        col_s = _clean_float_series(piv[col])
                        foreign_daily = (
                            foreign_daily.add(col_s, fill_value=0.0)
                            if not foreign_daily.empty
                            else col_s
                        )
                if "Investment_Trust" in piv.columns:
                    trust_daily = _clean_float_series(piv["Investment_Trust"])
    except Exception:
        pass

    if inst_total_daily.empty or len(inst_total_daily) < 5:
        missing.append("三大法人買賣超缺漏或天數不足")

    # 2. 融資融券
    try:
        df_margin = _finmind_get(
            "TaiwanStockMarginPurchaseShortSale",
            stock_id,
            chip_start,
            quota_guard=quota_guard,
        )
        if not df_margin.empty and {"date", "MarginPurchaseBuy", "MarginPurchaseSell"}.issubset(df_margin.columns):
            df_margin = df_margin.sort_values("date")
            last_m = df_margin.iloc[-1]
            m_buy_raw = _to_finite_float(last_m.get("MarginPurchaseBuy"))
            m_sell_raw = _to_finite_float(last_m.get("MarginPurchaseSell"))
            if m_buy_raw is not None or m_sell_raw is not None:
                margin_net_last = round((m_buy_raw or 0.0) - (m_sell_raw or 0.0), 1)
    except Exception:
        pass

    if margin_net_last is None:
        missing.append("融資增減資料缺漏")

    # 3. 集保戶股權分散表（優先使用 TDCC 每週快取；若無資料則退回 FinMind）
    if fetch_tdcc_holding_shares is not None:
        try:
            tdcc_res = fetch_tdcc_holding_shares(stock_id, force_refresh=False)
            if isinstance(tdcc_res, dict):
                maj_val = _to_finite_float(tdcc_res.get("major_ratio"))
                ret_val = _to_finite_float(tdcc_res.get("retail_ratio"))
                maj_chg = _to_finite_float(tdcc_res.get("major_change_1w"))
                ret_chg = _to_finite_float(tdcc_res.get("retail_change_1w"))
                if maj_val is not None:
                    major_holder_pct = round(maj_val, 2)
                if ret_val is not None:
                    retail_holder_pct = round(ret_val, 2)
                if maj_chg is not None:
                    major_increased_1w = bool(maj_chg > 0)
                if ret_chg is not None:
                    retail_decreased_1w = bool(ret_chg < 0)
        except Exception:
            pass

    if major_holder_pct is None or retail_holder_pct is None:
        try:
            df_hold = _finmind_get(
                "TaiwanStockHoldingSharesPer",
                stock_id,
                hold_start,
                quota_guard=quota_guard,
            )
            if not df_hold.empty and {"date", "HoldingSharesLevel", "percent"}.issubset(df_hold.columns):
                df_hold["pct"] = pd.to_numeric(df_hold["percent"], errors="coerce")
                df_hold["min_shares"] = df_hold["HoldingSharesLevel"].apply(_extract_min_shares)
                df_hold = df_hold.dropna(subset=["pct", "min_shares"])

                dates_sorted = sorted(df_hold["date"].unique())
                if len(dates_sorted) >= 2:
                    d_curr, d_prev = dates_sorted[-1], dates_sorted[-2]
                    curr_sub = df_hold[df_hold["date"] == d_curr]
                    prev_sub = df_hold[df_hold["date"] == d_prev]

                    major_curr = _to_finite_float(curr_sub[curr_sub["min_shares"] >= 400_001]["pct"].sum()) or 0.0
                    major_prev = _to_finite_float(prev_sub[prev_sub["min_shares"] >= 400_001]["pct"].sum()) or 0.0
                    retail_curr = _to_finite_float(curr_sub[curr_sub["min_shares"] <= 10_000]["pct"].sum()) or 0.0
                    retail_prev = _to_finite_float(prev_sub[prev_sub["min_shares"] <= 10_000]["pct"].sum()) or 0.0

                    if major_holder_pct is None:
                        major_holder_pct = round(major_curr, 2)
                    if retail_holder_pct is None:
                        retail_holder_pct = round(retail_curr, 2)
                    if major_increased_1w is None:
                        major_increased_1w = bool(major_curr > major_prev)
                    if retail_decreased_1w is None:
                        retail_decreased_1w = bool(retail_curr < retail_prev)
        except Exception:
            pass

    if major_holder_pct is None or retail_holder_pct is None:
        missing.append("集保大戶／散戶資料缺漏或尚未更新")

    inst_5d_lots = _to_finite_float(inst_total_daily.tail(5).sum()) if len(inst_total_daily) >= 5 else 0.0
    inst_5d_lots = inst_5d_lots or 0.0
    conc_5d_pct = 0.0
    if stock_df is not None and not stock_df.empty and "volume" in stock_df.columns and len(stock_df) >= 5:
        vol_5d_raw = _to_finite_float(pd.to_numeric(stock_df["volume"], errors="coerce").tail(5).sum()) or 0.0
        vol_5d_lots = (vol_5d_raw / 1000.0) if vol_5d_raw > 50_000 else vol_5d_raw
        if vol_5d_lots > 0:
            conc_5d_pct = round((inst_5d_lots / vol_5d_lots) * 100.0, 2)

    main_force_last = round(_to_finite_float(inst_total_daily.iloc[-1]) or 0.0, 1) if not inst_total_daily.empty else 0.0

    metrics_6 = {
        "main_force_net": main_force_last,
        "retail_net": 0,
        "margin_net": margin_net_last if margin_net_last is not None else 0.0,
        "chip_concentration_5d": conc_5d_pct,
        "major_holder_pct": major_holder_pct if major_holder_pct is not None else 0.0,
        "retail_holder_pct": retail_holder_pct if retail_holder_pct is not None else 0.0,
    }

    return {
        "complete": len(missing) == 0,
        "missing_reasons": missing,
        "metrics_6": metrics_6,
        "series": {
            "inst_total_daily": inst_total_daily,
            "foreign_daily": foreign_daily,
            "trust_daily": trust_daily,
            "inst_5d_lots": inst_5d_lots,
            "conc_5d_pct": conc_5d_pct,
            "major_increased_1w": major_increased_1w,
            "retail_decreased_1w": retail_decreased_1w,
        },
    }


def evaluate_chip_10(chip: dict) -> tuple[list[dict], int]:
    s = chip.get("series", {})
    tot = _clean_float_series(s.get("inst_total_daily", pd.Series(dtype=float)))
    f_s = _clean_float_series(s.get("foreign_daily", pd.Series(dtype=float)))
    t_s = _clean_float_series(s.get("trust_daily", pd.Series(dtype=float)))
    inst_5d = _to_finite_float(s.get("inst_5d_lots", 0.0)) or 0.0
    conc_5d = _to_finite_float(s.get("conc_5d_pct", 0.0)) or 0.0
    maj_inc = s.get("major_increased_1w")
    ret_dec = s.get("retail_decreased_1w")

    def _chk(label: str, cond: bool | None, val_str: str) -> dict:
        return {
            "label": label,
            "passed": bool(cond) if cond is not None else False,
            "value": val_str,
        }

    c1 = bool((tot.tail(3) > 1000).any()) if len(tot) >= 3 else None
    max_3d = _to_finite_float(tot.tail(3).max()) or 0.0
    v1 = f"最大:{max_3d:+.0f}張" if len(tot) >= 3 else "缺漏"

    last_tot = _to_finite_float(tot.iloc[-1]) if not tot.empty else None
    c2 = bool((last_tot is not None and last_tot > 0) and inst_5d > 0) if len(tot) >= 5 else None
    v2 = f"5日集中度:{conc_5d:+.2f}%" if len(tot) >= 5 else "缺漏"

    if not f_s.empty and not t_s.empty:
        common = pd.DataFrame({"f": f_s, "t": t_s}).dropna().tail(2)
        c3 = bool(((common["f"] > 0) & (common["t"] > 0)).all()) if len(common) == 2 else None
        v3 = "符合" if c3 else ("缺漏" if c3 is None else "未連買2日")
    else:
        c3, v3 = None, "缺漏"

    c4 = bool((tot.tail(2) > 0).all()) if len(tot) >= 2 else None
    v4 = "連2日買超" if c4 else ("缺漏" if c4 is None else "未連2日")

    c5 = bool(_max_consecutive_true((tot.tail(20) > 0).tolist()) >= 3) if len(tot) >= 5 else None
    v5 = f"最長{_max_consecutive_true((tot.tail(20) > 0).tolist())}日" if len(tot) >= 5 else "缺漏"

    c6 = bool(_max_consecutive_true((tot.tail(20) > 0).tolist()) >= 5) if len(tot) >= 5 else None
    v6 = f"最長{_max_consecutive_true((tot.tail(20) > 0).tolist())}日" if len(tot) >= 5 else "缺漏"

    if len(tot) >= 5 and last_tot is not None:
        avg5 = _to_finite_float(tot.tail(5).mean()) or 0.0
        c7 = bool(last_tot > avg5 and last_tot > 0)
        v7 = f"{last_tot:+.0f} (5日均{avg5:+.0f})"
    else:
        c7, v7 = None, "缺漏"

    sum_20d = _to_finite_float(tot.tail(20).sum()) or 0.0
    c8 = bool(sum_20d > 0) if len(tot) >= 5 else None
    v8 = f"{sum_20d:+.0f}張" if len(tot) >= 5 else "缺漏"

    c9 = bool(_max_consecutive_true((tot.tail(20) > 0).tolist()) >= 2) if len(tot) >= 5 else None
    v9 = "符合" if c9 else ("缺漏" if c9 is None else "未達標")

    c10 = bool(maj_inc and ret_dec) if (maj_inc is not None and ret_dec is not None) else None
    v10 = "符合" if c10 else ("缺漏" if c10 is None else "未同時符合")

    checks = [
        _chk(CHIP_LABELS[0], c1, v1),
        _chk(CHIP_LABELS[1], c2, v2),
        _chk(CHIP_LABELS[2], c3, v3),
        _chk(CHIP_LABELS[3], c4, v4),
        _chk(CHIP_LABELS[4], c5, v5),
        _chk(CHIP_LABELS[5], c6, v6),
        _chk(CHIP_LABELS[6], c7, v7),
        _chk(CHIP_LABELS[7], c8, v8),
        _chk(CHIP_LABELS[8], c9, v9),
        _chk(CHIP_LABELS[9], c10, v10),
    ]
    score = sum(1 for item in checks if item["passed"])
    return checks, score