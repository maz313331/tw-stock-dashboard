import argparse
import datetime as dt
import json
import math
from pathlib import Path
import re
import time
import requests

from calc_scores import build_stock_detail, write_json
from fetch_chips import fetch_chip_data, reset_chip_session_cache
from fetch_fundamental import (
    ApiQuotaGuard,
    DataHealthTracker,
    clean_numeric_or_none,
    evaluate_and_attach_missing,
    is_missing_value,
)
from fetch_sources import fetch_yf_history

try:
    from fetch_tpex import reset_tpex_session_cache
except Exception:
    try:
        from scripts.fetch_tpex import reset_tpex_session_cache
    except Exception:
        reset_tpex_session_cache = None

try:
    from fetch_fundamental import fetch_fundamental_data as _impl_fetch_fundamental_data
except ImportError:
    _impl_fetch_fundamental_data = None


def fetch_fundamental_data(symbol: str, guard=None, cached_metrics: dict | None = None) -> dict:
    """安全包裝基本面抓取函式：共用 quota_guard 並傳入既有快取指標，缺哪項補哪項。"""
    if callable(_impl_fetch_fundamental_data):
        try:
            res = _impl_fetch_fundamental_data(symbol, guard=guard, cached_metrics=cached_metrics)
            if isinstance(res, dict):
                return res
        except Exception as exc:
            print(f"[基本面提示] {symbol} 抓取發生例外，改採安全預設: {exc}")
    return {"complete": False, "metrics": cached_metrics or {}}


DEFAULT_WATCHLIST = [
    ("2330", "台積電", "TW"),
    ("2317", "鴻海", "TW"),
    ("2454", "聯發科", "TW"),
    ("0050", "元大台灣50", "TW"),
    ("00878", "國泰永續高股息", "TW"),
]

VALID_PHASES = ("auto", "close_tech", "chips", "fundamental")


def _is_valid_value(val) -> bool:
    if val is None:
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    if isinstance(val, str):
        s = val.strip()
        if not s or s in {"缺漏", "N/A", "NaN", "None", "--"}:
            return False
    return True


def _strip_non_serializable_series(data: dict) -> dict:
    """移除內含 pandas Series 的暫存欄位，避免寫入 JSON 時發生序列化錯誤。"""
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for k, v in data.items():
        if k == "series":
            continue
        cleaned[k] = v
    return cleaned


def merge_without_overwrite(old_data: dict, new_data: dict) -> dict:
    """
    防空值洗資料：遞迴合併新舊字典。
    若 new_data 的欄位為空/缺漏，而 old_data 有有效值，則保留 old_data。
    """
    if not isinstance(old_data, dict) or not old_data:
        return new_data or {}
    if not isinstance(new_data, dict) or not new_data:
        return old_data

    merged = dict(old_data)
    for k, new_val in new_data.items():
        if k in {"updated_at", "partial_missing", "missing_reasons", "excluded", "exclude_reason"}:
            if _is_valid_value(new_val):
                merged[k] = new_val
            elif _is_valid_value(old_data.get(k)) and k != "updated_at":
                merged[k] = old_data.get(k)
            else:
                merged[k] = new_val
            continue

        old_val = old_data.get(k)
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            merged[k] = merge_without_overwrite(old_val, new_val)
        elif isinstance(old_val, list) and isinstance(new_val, list):
            if not new_val and old_val:
                merged[k] = old_val
            elif old_val and len(old_val) == len(new_val) and all(
                isinstance(x, dict) for x in old_val + new_val
            ):
                merged_list = []
                for o_item, n_item in zip(old_val, new_val):
                    merged_list.append(merge_without_overwrite(o_item, n_item))
                merged[k] = merged_list
            else:
                old_missing = sum(
                    1 for item in old_val
                    if isinstance(item, dict) and not _is_valid_value(item.get("value"))
                )
                new_missing = sum(
                    1 for item in new_val
                    if isinstance(item, dict) and not _is_valid_value(item.get("value"))
                )
                if new_val and (len(new_val) >= len(old_val) and new_missing <= old_missing):
                    merged[k] = new_val
        else:
            if _is_valid_value(new_val):
                merged[k] = new_val
            elif _is_valid_value(old_val):
                merged[k] = old_val
    return merged


