"""Orchestration: ingest calls -> extract -> ledger -> reconcile -> commitments -> MCS."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from . import ledger
from .commitments import assess_entry, followup_periods
from .extractor import extract_claims
from .fmp import FMPClient, FMPError
from .reconcile import reconcile_entries
from .scoring import compute_mcs

Log = Callable[[str], None]


def get_transcript(ticker: str, fy: int, q: int, fmp: FMPClient | None, log: Log = print):
    """Stored transcript first (manual uploads / previous fetches), then FMP. Returns (text, meta) or None."""
    stored = ledger.load_transcript(ticker, fy, q)
    if stored:
        return stored
    if fmp is None:
        return None
    try:
        t = fmp.transcript(ticker, fy, q)
    except FMPError as e:
        log(f"  FMP: {e}")
        return None
    if not t:
        return None
    ledger.save_transcript(ticker, fy, q, t["content"], t["date"], "fmp")
    return t["content"], {"call_date": t["date"], "source": "fmp"}


def ingest_period(ticker: str, fy: int, q: int, fmp: FMPClient | None, force: bool = False, log: Log = print) -> dict:
    existing = ledger.load_entry(ticker, fy, q)
    if existing and not force:
        log(f"FY{fy} Q{q}: already in ledger ({len(existing['claims'])} claims) - skipped")
        return {"period": f"FY{fy}Q{q}", "status": "skipped", "claims": len(existing["claims"])}
    got = get_transcript(ticker, fy, q, fmp, log)
    if not got:
        log(f"FY{fy} Q{q}: transcript not available")
        return {"period": f"FY{fy}Q{q}", "status": "no_transcript"}
    text, meta = got
    log(f"FY{fy} Q{q}: extracting claims with Claude ({len(text):,} chars)...")
    entry = extract_claims(text, ticker, fy, q, meta.get("call_date", ""))
    entry["source"]["provider"] = "manual upload" if meta.get("source") == "manual" else entry["source"]["provider"]
    ledger.save_entry(entry)
    n_rev = sum(1 for c in entry["claims"] if c["review"]["status"] == "pending")
    log(f"FY{fy} Q{q}: {len(entry['claims'])} claims saved ({n_rev} flagged for review)")
    return {"period": f"FY{fy}Q{q}", "status": "ingested", "claims": len(entry["claims"]), "flagged": n_rev}


def select_periods(ticker: str, fmp: FMPClient, last_n: int) -> list[tuple[int, int]]:
    dates = fmp.transcript_dates(ticker)[:last_n]
    return sorted((d["fiscal_year"], d["quarter"]) for d in dates)


def analyze(ticker: str, fmp: FMPClient | None, check_commitments: bool = True, force_commitments: bool = False,
            log: Log = print) -> dict:
    """Recompute reconciliation, commitment assessments and MCS from the current ledger."""
    entries = ledger.load_all(ticker)
    prev = ledger.load_results(ticker) or {}
    fin: dict = {}
    if fmp is not None:
        try:
            log("Fetching reported financials from FMP...")
            fin = fmp.financials(ticker)
        except FMPError as e:
            log(f"FMP financials unavailable: {e}")
    recon = reconcile_entries(entries, fin)
    log(f"Reconciled {sum(1 for r in recon if r['status'] == 'evaluated')} of {len(recon)} guidance claims against actuals")

    old = {c["claim_id"]: c for c in prev.get("commitments", [])}
    commits: list[dict] = []
    for e in entries:
        mine = [c for c in e["claims"] if c["claim_type"] == "commitment" and c.get("review", {}).get("status") != "rejected"]
        if not mine:
            continue
        cached = [old[c["claim_id"]] for c in mine if c["claim_id"] in old]
        if (not check_commitments or len(cached) == len(mine)) and not force_commitments:
            commits.extend(cached)
            continue
        followups = []
        for fy, q in followup_periods(e["fiscal_year"], e["quarter"]):
            got = get_transcript(ticker, fy, q, fmp, log)
            if got:
                followups.append({"fy": fy, "q": q, "text": got[0]})
        log(f"FY{e['fiscal_year']} Q{e['quarter']}: assessing {len(mine)} commitment(s) against {len(followups)} later call(s)...")
        commits.extend(assess_entry(e, followups))

    mcs = compute_mcs(ticker, entries, recon, commits)
    results = {
        "ticker": ticker,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reconciliation": recon,
        "commitments": commits,
        "mcs": mcs,
    }
    ledger.save_results(ticker, results)
    log(f"MCS for {ticker}: {mcs['score']} ({mcs['confidence']} confidence)")
    return results


def review_queue(ticker: str | None = None) -> list[dict]:
    """Every claim/result that needs a human, with reasons."""
    items = []
    for t in ([ticker] if ticker else ledger.list_tickers()):
        res = ledger.load_results(t) or {}
        recon = {r["claim_id"]: r for r in res.get("reconciliation", [])}
        commits = {c["claim_id"]: c for c in res.get("commitments", [])}
        for e in ledger.load_all(t):
            for c in e["claims"]:
                st = c.get("review", {}).get("status")
                if st in ("approved", "rejected", "edited"):
                    continue
                reasons = list(c.get("flags", []))
                for extra in (recon.get(c["claim_id"]), commits.get(c["claim_id"])):
                    if extra:
                        reasons += [f for f in extra.get("flags", []) if f["severity"] in ("medium", "high")]
                reasons = [r for r in reasons if r["severity"] in ("medium", "high")]
                if not reasons and st != "pending":
                    continue
                items.append({
                    "ticker": t, "claim_id": c["claim_id"], "call_period": f"FY{e['fiscal_year']}Q{e['quarter']}",
                    "claim_type": c["claim_type"], "summary": c.get("summary"), "quote": c.get("quote"),
                    "speaker": c.get("speaker"), "metric": c.get("metric"), "basis": c.get("basis"),
                    "value_low": c.get("value_low"), "value_high": c.get("value_high"), "unit": c.get("unit"),
                    "scale": c.get("scale"), "resolved_period": c.get("resolved_period"),
                    "confidence": c.get("confidence"), "reasons": reasons,
                    "reconciliation": recon.get(c["claim_id"]), "assessment": commits.get(c["claim_id"]),
                })
    sev = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: (min((sev[r["severity"]] for r in i["reasons"]), default=3), i["ticker"], i["call_period"]))
    return items


EDITABLE = {"metric", "basis", "value_low", "value_high", "unit", "scale", "qualifier", "claim_type", "summary"}


def apply_review(ticker: str, claim_id: str, action: str, edits: dict | None, actual_override, note: str) -> dict:
    """approve | reject | edit. Recomputes reconciliation + MCS (cached commitment results are reused)."""
    from .extractor import SCALE
    from .periods import resolve_target

    def mutate(c: dict) -> None:
        if action == "edit":
            for k, v in (edits or {}).items():
                if k in EDITABLE:
                    c[k] = v
            tp = (edits or {}).get("target_period")
            if tp:
                c["target_period"] = tp
                m = re.search(r"-FY(\d{4})Q(\d)-\d+$", claim_id)
                fy, q = int(m.group(1)), int(m.group(2))
                r = resolve_target(tp, fy, q)
                c["resolved_period"] = {"fiscal_year": r[0], "period": r[1]} if r else None
            mult = SCALE.get(c.get("scale") or "units", 1) if c.get("unit") == "usd" else 1
            lo, hi = c.get("value_low"), c.get("value_high")
            c["norm_low"] = lo * mult if lo is not None else None
            c["norm_high"] = hi * mult if hi is not None else None
            c["quantitative"] = bool(c["claim_type"] == "guidance" and c.get("metric") and (lo is not None or hi is not None))
        if actual_override is not None:
            c["actual_override"] = actual_override
        c["flags"] = [] if action in ("approve", "edit") else c.get("flags", [])
        c["review"] = {"status": {"approve": "approved", "reject": "rejected", "edit": "edited"}[action],
                       "note": note, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    if action not in ("approve", "reject", "edit"):
        raise ValueError("action must be approve, reject or edit")
    c = ledger.update_claim(ticker, claim_id, mutate)
    return c
