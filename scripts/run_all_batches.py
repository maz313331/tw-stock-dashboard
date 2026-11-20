import argparse
import datetime as dt
import json
from pathlib import Path
import random
import subprocess
import sys
import time


def get_total_common_stocks(root: Path) -> int:
    common_path = root / "data" / "stock_list_common.json"
    if common_path.exists():
        try:
            data = json.loads(common_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return len(data)
        except Exception:
            pass
    return 1990


def run_single_batch(
    python_exe: str,
    batch_script: Path,
    root: Path,
    offset: int,
    limit: int,
    sleep_sec: float,
) -> int:
    jittered_sleep = max(0.05, round(sleep_sec + random.uniform(-0.05, 0.15), 3))
    cmd = [
        python_exe,
        "-u",
        str(batch_script),
        "--offset",
        str(offset),
        "--limit",
        str(limit),
        "--sleep",
        str(jittered_sleep),
    ]
    proc = subprocess.run(cmd, cwd=str(root), check=False)
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="全市場普通股自動分批排程器（短延遲版）")
    parser.add_argument("--start-offset", type=int, default=0, help="起始偏移量（預設 0，從第一筆開始）")
    parser.add_argument("--batch-size", type=int, default=250, help="每批處理檔數（預設 250）")
    parser.add_argument("--min-wait", type=int, default=10, help="批間最短等待秒數（預設 10 秒）")
    parser.add_argument("--max-wait", type=int, default=25, help="批間最長等待秒數（預設 25 秒，5 分鐘內）")
    parser.add_argument("--sleep", type=float, default=0.15, help="單批內每檔基礎間隔秒數（預設 0.15）")
    args = parser.parse_args()

    if args.max_wait > 300:
        print(f"[提示] max-wait={args.max_wait} 超過 300 秒（5 分鐘），已自動上限調整為 300 秒。")
        args.max_wait = 300

    root = Path(__file__).resolve().parent.parent
    batch_script = root / "scripts" / "run_dashboard_batch.py"
    total_stocks = get_total_common_stocks(root)

    offsets = list(range(args.start_offset, total_stocks, args.batch_size))
    if not offsets:
        print(f"[提示] start-offset={args.start_offset} 已大於或等於總檔數 {total_stocks}，無需執行。")
        return

    low_w = min(args.min_wait, args.max_wait)
    high_w = max(args.min_wait, args.max_wait)

    print(
        f"[排程啟動] 普通股總數={total_stocks} | 起始offset={args.start_offset} | "
        f"每批={args.batch_size} | 共 {len(offsets)} 批 | 批間休息={low_w}~{high_w}秒（5分鐘內）"
    )

    for idx, offset in enumerate(offsets):
        end_idx = min(offset + args.batch_size, total_stocks)
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"\n==================== [{now_str}] 第 {idx + 1}/{len(offsets)} 批 "
            f"(offset={offset} ~ {end_idx - 1}) ===================="
        )

        try:
            rc = run_single_batch(
                sys.executable, batch_script, root, offset, args.batch_size, args.sleep
            )
            if rc != 0:
                print(f"[警告] 第 {idx + 1} 批回傳碼={rc}，休息 8 秒後重試 1 次...")
                time.sleep(8)
                rc2 = run_single_batch(
                    sys.executable, batch_script, root, offset, args.batch_size, args.sleep + 0.1
                )
                if rc2 != 0:
                    print(f"[警告] 第 {idx + 1} 批重試仍回傳={rc2}，先繼續下一批。")
        except KeyboardInterrupt:
            print("\n[中斷] 使用者手動停止自動排程。")
            return
        except Exception as exc:
            print(f"[錯誤] 執行第 {idx + 1} 批時發生例外: {exc}")

        if idx < len(offsets) - 1:
            wait_sec = random.randint(low_w, high_w)
            resume_at = (dt.datetime.now() + dt.timedelta(seconds=wait_sec)).strftime("%H:%M:%S")
            print(
                f"[休息] 第 {idx + 1} 批完成，休息 {wait_sec} 秒（約 {wait_sec / 60:.1f} 分鐘），"
                f"預計於 {resume_at} 開始下一批..."
            )
            try:
                time.sleep(wait_sec)
            except KeyboardInterrupt:
                print("\n[中斷] 休息期間手動停止自動排程。")
                return

    print(f"\n[全部完成] 所有排程批次（至 offset {offsets[-1]}）已執行完畢！")


if __name__ == "__main__":
    main()