def load_batch_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_batch_state(state_path: Path, state_data: dict) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(state_path, state_data)
    except Exception:
        pass


def _clean_change_sign(raw_val) -> str:
    """移除 TWSE 欄位可能夾帶的 HTML 標籤並擷取漲跌方向/數值字串。"""
    s = re.sub(r"<[^>]+>", "", str(raw_val or "")).strip()
    return s


def fetch_twse_market_stats(quota_guard: ApiQuotaGuard | None = None, tracker: DataHealthTracker | None = None) -> dict:
    """
    嘗試從 TWSE 公開資料取得當日三大法人買賣超（億元）與上市漲跌家數。
    抓不到時回傳空字典，由呼叫端安全沿用既有數值。
    """
    result = {}
    headers = {"User-Agent": "Mozilla/5.0"}

    def _get_json(url: str, timeout: int):
        if quota_guard is not None:
            def _do_req():
                r = requests.get(url, headers=headers, timeout=timeout)
                try:
                    body = r.json()
                except Exception:
                    body = None
                return r.status_code, body

            ok, status_code, body, stop_reason = quota_guard.execute_with_guard(_do_req)
            if tracker is not None:
                healthy, _ = tracker.check_response_health("twse_openapi", status_code, body, rows_key="data", min_rows=1)
                if not healthy and stop_reason:
                    print(f"[配額提示] {stop_reason}")
            return body if ok else None

        r = requests.get(url, headers=headers, timeout=timeout)
        return r.json() if r.ok else None

    try:
        rows = _get_json("https://openapi.twse.com.tw/v1/fund/BFI82U", timeout=15)
        if isinstance(rows, list) and rows:
            foreign_yi = 0.0
            trust_yi = 0.0
            dealer_yi = 0.0
            matched_any = False
            for r in rows:
                name = str(r.get("單位名稱") or r.get("Name") or "")
                diff_raw = (
                    r.get("買賣差額")
                    or r.get("Difference")
                    or r.get("NetBuySell")
                    or 0
                )
                cleaned = _clean_change_sign(diff_raw).replace(",", "")
                val_yi = (clean_numeric_or_none(cleaned) or 0.0) / 1e8
                if "外資" in name or "Foreign" in name:
                    foreign_yi += val_yi
                    matched_any = True
                elif "投信" in name or "Trust" in name:
                    trust_yi += val_yi
                    matched_any = True
                elif "自營" in name or "Dealer" in name:
                    dealer_yi += val_yi
                    matched_any = True
            if matched_any:
                total_yi = round(foreign_yi + trust_yi + dealer_yi, 1)
                result["institutional_flow"] = {
                    "foreign_net": round(foreign_yi, 1),
                    "trust_net": round(trust_yi, 1),
                    "dealer_net": round(dealer_yi, 1),
                    "total_net": total_yi,
                }
    except Exception:
        pass

    try:
        rows = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=20)
        if isinstance(rows, list) and rows:
            adv = 0
            dec = 0
            for r in rows:
                chg_str = _clean_change_sign(r.get("Change") or r.get("漲跌") or "")
                if not chg_str:
                    continue
                if chg_str.startswith("+") or "漲" in chg_str:
                    adv += 1
                elif chg_str.startswith("-") or "跌" in chg_str:
                    dec += 1
                else:
                    val = clean_numeric_or_none(chg_str)
                    if val is not None:
                        if val > 0:
                            adv += 1
                        elif val < 0:
                            dec += 1
            if adv > 0 or dec > 0:
                result["advancing"] = adv
                result["declining"] = dec
    except Exception:
        pass

    return result


def fetch_otc_index_quote(prev_otc: dict | None = None) -> dict:
    """
    取得櫃買指數收盤與漲跌幅；yfinance ^TWOII / ^TPEX 已無資料，
    先沿用既有有效值避免重複打失效代號。
    """
    if isinstance(prev_otc, dict) and prev_otc:
        return dict(prev_otc)
    return {"close": None, "change_pct": None}


