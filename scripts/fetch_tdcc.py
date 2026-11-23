import datetime as dt
import io
import json
import math
from pathlib import Path
from typing import Optional
import pandas as pd
import requests


# TDCC 集保戶股權分散表常見分級代碼（1~7 為 <= 10 張；10~15 為 >= 400 張）
RETAIL_LEVELS = {1, 2, 3, 4, 5, 6, 7}
MAJOR_LEVELS = {10, 11, 12, 13, 14, 15}

TDCC_COLUMNS = [
    "資料日期",
    "證券代號",
    "持股分級",
    "人數",
    "股數",
    "占集保庫存數比例(%)",
]

# 同一次執行期間的記憶體快取，避免多檔查詢時重複下載整份 TDCC CSV
_SESSION_RAW_DF: pd.DataFrame | None = None
_SESSION_RAW_LOADED: bool = False


def _to_finite_float(val) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (ValueError, TypeError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _normalize_tdcc_date(raw_val) -> str:
    s = str(raw_val).strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        parsed_dt = pd.to_datetime(s, errors="coerce")
        if pd.notna(parsed_dt):
            return parsed_dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return s


def _cache_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "data" / "tdcc_cache.json"


def _load_cache() -> dict:
    p = _cache_path()
    if not p.exists():
        return {"updated_at": None, "stocks": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("stocks"), dict):
            return data
    except Exception:
        pass
    return {"updated_at": None, "stocks": {}}


def _save_cache(payload: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(p)


def _is_cache_fresh(cache: dict, max_age_days: int = 7) -> bool:
    updated_str = cache.get("updated_at")
    if not updated_str:
        return False
    try:
        last_dt = dt.datetime.fromisoformat(updated_str)
        return (dt.datetime.now() - last_dt).total_seconds() < max_age_days * 86400
    except Exception:
        return False


def _fetch_tdcc_raw_df(force_download: bool = False) -> pd.DataFrame:
    """
    從 TDCC 公開資料端點取得集保股權分散表資料。
    同一次執行內自動複用已下載的 DataFrame，避免重複請求。
    """
    global _SESSION_RAW_DF, _SESSION_RAW_LOADED
    if _SESSION_RAW_LOADED and not force_download:
        return _SESSION_RAW_DF if _SESSION_RAW_DF is not None else pd.DataFrame()

    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if not resp.ok or not resp.text.strip():
            _SESSION_RAW_DF = pd.DataFrame()
            _SESSION_RAW_LOADED = True
            return pd.DataFrame()

        text = resp.text.lstrip("\ufeff").strip()
        if text.startswith("[") or text.startswith("{"):
            data = json.loads(text)
            df = pd.DataFrame(data) if isinstance(data, list) and data else pd.DataFrame()
            _SESSION_RAW_DF = df
            _SESSION_RAW_LOADED = True
            return df

        df = pd.read_csv(io.StringIO(text), header=None, dtype=str)
        if df is None or df.empty:
            _SESSION_RAW_DF = pd.DataFrame()
            _SESSION_RAW_LOADED = True
            return pd.DataFrame()

        c0 = str(df.iloc[0, 0]).strip()
        c1 = str(df.iloc[0, 1]).strip() if df.shape[1] > 1 else ""
        if "日期" in c0 or "date" in c0.lower() or "代號" in c1 or "code" in c1.lower():
            df = df.iloc[1:, :6].copy()
        else:
            df = df.iloc[:, :6].copy()

        df.columns = TDCC_COLUMNS[: df.shape[1]]
        df = df.reset_index(drop=True)
        _SESSION_RAW_DF = df
        _SESSION_RAW_LOADED = True
        return df
    except Exception:
        _SESSION_RAW_DF = pd.DataFrame()
        _SESSION_RAW_LOADED = True
        return pd.DataFrame()


def _normalize_code(val: str) -> str:
    s = str(val).strip()
    stripped = s.lstrip("0")
    return stripped if stripped else "0"


def _parse_stock_holding(df: pd.DataFrame, stock_id: str) -> dict | None:
    """
    從集保分散表整理指定個股最新大戶(>=400張)與散戶(<=10張)比例及週增減。
    """
    if df is None or df.empty:
        return None

    col_map = {str(c).strip(): c for c in df.columns}
    date_col = next((col_map[k] for k in ["資料日期", "Date", "日期"] if k in col_map), None)
    code_col = next((col_map[k] for k in ["證券代號", "StockCode", "代號"] if k in col_map), None)
    level_col = next((col_map[k] for k in ["持股分級", "HoldingSharesLevel", "分級"] if k in col_map), None)
    pct_col = next(
        (col_map[k] for k in ["占集保庫存數比例(%)", "占集保庫存數比例", "Percent", "比例"] if k in col_map),
        None,
    )

    if not all([date_col, code_col, level_col, pct_col]):
        return None

    target_norm = _normalize_code(stock_id)
    target_raw = str(stock_id).strip()

    code_series = df[code_col].astype(str).str.strip()
    norm_series = code_series.map(_normalize_code)
    sub = df[(code_series == target_raw) | (norm_series == target_norm)].copy()
    if sub.empty:
        return None

    sub["_date"] = sub[date_col].map(_normalize_tdcc_date)
    sub["_level"] = pd.to_numeric(sub[level_col], errors="coerce")
    sub["_pct"] = pd.to_numeric(
        sub[pct_col].astype(str).str.replace("%", "", regex=False).str.strip(),
        errors="coerce",
    )
    sub = sub[sub["_date"] != ""].dropna(subset=["_level", "_pct"])

    dates = sorted(sub["_date"].unique())
    if not dates:
        return None

    latest_date = dates[-1]
    prev_date = dates[-2] if len(dates) >= 2 else None

    def _calc_for_date(d_str: str) -> tuple[float, float]:
        d_df = sub[sub["_date"] == d_str]
        major_raw = _to_finite_float(d_df[d_df["_level"].isin(MAJOR_LEVELS)]["_pct"].sum())
        retail_raw = _to_finite_float(d_df[d_df["_level"].isin(RETAIL_LEVELS)]["_pct"].sum())
        return round(major_raw if major_raw is not None else 0.0, 2), round(
            retail_raw if retail_raw is not None else 0.0, 2
        )

    major_now, retail_now = _calc_for_date(latest_date)
    if prev_date:
        major_prev, retail_prev = _calc_for_date(prev_date)
        major_chg = round(major_now - major_prev, 2)
        retail_chg = round(retail_now - retail_prev, 2)
    else:
        major_chg = None
        retail_chg = None

    return {
        "date": latest_date,
        "prev_date": prev_date,
        "major_ratio": major_now,
        "retail_ratio": retail_now,
        "major_change_1w": major_chg,
        "retail_change_1w": retail_chg,
    }


def _update_stock_entry_with_history(prev_entry: dict | None, parsed: dict) -> dict:
    """合併歷史週次紀錄，若有多週歷史則自動補算週增減，並保留既有有效週變動。"""
    hist_map: dict[str, dict] = {}
    if isinstance(prev_entry, dict) and isinstance(prev_entry.get("history"), list):
        for item in prev_entry["history"]:
            if isinstance(item, dict) and item.get("date"):
                d_norm = _normalize_tdcc_date(item["date"])
                if not d_norm:
                    continue
                maj_h = _to_finite_float(item.get("major_ratio"))
                ret_h = _to_finite_float(item.get("retail_ratio"))
                hist_map[d_norm] = {
                    "date": d_norm,
                    "major_ratio": round(maj_h, 2) if maj_h is not None else None,
                    "retail_ratio": round(ret_h, 2) if ret_h is not None else None,
                }

    curr_d = _normalize_tdcc_date(parsed["date"])
    maj_curr = _to_finite_float(parsed.get("major_ratio"))
    ret_curr = _to_finite_float(parsed.get("retail_ratio"))
    hist_map[curr_d] = {
        "date": curr_d,
        "major_ratio": round(maj_curr, 2) if maj_curr is not None else None,
        "retail_ratio": round(ret_curr, 2) if ret_curr is not None else None,
    }

    sorted_dates = sorted(hist_map.keys())
    history_list = [hist_map[d] for d in sorted_dates[-12:]]

    out = dict(parsed)
    out["date"] = curr_d
    out["major_ratio"] = round(maj_curr, 2) if maj_curr is not None else 0.0
    out["retail_ratio"] = round(ret_curr, 2) if ret_curr is not None else 0.0
    out["history"] = history_list

    if out.get("major_change_1w") is None and len(sorted_dates) >= 2:
        p_date = sorted_dates[-2]
        p_rec = hist_map[p_date]
        p_maj = _to_finite_float(p_rec.get("major_ratio"))
        p_ret = _to_finite_float(p_rec.get("retail_ratio"))
        if p_maj is not None and p_ret is not None and maj_curr is not None and ret_curr is not None:
            out["prev_date"] = p_date
            out["major_change_1w"] = round(maj_curr - p_maj, 2)
            out["retail_change_1w"] = round(ret_curr - p_ret, 2)

    if isinstance(prev_entry, dict):
        prev_maj_chg = _to_finite_float(prev_entry.get("major_change_1w"))
        prev_ret_chg = _to_finite_float(prev_entry.get("retail_change_1w"))
        if out.get("major_change_1w") is None and prev_maj_chg is not None:
            out["major_change_1w"] = round(prev_maj_chg, 2)
        if out.get("retail_change_1w") is None and prev_ret_chg is not None:
            out["retail_change_1w"] = round(prev_ret_chg, 2)

    return out


def fetch_tdcc_holding_shares(stock_id: str, force_refresh: bool = False) -> dict | None:
    """
    取得指定台股代號的集保大戶/散戶持股資料（每週更新一次快取）。
    抓不到或無資料時回傳 None，由既有缺漏排除規則處理。
    """
    sid = str(stock_id).strip()
    cache = _load_cache()
    stocks_cache = cache.setdefault("stocks", {})

    if not force_refresh and _is_cache_fresh(cache, max_age_days=7):
        cached = stocks_cache.get(sid)
        if cached is not None:
            return cached

    raw_df = _fetch_tdcc_raw_df(force_download=force_refresh)
    if raw_df.empty:
        return stocks_cache.get(sid)

    parsed = _parse_stock_holding(raw_df, sid)
    if parsed is None:
        return stocks_cache.get(sid)

    merged = _update_stock_entry_with_history(stocks_cache.get(sid), parsed)
    stocks_cache[sid] = merged
    cache["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _save_cache(cache)
    return merged


if __name__ == "__main__":
    for sid in ["2330", "2317", "2454", "0050", "00878"]:
        res = fetch_tdcc_holding_shares(sid, force_refresh=True)
        print(sid, "->", res)