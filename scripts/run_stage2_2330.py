# scripts/run_stage2_2330.py
import datetime as dt
import json
from pathlib import Path
from calc_scores import build_stock_detail, write_json
from fetch_chips import fetch_chip_data
from fetch_fundamental import fetch_fundamental_data
from fetch_sources import fetch_yf_history


def main():
    root = Path(__file__).resolve().parent.parent
    detail_path = root / "data" / "details" / "2330.json"
    summary_path = root / "data" / "summary.json"

    twii_df = fetch_yf_history("^TWII", period="6mo", limit=120)
    s2330_df = fetch_yf_history("2330.TW", period="6mo", limit=120)

    if s2330_df.empty or len(s2330_df) < 2:
        raise RuntimeError("無法取得 2330.TW 歷史日線")

    fund_data = fetch_fundamental_data("2330")
    chip_data = fetch_chip_data("2330", stock_df=s2330_df)

    latest_trade_date = str(s2330_df.iloc[-1]["date"])
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    updated_label = f"{now_str}（行情日 {latest_trade_date}）"

    detail_2330, summary_2330 = build_stock_detail(
        symbol="2330",
        name="台積電",
        updated_at=updated_label,
        stock_df=s2330_df,
        index_df=twii_df,
        fundamental_data=fund_data,
        chip_data=chip_data,
    )

    write_json(detail_path, detail_2330)

    existing_stocks = []
    if summary_path.exists():
        try:
            old = json.loads(summary_path.read_text(encoding="utf-8"))
            existing_stocks = [s for s in old.get("stocks", []) if s.get("symbol") != "2330"]
        except Exception:
            existing_stocks = []

    stocks_out = [summary_2330] + existing_stocks
    write_json(
        summary_path,
        {
            "updated_at": updated_label,
            "stocks": stocks_out,
        },
    )

    print(
        f"[完成] 2330 ({latest_trade_date}) | 收盤 {summary_2330['close']} ({summary_2330['change_pct']:+.2f}%) | "
        f"排除={detail_2330['excluded']} | 分數={detail_2330['total_score']}/30 | "
        f"原因={detail_2330['exclude_reason'] or '無'}"
    )


if __name__ == "__main__":
    main()