def refine_etf_exclusion_message(symbol: str, detail_obj: dict, row_obj: dict) -> tuple[dict, dict]:
    """
    針對 ETF（代號以 00 開頭）遞迴替換排除說明字串，
    明確標示 ETF 本身無個股估值/月營收/財報欄位，避免誤解為爬蟲漏抓。
    """
    if not str(symbol).strip().startswith("00"):
        return detail_obj, row_obj

    etf_reason = "ETF 無個股估值(PE/PB/殖利率)、月營收與財報(ROA/ROE)欄位；集保大戶／散戶資料缺漏或尚未更新"

    def _replace_in_obj(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and ("估值資料(PE/PB/殖利率)" in v or "財報/ROA/ROE" in v):
                    if "今日已依規則排除評分排行" in v:
                        obj[k] = (
                            f"今日已依規則排除評分排行：{etf_reason}。"
                            "請搭配大盤水位、量價結構與停損紀律綜合判斷。"
                        )
                    else:
                        obj[k] = etf_reason
                elif isinstance(v, (dict, list)):
                    _replace_in_obj(v)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                if isinstance(item, str) and ("估值資料(PE/PB/殖利率)" in item or "財報/ROA/ROE" in item):
                    obj[idx] = etf_reason
                elif isinstance(item, (dict, list)):
                    _replace_in_obj(item)

    _replace_in_obj(detail_obj)
    _replace_in_obj(row_obj)
    return detail_obj, row_obj


def resolve_effective_phase(phase_arg: str) -> str:
    """依參數與台灣時間 (UTC+8) 決定實際執行的資料更新分段。"""
    normalized = (phase_arg or "auto").strip().lower()
    if normalized in {"close_tech", "chips", "fundamental"}:
        return normalized

    cst_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    if cst_now.weekday() == 5:
        return "fundamental"
    if cst_now.hour >= 16:
        return "chips"
    return "close_tech"


def _extract_cached_fund_or_chip(detail_path: Path, kind: str) -> dict | None:
    """
    從既有明細檔取出快取的基本面或籌碼欄位，供非對應分段沿用，
    避免盤後收盤段重複抓取低頻基本面或尚未完全入齊的籌碼。
    """
    if not detail_path.exists():
        return None
    try:
        old = json.loads(detail_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(old, dict):
        return None

    if kind == "fund":
        cached = old.get("fundamental_data")
        if isinstance(cached, dict) and cached:
            return _strip_non_serializable_series(cached)
        return None

    if kind == "chip":
        cached = old.get("chip_data")
        if isinstance(cached, dict) and cached:
            return _strip_non_serializable_series(cached)
        m6 = old.get("metrics_6")
        if isinstance(m6, dict) and m6:
            return {
                "complete": True,
                "main_force_net": m6.get("main_force_net"),
                "retail_net": m6.get("retail_net"),
                "margin_net": m6.get("margin_net"),
                "chip_concentration_5d": m6.get("chip_concentration_5d"),
                "major_holder_pct": m6.get("major_holder_pct"),
                "retail_holder_pct": m6.get("retail_holder_pct"),
            }
    return None


def _normalize_market_code(raw_market: str | None, ticker: str | None = None) -> str:
    """將 'TWSE'/'TPEx'/'TW'/'TWO' 統一正規化為 'TW' 或 'TWO'，供日線後綴判斷使用。"""
    m = str(raw_market or "").strip().upper()
    t = str(ticker or "").strip().upper()
    if m in {"TWO", "TPEX", "OTC", "上櫃"} or t.endswith(".TWO"):
        return "TWO"
    return "TW"


def load_target_symbols(
    root: Path,
    offset: int = 0,
    limit: int = 250,
    priority_file: str | None = "data/priority_stocks.json",
    use_priority: bool = True,
) -> list[tuple[str, str, str]]:
    """
    優先讀取 data/priority_stocks.json（若啟用且存在）；
    若未啟用或檔案不存在，則讀取 data/stock_list_common.json，最後退回預設觀察清單。
    回傳格式：(symbol, name, market) 其中 market 為 'TW' 或 'TWO'。
    """
    full_pool: list[tuple[str, str, str]] = []
    seen_syms: set[str] = set()

    if use_priority and priority_file:
        p_path = Path(priority_file)
        if not p_path.is_absolute():
            p_path = root / p_path
        if p_path.exists():
            try:
                raw = json.loads(p_path.read_text(encoding="utf-8"))
                items = raw.get("items") if isinstance(raw, dict) else raw
                if isinstance(items, list) and items:
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        sym = str(item.get("symbol") or "").strip().upper()
                        if not sym or sym in seen_syms:
                            continue
                        seen_syms.add(sym)
                        name = str(item.get("name") or sym).strip()
                        mkt = _normalize_market_code(item.get("market"), item.get("ticker"))
                        full_pool.append((sym, name, mkt))
                    if full_pool:
                        print(f"[優先清單] 已載入 {p_path.name} 共 {len(full_pool)} 檔標的")
            except Exception as exc:
                print(f"[警告] 讀取 {p_path} 失敗，改用 stock_list_common.json: {exc}")

    if not full_pool:
        common_path = root / "data" / "stock_list_common.json"
        if common_path.exists():
            try:
                raw = json.loads(common_path.read_text(encoding="utf-8"))
                if isinstance(raw, list) and raw:
                    for item in raw:
                        sym = str(item.get("symbol") or "").strip().upper()
                        name = str(item.get("name") or sym).strip()
                        mkt = _normalize_market_code(item.get("market"), item.get("ticker"))
                        if sym and sym not in seen_syms:
                            seen_syms.add(sym)
                            full_pool.append((sym, name, mkt))
            except Exception as exc:
                print(f"[警告] 讀取 stock_list_common.json 失敗，改用預設清單: {exc}")

    if not full_pool:
        full_pool = DEFAULT_WATCHLIST

    start = max(0, offset)
    if limit is not None and limit > 0:
        return full_pool[start : start + limit]
    return full_pool[start:]


def filter_missing_symbols(
    root: Path, pool: list[tuple[str, str, str]], batch_state: dict, latest_trade_date: str
) -> list[tuple[str, str, str]]:
    """
    僅保留「尚未入庫」或「今日尚未嘗試補抓且先前有缺漏」的標的，
    避免同一行情日內重複對已知缺漏標的狂打 API 消耗額度。
    """
    summary_path = root / "data" / "summary.json"
    existing_syms: set[str] = set()

    if summary_path.exists():
        try:
            prev = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and isinstance(prev.get("stocks"), list):
                for r in prev["stocks"]:
                    s = str(r.get("symbol") or "").strip()
                    if s:
                        existing_syms.add(s)
        except Exception:
            pass

    selected: list[tuple[str, str, str]] = []
    for sym, name, mkt in pool:
        if sym not in existing_syms:
            selected.append((sym, name, mkt))
            continue

        st = batch_state.get(sym) if isinstance(batch_state.get(sym), dict) else {}
        already_tried_today = st.get("last_missing_retry_date") == latest_trade_date
        if not st.get("complete", False) and not already_tried_today:
            selected.append((sym, name, mkt))

    return selected


def fetch_stock_history_with_market(sym: str, mkt: str, s2330_df, quota_guard: ApiQuotaGuard | None = None):
    """依市場別嘗試抓取日線，上市優先 .TW，上櫃優先 .TWO 再退回 .TW；若配額熔斷則停止新請求"""
    if sym == "2330" and s2330_df is not None and not s2330_df.empty:
        return s2330_df

    if quota_guard is not None:
        allowed, _ = quota_guard.can_call()
        if not allowed:
            return None

    suffixes = [".TWO", ".TW"] if mkt.upper() == "TWO" else [".TW", ".TWO"]
    for sfx in suffixes:
        if quota_guard is not None:
            allowed, _ = quota_guard.can_call()
            if not allowed:
                break
            quota_guard.wait_before_call()
            now = time.monotonic()
            quota_guard.last_request_at = now
            quota_guard.call_timestamps.append(now)
            quota_guard.total_calls += 1

        df = fetch_yf_history(f"{sym}{sfx}", period="6mo", limit=120, retries=1, retry_delay=1.0)
        if df is not None and not df.empty and len(df) >= 2:
            return df
    return None


def merge_summary_stocks(summary_path: Path, new_rows: list[dict], merge: bool = True) -> list[dict]:
    """
    將本批次產出的股票列合併進既有 summary.json（依 symbol 防空值安全合併），
    確保分批執行時先前累積的股票與有效欄位不會被覆蓋掉。
    """
    if not merge or not summary_path.exists():
        return sorted(new_rows, key=lambda r: str(r.get("symbol") or ""))

    existing_rows: list[dict] = []
    try:
        prev = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(prev, dict) and isinstance(prev.get("stocks"), list):
            existing_rows = prev["stocks"]
    except Exception:
        existing_rows = []

    by_symbol: dict[str, dict] = {}
    for r in existing_rows:
        sym = str(r.get("symbol") or "").strip()
        if sym:
            by_symbol[sym] = r

    for r in new_rows:
        sym = str(r.get("symbol") or "").strip()
        if sym:
            if sym in by_symbol:
                by_symbol[sym] = merge_without_overwrite(by_symbol[sym], r)
            else:
                by_symbol[sym] = r

    return sorted(by_symbol.values(), key=lambda r: str(r.get("symbol") or ""))


def main():
    parser = argparse.ArgumentParser(description="台股盤勢與多維度評分批次更新")
    parser.add_argument("--offset", type=int, default=0, help="從清單第幾筆開始處理（預設 0）")
    parser.add_argument("--limit", type=int, default=250, help="單次最多處理幾檔（預設 250；設 0 代表全量）")
    parser.add_argument("--sleep", type=float, default=0.25, help="每檔處理間隔秒數，降低限流風險")
    parser.add_argument(
        "--max-hourly-calls",
        type=int,
        default=580,
        help="每小時請求安全上限（預設 580，低於 600/hr 會員上限留出緩衝）",
    )
    parser.add_argument(
        "--phase",
        type=str,
        default="auto",
        choices=list(VALID_PHASES),
        help="更新分段：auto（依時間自動分流）、close_tech（收盤/技術面）、chips（籌碼面）、fundamental（基本面）",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="僅針對尚未入庫或今日尚未補抓的缺漏標的補抓",
    )
    parser.add_argument(
        "--force-check",
        action="store_true",
        help="強制重抓並檢查所有標的（忽略同日已抓齊跳過規則）",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="不合併既有 summary.json，改以本批次結果直接覆蓋",
    )
    parser.add_argument(
        "--priority-file",
        type=str,
        default="data/priority_stocks.json",
        help="優先抓取清單 JSON 路徑（預設 data/priority_stocks.json）",
    )
    parser.add_argument(
        "--full-market",
        action="store_true",
        help="忽略 priority_stocks.json，改以 stock_list_common.json 全市場清單執行",
    )
    args = parser.parse_args()

    reset_chip_session_cache()
    if callable(reset_tpex_session_cache):
        reset_tpex_session_cache()

    effective_phase = resolve_effective_phase(args.phase)
    print(f"[分段設定] 指定={args.phase} -> 實際執行分段={effective_phase}")

    quota_guard = ApiQuotaGuard(
        max_calls_per_hour=max(50, int(args.max_hourly_calls)),
        min_interval_sec=max(0.2, float(args.sleep)),
        max_retries_on_429=2,
        base_backoff_sec=5.0,
    )
    tracker = DataHealthTracker(quota_guard=quota_guard)

    root = Path(__file__).resolve().parent.parent
    market_path = root / "data" / "market.json"
    summary_path = root / "data" / "summary.json"
    state_path = root / "data" / "cache" / "batch_state.json"

    batch_state = load_batch_state(state_path)

    prev_market = {}
    if market_path.exists():
        try:
            prev_market = json.loads(market_path.read_text(encoding="utf-8"))
        except Exception:
            prev_market = {}

    prev_history_map = {
        str(item.get("date")): float(item.get("institutional_net") or 0.0)
        for item in (prev_market.get("history") or [])
        if isinstance(item, dict) and item.get("date")
    }

    twii_df = fetch_yf_history("^TWII", period="6mo", limit=120, retries=1, retry_delay=0.5)
    s2330_df = fetch_yf_history("2330.TW", period="6mo", limit=120, retries=1, retry_delay=0.5)

    if s2330_df.empty or len(s2330_df) < 2:
        raise RuntimeError("無法取得 2330.TW 歷史日線")

    if effective_phase in {"chips", "close_tech"}:
        market_stats = fetch_twse_market_stats(quota_guard=quota_guard, tracker=tracker)
    else:
        market_stats = {}

    inst_flow = dict(prev_market.get(
        "institutional_flow",
        {"foreign_net": 0.0, "trust_net": 0.0, "dealer_net": 0.0},
    ))
    if "institutional_flow" in market_stats:
        inst_flow = market_stats["institutional_flow"]
    elif "total_net" not in inst_flow:
        inst_flow["total_net"] = round(
            float(inst_flow.get("foreign_net") or 0.0)
            + float(inst_flow.get("trust_net") or 0.0)
            + float(inst_flow.get("dealer_net") or 0.0),
            1,
        )

    latest_inst_total = float(inst_flow.get("total_net") or 0.0)

    if not twii_df.empty and len(twii_df) >= 2:
        last_tw = twii_df.iloc[-1]
        prev_tw = twii_df.iloc[-2]
        tw_close = round(float(last_tw["close"]), 2)
        tw_prev_close = round(float(prev_tw["close"]), 2)
        tw_chg_pct = round(((tw_close - tw_prev_close) / tw_prev_close) * 100.0, 2) if tw_prev_close else 0.0
        latest_trade_date = str(last_tw["date"])
        tw_index_obj = {
            "close": tw_close,
            "change_pct": tw_chg_pct,
            "ma_status": prev_market.get("tw_index", {}).get("ma_status", "均線下方震盪"),
        }
        tail_rows = twii_df.tail(120).reset_index(drop=True)
        history_out = []
        for idx_h, row in tail_rows.iterrows():
            d_str = str(row["date"])
            inst_val = prev_history_map.get(d_str, 0.0)
            if idx_h == len(tail_rows) - 1 and "institutional_flow" in market_stats:
                inst_val = latest_inst_total
            history_out.append({
                "date": d_str,
                "close": round(float(row["close"]), 2),
                "institutional_net": round(float(inst_val), 1),
            })
    else:
        print("[警告] ^TWII 暫時無資料，沿用既有大盤指數與走勢紀錄，繼續更新個股與法人統計")
        latest_trade_date = str(s2330_df.iloc[-1]["date"])
        tw_index_obj = prev_market.get(
            "tw_index",
            {"close": 0.0, "change_pct": 0.0, "ma_status": "均線下方震盪"},
        )
        history_out = prev_market.get("history", [])

    otc_index_obj = fetch_otc_index_quote(prev_market.get("otc_index"))

    use_priority = not args.full_market
    if args.only_missing:
        all_pool = load_target_symbols(
            root,
            offset=0,
            limit=0,
            priority_file=args.priority_file,
            use_priority=use_priority,
        )
        targets = filter_missing_symbols(root, all_pool, batch_state, latest_trade_date)
        if args.offset > 0:
            targets = targets[args.offset :]
        if args.limit and args.limit > 0:
            targets = targets[: args.limit]
        print(f"[補抓模式] 偵測到待補抓標的共 {len(targets)} 檔")
    else:
        targets = load_target_symbols(
            root,
            offset=args.offset,
            limit=args.limit,
            priority_file=args.priority_file,
            use_priority=use_priority,
        )

    batch_rows = []
    batch_excluded_cnt = 0
    batch_high_score_cnt = 0
    skipped_cnt = 0
    already_complete_cnt = 0
    quota_stopped_cnt = 0

    tz_cst = dt.timezone(dt.timedelta(hours=8))
    now_str = dt.datetime.now(tz_cst).strftime("%Y-%m-%d %H:%M")
    updated_label = f"{now_str}（行情日 {latest_trade_date}）"

    for idx, (sym, name, mkt) in enumerate(targets):
        allowed, stop_reason = quota_guard.can_call()
        if not allowed:
            quota_stopped_cnt = len(targets) - idx
            print(f"[配額熔斷] {stop_reason}，剩餘 {quota_stopped_cnt} 檔保留既有資料並結束本批。")
            break

        detail_path = root / "data" / "details" / f"{sym}.json"
        st_entry = batch_state.get(sym, {})
        phase_key = f"complete_{effective_phase}"

        if (
            not args.force_check
            and not args.only_missing
            and detail_path.exists()
            and st_entry.get("trade_date") == latest_trade_date
            and st_entry.get(phase_key) is True
        ):
            already_complete_cnt += 1
            continue

        if args.only_missing or (idx + 1) % 25 == 0 or idx == 0:
            print(f"[進度:{effective_phase}] {idx + 1}/{len(targets)} -> {sym} {name} ({mkt})")

        s_df = fetch_stock_history_with_market(sym, mkt, s2330_df, quota_guard=quota_guard)
        if s_df is None or s_df.empty or len(s_df) < 2:
            skipped_cnt += 1
            tracker.record_stock_missing(sym, "price_history", "日線行情暫時無法取得")
            print(f"[略過] {sym} {name} ({mkt}) 暫時無法取得日線行情（保留既有資料）")
            continue

        cached_fund = _extract_cached_fund_or_chip(detail_path, "fund")
        cached_chip = _extract_cached_fund_or_chip(detail_path, "chip")
        cached_fund_metrics = cached_fund.get("metrics") if isinstance(cached_fund, dict) else None

        if effective_phase == "fundamental":
            fund_data = fetch_fundamental_data(sym, guard=quota_guard, cached_metrics=cached_fund_metrics)
            chip_data = cached_chip or fetch_chip_data(sym, stock_df=s_df, quota_guard=quota_guard, cached_chip=cached_chip)
        elif effective_phase == "chips":
            fund_data = cached_fund or {"complete": str(sym).startswith("00")}
            chip_data = fetch_chip_data(sym, stock_df=s_df, quota_guard=quota_guard, cached_chip=cached_chip)
        else:
            fund_data = cached_fund or fetch_fundamental_data(sym, guard=quota_guard, cached_metrics=cached_fund_metrics)
            chip_data = cached_chip or fetch_chip_data(sym, stock_df=s_df, quota_guard=quota_guard, cached_chip=cached_chip)

        fund_metrics = fund_data.get("metrics") if isinstance(fund_data, dict) and isinstance(fund_data.get("metrics"), dict) else {}
        val_metrics = {
            "pe": fund_metrics.get("pe"),
            "pb": fund_metrics.get("pb"),
            "dividend_yield": fund_metrics.get("dividend_yield"),
        }
        rev_metrics = {
            "rev_mom_pct": fund_metrics.get("rev_mom_pct"),
        }
        fin_metrics = {
            "op_income": fund_metrics.get("op_income"),
            "net_income": fund_metrics.get("net_income"),
            "eps": fund_metrics.get("eps"),
            "roa_pct": fund_metrics.get("roa_pct"),
            "roe_pct": fund_metrics.get("roe_pct"),
        }
        chip_metrics = chip_data if isinstance(chip_data, dict) else {}

        missing_summary = evaluate_and_attach_missing(
            tracker=tracker,
            symbol=sym,
            val_metrics=val_metrics,
            rev_metrics=rev_metrics,
            fin_metrics=fin_metrics,
            chip_metrics=chip_metrics,
        )

        detail_obj, row_obj = build_stock_detail(
            symbol=sym,
            name=name,
            updated_at=updated_label,
            stock_df=s_df,
            index_df=twii_df,
            fundamental_data=fund_data,
            chip_data=chip_data,
        )
        detail_obj, row_obj = refine_etf_exclusion_message(sym, detail_obj, row_obj)

        if not str(sym).startswith("00"):
            detail_obj["partial_missing"] = missing_summary.get("partial_missing", "")
            detail_obj["missing_reasons"] = missing_summary.get("missing_reasons", [])
            row_obj["partial_missing"] = missing_summary.get("partial_missing", "")

        clean_fund_for_json = _strip_non_serializable_series(fund_data) if isinstance(fund_data, dict) else {}
        clean_chip_for_json = _strip_non_serializable_series(chip_data) if isinstance(chip_data, dict) else {}

        if clean_fund_for_json:
            detail_obj["fundamental_data"] = merge_without_overwrite(
                _strip_non_serializable_series(detail_obj.get("fundamental_data") or {}),
                clean_fund_for_json,
            )
        if clean_chip_for_json:
            detail_obj["chip_data"] = merge_without_overwrite(
                _strip_non_serializable_series(detail_obj.get("chip_data") or {}),
                clean_chip_for_json,
            )

        if detail_path.exists():
            try:
                old_detail = json.loads(detail_path.read_text(encoding="utf-8"))
                clean_missing_override = (not str(sym).startswith("00")) and (not missing_summary.get("partial_missing"))
                detail_obj = merge_without_overwrite(old_detail, detail_obj)
                if clean_missing_override:
                    detail_obj["partial_missing"] = ""
                    detail_obj["missing_reasons"] = []
                    row_obj["partial_missing"] = ""
            except Exception:
                pass

        write_json(detail_path, detail_obj)
        batch_rows.append(row_obj)

        is_fund_ok = bool(fund_data.get("complete", False)) or str(sym).startswith("00")
        is_chip_ok = bool(chip_data.get("complete", True))
        has_missing_msg = bool(detail_obj.get("partial_missing")) and not str(sym).startswith("00")
        is_complete = bool(is_fund_ok and is_chip_ok and not has_missing_msg)

        next_state = {
            **st_entry,
            "trade_date": latest_trade_date,
            "updated_at": now_str,
            phase_key: True,
            "complete": is_complete,
        }
        if args.only_missing and not is_complete:
            next_state["last_missing_retry_date"] = latest_trade_date

        batch_state[sym] = next_state

        if row_obj.get("excluded"):
            batch_excluded_cnt += 1
        elif row_obj.get("total_score", 0) >= 22:
            batch_high_score_cnt += 1

        if args.sleep > 0 and idx < len(targets) - 1:
            time.sleep(args.sleep)

    save_batch_state(state_path, batch_state)

    merged_rows = merge_summary_stocks(summary_path, batch_rows, merge=not args.no_merge)
    write_json(
        summary_path,
        {
            "updated_at": updated_label,
            "stocks": merged_rows,
        },
    )

    total_excluded_cnt = sum(1 for r in merged_rows if r.get("excluded"))
    total_high_score_cnt = sum(
        1 for r in merged_rows if (not r.get("excluded")) and (r.get("total_score", 0) >= 22)
    )

    breadth = dict(prev_market.get("market_breadth", {
        "advancing": 0,
        "declining": 0,
        "high_score_count": 0,
        "excluded_count": 0,
    }))
    if "advancing" in market_stats:
        breadth["advancing"] = market_stats["advancing"]
    if "declining" in market_stats:
        breadth["declining"] = market_stats["declining"]
    breadth["high_score_count"] = total_high_score_cnt
    breadth["excluded_count"] = total_excluded_cnt

    market_out = dict(prev_market)
    if effective_phase != "fundamental" or not market_out.get("updated_at"):
        market_out["updated_at"] = updated_label
    market_out["tw_index"] = tw_index_obj
    market_out["otc_index"] = otc_index_obj
    market_out["institutional_flow"] = inst_flow
    market_out["market_breadth"] = breadth
    market_out["history"] = history_out
    write_json(market_path, market_out)

    tracker.print_batch_report(len(targets))

    print(
        f"[完成:{effective_phase}] 批次更新 ({latest_trade_date}) | 本批更新={len(batch_rows)} "
        f"(同日已齊全跳過={already_complete_cnt}, 略過={skipped_cnt}, 配額熔斷未跑={quota_stopped_cnt}, "
        f"本批高分={batch_high_score_cnt}, 本批排除={batch_excluded_cnt}) | "
        f"累計總檔數={len(merged_rows)} (累計高分={total_high_score_cnt}, 累計排除={total_excluded_cnt})"
    )


if __name__ == "__main__":
    main()