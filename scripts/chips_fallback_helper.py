import datetime as dt
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


class ChipsFallbackManager:
    """上市股（TWSE T86 / MI_MARGN）按日全市場快取與 FinMind 權限熔斷器"""

    def __init__(self):
        self.blocked_finmind_datasets = set()
        self._t86_cache = {}      # key: YYYYMMDD -> dict[symbol, row]
        self._margin_cache = {}   # key: YYYYMMDD -> dict[symbol, row]

    def is_dataset_blocked(self, dataset: str) -> bool:
        return dataset in self.blocked_finmind_datasets

    def report_finmind_status(self, dataset: str, status_code: int):
        if status_code in (402, 403):
            self.blocked_finmind_datasets.add(dataset)

    @staticmethod
    def to_yyyymmdd(date_str: str) -> str:
        # 支援 '2026-11-24' 或 '20261124'
        cleaned = date_str.replace("-", "").replace("/", "").strip()
        if len(cleaned) == 8:
            return cleaned
        d = dt.date.fromisoformat(date_str[:10])
        return d.strftime("%Y%m%d")

    def get_twse_t86_for_symbol(self, symbol: str, date_str: str):
        ymd = self.to_yyyymmdd(date_str)
        if ymd not in self._t86_cache:
            url = "https://www.twse.com.tw/rwd/zh/fund/T86"
            params = {"date": ymd, "selectType": "ALL", "response": "json"}
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=15)
                payload = r.json()
                rows = payload.get("data", []) if payload.get("stat") == "OK" else []
                self._t86_cache[ymd] = {
                    str(row[0]).strip(): row for row in rows if row and row[0]
                }
            except Exception:
                self._t86_cache[ymd] = {}
        return self._t86_cache[ymd].get(str(symbol).strip())

    def get_twse_margin_for_symbol(self, symbol: str, date_str: str):
        ymd = self.to_yyyymmdd(date_str)
        if ymd not in self._margin_cache:
            url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
            params = {"date": ymd, "selectType": "ALL", "response": "json"}
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=15)
                payload = r.json()
                by_sym = {}
                for tbl in payload.get("tables", []) or []:
                    for row in tbl.get("data", []) or []:
                        if row and row[0]:
                            by_sym[str(row[0]).strip()] = row
                self._margin_cache[ymd] = by_sym
            except Exception:
                self._margin_cache[ymd] = {}
        return self._margin_cache[ymd].get(str(symbol).strip())