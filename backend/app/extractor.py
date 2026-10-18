"""Claude-powered extraction of management claims from an earnings call transcript."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from anthropic import Anthropic

from . import llm
from .config import settings
from .llm import LLMError as ExtractionError
from .periods import call_key, resolve_target
from .quotes import locate_quote

METRICS = [
    "revenue", "gross_profit", "gross_margin", "operating_income", "operating_margin",
    "net_income", "net_margin", "eps", "ebitda", "capex", "free_cash_flow", "other",
]
SCALE = {"units": 1, "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
PERIOD_REL = [
    "current_quarter", "next_quarter", "quarter_after_next", "current_year", "next_year",
    "explicit", "unspecified",
]

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_type": {"type": "string", "enum": ["guidance", "commitment", "risk", "statement"]},
        "speaker": {"type": "string", "description": "Name of the management speaker, as written in the transcript."},
        "role": {"type": "string", "description": "e.g. CEO, CFO, COO, IR. 'Unknown' if not stated."},
        "section": {"type": "string", "enum": ["prepared_remarks", "qa", "unknown"]},
        "summary": {"type": "string", "description": "One neutral sentence describing the claim, in your own words."},
        "quote": {"type": "string", "description": "VERBATIM contiguous text from the transcript, max 45 words."},
        "update_type": {"type": "string", "enum": ["new", "reiterated", "raised", "lowered", "unspecified"]},
        "hedged": {"type": "boolean", "description": "True if management used hedging language (hope, aim, expect to try, subject to)."},
        "metric": {"type": ["string", "null"], "enum": METRICS + [None],
                   "description": "Financial metric for guidance. null for non-financial claims."},
        "basis": {"type": ["string", "null"], "enum": ["absolute", "yoy_growth_pct", "qoq_growth_pct", "level_pct", None],
                  "description": "absolute: a level (e.g. revenue $4.2B). *_growth_pct: growth rate. level_pct: a margin percentage."},
        "value_low": {"type": ["number", "null"], "description": "Lower bound as stated (no scaling applied)."},
        "value_high": {"type": ["number", "null"], "description": "Upper bound as stated. Same as value_low for a point estimate."},
        "qualifier": {"type": ["string", "null"], "enum": ["range", "point", "approximately", "at_least", "at_most", None]},
        "unit": {"type": ["string", "null"], "enum": ["usd", "percent", "usd_per_share", "other", None]},
        "scale": {"type": ["string", "null"], "enum": list(SCALE) + [None]},
        "target_period": {
            "type": "object",
            "description": "The period the guidance applies to, or the deadline for a commitment.",
            "properties": {
                "relation": {"type": "string", "enum": PERIOD_REL},
                "fiscal_year": {"type": ["integer", "null"]},
                "period": {"type": ["string", "null"], "enum": ["Q1", "Q2", "Q3", "Q4", "FY", None]},
            },
            "required": ["relation"],
        },
        "deadline_text": {"type": ["string", "null"], "description": "Commitments: the timing phrase as stated, e.g. 'by end of Q3'."},
        "risk_category": {"type": ["string", "null"],
                          "enum": ["macro", "demand", "supply_chain", "competition", "regulatory", "execution", "fx", "liquidity", "other", None]},
        "severity": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
        "confidence": {"type": "number", "description": "0-1: your confidence that this record is complete and correctly interpreted."},
    },
    "required": ["claim_type", "speaker", "section", "summary", "quote", "confidence", "target_period"],
}

TOOL = {
    "name": "record_claims",
    "description": "Record every management claim found in the transcript.",
    "input_schema": {
        "type": "object",
        "properties": {"claims": {"type": "array", "items": CLAIM_SCHEMA}},
        "required": ["claims"],
    },
}

SYSTEM = """You are a meticulous research associate building an audit ledger of what company management said on an earnings call. Accuracy and traceability matter more than volume.

Extract ONLY statements made by company management (executives and investor relations). Never record an analyst's question, assumption or paraphrase as a management claim.

Claim types:
- guidance: a forward-looking expectation about a financial metric (revenue, margins, EPS, capex, free cash flow...). Quantitative when numbers are given; if only qualitative ("low double digit growth"), leave value fields null and describe it in `summary`.
- commitment: a specific promise to take an action or reach a milestone (product launch, cost program, buyback, dividend, hiring freeze, market entry, target date). Prefer ones with a verifiable outcome.
- risk: a risk, headwind or uncertainty management itself acknowledges.
- statement: any other materially forward-looking or strategic assertion. Use sparingly.

