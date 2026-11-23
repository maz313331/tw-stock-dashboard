import json
from pathlib import Path
import requests


def _write_json_atomic(target: Path, payload: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def fetch_twse_stocks() -> list[tuple[str, str, str]]:
    """從 TWSE OpenAPI 取得上市股票清單 (代號, 名稱, 市場別)"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    try:
        resp = requests.get(url, timeout=20)
        if resp.ok:
            rows = resp.json()
            if isinstance(rows, list):
                out = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    code = str(r.get("公司代號") or "").strip()
                    name = str(r.get("公司簡稱") or "").strip()
                    if code and name:
                        out.append((code, name, "TW"))
                if out:
                    return out
    except Exception as exc:
        print(f"[警告] TWSE 上市清單抓取失敗: {exc}")
    return []


def fetch_tpex_stocks() -> list[tuple[str, str, str]]:
    """從 TPEx OpenAPI 取得上櫃股票清單 (代號, 名稱, 市場別)"""
    url = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
    try:
        resp = requests.get(url, timeout=20)
        if resp.ok:
            rows = resp.json()
            if isinstance(rows, list):
                out = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    code = str(r.get("SecuritiesCompanyCode") or r.get("公司代號") or "").strip()
                    name = str(r.get("CompanyAbbreviation") or r.get("公司簡稱") or "").strip()
                    if code and name:
                        out.append((code, name, "TWO"))
                if out:
                    return out
    except Exception as exc:
        print(f"[警告] TPEx 上櫃清單抓取失敗: {exc}")
    return []


def fetch_etf_list() -> list[tuple[str, str, str]]:
    """從 TWSE 每日收盤行情篩出 00 開頭的 ETF 清單"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        resp = requests.get(url, timeout=20)
        if resp.ok:
            rows = resp.json()
            if isinstance(rows, list):
                out = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    code = str(r.get("Code") or "").strip()
                    name = str(r.get("Name") or "").strip()
                    if code.startswith("00") and name:
                        out.append((code, name, "TW"))
                return out
    except Exception as exc:
        print(f"[警告] ETF 清單抓取失敗: {exc}")
    return []


def main():
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    twse_list = fetch_twse_stocks()
    tpex_list = fetch_tpex_stocks()
    etf_list = fetch_etf_list()

    common_stocks = []
    seen = set()
    for code, name, mkt in twse_list + tpex_list:
        if len(code) == 4 and code.isdigit() and not code.startswith("00"):
            if code not in seen:
                seen.add(code)
                common_stocks.append({"symbol": code, "name": name, "market": mkt})

    common_stocks.sort(key=lambda x: x["symbol"])

    etf_seen = set()
    etf_items = []
    for code, name, mkt in sorted(etf_list, key=lambda x: x[0]):
        if code not in etf_seen:
            etf_seen.add(code)
            etf_items.append({"symbol": code, "name": name, "market": mkt})

    if common_stocks:
        _write_json_atomic(data_dir / "stock_list_common.json", common_stocks)
    if etf_items:
        _write_json_atomic(data_dir / "stock_list_etf.json", etf_items)

    print(
        f"[完成] 上市普通股={len(twse_list)} 筆 | 上櫃普通股={len(tpex_list)} 筆 | "
        f"合併普通股池={len(common_stocks)} 檔 | ETF 獨立清單={len(etf_items)} 檔"
    )


if __name__ == "__main__":
    main()