import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Optional
import pandas as pd
import requests

_TPEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}

# 同一次執行期間的記憶體快取，避免多檔查詢時重複請求 TPEx
_SESSION_VALUATION_MAP: dict[str, dict[str, Optional[float]]] | None = None
_SESSION_INST_MAP: dict[str, dict[str, Any]] | None = None
_SESSION_MARGIN_MAP: dict[str, dict[str, Any]] | None = None


def reset_tpex_session_cache() -> None:
    """重置單次執行期間的 TPEx 記憶體快取"""
    global _SESSION_VALUATION_MAP, _SESSION_INST_MAP, _SESSION_MARGIN_MAP
    _SESSION_VALUATION_MAP = None
    _SESSION_INST_MAP = None
    _SESSION_MARGIN_MAP = None


def _clean_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        return None if (math.isnan(num) or math.isinf(num)) else num
    s = str(val).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s.lower() in {"-", "--", "n/a", "na", "nan", "none", "null", "無", "除息", "除權"}:
        return None
    try:
        num = float(s)
        return None if (math.isnan(num) or math.isinf(num)) else num
    except (ValueError, TypeError):
        return None


def _pick_first_value(row: dict[str, Any], candidate_keys: list[str]) -> Any:
    for k in candidate_keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    # 模糊比對（忽略大小寫與空白）
    norm_map = {str(k).strip().lower(): v for k, v in row.items()}
    for k in candidate_keys:
        lk = k.strip().lower()
        if lk in norm_map and norm_map[lk] not in (None, ""):
            return norm_map[lk]
    return None


def _cache_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    d = root / "data" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_daily_cache(name: str, max_age_hours: int = 12) -> dict[str, Any] | None:
    p = _cache_dir() / f"tpex_{name}.json"
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return None
        updated_str = payload.get("updated_at")
        if updated_str:
            last_dt = dt.datetime.fromisoformat(updated_str)
            if (dt.datetime.now() - last_dt).total_seconds() < max_age_hours * 3600:
                return payload["data"]
    except Exception:
        pass
    return None


def _save_daily_cache(name: str, data: dict[str, Any]) -> None:
    p = _cache_dir() / f"tpex_{name}.json"
    try:
        payload = {
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "count": len(data),
            "data": data,
        }
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _fetch_json_rows(urls: list[str], timeout: int = 20) -> list[dict[str, Any]]:
    """依序嘗試多個 TPEx OpenAPI 端點，回傳 list[dict]"""
    for url in urls:
        try:
            resp = requests.get(url, headers=_TPEX_HEADERS, timeout=timeout)
            if not resp.ok:
                continue
            raw = resp.json()
            if isinstance(raw, list) and raw:
                return [r for r in raw if isinstance(r, dict)]
            if isinstance(raw, dict):
                for key in ("aaData", "data", "tables", "result"):
                    val = raw.get(key)
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        return val
        except Exception:
            continue
    return []


def get_tpex_valuation_map(force_refresh: bool = False) -> dict[str, dict[str, Optional[float]]]:
    """
    取得上櫃全市場估值字典：{symbol: {"pe": ..., "pb": ..., "dividend_yield": ...}}
    """
    global _SESSION_VALUATION_MAP
    if _SESSION_VALUATION_MAP is not None and not force_refresh:
        return _SESSION_VALUATION_MAP

    if not force_refresh:
        cached = _load_daily_cache("valuation", max_age_hours=16)
        if cached:
            _SESSION_VALUATION_MAP = cached
            return cached

    urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
    ]
    rows = _fetch_json_rows(urls, timeout=20)
    result: dict[str, dict[str, Optional[float]]] = {}

    for r in rows:
        sym = str(
            _pick_first_value(r, ["SecuritiesCompanyCode", "Code", "代號", "股票代號", "證券代號"]) or ""
        ).strip()
        if not sym or not sym[0].isdigit():
            continue

        pe = _clean_float(_pick_first_value(r, ["PEratio", "PER", "本益比"]))
        pb = _clean_float(_pick_first_value(r, ["PriceBookRatio", "PBR", "股價淨值比", "淨值比"]))
        dy = _clean_float(_pick_first_value(r, ["YieldRatio", "DividendYield", "殖利率(%)", "殖利率"]))

        if any(v is not None for v in (pe, pb, dy)):
            result[sym] = {
                "pe": pe,
                "pb": pb,
                "dividend_yield": dy,
            }

    if result:
        _save_daily_cache("valuation", result)
    _SESSION_VALUATION_MAP = result
    return result


def fetch_tpex_stock_valuation(symbol: str, force_refresh: bool = False) -> dict[str, Optional[float]] | None:
    """查詢單檔上櫃股票的 PE / PB / 殖利率（查本地全市場快取，不額外發請求）"""
    sym = str(symbol).strip()
    if not sym or sym.startswith("00"):
        return None
    vmap = get_tpex_valuation_map(force_refresh=force_refresh)
    return vmap.get(sym)


