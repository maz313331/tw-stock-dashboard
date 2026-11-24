#!/usr/bin/env python3
"""
第二階段：盤後重點股與核心池建置模組（build_priority_stocks.py）

功能說明：
  1. 維護固定 66 檔核心標的池（FIXED_CORE_ITEMS），包含 ETF、全體核心股、科技/AI、金融、傳產/保全與其他標的。
  2. 整合多來源盤後量額資料（成交金額 trade_amount、成交張數 trade_volume_lots）：
     - 優先順序 1：本機 summary.json / market.json 快取內的量額欄位
     - 優先順序 2：TWSE 證交所盤後全市場日成交資訊（STOCK_DAY_ALL）與 TPEx 櫃買盤後行情 OpenAPI
     - 優先順序 3：本機 data/details/<symbol>.json 最新日線收盤價與成交量估算兜底
  3. 依據成交金額前 N 名（--top-amount）與成交張數前 N 名（--top-volume，且成交金額達 --min-volume-amount）
     動態挑出核心池以外的熱門標的，補入 data/priority_stocks.json，並受 --max-total 總量上限控管。
  4. 支援 --no-live-market 離線模式、--verbose 詳細診斷輸出、--dry-run 預覽統計。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

import requests

TW_SYMBOL_RE = re.compile(r"^[0-9]{4,6}[A-Za-z]?$")
TZ_CST = dt.timezone(dt.timedelta(hours=8))

DEFAULT_TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
DEFAULT_TPEX_URLS = [
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
    "https://www.tpex.org.tw/openapi/v1/tpex_daily_close_quotes",
]

AMOUNT_FIELD_CANDIDATES: tuple[str, ...] = (
    "trade_amount",
    "amount",
    "turnover",
    "成交金額",
    "trade_value",
    "TradeValue",
    "TransactionAmount",
    "value",
)

VOLUME_LOTS_FIELD_CANDIDATES: tuple[str, ...] = (
    "trade_volume_lots",
    "volume_lots",
    "lots",
    "成交張數",
    "volume_k",
)

VOLUME_SHARES_FIELD_CANDIDATES: tuple[str, ...] = (
    "trade_volume",
    "volume",
    "成交股數",
    "成交量",
    "TradeVolume",
    "TradingShares",
)

PRICE_FIELD_CANDIDATES: tuple[str, ...] = (
    "close",
    "closing_price",
    "last_price",
    "price",
    "Close",
    "ClosingPrice",
    "收盤價",
    "收盤",
)

FIXED_CORE_ITEMS: list[dict[str, Any]] = [
    {"symbol": "0050", "ticker": "0050.TW", "name": "元大台灣50", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "0056", "ticker": "0056.TW", "name": "元大高股息", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00878", "ticker": "00878.TW", "name": "國泰永續高股息", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00919", "ticker": "00919.TW", "name": "群益台灣精選高息", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "006208", "ticker": "006208.TW", "name": "富邦台50", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00713", "ticker": "00713.TW", "name": "元大台灣高息低波", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00929", "ticker": "00929.TW", "name": "復華台灣科技優息", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00631L", "ticker": "00631L.TW", "name": "元大台灣50正2", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00981A", "ticker": "00981A.TW", "name": "主動統一台股增長", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "00406A", "ticker": "00406A.TW", "name": "主動中信台灣收益", "market": "TWSE", "category": "ETF", "tags": ["core"]},
    {"symbol": "1216", "ticker": "1216.TW", "name": "統一", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "1229", "ticker": "1229.TW", "name": "聯華", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "1477", "ticker": "1477.TW", "name": "儒鴻", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "1711", "ticker": "1711.TW", "name": "永光", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2312", "ticker": "2312.TW", "name": "金寶", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2337", "ticker": "2337.TW", "name": "旺宏", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2377", "ticker": "2377.TW", "name": "華碩", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2379", "ticker": "2379.TW", "name": "瑞昱", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2395", "ticker": "2395.TW", "name": "研華", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2492", "ticker": "2492.TW", "name": "華新科", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2539", "ticker": "2539.TW", "name": "櫻花建", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2618", "ticker": "2618.TW", "name": "長榮航", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2646", "ticker": "2646.TW", "name": "寶成", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2885", "ticker": "2885.TW", "name": "元大金", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2887", "ticker": "2887.TW", "name": "台新金", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "3380", "ticker": "3380.TW", "name": "明泰", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "4136", "ticker": "4136.TWO", "name": "泰宗", "market": "TPEx", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "4906", "ticker": "4906.TW", "name": "正文", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "4919", "ticker": "4919.TW", "name": "新唐", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "7769", "ticker": "7769.TW", "name": "鴻勁", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "8046", "ticker": "8046.TW", "name": "南電", "market": "TWSE", "category": "ALL_USERS_CORE", "tags": ["core"]},
    {"symbol": "2330", "ticker": "2330.TW", "name": "台積電", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2303", "ticker": "2303.TW", "name": "聯電", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "6285", "ticker": "6285.TW", "name": "啟碁", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2317", "ticker": "2317.TW", "name": "鴻海", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2454", "ticker": "2454.TW", "name": "聯發科", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2383", "ticker": "2383.TW", "name": "台光電", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2382", "ticker": "2382.TW", "name": "廣達", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "3231", "ticker": "3231.TW", "name": "緯創", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "6669", "ticker": "6669.TW", "name": "緯穎", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2353", "ticker": "2353.TW", "name": "宏碁", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2356", "ticker": "2356.TW", "name": "英業達", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "3017", "ticker": "3017.TW", "name": "奇鋐", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2368", "ticker": "2368.TW", "name": "金像電", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2327", "ticker": "2327.TW", "name": "國巨", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "6415", "ticker": "6415.TW", "name": "矽力", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "8069", "ticker": "8069.TWO", "name": "元太", "market": "TPEx", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "8096", "ticker": "8096.TWO", "name": "擎亞", "market": "TPEx", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "3406", "ticker": "3406.TW", "name": "玉晶光", "market": "TWSE", "category": "TECH_AI", "tags": ["core"]},
    {"symbol": "2881", "ticker": "2881.TW", "name": "富邦金", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2882", "ticker": "2882.TW", "name": "國泰金", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2886", "ticker": "2886.TW", "name": "兆豐金", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2891", "ticker": "2891.TW", "name": "中信金", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2883", "ticker": "2883.TW", "name": "凱基金", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2834", "ticker": "2834.TW", "name": "臺企銀", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2812", "ticker": "2812.TW", "name": "台中銀", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "2851", "ticker": "2851.TW", "name": "中再保", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "5880", "ticker": "5880.TW", "name": "合庫金", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "5876", "ticker": "5876.TW", "name": "上海商銀", "market": "TWSE", "category": "FINANCE", "tags": ["core"]},
    {"symbol": "1301", "ticker": "1301.TW", "name": "台塑", "market": "TWSE", "category": "TRAD_SECURITY", "tags": ["core"]},
    {"symbol": "1303", "ticker": "1303.TW", "name": "南亞", "market": "TWSE", "category": "TRAD_SECURITY", "tags": ["core"]},
    {"symbol": "1326", "ticker": "1326.TW", "name": "台化", "market": "TWSE", "category": "TRAD_SECURITY", "tags": ["core"]},
    {"symbol": "9925", "ticker": "9925.TW", "name": "新保", "market": "TWSE", "category": "TRAD_SECURITY", "tags": ["core"]},
    {"symbol": "2325", "ticker": "2325.TW", "name": "2325", "market": "TWSE", "category": "EXTRA", "tags": ["core"]},
    {"symbol": "2498", "ticker": "2498.TW", "name": "宏達電", "market": "TWSE", "category": "EXTRA", "tags": ["core"]},
    {"symbol": "6788", "ticker": "6788.TW", "name": "6788", "market": "TWSE", "category": "EXTRA", "tags": ["core"]},
]


def now_cst_iso() -> str:
    """取得台灣時區（UTC+8）目前時間的 ISO-8601 字串。"""
    return dt.datetime.now(TZ_CST).isoformat()


def today_cst_str() -> str:
    """取得台灣時區（UTC+8）目前日期字串（YYYY-MM-DD）。"""
    return dt.datetime.now(TZ_CST).strftime("%Y-%m-%d")


def normalize_symbol(raw: Any) -> str:
    """標準化台股代號（去除空白、.TW / .TWO 後綴、轉大寫）。"""
    if raw is None:
        return ""
    s = str(raw).strip().upper()
    if s.endswith(".TWO"):
        s = s[:-4]
    elif s.endswith(".TW"):
        s = s[:-3]
    s = s.strip()
    return s if TW_SYMBOL_RE.match(s) else ""


def infer_market(symbol: str, row: dict[str, Any] | None = None) -> str:
    """推斷證券所歸屬：TWSE 或 TPEx。"""
    row = row or {}
    raw_mkt = str(row.get("market") or row.get("exchange") or row.get("市場") or "").strip().upper()
    if raw_mkt in {"TPEX", "TWO", "OTC", "上櫃"}:
        return "TPEx"
    if raw_mkt in {"TWSE", "TW", "TSE", "上市"}:
        return "TWSE"
    ticker = str(row.get("ticker") or "").strip().upper()
    if ticker.endswith(".TWO"):
        return "TPEx"
    if ticker.endswith(".TW"):
        return "TWSE"
    if symbol.startswith("00") and len(symbol) >= 5:
        return "TWSE"
    return "TWSE"


def build_ticker(symbol: str, market: str) -> str:
    """依代號與市場別產生 Yahoo/行情代碼（.TW 或 .TWO）。"""
    sym = normalize_symbol(symbol)
    if market == "TPEx":
        return f"{sym}.TWO"
    return f"{sym}.TW"


def parse_number(raw: Any) -> float:
    """
    安全解析數值欄位，支援千分位逗號、HTML 標籤、中文單位或空白字串。
    無法解析或 NaN / Inf 一律回傳 0.0。
    """
    if raw is None:
        return 0.0
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        val = float(raw)
        return 0.0 if (math.isnan(val) or math.isinf(val)) else val

    text = re.sub(r"<[^>]+>", "", str(raw)).strip()
    if not text or text in {"--", "---", "N/A", "NA", "NaN", "None", "null", ""}:
        return 0.0

    multiplier = 1.0
    if text.endswith("億元"):
        multiplier = 100_000_000.0
        text = text[:-2].strip()
    elif text.endswith("萬元"):
        multiplier = 10_000.0
        text = text[:-2].strip()
    elif text.endswith("億"):
        multiplier = 100_000_000.0
        text = text[:-1].strip()
    elif text.endswith("萬"):
        multiplier = 10_000.0
        text = text[:-1].strip()

    cleaned = text.replace(",", "").replace(" ", "").strip()
    try:
        val = float(cleaned) * multiplier
        return 0.0 if (math.isnan(val) or math.isinf(val)) else val
    except ValueError:
        return 0.0


def extract_close_price(row: dict[str, Any]) -> float:
    """從行情列抽取最新收盤價（若存在）。"""
    for key in PRICE_FIELD_CANDIDATES:
        if key in row:
            px = parse_number(row.get(key))
            if px > 0:
                return px
    return 0.0


def extract_amount_and_volume(row: dict[str, Any]) -> tuple[float, float]:
    """
    從單筆行情或摘要列抽取：
      - trade_amount（成交金額，單位：元）
      - trade_volume_lots（成交張數，單位：張，1 張 = 1,000 股）

    處理規則：
      1. 先找明確的成交金額欄位（TradeValue、trade_amount、成交金額等）。
      2. 再找明確的成交張數欄位（trade_volume_lots、成交張數等）。
      3. 若只有成交股數/成交量欄位，自動換算為張數（1 張 = 1,000 股）。
      4. 若有成交張數與收盤價但缺成交金額，可反推估算成交金額。
    """
    amount = 0.0
    for key in AMOUNT_FIELD_CANDIDATES:
        if key in row:
            val = parse_number(row.get(key))
            if val > 0:
                amount = val
                break

    lots = 0.0
    for key in VOLUME_LOTS_FIELD_CANDIDATES:
        if key in row:
            val = parse_number(row.get(key))
            if val > 0:
                lots = val
                break

    if lots <= 0:
        for key in VOLUME_SHARES_FIELD_CANDIDATES:
            if key in row:
                val = parse_number(row.get(key))
                if val > 0:
                    lots = val / 1000.0 if val >= 1000.0 else val
                    break

    if amount <= 0 and lots > 0:
        close_px = extract_close_price(row)
        if close_px > 0:
            amount = lots * 1000.0 * close_px

    return amount, lots


def _extract_candidate_rows_from_json(payload: Any) -> list[dict[str, Any]]:
    """從各式 JSON 結構中安全取出可能包含股票列的 list[dict]。"""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for container_key in ("stocks", "items", "data", "rows", "quotes", "records"):
            sub = payload.get(container_key)
            if isinstance(sub, list):
                return [r for r in sub if isinstance(r, dict)]
    return []


def load_local_market_rows(
    summary_path: Path,
    market_path: Path,
    details_dir: Path | None = None,
    verbose: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """
    優先順序 1 與 3：
      1. 檢查本機 summary.json / market.json 是否有有效成交金額或成交張數。
      2. 若皆無，嘗試從本機 data/details/<symbol>.json 最新日線資料估算量額。
    """
    for candidate_path in (summary_path, market_path):
        if not candidate_path.exists():
            continue
        try:
            raw_obj = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception as exc:
            if verbose:
                print(f"[警告] 讀取 {candidate_path} 失敗: {exc}")
            continue

        rows = _extract_candidate_rows_from_json(raw_obj)
        valid_rows: list[dict[str, Any]] = []
        for r in rows:
            sym = normalize_symbol(r.get("symbol") or r.get("code") or r.get("ticker"))
            if not sym:
                continue
            amt, vol = extract_amount_and_volume(r)
            if amt > 0 or vol > 0:
                copied = dict(r)
                copied["symbol"] = sym
                valid_rows.append(copied)

        if valid_rows:
            if verbose:
                print(f"[量額來源] 由本機快取 {candidate_path} 取得 {len(valid_rows)} 筆量額資料")
            return valid_rows, f"local:{candidate_path.name}"

    if details_dir and details_dir.exists() and details_dir.is_dir():
        detail_rows: list[dict[str, Any]] = []
        for json_file in details_dir.glob("*.json"):
            sym = normalize_symbol(json_file.stem)
            if not sym:
                continue
            try:
                d_obj = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(d_obj, dict):
                continue
            history = d_obj.get("history") or d_obj.get("prices") or d_obj.get("candles") or []
            if not isinstance(history, list) or not history:
                continue
            last_pt = history[-1]
            if not isinstance(last_pt, dict):
                continue
            amt, vol = extract_amount_and_volume(last_pt)
            if amt > 0 or vol > 0:
                detail_rows.append({
                    "symbol": sym,
                    "name": str(d_obj.get("name") or sym).strip(),
                    "market": infer_market(sym, d_obj),
                    "trade_amount": amt,
                    "trade_volume_lots": vol,
                })
        if detail_rows:
            if verbose:
                print(f"[量額來源] 由本機 details 目錄估算取得 {len(detail_rows)} 筆量額資料")
            return detail_rows, "local:details_fallback"

    return [], "none"


def fetch_twse_rows(url: str = DEFAULT_TWSE_URL, timeout: int = 18, verbose: bool = False) -> list[dict[str, Any]]:
    """抓取 TWSE 證交所盤後全市場日成交資訊（STOCK_DAY_ALL）。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if not resp.ok:
            if verbose:
                print(f"[警告] TWSE OpenAPI HTTP {resp.status_code}")
            return []
        payload = resp.json()
        if not isinstance(payload, list):
            if verbose:
                print("[警告] TWSE OpenAPI 回傳非 list 格式")
            return []

        out: list[dict[str, Any]] = []
        for r in payload:
            if not isinstance(r, dict):
                continue
            sym = normalize_symbol(r.get("Code") or r.get("證券代號") or r.get("symbol"))
            if not sym:
                continue
            name = str(r.get("Name") or r.get("證券名稱") or r.get("name") or sym).strip()
            shares = parse_number(r.get("TradeVolume") or r.get("成交股數") or r.get("trade_volume"))
            amount = parse_number(r.get("TradeValue") or r.get("成交金額") or r.get("trade_amount"))
            close_px = parse_number(r.get("ClosingPrice") or r.get("收盤價") or r.get("close"))
            lots = shares / 1000.0 if shares > 0 else 0.0
            if amount <= 0 and lots > 0 and close_px > 0:
                amount = lots * 1000.0 * close_px
            if amount > 0 or lots > 0:
                out.append({
                    "symbol": sym,
                    "name": name,
                    "market": "TWSE",
                    "trade_amount": amount,
                    "trade_volume_lots": lots,
                })
        if verbose:
            print(f"[TWSE] 取得有效量額標的 {len(out)} 檔（原始 {len(payload)} 筆）")
        return out
    except Exception as exc:
        if verbose:
            print(f"[警告] 抓取 TWSE OpenAPI 例外: {exc}")
        return []


