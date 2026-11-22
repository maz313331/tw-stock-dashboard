import argparse
import datetime as dt
import json
import math
from pathlib import Path
import re
import time
import requests
from calc_scores import build_stock_detail, write_json
from fetch_chips import fetch_chip_data
from fetch_fundamental import fetch_fundamental_data
from fetch_sources import fetch_yf_history


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
        state_path.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _clean_change_sign(raw_val) -> str:
    """移除 TWSE 欄位可能夾帶的 HTML 標籤並擷取漲跌方向/數值字串。"""
    s = re.sub(r"<[^>]+>", "", str(raw_val or "")).strip()
    return s


def fetch_twse_market_stats() -> dict:
    """
    嘗試從 TWSE 公開資料取得當日三大法人買賣超（億元）與上市漲跌家數。
    抓不到時回傳空字典，由呼叫端安全沿用既有數值。
    """
    result = {}
    headers = {"User-Agent": "Mozilla/5.0"}

    # 1. 三大法人買賣超日報
    try:
        url_inst = "https://openapi.twse.com.tw/v1/fund/BFI82U"
        resp_inst = requests.get(url_inst, headers=headers, timeout=15)
        if resp_inst.ok:
            rows = resp_inst.json()
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
                    val_yi = float(cleaned or 0) / 1e8
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

    # 2. 上市每日收盤行情漲跌家數統計
    try:
        url_stock = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        resp_stock = requests.get(url_stock, headers=headers, timeout=20)
        if resp_stock.ok:
            rows = resp_stock.json()
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
                        try:
                            val = float(chg_str.replace(",", ""))
                            if val > 0:
                                adv += 1
                            elif val < 0:
                                dec += 1
                        except Exception:
                            pass
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
    if cst_now.weekday() == 5:  # 週六低頻基本面
        return "fundamental"
    if cst_now.hour >= 16:  # 16:00 以後三大法人與籌碼補齊
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
            return cached
        return None

    if kind == "chip":
        cached = old.get("chip_data")
        if isinstance(cached, dict) and cached:
            return cached
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


def load_target_symbols(root: Path, offset: int = 0, limit: int = 250) -> list[tuple[str, str, str]]:
    """
    優先讀取 data/stock_list_common.json（全市場普通股清單），
    若檔案不存在則退回預設觀察清單。
    """
    common_path = root / "data" / "stock_list_common.json"
    full_pool: list[tuple[str, str, str]] = []

    if common_path.exists():
        try:
            raw = json.loads(common_path.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                for item in raw:
                    sym = str(item.get("symbol") or "").strip()
                    name = str(item.get("name") or sym).strip()
                    mkt = str(item.get("market") or "TW").strip()
                    if sym:
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
    root: Path, pool: list[tuple[str, str, str]], batch_state: dict
) -> list[tuple[str, str, str]]:
    """保留尚未存在於 summary.json，或先前抓取標記為缺漏 (complete=False) 的標的。"""
    summary_path = root / "data" / "summary.json"
    existing_syms: set[str] = set()
    incomplete_syms: set[str] = set()

    if summary_path.exists():
        try:
            prev = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and isinstance(prev.get("stocks"), list):
                for r in prev["stocks"]:
                    s = str(r.get("symbol") or "").strip()
                    if s:
                        existing_syms.add(s)
                        if r.get("partial_missing"):
                            incomplete_syms.add(s)
        except Exception:
            pass

    for sym, st in batch_state.items():
        if isinstance(st, dict) and not st.get("complete", False):
            incomplete_syms.add(sym)

    return [
        (sym, name, mkt)
        for (sym, name, mkt) in pool
        if (sym not in existing_syms) or (sym in incomplete_syms)
    ]