def get_tpex_inst_map(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """
    取得上櫃全市場當日三大法人買賣超（單位：股，對齊 FinMind / TWSE T86 格式）
    """
    global _SESSION_INST_MAP
    if _SESSION_INST_MAP is not None and not force_refresh:
        return _SESSION_INST_MAP

    if not force_refresh:
        cached = _load_daily_cache("inst", max_age_hours=12)
        if cached:
            _SESSION_INST_MAP = cached
            return cached

    urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading",
    ]
    rows = _fetch_json_rows(urls, timeout=20)
    today_iso = dt.date.today().isoformat()
    result: dict[str, dict[str, Any]] = {}

    for r in rows:
        sym = str(
            _pick_first_value(r, ["SecuritiesCompanyCode", "Code", "代號", "股票代號", "證券代號"]) or ""
        ).strip()
        if not sym or not sym[0].isdigit():
            continue

        raw_date = str(_pick_first_value(r, ["Date", "資料日期", "日期"]) or "").strip()
        iso_date = today_iso
        digits = "".join(ch for ch in raw_date if ch.isdigit())
        if len(digits) == 8:
            iso_date = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        elif len(digits) == 7:  # 民國年 YYYMMDD
            yr = int(digits[:3]) + 1911
            iso_date = f"{yr:04d}-{digits[3:5]}-{digits[5:7]}"

        f_buy = _clean_float(_pick_first_value(r, ["ForeignInvestorsBuy", "外資及陸資(不含外資自營商)買進股數", "外資買進股數"])) or 0.0
        f_sell = _clean_float(_pick_first_value(r, ["ForeignInvestorsSell", "外資及陸資(不含外資自營商)賣出股數", "外資賣出股數"])) or 0.0
        t_buy = _clean_float(_pick_first_value(r, ["InvestmentTrustBuy", "投信買進股數"])) or 0.0
        t_sell = _clean_float(_pick_first_value(r, ["InvestmentTrustSell", "投信賣出股數"])) or 0.0
        d_buy = _clean_float(_pick_first_value(r, ["DealerSelfBuy", "自營商(自行買賣)買進股數", "自營商買進股數"])) or 0.0
        d_sell = _clean_float(_pick_first_value(r, ["DealerSelfSell", "自營商(自行買賣)賣出股數", "自營商賣出股數"])) or 0.0

        result[sym] = {
            "date": iso_date,
            "foreign_buy": f_buy,
            "foreign_sell": f_sell,
            "trust_buy": t_buy,
            "trust_sell": t_sell,
            "dealer_buy": d_buy,
            "dealer_sell": d_sell,
        }

    if result:
        _save_daily_cache("inst", result)
    _SESSION_INST_MAP = result
    return result


def fetch_tpex_inst_df(stock_id: str) -> pd.DataFrame:
    """回傳與 fetch_chips.py 相同欄位的三大法人 DataFrame"""
    sym = str(stock_id).strip()
    item = get_tpex_inst_map().get(sym)
    if not item:
        return pd.DataFrame()
    d_str = item["date"]
    records = [
        {"date": d_str, "name": "Foreign_Investor", "buy": item["foreign_buy"], "sell": item["foreign_sell"]},
        {"date": d_str, "name": "Investment_Trust", "buy": item["trust_buy"], "sell": item["trust_sell"]},
        {"date": d_str, "name": "Dealer_self", "buy": item["dealer_buy"], "sell": item["dealer_sell"]},
    ]
    return pd.DataFrame(records)


def get_tpex_margin_map(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """取得上櫃全市場當日融資融券（對齊 fetch_chips.py 格式）"""
    global _SESSION_MARGIN_MAP
    if _SESSION_MARGIN_MAP is not None and not force_refresh:
        return _SESSION_MARGIN_MAP

    if not force_refresh:
        cached = _load_daily_cache("margin", max_age_hours=12)
        if cached:
            _SESSION_MARGIN_MAP = cached
            return cached

    urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
    ]
    rows = _fetch_json_rows(urls, timeout=20)
    today_iso = dt.date.today().isoformat()
    result: dict[str, dict[str, Any]] = {}

    for r in rows:
        sym = str(
            _pick_first_value(r, ["SecuritiesCompanyCode", "Code", "代號", "股票代號", "證券代號"]) or ""
        ).strip()
        if not sym or not sym[0].isdigit():
            continue

        raw_date = str(_pick_first_value(r, ["Date", "資料日期", "日期"]) or "").strip()
        iso_date = today_iso
        digits = "".join(ch for ch in raw_date if ch.isdigit())
        if len(digits) == 8:
            iso_date = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        elif len(digits) == 7:
            yr = int(digits[:3]) + 1911
            iso_date = f"{yr:04d}-{digits[3:5]}-{digits[5:7]}"

        m_buy = _clean_float(_pick_first_value(r, ["MarginPurchaseBuy", "資買", "融資買進"])) or 0.0
        m_sell = _clean_float(_pick_first_value(r, ["MarginPurchaseSell", "資賣", "融資賣出"])) or 0.0

        result[sym] = {
            "date": iso_date,
            "MarginPurchaseBuy": m_buy,
            "MarginPurchaseSell": m_sell,
        }

    if result:
        _save_daily_cache("margin", result)
    _SESSION_MARGIN_MAP = result
    return result


def fetch_tpex_margin_df(stock_id: str) -> pd.DataFrame:
    """回傳與 fetch_chips.py 相同欄位的融資融券 DataFrame"""
    sym = str(stock_id).strip()
    item = get_tpex_margin_map().get(sym)
    if not item:
        return pd.DataFrame()
    return pd.DataFrame([item])


if __name__ == "__main__":
    v_map = get_tpex_valuation_map(force_refresh=True)
    i_map = get_tpex_inst_map(force_refresh=True)
    m_map = get_tpex_margin_map(force_refresh=True)
    print(f"TPEx 快取測試 -> 估值={len(v_map)} 檔, 法人={len(i_map)} 檔, 融資券={len(m_map)} 檔")
    for test_sym in ["1584", "3413", "6488", "8069"]:
        print(test_sym, "估值:", fetch_tpex_stock_valuation(test_sym))