def fetch_tpex_rows(
    urls: Iterable[str] = DEFAULT_TPEX_URLS,
    timeout: int = 18,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """抓取 TPEx 櫃買中心盤後全市場日成交資訊，支援多組端點自動切換。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if not resp.ok:
                if verbose:
                    print(f"[警告] TPEx OpenAPI ({url}) HTTP {resp.status_code}")
                continue
            payload = resp.json()
            if not isinstance(payload, list) or not payload:
                continue

            out: list[dict[str, Any]] = []
            for r in payload:
                if not isinstance(r, dict):
                    continue
                sym = normalize_symbol(
                    r.get("SecuritiesCompanyCode")
                    or r.get("Code")
                    or r.get("代號")
                    or r.get("symbol")
                )
                if not sym:
                    continue
                name = str(
                    r.get("CompanyName")
                    or r.get("Name")
                    or r.get("名稱")
                    or r.get("name")
                    or sym
                ).strip()
                shares = parse_number(
                    r.get("TradingShares")
                    or r.get("TradeVolume")
                    or r.get("成交股數")
                    or r.get("成交量")
                )
                amount = parse_number(
                    r.get("TransactionAmount")
                    or r.get("TradeValue")
                    or r.get("成交金額")
                )
                close_px = parse_number(r.get("Close") or r.get("收盤") or r.get("收盤價"))
                lots = shares / 1000.0 if shares > 0 else 0.0
                if amount <= 0 and lots > 0 and close_px > 0:
                    amount = lots * 1000.0 * close_px
                if amount > 0 or lots > 0:
                    out.append({
                        "symbol": sym,
                        "name": name,
                        "market": "TPEx",
                        "trade_amount": amount,
                        "trade_volume_lots": lots,
                    })
            if out:
                if verbose:
                    print(f"[TPEx] 由 {url} 取得有效量額標的 {len(out)} 檔")
                return out
        except Exception as exc:
            if verbose:
                print(f"[警告] 抓取 TPEx OpenAPI ({url}) 例外: {exc}")
            continue
    return []


def fetch_twse_tpex_market_rows(timeout: int = 18, verbose: bool = False) -> list[dict[str, Any]]:
    """合併抓取 TWSE 與 TPEx 盤後全市場量額資料。"""
    twse_rows = fetch_twse_rows(timeout=timeout, verbose=verbose)
    tpex_rows = fetch_tpex_rows(timeout=timeout, verbose=verbose)
    return twse_rows + tpex_rows


def _merge_market_stats(market_rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """彙整全市場量額資料，同一代號保留較大成交額/張數並回傳去重後清單。"""
    stats_map: dict[str, dict[str, Any]] = {}
    for r in market_rows:
        sym = normalize_symbol(r.get("symbol") or r.get("code") or r.get("ticker"))
        if not sym:
            continue
        amt, vol = extract_amount_and_volume(r)
        mkt = infer_market(sym, r)
        name = str(r.get("name") or sym).strip()
        candidate = {
            "symbol": sym,
            "market": mkt,
            "name": name,
            "trade_amount": round(amt, 2),
            "trade_volume_lots": round(vol, 2),
        }
        existing = stats_map.get(sym)
        if existing is None:
            stats_map[sym] = candidate
        else:
            better_amt = max(existing["trade_amount"], candidate["trade_amount"])
            better_vol = max(existing["trade_volume_lots"], candidate["trade_volume_lots"])
            best_name = existing["name"] if len(existing["name"]) >= len(candidate["name"]) and existing["name"] != sym else candidate["name"]
            best_mkt = existing["market"] if existing.get("market") in {"TWSE", "TPEx"} else candidate["market"]
            stats_map[sym] = {
                "symbol": sym,
                "market": best_mkt,
                "name": best_name,
                "trade_amount": round(better_amt, 2),
                "trade_volume_lots": round(better_vol, 2),
            }
    parsed_rows = list(stats_map.values())
    return stats_map, parsed_rows


def build_priority_stocks(
    fixed_core: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    top_amount_n: int = 60,
    top_volume_n: int = 40,
    min_volume_amount: float = 50_000_000.0,
    max_total: int = 150,
    source_label: str = "none",
) -> dict[str, Any]:
    """
    以固定核心池為基準，疊加全市場成交金額前 N 名與成交張數前 N 名熱門標的。
    """
    stats_map, parsed_rows = _merge_market_stats(market_rows)

    by_amount = sorted(parsed_rows, key=lambda x: x["trade_amount"], reverse=True)[: max(0, top_amount_n)]
    by_volume = sorted(
        [x for x in parsed_rows if x["trade_amount"] >= min_volume_amount],
        key=lambda x: x["trade_volume_lots"],
        reverse=True,
    )[: max(0, top_volume_n)]

    amount_syms = {x["symbol"] for x in by_amount if x["trade_amount"] > 0}
    volume_syms = {x["symbol"] for x in by_volume if x["trade_volume_lots"] > 0}

    combined: dict[str, dict[str, Any]] = {}

    # 1. 固定核心池（保留順序、分類與核心標籤，並附上今日量額排行資訊）
    for item in fixed_core:
        sym = normalize_symbol(item.get("symbol"))
        if not sym:
            continue
        st = stats_map.get(sym, {})
        mkt = st.get("market") or item.get("market") or infer_market(sym, item)
        ticker = item.get("ticker") or build_ticker(sym, mkt)
        name = str(item.get("name") or st.get("name") or sym).strip()
        tags = list(dict.fromkeys(item.get("tags", ["core"])))
        if "core" not in tags:
            tags.insert(0, "core")
        if sym in amount_syms and "top_amount" not in tags:
            tags.append("top_amount")
        if sym in volume_syms and "top_volume" not in tags:
            tags.append("top_volume")

        combined[sym] = {
            "symbol": sym,
            "ticker": ticker,
            "market": mkt,
            "name": name,
            "category": item.get("category", "CORE"),
            "tags": tags,
            "trade_amount": st.get("trade_amount", 0.0),
            "trade_volume_lots": st.get("trade_volume_lots", 0.0),
        }

    # 2. 動態加入成交金額前 N 名（排除已在核心池者）
    dynamic_amount_added = 0
    for x in by_amount:
        sym = x["symbol"]
        if x["trade_amount"] <= 0 or sym in combined:
            continue
        tags = ["top_amount"] + (["top_volume"] if sym in volume_syms else [])
        combined[sym] = {
            "symbol": sym,
            "ticker": build_ticker(sym, x["market"]),
            "market": x["market"],
            "name": x["name"],
            "category": "DYNAMIC_AMOUNT",
            "tags": tags,
            "trade_amount": x["trade_amount"],
            "trade_volume_lots": x["trade_volume_lots"],
        }
        dynamic_amount_added += 1

    # 3. 動態加入成交張數前 N 名（排除已在核心池與成交額動態池者）
    dynamic_volume_added = 0
    for x in by_volume:
        sym = x["symbol"]
        if x["trade_volume_lots"] <= 0 or sym in combined:
            continue
        combined[sym] = {
            "symbol": sym,
            "ticker": build_ticker(sym, x["market"]),
            "market": x["market"],
            "name": x["name"],
            "category": "DYNAMIC_VOLUME",
            "tags": ["top_volume"],
            "trade_amount": x["trade_amount"],
            "trade_volume_lots": x["trade_volume_lots"],
        }
        dynamic_volume_added += 1

    items_list = list(combined.values())
    trimmed = False
    if max_total > 0 and len(items_list) > max_total:
        def priority_rank(it: dict[str, Any]) -> int:
            tag_set = set(it.get("tags", []))
            if "core" in tag_set:
                return 0
            if "top_amount" in tag_set:
                return 1
            return 2

        items_list.sort(key=priority_rank)
        items_list = items_list[:max_total]
        trimmed = True

    actual_dyn_amount = sum(1 for it in items_list if it.get("category") == "DYNAMIC_AMOUNT")
    actual_dyn_volume = sum(1 for it in items_list if it.get("category") == "DYNAMIC_VOLUME")

    return {
        "generated_at": now_cst_iso(),
        "as_of_date": today_cst_str(),
        "market_stats_source": source_label,
        "rules": {
            "top_amount_n": top_amount_n,
            "top_volume_n": top_volume_n,
            "min_volume_amount": min_volume_amount,
            "max_total": max_total,
        },
        "fixed_core_count": len(fixed_core),
        "dynamic_amount_added": actual_dyn_amount,
        "dynamic_volume_added": actual_dyn_volume,
        "dynamic_added_count": max(0, len(items_list) - len(fixed_core)),
        "total_count": len(items_list),
        "trimmed_by_max_total": trimmed,
        "items": items_list,
    }


def format_amount_yi(amount_yuan: float) -> str:
    """將成交金額（元）格式化為「億元」字串，方便終端機摘要閱讀。"""
    if amount_yuan <= 0:
        return "0.00億"
    return f"{amount_yuan / 100_000_000.0:.2f}億"


def print_top_dynamic_preview(payload: dict[str, Any], limit: int = 10) -> None:
    """印出前幾筆動態新增標的的量額摘要，方便快速驗證。"""
    dyn_items = [
        it for it in payload.get("items", [])
        if str(it.get("category", "")).startswith("DYNAMIC_")
    ]
    if not dyn_items:
        return
    print(f"[動態熱門預覽] 共 {len(dyn_items)} 檔（顯示前 {min(limit, len(dyn_items))} 檔）：")
    for idx, it in enumerate(dyn_items[:limit], start=1):
        amt_str = format_amount_yi(parse_number(it.get("trade_amount")))
        vol_str = f"{parse_number(it.get('trade_volume_lots')):,.0f}張"
        cat = it.get("category", "")
        print(f"  {idx:2d}. {it.get('symbol')} {it.get('name')} ({it.get('market')}) [{cat}] 成交額={amt_str} / 成交量={vol_str}")


def build_core_payload(fixed_core: list[dict[str, Any]]) -> dict[str, Any]:
    """建置 data/priority_core.json 結構。"""
    return {
        "updated_at": now_cst_iso(),
        "mode": "direct_fixed_core_pool",
        "total_count": len(fixed_core),
        "excluded_non_tw_or_options": ["VOO", "QQQ", "SCHD", "NDXP*"],
        "items": fixed_core,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="以固定核心池 + 盤後量額動態熱門股產出重點股清單")
    parser.add_argument("--summary-file", default="data/summary.json", help="盤後摘要快取路徑")
    parser.add_argument("--market-file", default="data/market.json", help="盤後行情快取路徑")
    parser.add_argument("--details-dir", default="data/details", help="個股明細 JSON 目錄（兜底估算用）")
    parser.add_argument("--core-out", default="data/priority_core.json", help="固定核心池輸出檔案")
    parser.add_argument("--priority-out", default="data/priority_stocks.json", help="盤後重點股輸出檔案")
    parser.add_argument("--top-amount", type=int, default=60, help="成交額前 N 名")
    parser.add_argument("--top-volume", type=int, default=40, help="成交張數前 N 名")
    parser.add_argument("--min-volume-amount", type=float, default=50_000_000.0, help="入選成交張數前段的最低成交額（元）")
    parser.add_argument("--max-total", type=int, default=150, help="重點股清單總量上限")
    parser.add_argument("--timeout", type=int, default=18, help="外部 OpenAPI 請求逾時秒數")
    parser.add_argument("--no-live-market", action="store_true", help="不連網抓 TWSE/TPEx 盤後量額，僅讀本機檔案")
    parser.add_argument("--verbose", action="store_true", help="顯示詳細資料來源與抓取統計")
    parser.add_argument("--dry-run", action="store_true", help="僅計算並印出結果，不寫入檔案")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    core_payload = build_core_payload(FIXED_CORE_ITEMS)
    if not args.dry_run:
        Path(args.core_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.core_out, "w", encoding="utf-8") as f:
            json.dump(core_payload, f, ensure_ascii=False, indent=2)

    market_rows, source_label = load_local_market_rows(
        summary_path=Path(args.summary_file),
        market_path=Path(args.market_file),
        details_dir=Path(args.details_dir) if args.details_dir else None,
        verbose=args.verbose,
    )

    if not market_rows and not args.no_live_market:
        live_rows = fetch_twse_tpex_market_rows(timeout=args.timeout, verbose=args.verbose)
        if live_rows:
            market_rows = live_rows
            source_label = "twse_tpex_openapi"

    priority_payload = build_priority_stocks(
        fixed_core=FIXED_CORE_ITEMS,
        market_rows=market_rows,
        top_amount_n=args.top_amount,
        top_volume_n=args.top_volume,
        min_volume_amount=args.min_volume_amount,
        max_total=args.max_total,
        source_label=source_label if market_rows else "none",
    )

    if not args.dry_run:
        Path(args.priority_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.priority_out, "w", encoding="utf-8") as f:
            json.dump(priority_payload, f, ensure_ascii=False, indent=2)

    print(f"[完成] 固定核心池: {len(FIXED_CORE_ITEMS)} 檔 -> {args.core_out}")
    print(
        f"[完成] 盤後重點股: {priority_payload['total_count']} 檔 "
        f"(量額動態新增 {priority_payload['dynamic_added_count']} 檔："
        f"成交額+{priority_payload['dynamic_amount_added']}、成交張數+{priority_payload['dynamic_volume_added']}；"
        f"量額來源={priority_payload['market_stats_source']}) -> {args.priority_out}"
    )
    if args.verbose:
        print_top_dynamic_preview(priority_payload, limit=10)


if __name__ == "__main__":
    main()


# ==================== 進階診斷與驗證輔助函式 ====================

def validate_priority_payload(payload: dict[str, Any]) -> list[str]:
    """檢查產出的 priority_stocks payload 結構與數值合理性，回傳警告清單。"""
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return ["payload 不是 dict 物件"]

    items = payload.get("items")
    if not isinstance(items, list):
        return ["payload.items 不是 list 物件"]

    seen_symbols: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            warnings.append(f"第 {idx} 筆 item 非 dict")
            continue
        sym = str(item.get("symbol") or "").strip().upper()
        tkr = str(item.get("ticker") or "").strip().upper()
        mkt = str(item.get("market") or "").strip()
        if not sym:
            warnings.append(f"第 {idx} 筆缺少 symbol")
            continue
        if sym in seen_symbols:
            warnings.append(f"重複的股票代號: {sym}")
        seen_symbols.add(sym)

        if mkt == "TPEx" and not tkr.endswith(".TWO"):
            warnings.append(f"{sym} 市場為 TPEx 但 ticker 非 .TWO: {tkr}")
        if mkt == "TWSE" and not tkr.endswith(".TW"):
            warnings.append(f"{sym} 市場為 TWSE 但 ticker 非 .TW: {tkr}")

        amt = parse_number(item.get("trade_amount"))
        vol = parse_number(item.get("trade_volume_lots"))
        if amt < 0 or vol < 0:
            warnings.append(f"{sym} 出現負數成交額或張數: amt={amt}, vol={vol}")

    declared_total = int(payload.get("total_count") or 0)
    if declared_total != len(items):
        warnings.append(f"total_count ({declared_total}) 與實際 items 數量 ({len(items)}) 不符")

    return warnings


def summarize_by_category(payload: dict[str, Any]) -> dict[str, int]:
    """統計各 category 的標的數量分佈。"""
    counts: dict[str, int] = {}
    for item in payload.get("items", []):
        if isinstance(item, dict):
            cat = str(item.get("category") or "UNKNOWN")
            counts[cat] = counts.get(cat, 0) + 1
    return counts


def summarize_by_market(payload: dict[str, Any]) -> dict[str, int]:
    """統計各市場別（TWSE / TPEx）的標的數量分佈。"""
    counts: dict[str, int] = {}
    for item in payload.get("items", []):
        if isinstance(item, dict):
            mkt = str(item.get("market") or "UNKNOWN")
            counts[mkt] = counts.get(mkt, 0) + 1
    return counts


def summarize_by_tag(payload: dict[str, Any]) -> dict[str, int]:
    """統計各標籤（core / top_amount / top_volume）出現次數。"""
    counts: dict[str, int] = {}
    for item in payload.get("items", []):
        if isinstance(item, dict):
            for tag in item.get("tags", []):
                t = str(tag)
                counts[t] = counts.get(t, 0) + 1
    return counts


def filter_items_by_tag(payload: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    """篩選出包含指定 tag 的標的列。"""
    target = tag.strip()
    return [
        it for it in payload.get("items", [])
        if isinstance(it, dict) and target in (it.get("tags") or [])
    ]


def export_symbols_txt(payload: dict[str, Any], out_path: Path) -> int:
    """將清單內所有代號匯出為純文字代號檔（每行一檔），方便外部腳本串接。"""
    syms = [
        str(it.get("symbol")).strip()
        for it in payload.get("items", [])
        if isinstance(it, dict) and it.get("symbol")
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(syms) + ("\n" if syms else ""), encoding="utf-8")
    return len(syms)
