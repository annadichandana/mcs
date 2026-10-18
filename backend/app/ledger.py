"""Structured guidance ledger: one JSON file per company per call, plus stored transcripts for audit."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .config import settings

_lock = threading.RLock()
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


def norm_ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not _TICKER_RE.match(t):
        raise ValueError(f"Invalid ticker: {t!r}")
    return t


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def ledger_path(ticker: str, fy: int, q: int) -> Path:
    return settings.ledger_dir / norm_ticker(ticker) / f"FY{fy}Q{q}.json"


def transcript_path(ticker: str, fy: int, q: int) -> Path:
    return settings.transcript_dir / norm_ticker(ticker) / f"FY{fy}Q{q}.txt"


def save_entry(entry: dict) -> None:
    with _lock:
        _atomic_write(ledger_path(entry["ticker"], entry["fiscal_year"], entry["quarter"]),
                      json.dumps(entry, indent=2, ensure_ascii=False))


def load_entry(ticker: str, fy: int, q: int) -> dict | None:
    p = ledger_path(ticker, fy, q)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def load_all(ticker: str) -> list[dict]:
    d = settings.ledger_dir / norm_ticker(ticker)
    if not d.exists():
        return []
    entries = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("FY*Q*.json")]
    entries.sort(key=lambda e: (e["fiscal_year"], e["quarter"]))
    return entries


def list_tickers() -> list[str]:
    d = settings.ledger_dir
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []


def save_transcript(ticker: str, fy: int, q: int, text: str, call_date: str = "", source: str = "fmp") -> None:
    p = transcript_path(ticker, fy, q)
    _atomic_write(p, text)
    _atomic_write(p.with_suffix(".meta.json"), json.dumps({"call_date": call_date, "source": source}))


def load_transcript(ticker: str, fy: int, q: int) -> tuple[str, dict] | None:
    p = transcript_path(ticker, fy, q)
    if not p.exists():
        return None
    meta_p = p.with_suffix(".meta.json")
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    return p.read_text(encoding="utf-8"), meta


def update_claim(ticker: str, claim_id: str, mutate) -> dict:
    """Apply mutate(claim) to one claim and persist. Returns the updated claim."""
    with _lock:
        for e in load_all(ticker):
            for c in e["claims"]:
                if c["claim_id"] == claim_id:
                    mutate(c)
                    save_entry(e)
                    return c
    raise KeyError(claim_id)


# ---- analysis results (reconciliation, commitment assessments, MCS) ----
def results_path(ticker: str) -> Path:
    return settings.results_dir / f"{norm_ticker(ticker)}.json"


def save_results(ticker: str, results: dict) -> None:
    with _lock:
        _atomic_write(results_path(ticker), json.dumps(results, indent=2, ensure_ascii=False))


def load_results(ticker: str) -> dict | None:
    p = results_path(ticker)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
