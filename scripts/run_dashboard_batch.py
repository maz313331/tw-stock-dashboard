import argparse
import datetime as dt
import json
from pathlib import Path
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


def fetch_twse_market_stats() -> dict:
    """
    嘗試從 TWSE 公開資料取得當日三大法人買賣超（億元）與上市漲跌家數。
    抓不到時回傳空字典，由呼叫端安全沿用既有數值。
    """
    result = {}

    # 1. 三大法人買賣超日報
    try:
        url_inst = "https://openapi.twse.com.tw/v1/fund/BFI82U"
        resp_inst = requests.get(url_inst, timeout=15)
        if resp_inst.ok:
            rows = resp_inst.json()
            if isinstance(rows, list) and rows:
                foreign_yi = 0.0
                trust_yi = 0.0
                dealer_yi = 0.0
                for r in rows:
                    name = str(r.get("單位名稱") or r.get("Name") or "")
                    diff_raw = (
                        r.get("買賣差額")
                        or r.get("Difference")
                        or r.get("NetBuySell")
                        or 0
                    )
                    val_yi = float(str(diff_raw).replace(",", "").strip() or 0) / 1e8
                    if "外資" in name or "Foreign" in name:
                        foreign_yi += val_yi
                    elif "投信" in name or "Trust" in name:
                        trust_yi += val_yi
                    elif "自營" in name or "Dealer" in name:
                        dealer_yi += val_yi
                total_yi = round(foreign_yi + trust_yi + dealer_yi, 1)
                if total_yi != 0 or foreign_yi != 0 or trust_yi != 0:
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
        resp_stock = requests.get(url_stock, timeout=20)
        if resp_stock.ok:
            rows = resp_stock.json()
            if isinstance(rows, list) and rows:
                adv = 0
                dec = 0
                for r in rows:
                    chg_raw = str(r.get("Change") or r.get("漲跌") or "").strip()
                    if not chg_raw:
                        continue
                    if chg_raw.startswith("+") or "漲" in chg_raw:
                        adv += 1
                    elif chg_raw.startswith("-") or "跌" in chg_raw:
                        dec += 1
                    else:
                        try:
                            val = float(chg_raw.replace(",", ""))
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
    root: Path, pool: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """只保留尚未存在於 data/summary.json 的標的，方便回頭補抓略過清單。"""
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
    return [(sym, name, mkt) for (sym, name, mkt) in pool if sym not in existing_syms]


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
    將本批次產出的股票列合併進既有 summary.json（依 symbol 更新或新增），
    確保分批執行時先前累積的股票不會被覆蓋掉。
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
            by_symbol[sym] = r

    return sorted(by_symbol.values(), key=lambda r: str(r.get("symbol") or ""))


def main():
    parser = argparse.ArgumentParser(description="台股盤勢與多維度評分批次更新")
    parser.add_argument("--offset", type=int, default=0, help="從清單第幾筆開始處理（預設 0）")
    parser.add_argument("--limit", type=int, default=250, help="單次最多處理幾檔（預設 250；設 0 代表全量）")
    parser.add_argument("--sleep", type=float, default=0.1, help="每檔處理間隔秒數，降低限流風險")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="僅針對 data/stock_list_common.json 中尚未寫入 summary.json 的標的補抓",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="不合併既有 summary.json，改以本批次結果直接覆蓋",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    market_path = root / "data" / "market.json"
    summary_path = root / "data" / "summary.json"

    prev_market = {}
    if market_path.exists():
        try:
            prev_market = json.loads(market_path.read_text(encoding="utf-8"))
        except Exception:
            prev_market = {}

    twii_df = fetch_yf_history("^TWII", period="6mo", limit=120, retries=1, retry_delay=0.5)
    s2330_df = fetch_yf_history("2330.TW", period="6mo", limit=120, retries=1, retry_delay=0.5)

    if s2330_df.empty or len(s2330_df) < 2:
        raise RuntimeError("無法取得 2330.TW 歷史日線")

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
        history_out = [
            {
                "date": str(row["date"]),
                "close": round(float(row["close"]), 2),
                "institutional_net": 0.0,
            }
            for _, row in twii_df.tail(120).iterrows()
        ]
    else:
        print("[警告] ^TWII 暫時無資料，沿用既有大盤指數與走勢紀錄，繼續更新個股與法人統計")
        latest_trade_date = str(s2330_df.iloc[-1]["date"])
        tw_index_obj = prev_market.get(
            "tw_index",
            {"close": 0.0, "change_pct": 0.0, "ma_status": "均線下方震盪"},
        )
        history_out = prev_market.get("history", [])

    market_stats = fetch_twse_market_stats()
    inst_flow = prev_market.get(
        "institutional_flow",
        {"foreign_net": 0.0, "trust_net": 0.0, "dealer_net": 0.0},
    )
    if "institutional_flow" in market_stats:
        inst_flow = market_stats["institutional_flow"]

    if args.only_missing:
        all_pool = load_target_symbols(root, offset=0, limit=0)
        targets = filter_missing_symbols(root, all_pool)
        if args.offset > 0:
            targets = targets[args.offset :]
        if args.limit and args.limit > 0:
            targets = targets[: args.limit]
        print(f"[補抓模式] 偵測到尚未入庫普通股共 {len(targets)} 檔：{[t[0] for t in targets]}")
    else:
        targets = load_target_symbols(root, offset=args.offset, limit=args.limit)

    batch_rows = []
    batch_excluded_cnt = 0
    batch_high_score_cnt = 0
    skipped_cnt = 0

    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    updated_label = f"{now_str}（行情日 {latest_trade_date}）"

    for idx, (sym, name, mkt) in enumerate(targets):
        if args.only_missing or (idx + 1) % 25 == 0 or idx == 0:
            print(f"[進度] {idx + 1}/{len(targets)} -> {sym} {name} ({mkt})")

        s_df = fetch_stock_history_with_market(sym, mkt, s2330_df)
        if s_df is None or s_df.empty or len(s_df) < 2:
            skipped_cnt += 1
            print(f"[略過] {sym} {name} ({mkt}) 暫時無法取得日線行情")
            continue

        fund_data = fetch_fundamental_data(sym)
        chip_data = fetch_chip_data(sym, stock_df=s_df)

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

        write_json(root / "data" / "details" / f"{sym}.json", detail_obj)
        batch_rows.append(row_obj)

        if row_obj.get("excluded"):
            batch_excluded_cnt += 1
        elif row_obj.get("total_score", 0) >= 22:
            batch_high_score_cnt += 1

        if args.sleep > 0 and idx < len(targets) - 1:
            time.sleep(args.sleep)

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
    market_out["updated_at"] = updated_label
    market_out["tw_index"] = tw_index_obj
    market_out["institutional_flow"] = inst_flow
    market_out["market_breadth"] = breadth
    market_out["history"] = history_out
    write_json(market_path, market_out)

    print(
        f"[完成] 批次更新 ({latest_trade_date}) | 本批成功={len(batch_rows)} (略過={skipped_cnt}, "
        f"本批高分={batch_high_score_cnt}, 本批排除={batch_excluded_cnt}) | "
        f"累計總檔數={len(merged_rows)} (累計高分={total_high_score_cnt}, 累計排除={total_excluded_cnt})"
    )


if __name__ == "__main__":
    main()