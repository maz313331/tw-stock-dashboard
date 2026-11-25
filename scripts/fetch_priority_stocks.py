import argparse
import json
import random
import time
from pathlib import Path


def load_priority_items(priority_path: Path) -> list[dict]:
    """讀取 priority_stocks.json 的 items 清單。"""
    with open(priority_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        items = data.get("items", [])
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("priority_stocks.json 格式不符預期（需為 dict 或 list）")

    seen = set()
    unique_items = []
    for item in items:
        sym = str(item.get("symbol", "")).strip()
        if sym and sym not in seen:
            seen.add(sym)
            unique_items.append(item)
    return unique_items


def is_record_complete(rec: dict) -> bool:
    """判斷既有紀錄是否已完整，可依實際欄位調整。"""
    if not rec or rec.get("status") != "ok":
        return False
    return True


def fetch_symbol_data(item: dict) -> dict:
    """
    【請替換為您的實際抓股 / 算分邏輯】
    輸入：priority_stocks.json 中的單筆 item
    輸出：該標的的抓取或分析結果 dict
    """
    return {
        "symbol": item.get("symbol"),
        "ticker": item.get("ticker"),
        "market": item.get("market"),
        "name": item.get("name", item.get("symbol")),
        "status": "ok",
    }


def main():
    parser = argparse.ArgumentParser(description="全自動分段跑完 priority_stocks.json")
    parser.add_argument("--input", default="priority_stocks.json", help="輸入清單 JSON 路徑")
    parser.add_argument("--out-dir", default="local_output", help="輸出與進度目錄")
    parser.add_argument("--batch-size", type=int, default=25, help="每批檔數（大量可設 100~200）")
    parser.add_argument("--sleep", type=float, default=None, help="固定每檔間隔秒數")
    parser.add_argument("--min-wait", type=float, default=0.8, help="隨機等待下限（秒）")
    parser.add_argument("--max-wait", type=float, default=2.0, help="隨機等待上限（秒）")
    parser.add_argument("--batch-pause", type=float, default=5.0, help="每批間自動休息秒數")
    parser.add_argument("--stage-batches", type=int, default=4, help="每幾批為一個大段（0 表示不分大段）")
    parser.add_argument("--stage-pause", type=float, default=30.0, help="每個大段結束後的自動冷卻秒數")
    parser.add_argument("--max-retries", type=int, default=3, help="單檔重試次數")
    parser.add_argument("--only-missing", action="store_true", help="只跑尚未完成或欄位不完整的標的")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "progress.json"
    merged_path = out_dir / "summary_local.json"

    all_items = load_priority_items(Path(args.input))

    if progress_path.exists():
        with open(progress_path, "r", encoding="utf-8") as f:
            progress = json.load(f)
    else:
        progress = {"records": {}, "errors": {}}

    records = progress.get("records", {})
    errors = progress.get("errors", {})

    targets = (
        [it for it in all_items if not is_record_complete(records.get(str(it["symbol"])))]
        if args.only_missing
        else all_items
    )

    total_targets = len(targets)
    print(f"清單總數：{len(all_items)}｜本次待跑：{total_targets}｜全自動分段執行中")

    if total_targets == 0:
        print("沒有需要補跑的標的。")
        return

    total_batches = (total_targets + args.batch_size - 1) // args.batch_size

    try:
        for b_idx in range(total_batches):
            batch_num = b_idx + 1
            chunk = targets[b_idx * args.batch_size : batch_num * args.batch_size]
            batch_rows = []
            print(f"\n=== [批次 {batch_num}/{total_batches}] 處理 {len(chunk)} 檔 ===")

            for item in chunk:
                sym = str(item["symbol"])
                ok = False
                last_err = ""

                for attempt in range(1, args.max_retries + 1):
                    try:
                        res = fetch_symbol_data(item)
                        records[sym] = res
                        errors.pop(sym, None)
                        batch_rows.append(res)
                        ok = True
                        break
                    except Exception as exc:
                        last_err = f"{type(exc).__name__}: {exc}"
                        backoff = args.min_wait * (2 ** (attempt - 1))
                        print(f"  [{sym}] 重試 {attempt}/{args.max_retries}（{last_err}），等 {backoff:.1f}s...")
                        time.sleep(backoff)

                if not ok:
                    errors[sym] = last_err
                    print(f"  [{sym}] 失敗已記錄：{last_err}")

                wait_sec = args.sleep if args.sleep is not None else random.uniform(args.min_wait, args.max_wait)
                time.sleep(max(0.0, wait_sec))

            # 每批自動存檔
            batch_file = out_dir / f"batch_{batch_num:03d}.json"
            with open(batch_file, "w", encoding="utf-8") as f:
                json.dump(batch_rows, f, ensure_ascii=False, indent=2)

            progress["records"] = records
            progress["errors"] = errors
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump(progress, f, ensure_ascii=False, indent=2)

            print(f"[批次 {batch_num}/{total_batches}] 已存檔 -> {batch_file}")

            # 判斷是否進入「大段自動冷卻」，否則走一般「批間休息」
            if batch_num < total_batches:
                if args.stage_batches > 0 and (batch_num % args.stage_batches == 0):
                    print(f"--- 已完成 {batch_num} 批，進入大段自動冷卻 {args.stage_pause:.1f} 秒後繼續 ---")
                    time.sleep(args.stage_pause)
                elif args.batch_pause > 0:
                    print(f"批間自動休息 {args.batch_pause:.1f} 秒...")
                    time.sleep(args.batch_pause)

    except KeyboardInterrupt:
        print("\n偵測到手動中斷（Ctrl+C），進度已存於 progress.json；下次加 --only-missing 即可接續。")

    merged_rows = [records[str(it["symbol"])] for it in all_items if str(it["symbol"]) in records]
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged_rows, f, ensure_ascii=False, indent=2)

    print(f"\n全數流程結束！已完成 {len(merged_rows)}/{len(all_items)} 檔，失敗 {len(errors)} 檔 -> {merged_path}")


if __name__ == "__main__":
    main()