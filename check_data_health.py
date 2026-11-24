import argparse
import glob
import json
import os


def scan_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        for bad in ("NaN", "Infinity", "-Infinity"):
            if bad in raw:
                return False, f"contains_non_standard_float({bad})", None
        data = json.loads(raw)
        return True, "ok", data
    except Exception as e:
        return False, f"json_error({e})", None


def check_zeros_and_missing(summary_data):
    items = (
        summary_data
        if isinstance(summary_data, list)
        else summary_data.get("items", [])
        if isinstance(summary_data, dict)
        else []
    )
    zero_score = 0
    missing_flagged = 0
    for row in items:
        if not isinstance(row, dict):
            continue
        score = row.get("score")
        if score == 0 or score == 0.0:
            zero_score += 1
        if row.get("missing") or row.get("is_missing") or row.get("partial_missing"):
            missing_flagged += 1
    return len(items), zero_score, missing_flagged


def main():
    parser = argparse.ArgumentParser(description="台股 Dashboard data/ 多用途健檢工具")
    parser.add_argument("--data-dir", default="data", help="data 目錄路徑（預設: data）")
    parser.add_argument("--symbol", default="", help="指定抽查個股代號，例如 1101 或 9958")
    args = parser.parse_args()

    json_files = sorted(glob.glob(os.path.join(args.data_dir, "**", "*.json"), recursive=True))
    print(f"共找到 {len(json_files)} 個 JSON 檔案")

    bad_files = []
    for fp in json_files:
        ok, reason, _ = scan_json_file(fp)
        if not ok:
            bad_files.append((fp, reason))

    print(f"JSON 語法與數值合法性檢查：異常 {len(bad_files)} 個")
    for fp, reason in bad_files[:20]:
        print(f"  - [異常] {fp}: {reason}")

    summary_path = os.path.join(args.data_dir, "summary.json")
    if os.path.exists(summary_path):
        ok, reason, sdata = scan_json_file(summary_path)
        if ok and sdata is not None:
            total, zeros, missing = check_zeros_and_missing(sdata)
            print(f"summary.json 摘要：總筆數={total} | score為0筆數={zeros} | 含缺漏標記={missing}")
        else:
            print(f"summary.json 讀取失敗：{reason}")

    if args.symbol:
        detail_path = os.path.join(args.data_dir, "details", f"{args.symbol}.json")
        if os.path.exists(detail_path):
            ok, reason, ddata = scan_json_file(detail_path)
            print(f"明細抽查 [{args.symbol}] ({detail_path}) -> ok={ok}, status={reason}")
            if ok and isinstance(ddata, dict):
                print(f"  頂層欄位: {list(ddata.keys())[:12]}")
        else:
            print(f"找不到明細檔：{detail_path}")


if __name__ == "__main__":
    main()