Rules:
1. `quote` MUST be copied verbatim and contiguously from the transcript, at most 45 words. Never paraphrase, never splice sentences, never use ellipses. It is machine-checked against the source; a non-matching quote invalidates the record.
2. Put numbers in value_low/value_high exactly as stated and describe units separately. "$4.2 to $4.4 billion" -> value_low 4.2, value_high 4.4, unit usd, scale billion, qualifier range. "about $5 billion" -> both 5, qualifier approximately. "at least 10%" -> value_low 10, qualifier at_least.
3. basis: "absolute" for levels, "yoy_growth_pct"/"qoq_growth_pct" for growth rates, "level_pct" for margin percentages. Use metric "other" if the metric is not in the enum.
4. target_period: use relation current_quarter / next_quarter / quarter_after_next / current_year / next_year relative to THIS call's fiscal period (given in the user message). Use relation "explicit" with fiscal_year and period when the speaker names a specific fiscal period. Use "unspecified" if the timing is genuinely unclear. Never guess.
5. update_type: reiterated / raised / lowered when management says so relative to prior guidance.
6. Set confidence honestly: below 0.6 when the figure, metric, period or attribution is ambiguous.
7. If a statement is made in Q&A, set section to qa. Include reiterations; do not collapse distinct claims.
8. If the transcript contains no qualifying claims, return an empty list."""


def get_client() -> Anthropic | None:
    """Anthropic client for the API backend; None means 'use the claude CLI'."""
    b = llm.backend()
    if b == "cli":
        if not llm.cli_path():
            raise ExtractionError("The `claude` CLI was not found. Install Claude Code and log in, or set ANTHROPIC_API_KEY.")
        return None
    if not settings.anthropic_api_key:
        raise ExtractionError("No ANTHROPIC_API_KEY set and the `claude` CLI was not found.")
    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    return Anthropic(api_key=settings.anthropic_api_key, max_retries=4, default_headers=headers)


def chunk_transcript(text: str, size: int | None = None, overlap: int = 3000) -> list[str]:
    size = size or settings.chunk_chars
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            nl = text.rfind("\n", start + size // 2, end)
            end = nl if nl != -1 else end
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _call_claude(client: Anthropic, chunk: str, i: int, n: int, ticker: str, fy: int, q: int, date: str) -> list[dict]:
    part = f" (part {i + 1} of {n}; earlier/later parts are processed separately)" if n > 1 else ""
    user = (
        f"Company: {ticker}\nThis call covers fiscal {fy} Q{q}; call date {date or 'unknown'}.{part}\n"
        f"Resolve all relative timing against fiscal {fy} Q{q}.\n\n<transcript>\n{chunk}\n</transcript>"
    )
    return list(llm.structured(client, SYSTEM, user, TOOL, 16000).get("claims", []))


def _flag(code: str, message: str, severity: str) -> dict:
    return {"code": code, "message": message, "severity": severity}


def finalize_claims(raw: list[dict], transcript: str, ticker: str, fy: int, q: int) -> list[dict]:
    """Deterministic post-processing: dedupe, verify citations, normalise units, resolve periods, flag."""
    claims, seen = [], set()
    for r in raw:
        quote = (r.get("quote") or "").strip()
        key = " ".join(quote.lower().split())
        if key and key in seen:
            continue
        seen.add(key)

        c = dict(r)
        c["confidence"] = max(0.0, min(1.0, float(c.get("confidence", 0.5))))
        flags: list[dict] = []

        # 1. citation check
        loc = locate_quote(transcript, quote)
        c["quote_verified"] = loc is not None
        c["source"] = {"char_start": loc[0], "char_end": loc[1]} if loc else None
        if not loc:
            c["confidence"] = min(c["confidence"], 0.4)
            flags.append(_flag("quote_not_found", "Quote could not be matched verbatim in the transcript.", "high"))

        # 2. unit normalisation
        lo, hi = c.get("value_low"), c.get("value_high")
        mult = SCALE.get(c.get("scale") or "units", 1) if c.get("unit") == "usd" else 1
        c["norm_low"] = lo * mult if lo is not None else None
        c["norm_high"] = hi * mult if hi is not None else None

        # 3. period resolution
        tgt = resolve_target(c.get("target_period"), fy, q)
        c["resolved_period"] = {"fiscal_year": tgt[0], "period": tgt[1]} if tgt else None

        quant = c["claim_type"] == "guidance" and c.get("metric") and (lo is not None or hi is not None)
        c["quantitative"] = bool(quant)
        if quant and not tgt:
            flags.append(_flag("missing_target_period", "Quantitative guidance has no resolvable target period.", "high"))
        if quant and c.get("metric") == "other":
            flags.append(_flag("unsupported_metric", "Metric is not available from FMP; cannot be verified automatically.", "low"))
        if c["confidence"] < 0.6 and c["quote_verified"]:
            flags.append(_flag("low_confidence", f"Extraction confidence {c['confidence']:.2f} is below 0.60.", "medium"))
        if quant and lo is not None and hi is not None and lo > hi:
            c["value_low"], c["value_high"] = hi, lo
            c["norm_low"], c["norm_high"] = c["norm_high"], c["norm_low"]

        c["flags"] = flags
        pend = any(f["severity"] in ("medium", "high") for f in flags)
        c["review"] = {"status": "pending" if pend else "none"}
        claims.append(c)

    order = {"guidance": 0, "commitment": 1, "risk": 2, "statement": 3}
    claims.sort(key=lambda c: (order.get(c["claim_type"], 9), (c.get("source") or {}).get("char_start", 10**9)))
    prefix = f"{ticker}-{call_key(fy, q)}"
    for n, c in enumerate(claims, 1):
        c["claim_id"] = f"{prefix}-{n:03d}"
    return claims


def extract_claims(transcript: str, ticker: str, fy: int, q: int, call_date: str = "", client: Anthropic | None = None) -> dict:
    """Return a complete ledger entry (without persisting)."""
    client = client or get_client()
    chunks = chunk_transcript(transcript)
    raw: list[dict] = []
    for i, ch in enumerate(chunks):
        raw.extend(_call_claude(client, ch, i, len(chunks), ticker, fy, q, call_date))
    claims = finalize_claims(raw, transcript, ticker, fy, q)
    return {
        "schema_version": 1,
        "ticker": ticker,
        "fiscal_year": fy,
        "quarter": q,
        "call_date": call_date,
        "source": {
            "provider": "FMP earning-call-transcript",
            "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
            "transcript_chars": len(transcript),
            "extractor_model": settings.model if client is not None else "claude CLI",
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chunks": len(chunks),
        },
        "claims": claims,
    }
