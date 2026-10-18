"""Commitment delivery: judge each management commitment against later earnings calls, with verified evidence."""
from __future__ import annotations

from datetime import date, datetime, timezone

from anthropic import Anthropic

from . import llm
from .extractor import get_client
from .periods import call_key, idx, next_quarter
from .quotes import locate_quote

TOOL = {
    "name": "record_assessments",
    "description": "Record a delivery assessment for every commitment.",
    "input_schema": {
        "type": "object",
        "properties": {"assessments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string"},
                "status": {"type": "string", "enum": ["delivered", "partially_delivered", "not_delivered", "no_evidence", "superseded"]},
                "evidence_quote": {"type": ["string", "null"], "description": "Verbatim contiguous text (max 45 words) from a FOLLOW-UP transcript."},
                "evidence_period": {"type": ["string", "null"], "description": "Which follow-up call the evidence is from, e.g. FY2025Q3."},
                "rationale": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["claim_id", "status", "rationale", "confidence"],
        }}},
        "required": ["assessments"],
    },
}

SYSTEM = """You audit whether management delivered on commitments made on an earlier earnings call, using ONLY the later call transcripts provided.

For each commitment choose exactly one status:
- delivered: a follow-up transcript clearly shows it was completed or achieved.
- partially_delivered: clearly shows meaningful but incomplete progress, a reduced scope, or a delay.
- not_delivered: management explicitly says it was abandoned, missed or cancelled.
- superseded: management replaced it with a different, explicitly stated plan.
- no_evidence: the follow-up transcripts do not address it. Silence is NOT evidence of failure; never mark not_delivered because a topic is absent.

Every status other than no_evidence requires `evidence_quote`: verbatim, contiguous text (max 45 words) copied from a follow-up transcript, with `evidence_period`. Quotes are machine-checked. Judge conservatively and set confidence honestly."""

SCORE = {"delivered": 100, "partially_delivered": 50, "not_delivered": 0}


def _days_since(d: str) -> int | None:
    try:
        return (date.today() - datetime.strptime(d[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return None


def followup_periods(fy: int, q: int, n: int = 2) -> list[tuple[int, int]]:
    out, cur = [], (fy, q)
    for _ in range(n):
        cur = next_quarter(*cur)
        out.append(cur)
    return out


def assess_entry(entry: dict, followups: list[dict], client: Anthropic | None = None) -> list[dict]:
    """followups: [{fy, q, text}] for transcripts that exist. Returns one result per commitment claim."""
    commits = [c for c in entry["claims"] if c["claim_type"] == "commitment" and c.get("review", {}).get("status") != "rejected"]
    if not commits:
        return []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = lambda c: {
        "claim_id": c["claim_id"], "ticker": entry["ticker"],
        "call_period": call_key(entry["fiscal_year"], entry["quarter"]),
        "summary": c.get("summary"), "quote": c.get("quote"), "speaker": c.get("speaker"),
        "deadline_text": c.get("deadline_text"), "claim_confidence": c.get("confidence"),
        "followups_checked": [call_key(f["fy"], f["q"]) for f in followups],
        "assessed_at": now, "flags": [], "evidence_quote": None, "evidence_period": None,
        "evidence_verified": False,
    }
    if not followups:
        age = _days_since(entry.get("call_date", ""))
        res = []
        for c in commits:
            r = base(c)
            if age is not None and age < 200:
                r.update(status="pending", rationale="No follow-up call is available yet.", confidence=0.0)
            else:
                r.update(status="no_evidence", rationale="No follow-up transcripts could be retrieved.", confidence=0.0)
                r["flags"].append({"code": "no_followup_transcript", "message": "Follow-up transcripts are unavailable; commitment cannot be assessed.", "severity": "medium"})
            res.append(r)
        return res

    client = client or get_client()
    listing = "\n".join(
        f"- claim_id={c['claim_id']} | said by {c.get('speaker')} | commitment: {c.get('summary')} | quote: \"{c.get('quote')}\" | deadline: {c.get('deadline_text') or 'not stated'}"
        for c in commits
    )
    docs = "\n\n".join(
        f"<followup_transcript period=\"{call_key(f['fy'], f['q'])}\">\n{f['text'][:120000]}\n</followup_transcript>" for f in followups
    )
    user = (f"Commitments made on the {entry['ticker']} {call_key(entry['fiscal_year'], entry['quarter'])} call:\n{listing}\n\n"
            f"Later calls to use as evidence:\n{docs}")
    raw = llm.structured(client, SYSTEM, user, TOOL, 8000).get("assessments", [])
    by_id = {a.get("claim_id"): a for a in raw}
    latest_idx = max(idx(f["fy"], f"Q{f['q']}") for f in followups)
    texts = {call_key(f["fy"], f["q"]): f["text"] for f in followups}

    out = []
    for c in commits:
        r, a = base(c), by_id.get(c["claim_id"])
        if not a:
            r.update(status="no_evidence", rationale="Model returned no assessment.", confidence=0.0)
            r["flags"].append({"code": "missing_assessment", "message": "No assessment returned for this commitment.", "severity": "medium"})
            out.append(r)
            continue
        r.update(status=a["status"], rationale=a.get("rationale", ""), confidence=float(a.get("confidence", 0.5)),
                 evidence_quote=a.get("evidence_quote"), evidence_period=a.get("evidence_period"))
        if r["status"] != "no_evidence":
            ep = (r["evidence_period"] or "").replace(" ", "")
            hay = texts.get(ep) or "\n".join(texts.values())
            r["evidence_verified"] = bool(r["evidence_quote"]) and locate_quote(hay, r["evidence_quote"]) is not None
            if not r["evidence_verified"]:
                r["flags"].append({"code": "evidence_unverified", "message": f"Evidence quote for '{r['status']}' could not be matched in the follow-up transcripts; status downgraded.", "severity": "high"})
                r.update(status="no_evidence", confidence=min(r["confidence"], 0.3))
        if r["status"] == "no_evidence":
            dl = c.get("resolved_period")
            if dl and idx(dl["fiscal_year"], dl["period"]) > latest_idx:
                r["status"] = "pending"
                r["rationale"] = (r.get("rationale") or "") + " (Deadline falls after the latest available call.)"
        if r["confidence"] < 0.6 and r["status"] in SCORE:
            r["flags"].append({"code": "low_confidence_assessment", "message": "Assessment confidence is below 0.60.", "severity": "medium"})
        out.append(r)
    return out
