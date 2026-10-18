"""Financial Modeling Prep client (stable API) with on-disk caching."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

from .config import settings

BASE = "https://financialmodelingprep.com/stable"


class FMPError(Exception):
    pass


class FMPClient:
    def __init__(self, api_key: str | None = None, cache_dir: Path | None = None):
        self.key = api_key or settings.fmp_api_key
        self.cache_dir = cache_dir or settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- transport
    def _get(self, path: str, params: dict, ttl: float | None):
        """ttl=None -> cache forever; ttl=0 -> never cache; otherwise seconds."""
        if not self.key:
            raise FMPError("FMP_API_KEY is not set.")
        ck = hashlib.sha1(f"{path}|{sorted(params.items())}".encode()).hexdigest()
        cf = self.cache_dir / f"fmp_{ck}.json"
        if cf.exists() and ttl != 0:
            if ttl is None or time.time() - cf.stat().st_mtime < ttl:
                return json.loads(cf.read_text())
        r = requests.get(f"{BASE}/{path}", params={**params, "apikey": self.key}, timeout=45)
        if r.status_code in (401, 402, 403):
            raise FMPError(
                f"FMP returned {r.status_code} for '{path}'. Your FMP plan may not include this endpoint "
                "(earnings transcripts require a premium plan) or the key is invalid."
            )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and ("Error Message" in data or "error" in data):
            raise FMPError(str(data.get("Error Message") or data.get("error")))
        if data:  # never cache empty responses (transcript may simply not be published yet)
            cf.write_text(json.dumps(data))
        return data

    # ---------------------------------------------------------------- transcripts
    def transcript_dates(self, symbol: str) -> list[dict]:
        """Available transcripts: [{fiscal_year, quarter, date}], newest first."""
        data = self._get("earning-call-transcript-dates", {"symbol": symbol}, ttl=12 * 3600) or []
        out = []
        for d in data:
            fy = d.get("fiscalYear") or d.get("year")
            q = d.get("quarter") or d.get("period")
            if isinstance(q, str):
                q = int(q.replace("Q", ""))
            if fy and q:
                out.append({"fiscal_year": int(fy), "quarter": int(q), "date": str(d.get("date", ""))[:10]})
        out.sort(key=lambda x: (x["fiscal_year"], x["quarter"]), reverse=True)
        return out

    def transcript(self, symbol: str, year: int, quarter: int) -> dict | None:
        data = self._get("earning-call-transcript", {"symbol": symbol, "year": year, "quarter": quarter}, ttl=None)
        if not data:
            return None
        item = data[0] if isinstance(data, list) else data
        content = item.get("content") or ""
        if not content.strip():
            return None
        return {"content": content, "date": str(item.get("date", ""))[:10]}

    # ---------------------------------------------------------------- financials
    @staticmethod
    def _fy_period(row: dict) -> tuple[int, str] | None:
        fy = row.get("fiscalYear") or row.get("calendarYear") or str(row.get("date", ""))[:4]
        per = row.get("period")
        try:
            fy = int(fy)
        except (TypeError, ValueError):
            return None
        if per not in {"Q1", "Q2", "Q3", "Q4", "FY"}:
            return None
        return fy, per

    def financials(self, symbol: str, limit_q: int = 48, limit_a: int = 12) -> dict[tuple[int, str], dict]:
        """Merged quarterly+annual actuals keyed by (fiscal_year, period)."""
        out: dict[tuple[int, str], dict] = {}
        for kind, limit in (("quarter", limit_q), ("annual", limit_a)):
            inc = self._get("income-statement", {"symbol": symbol, "period": kind, "limit": limit}, ttl=24 * 3600) or []
            try:
                cfs = self._get("cash-flow-statement", {"symbol": symbol, "period": kind, "limit": limit}, ttl=24 * 3600) or []
            except FMPError:
                cfs = []
            for r in inc:
                key = self._fy_period(r)
                if not key:
                    continue
                rev, gp = r.get("revenue"), r.get("grossProfit")
                oi, ni = r.get("operatingIncome"), r.get("netIncome")
                eps = r.get("epsDiluted", r.get("epsdiluted", r.get("eps")))
                rec = out.setdefault(key, {})
                rec.update({
                    "revenue": rev, "gross_profit": gp, "operating_income": oi, "net_income": ni,
                    "ebitda": r.get("ebitda"), "eps": eps,
                    "period_end": str(r.get("date", ""))[:10], "currency": r.get("reportedCurrency"),
                })
                if rev:
                    rec["gross_margin"] = gp / rev * 100 if gp is not None else None
                    rec["operating_margin"] = oi / rev * 100 if oi is not None else None
                    rec["net_margin"] = ni / rev * 100 if ni is not None else None
            for r in cfs:
                key = self._fy_period(r)
                if not key:
                    continue
                rec = out.setdefault(key, {})
                capex = r.get("capitalExpenditure")
                rec["capex"] = abs(capex) if capex is not None else None
                rec["free_cash_flow"] = r.get("freeCashFlow")
        return out
