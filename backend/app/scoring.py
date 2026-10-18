"""Management Credibility Score (MCS).

MCS = weighted blend of three components, each 0-100:
  forecast_accuracy   (50%)  how close guidance landed to reported actuals
  commitment_delivery (30%)  share of commitments management demonstrably delivered
  evidence_quality    (20%)  how well the underlying data can be trusted (verified quotes, checkable guidance, extraction confidence)

Small samples are shrunk toward a neutral 50 so one lucky/unlucky quarter cannot dominate. Items are weighted by
extraction confidence and recency (half-life 8 quarters). Missing components are excluded and the remaining weights
re-normalised; every gap is surfaced as a flag for human review.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .commitments import SCORE as COMMIT_SCORE

METHOD_VERSION = "1.0"
WEIGHTS = {"forecast_accuracy": 0.50, "commitment_delivery": 0.30, "evidence_quality": 0.20}
PRIOR, PRIOR_W = 50.0, 1.0
HALF_LIFE_Q = 8.0


def _grade(score: float) -> str:
    return ("Highly credible" if score >= 85 else "Generally reliable" if score >= 70
            else "Mixed record" if score >= 55 else "Low credibility")


def _shrink(items: list[tuple[float, float]]):
    if not items:
        return None, None
    sw = sum(w for _, w in items)
    raw = sum(s * w for s, w in items) / sw
    adj = (sum(s * w for s, w in items) + PRIOR * PRIOR_W) / (sw + PRIOR_W)
    return round(raw, 1), round(adj, 1)


def _flag(code, message, severity):
    return {"code": code, "message": message, "severity": severity}


def _call_idx(period: str) -> int:  # "FY2025Q3" -> index
    return int(period[2:6]) * 4 + int(period[-1])


def compute_mcs(ticker: str, entries: list[dict], recon: list[dict], commits: list[dict]) -> dict:
    flags: list[dict] = []
    if not entries:
        return {"ticker": ticker, "score": None, "grade": "No data", "confidence": "none", "needs_human_review": True,
                "flags": [_flag("no_ledger", "No calls have been ingested for this company.", "high")],
                "components": {}, "method_version": METHOD_VERSION}

    latest = max(e["fiscal_year"] * 4 + e["quarter"] for e in entries)
    recency = lambda cp: 0.5 ** (max(0, latest - _call_idx(cp)) / HALF_LIFE_Q)

    # --- forecast accuracy
    f_items = [(r["accuracy_score"], max(0.3, r["claim_confidence"] or 0.5) * recency(r["call_period"]))
               for r in recon if r["status"] == "evaluated"]
    f_raw, f_adj = _shrink(f_items)
    denom_quant = [r for r in recon if r["status"] in ("evaluated", "missing_actual", "unresolved_period", "unverifiable_metric")]
    coverage = len([r for r in recon if r["status"] == "evaluated"]) / len(denom_quant) if denom_quant else None

    # --- commitment delivery
    c_items = [(COMMIT_SCORE[c["status"]], max(0.3, c["confidence"]) * recency(c["call_period"]))
               for c in commits if c["status"] in COMMIT_SCORE]
    c_raw, c_adj = _shrink(c_items)
    c_unassessed = [c for c in commits if c["status"] == "no_evidence"]

    # --- evidence quality
    claims = [c for e in entries for c in e["claims"] if c.get("review", {}).get("status") != "rejected"]
    ev_adj = ev_raw = None
    q_rate = None
    if claims:
        q_rate = sum(1 for c in claims if c.get("quote_verified") or c.get("review", {}).get("status") in ("approved", "edited")) / len(claims)
        verif = coverage if coverage is not None else 0.5
        avg_conf = sum(c["confidence"] for c in claims) / len(claims)
        ev_raw = ev_adj = round(100 * (0.4 * q_rate + 0.35 * verif + 0.25 * avg_conf), 1)

    comps = {
        "forecast_accuracy": {"score": f_adj, "raw": f_raw, "n": len(f_items), "coverage": coverage},
        "commitment_delivery": {"score": c_adj, "raw": c_raw, "n": len(c_items), "unassessed": len(c_unassessed),
                                "pending": len([c for c in commits if c["status"] == "pending"])},
        "evidence_quality": {"score": ev_adj, "raw": ev_raw, "n": len(claims), "quote_verified_rate": q_rate},
    }
    avail = {k: v for k, v in comps.items() if v["score"] is not None and (k == "evidence_quality" or v["n"] > 0)}
    scorable = [k for k in avail if k != "evidence_quality"]
    used_w = {k: WEIGHTS[k] for k in avail}
    tw = sum(used_w.values())
    used_w = {k: round(w / tw, 3) for k, w in used_w.items()} if tw else {}
    for k, v in comps.items():
        v["weight"] = used_w.get(k, 0.0)

    score = None
    if scorable:
        score = round(sum(avail[k]["score"] * used_w[k] for k in avail), 1)

    # --- flags for human review
    if "forecast_accuracy" not in scorable:
        flags.append(_flag("no_forecast_data", "No quantitative guidance could be matched with reported actuals.", "high"))
    if "commitment_delivery" not in scorable:
        flags.append(_flag("no_commitment_data", "No commitments have a verified delivery assessment yet.", "medium"))
    n_items = len(f_items) + len(c_items)
    if scorable and n_items < 4:
        flags.append(_flag("few_observations", f"Only {n_items} scored item(s); the score is heavily shrunk toward neutral.", "high"))
    if len(entries) < 3:
        flags.append(_flag("few_calls", f"Only {len(entries)} call(s) ingested; track record is thin.", "medium"))
    if coverage is not None and coverage < 0.5:
        flags.append(_flag("low_coverage", f"Only {coverage:.0%} of quantitative guidance could be verified against actuals.", "medium"))
    missing = [r for r in recon if r["status"] == "missing_actual"]
    if missing:
        flags.append(_flag("missing_actuals", f"{len(missing)} guidance item(s) have no matching reported actual in FMP.", "medium"))
    if q_rate is not None and q_rate < 0.9:
        flags.append(_flag("unverified_quotes", f"{(1 - q_rate):.0%} of claims have quotes not found verbatim in the transcript.", "medium"))
    pending_review = sum(1 for c in claims if c.get("review", {}).get("status") == "pending")
    if pending_review:
        flags.append(_flag("pending_review_items", f"{pending_review} claim(s) are awaiting human review.", "medium"))
    low_conf = [x for x in f_items if x[1] < 0.3]
    if c_unassessed:
        flags.append(_flag("unassessed_commitments", f"{len(c_unassessed)} commitment(s) had no evidence either way and were excluded.", "low"))
    if any(c["status"] in COMMIT_SCORE and c["confidence"] < 0.6 for c in commits):
        flags.append(_flag("low_confidence_assessments", "Some commitment assessments have confidence below 0.60.", "medium"))

    sev = {f["severity"] for f in flags}
    if score is None:
        confidence = "none"
    elif "high" in sev or n_items < 4:
        confidence = "low"
    elif n_items >= 8 and len(entries) >= 3 and (coverage or 0) >= 0.7 and not flags_has(flags, "medium"):
        confidence = "high"
    else:
        confidence = "medium"

    return {
        "ticker": ticker, "score": score,
        "grade": _grade(score) if score is not None else "Insufficient data",
        "provisional": confidence in ("low", "none"),
        "confidence": confidence,
        "needs_human_review": confidence in ("low", "none") or any(f["severity"] in ("medium", "high") for f in flags),
        "components": comps, "flags": flags,
        "calls_covered": len(entries),
        "counts": {
            "claims": len(claims),
            "guidance_evaluated": len(f_items),
            "commitments_scored": len(c_items),
        },
        "method_version": METHOD_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def flags_has(flags: list[dict], severity: str) -> bool:
    return any(f["severity"] == severity for f in flags)
