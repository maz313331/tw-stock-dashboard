import datetime as dt
import json
import os
from pathlib import Path
import time
import pandas as pd
import requests

FUNDAMENTAL_LABELS = [
    "本益比大於等於 10",
    "股價淨值比大於等於 0.5",
    "現金股利殖利率大於 3%",
    "月營收創 10 個月以上新高",
    "月營收 MoM 大於 0",
    "最近一季營業淨利大於 0",
    "最近一季稅後淨利大於 0",
    "最近一季每股盈餘大於 1",
    "最新年度 ROA 大於等於 5",
    "最新年度 ROE 大於等於 8",
]

# 快取目錄與有效期設定（天數）
ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "data" / "cache" / "fundamental"
REV_CACHE_DAYS = 5       # 月營收快取 5 天
FIN_CACHE_DAYS = 14      # 季報與資產負債表快取 14 天


def _load_stock_cache(stock_id: str) -> dict:
    cache_file = CACHE_DIR / f"{stock_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_stock_cache(stock_id: str, cache_data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"{stock_id}.json"
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _is_cache_valid(updated_str: str | None, valid_days: int) -> bool:
    if not updated_str:
        return False
    try:
        cached_date = dt.date.fromisoformat(updated_str)
        return (dt.date.today() - cached_date).days < valid_days
    except ValueError:
        return False


def _finmind_get(dataset: str, stock_id: str, start_date: str, retries: int = 3) -> pd.DataFrame:
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
    }
    token = os.getenv("FINMIND_TOKEN", "").strip()
    if token:
        params["token"] = token

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=12)
            if resp.status_code == 429:
                wait_sec = 1.5 * (attempt + 1)
                if attempt < retries:
                    print(f"[限流] {stock_id} {dataset} 觸發 429，等待 {wait_sec:.1f}s 後重試 ({attempt + 1}/{retries})")
                    time.sleep(wait_sec)
                    continue
                print(f"[警告] {stock_id} {dataset} 多次觸發 429 限流，略過本次抓取")
                return pd.DataFrame()
            if resp.status_code >= 500:
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return pd.DataFrame()
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if rows:
                return pd.DataFrame(rows)
            return pd.DataFrame()
        except Exception as exc:
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
            else:
                print(f"[警告] {stock_id} {dataset} 請求異常: {exc}")
                return pd.DataFrame()
    return pd.DataFrame()


def _pick_first_value(row: pd.Series, cols: pd.Index, candidates: list[str]) -> float | None:
    for c in candidates:
        if c in cols and pd.notna(row.get(c)):
            try:
                return float(row[c])
            except (TypeError, ValueError):
                pass

    def _norm(s: str) -> str:
        return (
            str(s)
            .lower()
            .replace("_", "")
            .replace(" ", "")
            .replace("（", "(")
            .replace("）", ")")
        )

    norm_map = {_norm(col): col for col in cols}
    for c in candidates:
        key = _norm(c)
        if key in norm_map:
            col = norm_map[key]
            if pd.notna(row.get(col)):
                try:
                    return float(row[col])
                except (TypeError, ValueError):
                    pass
    return None


