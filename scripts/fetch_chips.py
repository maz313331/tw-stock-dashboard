import datetime as dt
import os
import re
import time
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


def _finmind_get(dataset: str, stock_id: str, start_date: str, retries: int = 3) -> pd.DataFrame:
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
    }
    token = os.getenv("FINMIND_TOKEN", "").strip()
    if token:
        params["token"] = token

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


def fetch_chip_data(stock_id: str, stock_df: pd.DataFrame | None = None) -> dict:
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
        df_inst = _finmind_get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, chip_start)
        if not df_inst.empty and {"date", "name", "buy", "sell"}.issubset(df_inst.columns):
            df_inst["net_lots"] = (
                pd.to_numeric(df_inst["buy"], errors="coerce").fillna(0.0)
                - pd.to_numeric(df_inst["sell"], errors="coerce").fillna(0.0)
            ) / 1000.0
            piv = df_inst.pivot_table(
                index="date", columns="name", values="net_lots", aggfunc="sum"
            ).fillna(0.0).sort_index()

            if not piv.empty:
                inst_total_daily = piv.sum(axis=1)
                for col in ["Foreign_Investor", "Foreign_Dealer_Self"]:
                    if col in piv.columns:
                        foreign_daily = (
                            foreign_daily.add(piv[col], fill_value=0.0)
                            if not foreign_daily.empty
                            else piv[col]
                        )
                if "Investment_Trust" in piv.columns:
                    trust_daily = piv["Investment_Trust"]
    except Exception:
        pass

    if inst_total_daily.empty or len(inst_total_daily) < 5:
        missing.append("三大法人買賣超缺漏或天數不足")

    # 2. 融資融券
    try:
        df_margin = _finmind_get("TaiwanStockMarginPurchaseShortSale", stock_id, chip_start)
        if not df_margin.empty and {"date", "MarginPurchaseBuy", "MarginPurchaseSell"}.issubset(df_margin.columns):
            df_margin = df_margin.sort_values("date")
            last_m = df_margin.iloc[-1]
            m_buy = float(pd.to_numeric(last_m.get("MarginPurchaseBuy"), errors="coerce") or 0.0)
            m_sell = float(pd.to_numeric(last_m.get("MarginPurchaseSell"), errors="coerce") or 0.0)
            margin_net_last = round(m_buy - m_sell, 1)
    except Exception:
        pass

    if margin_net_last is None:
        missing.append("融資增減資料缺漏")

    # 3. 集保戶股權分散表（優先使用 TDCC 每週快取；若無資料則退回 FinMind）
    if fetch_tdcc_holding_shares is not None:
        try:
            tdcc_res = fetch_tdcc_holding_shares(stock_id, force_refresh=False)
            if isinstance(tdcc_res, dict):
                maj_val = tdcc_res.get("major_ratio")
                ret_val = tdcc_res.get("retail_ratio")
                maj_chg = tdcc_res.get("major_change_1w")
                ret_chg = tdcc_res.get("retail_change_1w")
                if maj_val is not None or ret_val is not None:
                    major_holder_pct = round(float(maj_val or 0.0), 2)
                    retail_holder_pct = round(float(ret_val or 0.0), 2)
                    if maj_chg is not None:
                        major_increased_1w = bool(float(maj_chg) > 0)
                    if ret_chg is not None:
                        retail_decreased_1w = bool(float(ret_chg) < 0)
        except Exception:
            pass

    if major_holder_pct is None and retail_holder_pct is None:
        try:
            df_hold = _finmind_get("TaiwanStockHoldingSharesPer", stock_id, hold_start)
            if not df_hold.empty and {"date", "HoldingSharesLevel", "percent"}.issubset(df_hold.columns):
                df_hold["pct"] = pd.to_numeric(df_hold["percent"], errors="coerce")
                df_hold["min_shares"] = df_hold["HoldingSharesLevel"].apply(_extract_min_shares)
                df_hold = df_hold.dropna(subset=["pct", "min_shares"])

                dates_sorted = sorted(df_hold["date"].unique())
                if len(dates_sorted) >= 2:
                    d_curr, d_prev = dates_sorted[-1], dates_sorted[-2]
                    curr_sub = df_hold[df_hold["date"] == d_curr]
                    prev_sub = df_hold[df_hold["date"] == d_prev]

                    major_curr = float(curr_sub[curr_sub["min_shares"] >= 400_001]["pct"].sum())
                    major_prev = float(prev_sub[prev_sub["min_shares"] >= 400_001]["pct"].sum())
                    retail_curr = float(curr_sub[curr_sub["min_shares"] <= 10_000]["pct"].sum())
                    retail_prev = float(prev_sub[prev_sub["min_shares"] <= 10_000]["pct"].sum())

                    if major_curr > 0 or retail_curr > 0:
                        major_holder_pct = round(major_curr, 2)
                        retail_holder_pct = round(retail_curr, 2)
                        major_increased_1w = bool(major_curr > major_prev)
                        retail_decreased_1w = bool(retail_curr < retail_prev)
        except Exception:
            pass

    if major_holder_pct is None or retail_holder_pct is None:
        missing.append("集保大戶／散戶資料缺漏或尚未更新")

    # 計算 5 日籌碼集中度百分比（近 5 日法人淨買超張數 / 近 5 日成交量張數 * 100）
    inst_5d_lots = float(inst_total_daily.tail(5).sum()) if len(inst_total_daily) >= 5 else 0.0
    conc_5d_pct = 0.0
    if stock_df is not None and not stock_df.empty and "volume" in stock_df.columns and len(stock_df) >= 5:
        vol_5d_raw = float(pd.to_numeric(stock_df["volume"], errors="coerce").tail(5).sum())
        vol_5d_lots = (vol_5d_raw / 1000.0) if vol_5d_raw > 50_000 else vol_5d_raw
        if vol_5d_lots > 0:
            conc_5d_pct = round((inst_5d_lots / vol_5d_lots) * 100.0, 2)

    main_force_last = round(float(inst_total_daily.iloc[-1]), 1) if not inst_total_daily.empty else 0

    metrics_6 = {
        "main_force_net": main_force_last,
        "retail_net": 0,
        "margin_net": margin_net_last if margin_net_last is not None else 0,
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
    tot = s.get("inst_total_daily", pd.Series(dtype=float))
    f_s = s.get("foreign_daily", pd.Series(dtype=float))
    t_s = s.get("trust_daily", pd.Series(dtype=float))
    inst_5d = float(s.get("inst_5d_lots", 0.0))
    conc_5d = float(s.get("conc_5d_pct", 0.0))
    maj_inc = s.get("major_increased_1w")
    ret_dec = s.get("retail_decreased_1w")

    def _chk(label: str, cond: bool | None, val_str: str) -> dict:
        return {
            "label": label,
            "passed": bool(cond) if cond is not None else False,
            "value": val_str,
        }

    c1 = bool((tot.tail(3) > 1000).any()) if len(tot) >= 3 else None
    v1 = f"最大:{tot.tail(3).max():+.0f}張" if len(tot) >= 3 else "缺漏"

    c2 = bool(float(tot.iloc[-1]) > 0 and inst_5d > 0) if len(tot) >= 5 else None
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

    if len(tot) >= 5:
        avg5 = float(tot.tail(5).mean())
        c7 = bool(float(tot.iloc[-1]) > avg5 and float(tot.iloc[-1]) > 0)
        v7 = f"{tot.iloc[-1]:+.0f} (5日均{avg5:+.0f})"
    else:
        c7, v7 = None, "缺漏"

    c8 = bool(float(tot.tail(20).sum()) > 0) if len(tot) >= 5 else None
    v8 = f"{tot.tail(20).sum():+.0f}張" if len(tot) >= 5 else "缺漏"

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