def fetch_stock_history_with_market(sym: str, mkt: str, s2330_df):
    """依市場別嘗試抓取日線，上市優先 .TW，上櫃優先 .TWO 再退回 .TW"""
    if sym == "2330" and s2330_df is not None and not s2330_df.empty:
        return s2330_df

    suffixes = [".TWO", ".TW"] if mkt.upper() == "TWO" else [".TW", ".TWO"]
    for sfx in suffixes:
        df = fetch_yf_history(f"{sym}{sfx}", period="6mo", limit=120, retries=2, retry_delay=1.0)
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
    parser.add_argument("--sleep", type=float, default=0.1, help="每檔處理間隔秒數，降低限流風險")
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
        help="僅針對尚未入庫或缺漏的標的補抓",
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
    args = parser.parse_args()

    effective_phase = resolve_effective_phase(args.phase)
    print(f"[分段設定] 指定={args.phase} -> 實際執行分段={effective_phase}")

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

    # 籌碼或自動分段時抓 TWSE 法人與漲跌統計；純基本面段優先沿用既有統計
    if effective_phase in {"chips", "close_tech"}:
        market_stats = fetch_twse_market_stats()
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

    if args.only_missing:
        all_pool = load_target_symbols(root, offset=0, limit=0)
        targets = filter_missing_symbols(root, all_pool, batch_state)
        if args.offset > 0:
            targets = targets[args.offset :]
        if args.limit and args.limit > 0:
            targets = targets[: args.limit]
        print(f"[補抓模式] 偵測到尚未入庫或缺漏標的共 {len(targets)} 檔")
    else:
        targets = load_target_symbols(root, offset=args.offset, limit=args.limit)

    batch_rows = []
    batch_excluded_cnt = 0
    batch_high_score_cnt = 0
    skipped_cnt = 0
    already_complete_cnt = 0

    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    updated_label = f"{now_str}（行情日 {latest_trade_date}）"

    for idx, (sym, name, mkt) in enumerate(targets):
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

        s_df = fetch_stock_history_with_market(sym, mkt, s2330_df)
        if s_df is None or s_df.empty or len(s_df) < 2:
            skipped_cnt += 1
            print(f"[略過] {sym} {name} ({mkt}) 暫時無法取得日線行情（保留既有資料）")
            continue

        # 依分段決定重抓或沿用既有基本面／籌碼
        if effective_phase == "fundamental":
            fund_data = fetch_fundamental_data(sym)
            chip_data = _extract_cached_fund_or_chip(detail_path, "chip") or fetch_chip_data(sym, stock_df=s_df)
        elif effective_phase == "chips":
            fund_data = _extract_cached_fund_or_chip(detail_path, "fund") or {"complete": str(sym).startswith("00")}
            chip_data = fetch_chip_data(sym, stock_df=s_df)
        else:  # close_tech
            fund_data = _extract_cached_fund_or_chip(detail_path, "fund") or fetch_fundamental_data(sym)
            chip_data = _extract_cached_fund_or_chip(detail_path, "chip") or fetch_chip_data(sym, stock_df=s_df)

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

        if isinstance(fund_data, dict):
            detail_obj["fundamental_data"] = merge_without_overwrite(detail_obj.get("fundamental_data") or {}, fund_data)
        if isinstance(chip_data, dict):
            detail_obj["chip_data"] = merge_without_overwrite(detail_obj.get("chip_data") or {}, chip_data)

        if detail_path.exists():
            try:
                old_detail = json.loads(detail_path.read_text(encoding="utf-8"))
                detail_obj = merge_without_overwrite(old_detail, detail_obj)
            except Exception:
                pass

        write_json(detail_path, detail_obj)
        batch_rows.append(row_obj)

        is_fund_ok = bool(fund_data.get("complete", False)) or str(sym).startswith("00")
        is_chip_ok = bool(chip_data.get("complete", True))
        has_missing_msg = bool(detail_obj.get("partial_missing"))
        batch_state[sym] = {
            **st_entry,
            "trade_date": latest_trade_date,
            "updated_at": now_str,
            phase_key: True,
            "complete": bool(is_fund_ok and is_chip_ok and not has_missing_msg),
        }

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

    print(
        f"[完成:{effective_phase}] 批次更新 ({latest_trade_date}) | 本批更新={len(batch_rows)} "
        f"(同日已齊全跳過={already_complete_cnt}, 略過={skipped_cnt}, "
        f"本批高分={batch_high_score_cnt}, 本批排除={batch_excluded_cnt}) | "
        f"累計總檔數={len(merged_rows)} (累計高分={total_high_score_cnt}, 累計排除={total_excluded_cnt})"
    )


if __name__ == "__main__":
    main()[cite: 13]