def fetch_fundamental_data(stock_id: str, force_refresh: bool = False) -> dict:
    today = dt.date.today()
    today_str = today.isoformat()
    per_start = (today - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    rev_start = (today - dt.timedelta(days=600)).strftime("%Y-%m-%d")
    fin_start = (today - dt.timedelta(days=950)).strftime("%Y-%m-%d")

    cache = {} if force_refresh else _load_stock_cache(stock_id)
    cache_dirty = False

    missing = []
    pe = None
    pb = None
    div_yield = None
    rev_new_10m = None
    rev_mom_pct = None
    op_income = None
    net_income = None
    eps = None
    roa_pct = None
    roe_pct = None

    # 1. 估值（本益比、股價淨值比、殖利率 - 每日更新）
    try:
        df_per = _finmind_get("TaiwanStockPER", stock_id, per_start)
        if not df_per.empty:
            df_per = df_per.sort_values("date")
            last_per = df_per.iloc[-1]
            pe = _pick_first_value(last_per, df_per.columns, ["PER", "pe_ratio"])
            pb = _pick_first_value(last_per, df_per.columns, ["PBR", "pb_ratio"])
            div_yield = _pick_first_value(
                last_per,
                df_per.columns,
                ["dividend_yield", "DividendYield", "dividend_yield_rate"],
            )
    except Exception:
        pass

    if any(x is None for x in [pe, pb, div_yield]):
        missing.append("估值資料(PE/PB/殖利率)部分缺漏")

    # 2. 月營收（優先讀取 5 天內有效快取）
    rev_cache = cache.get("revenue", {})
    if (
        not force_refresh
        and _is_cache_valid(rev_cache.get("updated_at"), REV_CACHE_DAYS)
        and rev_cache.get("rev_new_10m") is not None
        and rev_cache.get("rev_mom_pct") is not None
    ):
        rev_new_10m = rev_cache["rev_new_10m"]
        rev_mom_pct = rev_cache["rev_mom_pct"]
    else:
        try:
            df_rev = _finmind_get("TaiwanStockMonthRevenue", stock_id, rev_start)
            if not df_rev.empty and "revenue" in df_rev.columns:
                df_rev["ym"] = (
                    pd.to_numeric(df_rev["revenue_year"], errors="coerce") * 100
                    + pd.to_numeric(df_rev["revenue_month"], errors="coerce")
                )
                df_rev["rev_val"] = pd.to_numeric(df_rev["revenue"], errors="coerce")
                df_rev = df_rev.dropna(subset=["ym", "rev_val"]).sort_values("ym")

                if len(df_rev) >= 11:
                    curr_rev = float(df_rev.iloc[-1]["rev_val"])
                    prev_10_max = float(df_rev.iloc[-11:-1]["rev_val"].max())
                    prev_1_rev = float(df_rev.iloc[-2]["rev_val"])
                    rev_new_10m = bool(curr_rev > prev_10_max)
                    if prev_1_rev and prev_1_rev != 0:
                        rev_mom_pct = ((curr_rev - prev_1_rev) / prev_1_rev) * 100.0

            if rev_new_10m is not None and rev_mom_pct is not None:
                cache["revenue"] = {
                    "updated_at": today_str,
                    "rev_new_10m": rev_new_10m,
                    "rev_mom_pct": rev_mom_pct,
                }
                cache_dirty = True
        except Exception:
            pass

    if rev_new_10m is None or rev_mom_pct is None:
        missing.append("月營收資料缺漏或尚未公布")

    # 3. 季報與資產負債表（優先讀取 14 天內有效快取）
    fin_cache = cache.get("financial", {})
    if (
        not force_refresh
        and _is_cache_valid(fin_cache.get("updated_at"), FIN_CACHE_DAYS)
        and all(
            fin_cache.get(k) is not None
            for k in ["op_income", "net_income", "eps", "roa_pct", "roe_pct"]
        )
    ):
        op_income = fin_cache["op_income"]
        net_income = fin_cache["net_income"]
        eps = fin_cache["eps"]
        roa_pct = fin_cache["roa_pct"]
        roe_pct = fin_cache["roe_pct"]
    else:
        piv_fin = pd.DataFrame()
        piv_bs = pd.DataFrame()

        try:
            df_fin = _finmind_get("TaiwanStockFinancialStatements", stock_id, fin_start)
            if not df_fin.empty and {"date", "type", "value"}.issubset(df_fin.columns):
                df_fin["val"] = pd.to_numeric(df_fin["value"], errors="coerce")
                piv_fin = df_fin.pivot_table(
                    index="date", columns="type", values="val", aggfunc="last"
                ).sort_index()
        except Exception:
            pass

        try:
            df_bs = _finmind_get("TaiwanStockBalanceSheet", stock_id, fin_start)
            if not df_bs.empty and {"date", "type", "value"}.issubset(df_bs.columns):
                df_bs["val"] = pd.to_numeric(df_bs["value"], errors="coerce")
                piv_bs = df_bs.pivot_table(
                    index="date", columns="type", values="val", aggfunc="last"
                ).sort_index()
        except Exception:
            pass

        op_candidates = [
            "OperatingIncome",
            "OperatingProfitLoss",
            "TotalOperatingIncome",
            "營業利益(損失)",
            "營業淨利(損)",
        ]
        net_candidates = [
            "IncomeAfterTaxes",
            "ProfitLossAfterTax",
            "NetIncome",
            "ProfitLoss",
            "IncomeContinuingOperationsAfterTax",
            "本期淨利(淨損)",
            "稅後淨利(淨損)",
        ]
        eps_candidates = [
            "EPS",
            "EarningsPerShare",
            "BasicEarningsPerShare",
            "基本每股盈餘(元)",
            "基本每股盈餘",
        ]
        assets_candidates = [
            "TotalAssets",
            "Assets",
            "資產總計",
        ]
        equity_candidates = [
            "Equity",
            "TotalEquity",
            "EquityAttributableToOwnersOfParent",
            "權益總計",
            "母公司業主權益合計",
        ]

        if not piv_fin.empty:
            latest_q = piv_fin.iloc[-1]
            op_income = _pick_first_value(latest_q, piv_fin.columns, op_candidates)
            net_income = _pick_first_value(latest_q, piv_fin.columns, net_candidates)
            eps = _pick_first_value(latest_q, piv_fin.columns, eps_candidates)

            net_col = None
            norm_fin_cols = {
                str(c).lower().replace("_", "").replace(" ", "").replace("（", "(").replace("）", ")"): c
                for c in piv_fin.columns
            }
            for c in net_candidates:
                key = c.lower().replace("_", "").replace(" ", "").replace("（", "(").replace("）", ")")
                if key in norm_fin_cols:
                    net_col = norm_fin_cols[key]
                    break

            ttm_net = None
            if net_col and len(piv_fin) >= 4:
                ttm_net = float(pd.to_numeric(piv_fin[net_col], errors="coerce").tail(4).sum())
            elif net_income is not None:
                ttm_net = net_income * 4.0

            total_assets = None
            total_equity = None

            if not piv_bs.empty:
                latest_bs = piv_bs.iloc[-1]
                total_assets = _pick_first_value(latest_bs, piv_bs.columns, assets_candidates)
                total_equity = _pick_first_value(latest_bs, piv_bs.columns, equity_candidates)
                if total_equity is None:
                    parent_eq = _pick_first_value(
                        latest_bs, piv_bs.columns, ["EquityAttributableToOwnersOfParent", "母公司業主權益合計"]
                    )
                    nci = (
                        _pick_first_value(latest_bs, piv_bs.columns, ["NoncontrollingInterests", "非控制權益"])
                        or 0.0
                    )
                    if parent_eq is not None:
                        total_equity = parent_eq + nci

            if total_assets is None:
                total_assets = _pick_first_value(latest_q, piv_fin.columns, assets_candidates)
            if total_equity is None:
                total_equity = _pick_first_value(latest_q, piv_fin.columns, equity_candidates)

            if ttm_net is not None and total_assets and total_assets > 0:
                roa_pct = (ttm_net / total_assets) * 100.0
            if ttm_net is not None and total_equity and total_equity > 0:
                roe_pct = (ttm_net / total_equity) * 100.0

        if all(x is not None for x in [op_income, net_income, eps, roa_pct, roe_pct]):
            cache["financial"] = {
                "updated_at": today_str,
                "op_income": op_income,
                "net_income": net_income,
                "eps": eps,
                "roa_pct": roa_pct,
                "roe_pct": roe_pct,
            }
            cache_dirty = True

    if any(x is None for x in [op_income, net_income, eps, roa_pct, roe_pct]):
        missing.append("財報/ROA/ROE欄位尚未公布或缺漏")

    if cache_dirty:
        _save_stock_cache(stock_id, cache)

    return {
        "complete": len(missing) == 0,
        "missing_reasons": missing,
        "metrics": {
            "pe": pe,
            "pb": pb,
            "dividend_yield": div_yield,
            "rev_new_10m": rev_new_10m,
            "rev_mom_pct": rev_mom_pct,
            "op_income": op_income,
            "net_income": net_income,
            "eps": eps,
            "roa_pct": roa_pct,
            "roe_pct": roe_pct,
        },
    }


def evaluate_fundamental_10(fund: dict) -> tuple[list[dict], int]:
    m = fund.get("metrics", {})

    def _chk(label: str, cond: bool | None, val_str: str) -> dict:
        return {
            "label": label,
            "passed": bool(cond) if cond is not None else False,
            "value": val_str,
        }

    pe = m.get("pe")
    pb = m.get("pb")
    dy = m.get("dividend_yield")
    rev10 = m.get("rev_new_10m")
    mom = m.get("rev_mom_pct")
    op = m.get("op_income")
    ni = m.get("net_income")
    eps = m.get("eps")
    roa = m.get("roa_pct")
    roe = m.get("roe_pct")

    checks = [
        _chk("本益比大於等於 10", (pe >= 10.0) if pe is not None else None, f"{pe:.1f}" if pe is not None else "缺漏"),
        _chk("股價淨值比大於等於 0.5", (pb >= 0.5) if pb is not None else None, f"{pb:.2f}" if pb is not None else "缺漏"),
        _chk("現金股利殖利率大於 3%", (dy > 3.0) if dy is not None else None, f"{dy:.2f}%" if dy is not None else "缺漏"),
        _chk("月營收創 10 個月以上新高", rev10, ("創10月新高" if rev10 else "未創高") if rev10 is not None else "缺漏"),
        _chk("月營收 MoM 大於 0", (mom > 0.0) if mom is not None else None, f"{mom:+.2f}%" if mom is not None else "缺漏"),
        _chk("最近一季營業淨利大於 0", (op > 0.0) if op is not None else None, "正值" if (op is not None and op > 0) else ("非正值" if op is not None else "缺漏")),
        _chk("最近一季稅後淨利大於 0", (ni > 0.0) if ni is not None else None, "正值" if (ni is not None and ni > 0) else ("非正值" if ni is not None else "缺漏")),
        _chk("最近一季每股盈餘大於 1", (eps > 1.0) if eps is not None else None, f"{eps:.2f} 元" if eps is not None else "缺漏"),
        _chk("最新年度 ROA 大於等於 5", (roa >= 5.0) if roa is not None else None, f"{roa:.1f}%" if roa is not None else "缺漏"),
        _chk("最新年度 ROE 大於等於 8", (roe >= 8.0) if roe is not None else None, f"{roe:.1f}%" if roe is not None else "缺漏"),
    ]
    score = sum(1 for item in checks if item["passed"])
    return checks, score