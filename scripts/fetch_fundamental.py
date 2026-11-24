import datetime as dt
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional
import requests

try:
    from fetch_tpex import fetch_tpex_stock_valuation
except Exception:
    try:
        from scripts.fetch_tpex import fetch_tpex_stock_valuation
    except Exception:
        fetch_tpex_stock_valuation = None

FUNDAMENTAL_LABELS: list[str] = [
    "本益比合理",
    "股價淨值比合理",
    "殖利率具吸引力",
    "月營收月增",
    "月營收年增",
    "營業利益為正",
    "本期淨利為正",
    "每股盈餘為正",
    "ROA 表現穩健",
    "ROE 表現穩健",
]


def _load_finmind_token() -> str:
    token = (os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_TOKEN") or "").strip()
    if token:
        return token

    for env_file in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    if k.strip() in {"FINMIND_TOKEN", "FINMIND_API_TOKEN"}:
                        val = v.strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
    return ""


def is_missing_value(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return math.isnan(val) or math.isinf(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return True
        return s.lower() in {"none", "null", "nan", "n/a", "na", "--", "-", "缺漏", "無資料"}
    if isinstance(val, (list, tuple, dict, set)):
        return len(val) == 0
    return False


def clean_numeric_or_none(val: Any, allow_zero: bool = True) -> Optional[float]:
    if is_missing_value(val):
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
    else:
        s = str(val).strip().replace(",", "").replace("%", "")
        if not s:
            return None
        try:
            num = float(s)
        except (ValueError, TypeError):
            return None

    if math.isnan(num) or math.isinf(num):
        return None
    if not allow_zero and num == 0.0:
        return None
    return num


class ApiQuotaGuard:
    def __init__(
        self,
        max_calls_per_hour: int = 580,
        min_interval_sec: float = 0.25,
        max_retries_on_429: int = 2,
        base_backoff_sec: float = 5.0,
    ):
        self.max_calls_per_hour = max(1, int(max_calls_per_hour))
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.max_retries_on_429 = max(0, int(max_retries_on_429))
        self.base_backoff_sec = max(0.5, float(base_backoff_sec))

        self.call_timestamps: list[float] = []
        self.last_request_at: Optional[float] = None
        self.total_calls: int = 0
        self.rate_limit_hits: int = 0
        self.circuit_open: bool = False
        self.circuit_reason: str = ""

    def _prune_old_calls(self, now: Optional[float] = None) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - 3600.0
        self.call_timestamps = [t for t in self.call_timestamps if t >= cutoff]

    def calls_in_last_hour(self) -> int:
        self._prune_old_calls()
        return len(self.call_timestamps)

    def remaining_hourly_quota(self) -> int:
        return max(0, self.max_calls_per_hour - self.calls_in_last_hour())

    def can_call(self) -> tuple[bool, str]:
        if self.circuit_open:
            return False, self.circuit_reason or "API 配額熔斷已開啟"
        if self.remaining_hourly_quota() <= 0:
            self.circuit_open = True
            self.circuit_reason = f"已達每小時請求安全上限 ({self.max_calls_per_hour}/hr)"
            return False, self.circuit_reason
        return True, ""

    def wait_before_call(self) -> None:
        if self.min_interval_sec <= 0:
            return
        if self.last_request_at is not None:
            elapsed = time.monotonic() - self.last_request_at
            remain = self.min_interval_sec - elapsed
            if remain > 0:
                time.sleep(remain)

    def record_call(self) -> None:
        now = time.monotonic()
        self.last_request_at = now
        self.call_timestamps.append(now)
        self.total_calls += 1

    @staticmethod
    def _is_rate_limited(status_code: Optional[int], payload: Any) -> bool:
        if status_code in (429, 402, 403):
            return True
        if isinstance(payload, dict):
            msg = str(payload.get("msg") or payload.get("message") or "").lower()
            status_txt = str(payload.get("status") or "").lower()
            if any(
                k in msg or k in status_txt
                for k in ("rate limit", "too many requests", "quota", "limit exceeded", "429")
            ):
                return True
        elif isinstance(payload, str):
            low = payload.lower()
            if any(k in low for k in ("rate limit", "too many requests", "quota exceeded", "429")):
                return True
        return False

    def execute_with_guard(
        self,
        request_fn: Callable[[], tuple[Optional[int], Any]],
    ) -> tuple[bool, Optional[int], Any, str]:
        allowed, reason = self.can_call()
        if not allowed:
            return False, None, None, reason

        last_status: Optional[int] = None
        last_payload: Any = None

        for attempt in range(self.max_retries_on_429 + 1):
            allowed, reason = self.can_call()
            if not allowed:
                return False, last_status, last_payload, reason

            self.wait_before_call()
            self.record_call()

            try:
                status_code, payload = request_fn()
            except Exception as exc:
                last_payload = str(exc)
                return False, last_status, last_payload, f"請求例外: {exc}"

            last_status = status_code
            last_payload = payload

            if not self._is_rate_limited(status_code, payload):
                return True, status_code, payload, ""

            self.rate_limit_hits += 1
            if attempt < self.max_retries_on_429:
                backoff = self.base_backoff_sec * (2 ** attempt)
                print(
                    f"[限流退避] 偵測到請求限制 (status={status_code})，"
                    f"{backoff:.1f}s 後進行第 {attempt + 1}/{self.max_retries_on_429} 次重試..."
                )
                time.sleep(backoff)
            else:
                self.circuit_open = True
                self.circuit_reason = f"連續觸發 API 限流 (status={status_code})，啟動熔斷保護"
                print(f"[熔斷啟動] {self.circuit_reason}")
                return False, status_code, payload, self.circuit_reason

        return False, last_status, last_payload, self.circuit_reason or "請求未完成"


class DataHealthTracker:
    def __init__(self, quota_guard: Optional[ApiQuotaGuard] = None):
        self.quota_guard = quota_guard
        self.missing_by_category: dict[str, int] = {}
        self.missing_by_reason: dict[str, int] = {}
        self.stock_missing_map: dict[str, list[str]] = {}
        self.source_health: dict[str, dict[str, Any]] = {}

    def check_response_health(
        self,
        source_name: str,
        status_code: Optional[int],
        payload: Any,
        rows_key: str = "data",
        min_rows: int = 1,
    ) -> tuple[bool, str]:
        entry = self.source_health.setdefault(
            source_name,
            {"ok_count": 0, "empty_count": 0, "error_count": 0, "last_reason": ""},
        )

        if status_code is not None and status_code >= 400:
            reason = f"{source_name} HTTP {status_code}"
            entry["error_count"] += 1
            entry["last_reason"] = reason
            self.record_reason(reason)
            return False, reason

        rows = None
        if isinstance(payload, dict):
            if rows_key in payload and isinstance(payload[rows_key], list):
                rows = payload[rows_key]
            elif "data" in payload and isinstance(payload["data"], list):
                rows = payload["data"]
        elif isinstance(payload, list):
            rows = payload

        if rows is None:
            reason = f"{source_name} 回傳格式非預期或為空"
            entry["empty_count"] += 1
            entry["last_reason"] = reason
            self.record_reason(reason)
            return False, reason

        if len(rows) < min_rows:
            reason = f"{source_name} 回傳筆數不足 ({len(rows)} < {min_rows})"
            entry["empty_count"] += 1
            entry["last_reason"] = reason
            self.record_reason(reason)
            return False, reason

        entry["ok_count"] += 1
        entry["last_reason"] = "OK"
        return True, ""

    def record_reason(self, reason: str) -> None:
        if not reason:
            return
        self.missing_by_reason[reason] = self.missing_by_reason.get(reason, 0) + 1

    def record_stock_missing(self, symbol: str, category: str, reason: str) -> None:
        sym = str(symbol).strip()
        self.missing_by_category[category] = self.missing_by_category.get(category, 0) + 1
        self.record_reason(reason)

        reasons = self.stock_missing_map.setdefault(sym, [])
        if reason and reason not in reasons:
            reasons.append(reason)

    def print_batch_report(self, total_targets: int) -> None:
        affected_stocks = len(self.stock_missing_map)
        print("\n========== [缺資料診斷統計] ==========")
        print(f"本批目標檔數: {total_targets} | 含缺漏標的數: {affected_stocks}")

        if self.missing_by_category:
            cat_str = ", ".join(f"{k}={v}" for k, v in sorted(self.missing_by_category.items()))
            print(f"缺漏類別統計: {cat_str}")
        else:
            print("缺漏類別統計: 無")

        if self.missing_by_reason:
            print("缺漏原因分佈:")
            for reason, cnt in sorted(self.missing_by_reason.items(), key=lambda x: (-x[1], x[0])):
                print(f"  - {reason}: {cnt}")
        else:
            print("缺漏原因分佈: 無")

        if self.source_health:
            print("資料來源健康度:")
            for src, st in sorted(self.source_health.items()):
                print(
                    f"  - {src}: ok={st['ok_count']}, empty={st['empty_count']}, "
                    f"err={st['error_count']} (last={st['last_reason'] or 'N/A'})"
                )

        if self.quota_guard is not None:
            print(
                f"API 配額狀態: 近1小時已用={self.quota_guard.calls_in_last_hour()}/{self.quota_guard.max_calls_per_hour}, "
                f"剩餘={self.quota_guard.remaining_hourly_quota()}, "
                f"累計請求={self.quota_guard.total_calls}, "
                f"限流次數={self.quota_guard.rate_limit_hits}, "
                f"熔斷={self.quota_guard.circuit_open}"
            )
        print("======================================\n")


def evaluate_and_attach_missing(
    tracker: DataHealthTracker,
    symbol: str,
    val_metrics: dict[str, Any],
    rev_metrics: dict[str, Any],
    fin_metrics: dict[str, Any],
    chip_metrics: dict[str, Any],
) -> dict[str, Any]:
    sym = str(symbol).strip()
    if sym.startswith("00"):
        return {"partial_missing": "", "missing_reasons": []}

    reasons: list[str] = []

    val_fields = [
        clean_numeric_or_none(val_metrics.get("pe")),
        clean_numeric_or_none(val_metrics.get("pb")),
        clean_numeric_or_none(val_metrics.get("dividend_yield")),
    ]
    if any(v is None for v in val_fields):
        reason = "估值資料(PE/PB/殖利率)部分缺漏"
        reasons.append(reason)
        tracker.record_stock_missing(sym, "valuation", reason)

    rev_mom = clean_numeric_or_none(rev_metrics.get("rev_mom_pct"))
    if rev_mom is None:
        reason = "月營收資料缺漏或尚未公布"
        reasons.append(reason)
        tracker.record_stock_missing(sym, "revenue", reason)

    fin_fields = [
        clean_numeric_or_none(fin_metrics.get("op_income")),
        clean_numeric_or_none(fin_metrics.get("net_income")),
        clean_numeric_or_none(fin_metrics.get("eps")),
        clean_numeric_or_none(fin_metrics.get("roa_pct")),
        clean_numeric_or_none(fin_metrics.get("roe_pct")),
    ]
    if any(v is None for v in fin_fields):
        reason = "財報/ROA/ROE欄位尚未公布或缺漏"
        reasons.append(reason)
        tracker.record_stock_missing(sym, "financial", reason)

    chip_complete = chip_metrics.get("complete")
    chip_m6 = chip_metrics.get("metrics_6") if isinstance(chip_metrics.get("metrics_6"), dict) else {}
    chip_fields = [
        clean_numeric_or_none(chip_metrics.get("main_force_net", chip_m6.get("main_force_net"))),
        clean_numeric_or_none(chip_metrics.get("margin_net", chip_m6.get("margin_net"))),
        clean_numeric_or_none(chip_metrics.get("chip_concentration_5d", chip_m6.get("chip_concentration_5d"))),
        clean_numeric_or_none(chip_metrics.get("major_holder_pct", chip_m6.get("major_holder_pct"))),
        clean_numeric_or_none(chip_metrics.get("retail_holder_pct", chip_m6.get("retail_holder_pct"))),
    ]
    if chip_complete is False or any(v is None for v in chip_fields):
        reason = "籌碼/集保欄位尚未更新或缺漏"
        reasons.append(reason)
        tracker.record_stock_missing(sym, "chip", reason)

    return {
        "partial_missing": "；".join(reasons),
        "missing_reasons": reasons,
    }


def evaluate_fundamental_10(fundamental_data: dict[str, Any] | None) -> tuple[list[dict[str, Any]], int]:
    metrics = fundamental_data.get("metrics") if isinstance(fundamental_data, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}

    pe = clean_numeric_or_none(metrics.get("pe"))
    pb = clean_numeric_or_none(metrics.get("pb"))
    dy = clean_numeric_or_none(metrics.get("dividend_yield"))
    mom = clean_numeric_or_none(metrics.get("rev_mom_pct"))
    yoy = clean_numeric_or_none(metrics.get("rev_yoy_pct"))
    op_inc = clean_numeric_or_none(metrics.get("op_income"))
    net_inc = clean_numeric_or_none(metrics.get("net_income"))
    eps = clean_numeric_or_none(metrics.get("eps"))
    roa = clean_numeric_or_none(metrics.get("roa_pct"))
    roe = clean_numeric_or_none(metrics.get("roe_pct"))

    def _item(label: str, passed: Optional[bool], val_str: str) -> dict[str, Any]:
        return {
            "label": label,
            "passed": bool(passed) if passed is not None else False,
            "value": val_str,
        }

    checks = [
        _item(FUNDAMENTAL_LABELS[0], (0 < pe <= 25) if pe is not None else None, f"{pe:.2f}" if pe is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[1], (0 < pb <= 4) if pb is not None else None, f"{pb:.2f}" if pb is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[2], (dy >= 3.0) if dy is not None else None, f"{dy:.2f}%" if dy is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[3], (mom > 0) if mom is not None else None, f"{mom:+.2f}%" if mom is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[4], (yoy > 0) if yoy is not None else None, f"{yoy:+.2f}%" if yoy is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[5], (op_inc > 0) if op_inc is not None else None, f"{op_inc:,.0f}" if op_inc is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[6], (net_inc > 0) if net_inc is not None else None, f"{net_inc:,.0f}" if net_inc is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[7], (eps > 0) if eps is not None else None, f"{eps:.2f}" if eps is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[8], (roa >= 1.0) if roa is not None else None, f"{roa:.2f}%" if roa is not None else "缺漏"),
        _item(FUNDAMENTAL_LABELS[9], (roe >= 3.0) if roe is not None else None, f"{roe:.2f}%" if roe is not None else "缺漏"),
    ]

    score = sum(1 for c in checks if c["passed"])
    return checks, score


_DEFAULT_FUND_GUARD = ApiQuotaGuard(
    max_calls_per_hour=580,
    min_interval_sec=0.35,
    max_retries_on_429=2,
    base_backoff_sec=5.0,
)


def _finmind_query(
    dataset: str,
    data_id: str,
    start_date: str,
    guard: ApiQuotaGuard = _DEFAULT_FUND_GUARD,
) -> list[dict[str, Any]]:
    allowed, _ = guard.can_call()
    if not allowed:
        return []

    token = _load_finmind_token()
    params: dict[str, Any] = {
        "dataset": dataset,
        "data_id": str(data_id).strip(),
        "start_date": start_date,
    }
    if token:
        params["token"] = token

    def _do_req():
        resp = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=20)
        try:
            body = resp.json()
        except Exception:
            body = None
        return resp.status_code, body

    ok, _, payload, _ = guard.execute_with_guard(_do_req)
    if not ok or not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    return rows if isinstance(rows, list) else []


def fetch_fundamental_data(
    symbol: str,
    guard: Optional[ApiQuotaGuard] = None,
    cached_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    三層分流抓基本面：
    1. 估值優先查 TPEx 全市場公開快取（命中則省下 FinMind TaiwanStockPER 請求）；
    2. 已有 cached_metrics 的低頻欄位優先沿用，缺哪項才打 FinMind 補哪項；
    3. 一併計算月營收 MoM 與 YoY，並共用同一個 quota_guard。
    """
    sym = str(symbol).strip()
    q_guard = guard or _DEFAULT_FUND_GUARD
    prev_m = cached_metrics if isinstance(cached_metrics, dict) else {}

    metrics: dict[str, Optional[float]] = {
        "pe": clean_numeric_or_none(prev_m.get("pe")),
        "pb": clean_numeric_or_none(prev_m.get("pb")),
        "dividend_yield": clean_numeric_or_none(prev_m.get("dividend_yield")),
        "rev_mom_pct": clean_numeric_or_none(prev_m.get("rev_mom_pct")),
        "rev_yoy_pct": clean_numeric_or_none(prev_m.get("rev_yoy_pct")),
        "op_income": clean_numeric_or_none(prev_m.get("op_income")),
        "net_income": clean_numeric_or_none(prev_m.get("net_income")),
        "eps": clean_numeric_or_none(prev_m.get("eps")),
        "roa_pct": clean_numeric_or_none(prev_m.get("roa_pct")),
        "roe_pct": clean_numeric_or_none(prev_m.get("roe_pct")),
    }
    sources_ok = 0

    if sym.startswith("00"):
        return {"complete": True, "metrics": metrics}

    today = dt.date.today()
    per_start = (today - dt.timedelta(days=45)).isoformat()
    rev_start = (today - dt.timedelta(days=420)).isoformat()  # 拉長至 14 個月以便算 YoY
    fin_start = (today - dt.timedelta(days=420)).isoformat()

    # 1. 估值 (PE / PB / 殖利率)：優先查 TPEx 公開快取
    if fetch_tpex_stock_valuation is not None:
        try:
            tpex_val = fetch_tpex_stock_valuation(sym)
            if isinstance(tpex_val, dict):
                if tpex_val.get("pe") is not None:
                    metrics["pe"] = clean_numeric_or_none(tpex_val.get("pe"))
                if tpex_val.get("pb") is not None:
                    metrics["pb"] = clean_numeric_or_none(tpex_val.get("pb"))
                if tpex_val.get("dividend_yield") is not None:
                    metrics["dividend_yield"] = clean_numeric_or_none(tpex_val.get("dividend_yield"))
        except Exception:
            pass

    if any(metrics[k] is None for k in ("pe", "pb", "dividend_yield")):
        per_rows = _finmind_query("TaiwanStockPER", sym, per_start, q_guard)
        if per_rows:
            per_rows_sorted = sorted(per_rows, key=lambda r: str(r.get("date") or ""))
            latest_per = per_rows_sorted[-1]
            pe_val = clean_numeric_or_none(latest_per.get("PER"))
            pb_val = clean_numeric_or_none(latest_per.get("PBR"))
            dy_val = clean_numeric_or_none(latest_per.get("dividend_yield"))
            if pe_val is not None:
                metrics["pe"] = pe_val
            if pb_val is not None:
                metrics["pb"] = pb_val
            if dy_val is not None:
                metrics["dividend_yield"] = dy_val

    if any(metrics[k] is not None for k in ("pe", "pb", "dividend_yield")):
        sources_ok += 1

    # 2. 月營收與 MoM / YoY
    rev_rows = _finmind_query("TaiwanStockMonthRevenue", sym, rev_start, q_guard)
    if rev_rows:
        by_ym: dict[tuple[int, int], float] = {}
        for r in rev_rows:
            yr = clean_numeric_or_none(r.get("revenue_year"))
            mo = clean_numeric_or_none(r.get("revenue_month"))
            rev_val = clean_numeric_or_none(r.get("revenue"))
            if yr is not None and mo is not None and rev_val is not None:
                by_ym[(int(yr), int(mo))] = rev_val
        ordered_ym = sorted(by_ym.keys())
        if ordered_ym:
            curr_ym = ordered_ym[-1]
            curr_rev = by_ym[curr_ym]
            if len(ordered_ym) >= 2:
                prev_rev = by_ym[ordered_ym[-2]]
                if prev_rev != 0:
                    metrics["rev_mom_pct"] = round(((curr_rev - prev_rev) / abs(prev_rev)) * 100.0, 2)
            last_year_ym = (curr_ym[0] - 1, curr_ym[1])
            if last_year_ym in by_ym and by_ym[last_year_ym] != 0:
                ly_rev = by_ym[last_year_ym]
                metrics["rev_yoy_pct"] = round(((curr_rev - ly_rev) / abs(ly_rev)) * 100.0, 2)

    if metrics["rev_mom_pct"] is not None or metrics["rev_yoy_pct"] is not None:
        sources_ok += 1

    # 3. 季報財務指標：損益表 + 資產負債表（僅在缺欄位時才打 FinMind）
    need_fin = any(metrics[k] is None for k in ("op_income", "net_income", "eps", "roa_pct", "roe_pct"))
    fin_rows = _finmind_query("TaiwanStockFinancialStatements", sym, fin_start, q_guard) if need_fin else []
    need_bs = any(metrics[k] is None for k in ("roa_pct", "roe_pct"))
    bs_rows = _finmind_query("TaiwanStockBalanceSheet", sym, fin_start, q_guard) if need_bs else []

    if fin_rows:
        dates = sorted({str(r.get("date") or "") for r in fin_rows if r.get("date")})
        if dates:
            latest_q_date = dates[-1]
            q_rows = [r for r in fin_rows if str(r.get("date") or "") == latest_q_date]
            item_map: dict[str, Optional[float]] = {}
            for r in q_rows:
                t_id = str(r.get("type") or "").strip()
                t_name = str(r.get("origin_name") or "").strip()
                val_num = clean_numeric_or_none(r.get("value"))
                if val_num is not None:
                    if t_id:
                        item_map[t_id] = val_num
                    if t_name:
                        item_map[t_name] = val_num

            op_inc = (
                item_map.get("OperatingIncome")
                or item_map.get("營業利益（損失）")
                or item_map.get("營業淨利（損）")
            )
            net_inc = (
                item_map.get("IncomeAfterTaxes")
                or item_map.get("ProfitLossForThePeriod")
                or item_map.get("本期淨利（淨損）")
            )
            eps_val = item_map.get("EPS") or item_map.get("基本每股盈餘（元）")

            if op_inc is not None:
                metrics["op_income"] = op_inc
            if net_inc is not None:
                metrics["net_income"] = net_inc
            if eps_val is not None:
                metrics["eps"] = eps_val

            total_assets = None
            total_equity = None
            if bs_rows:
                bs_dates = sorted({str(r.get("date") or "") for r in bs_rows if r.get("date")})
                matched_bs_date = latest_q_date if latest_q_date in bs_dates else (bs_dates[-1] if bs_dates else "")
                if matched_bs_date:
                    bs_map: dict[str, Optional[float]] = {}
                    for r in bs_rows:
                        if str(r.get("date") or "") != matched_bs_date:
                            continue
                        t_id = str(r.get("type") or "").strip()
                        t_name = str(r.get("origin_name") or "").strip()
                        val_num = clean_numeric_or_none(r.get("value"))
                        if val_num is not None:
                            if t_id:
                                bs_map[t_id] = val_num
                            if t_name:
                                bs_map[t_name] = val_num

                    total_assets = bs_map.get("TotalAssets") or bs_map.get("資產總計") or bs_map.get("Assets")
                    total_equity = bs_map.get("Equity") or bs_map.get("TotalEquity") or bs_map.get("權益總計")

            eff_net = metrics["net_income"]
            if eff_net is not None and total_assets not in (None, 0):
                metrics["roa_pct"] = round((eff_net / total_assets) * 100.0, 2)
            if eff_net is not None and total_equity not in (None, 0):
                metrics["roe_pct"] = round((eff_net / total_equity) * 100.0, 2)

    if any(metrics[k] is not None for k in ("op_income", "net_income", "eps", "roa_pct", "roe_pct")):
        sources_ok += 1

    all_populated = all(v is not None for v in metrics.values())
    return {
        "complete": bool(all_populated and sources_ok >= 3),
        "metrics": metrics,
    }