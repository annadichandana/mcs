"""Run the system WITHOUT the Anthropic API (no credits needed).

You do the Claude step by hand in the normal Claude chat (claude.ai); this script does everything else.
Put this file in earnings-credibility/backend/ and run from there.

Step 1 - make the prompt file (uses FMP only):
    python free_mode.py prepare MSFT            # second-newest call
    python free_mode.py prepare MSFT 2026 3     # a specific call

Step 2 - open the .txt file it created, copy ALL of it into a new claude.ai chat, send it.
         Claude replies with JSON. Copy that reply into a file, e.g. reply.json

Step 3 - load the reply (checks quotes, saves ledger, compares with actuals, scores):
    python free_mode.py load MSFT 2026 3 reply.json
"""
import json
import re
import sys
from datetime import datetime, timezone

from app import ledger, pipeline
from app.config import settings
from app.extractor import CLAIM_SCHEMA, SYSTEM, finalize_claims
from app.fmp import FMPClient, FMPError


def _describe(props: dict) -> str:
    lines = []
    for name, p in props.items():
        t = p.get("type")
        t = "|".join(map(str, t)) if isinstance(t, list) else t
        enum = p.get("enum")
        extra = f" one of {[e for e in enum if e is not None]}" if enum else ""
        lines.append(f'- "{name}" ({t}){extra}: {p.get("description", "")}'.rstrip(": "))
        if name == "target_period":
            lines.append('    object with "relation" (one of current_quarter, next_quarter, quarter_after_next, '
                         'current_year, next_year, explicit, unspecified), "fiscal_year" (integer|null), '
                         '"period" (Q1|Q2|Q3|Q4|FY|null)')
    return "\n".join(lines)


def prepare(ticker: str, fy: int | None, q: int | None) -> None:
    ticker = ledger.norm_ticker(ticker)
    if fy is None:
        if not settings.fmp_api_key:
            sys.exit("FMP_API_KEY missing in .env")
        try:
            dates = FMPClient().transcript_dates(ticker)
        except FMPError as e:
            sys.exit(f"FMP error: {e}")
        if not dates:
            sys.exit("No transcripts found.")
        pick = dates[1] if len(dates) > 1 else dates[0]
        fy, q = pick["fiscal_year"], pick["quarter"]

    got = ledger.load_transcript(ticker, fy, q)
    if not got:
        t = FMPClient().transcript(ticker, fy, q)
        if not t:
            sys.exit("Transcript not available.")
        ledger.save_transcript(ticker, fy, q, t["content"], t["date"], "fmp")
        got = (t["content"], {"call_date": t["date"]})
    text, meta = got

    prompt = f"""{SYSTEM}

OUTPUT FORMAT
Reply with ONE JSON object and nothing else (a single ```json code block is fine): {{"claims": [ ... ]}}
Each claim is an object with these fields (omit fields that do not apply, use null where allowed):
{_describe(CLAIM_SCHEMA["properties"])}
Required on every claim: {", ".join(CLAIM_SCHEMA["required"])}.

Company: {ticker}
This call covers fiscal {fy} Q{q}; call date {meta.get("call_date") or "unknown"}.
Resolve all relative timing against fiscal {fy} Q{q}.

<transcript>
{text}
</transcript>
"""
    out = settings.data_dir / "chat_prompts"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ticker}_FY{fy}Q{q}.txt"
    path.write_text(prompt, encoding="utf-8")
    print(f"Prompt written: {path}  ({len(prompt):,} characters)")
    print(f"\nNext: paste its contents into claude.ai, save Claude's JSON reply to a file, then run:")
    print(f"  python free_mode.py load {ticker} {fy} {q} reply.json")


def load(ticker: str, fy: int, q: int, file: str) -> None:
    ticker = ledger.norm_ticker(ticker)
    got = ledger.load_transcript(ticker, fy, q)
    if not got:
        sys.exit("Transcript not stored yet. Run the 'prepare' step first.")
    text, meta = got

    raw_text = open(file, encoding="utf-8").read().strip()
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text)
    if not raw_text.startswith(("{", "[")):  # tolerate chatter around the JSON
        raw_text = raw_text[raw_text.find("{"): raw_text.rfind("}") + 1]
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not read the JSON: {e}. Make sure you copied Claude's full reply.")
    raw = data["claims"] if isinstance(data, dict) else data

    claims = finalize_claims(raw, text, ticker, fy, q)
    ledger.save_entry({
        "schema_version": 1, "ticker": ticker, "fiscal_year": fy, "quarter": q,
        "call_date": meta.get("call_date", ""),
        "source": {"provider": "FMP transcript + manual Claude chat extraction", "transcript_chars": len(text),
                   "extractor_model": "claude.ai chat (manual)",
                   "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "claims": claims,
    })
    kinds = {}
    for c in claims:
        kinds[c["claim_type"]] = kinds.get(c["claim_type"], 0) + 1
    print(f"{len(claims)} claims saved {kinds}; quotes verified {sum(c['quote_verified'] for c in claims)}/{len(claims)}; "
          f"flagged for review {sum(c['review']['status'] == 'pending' for c in claims)}")
    print(f"Ledger file: {ledger.ledger_path(ticker, fy, q)}")

    fmp = FMPClient() if settings.fmp_api_key else None
    res = pipeline.analyze(ticker, fmp, check_commitments=False)
    print("\n--- Guidance vs actual ---")
    for r in res["reconciliation"]:
        if r["status"] == "evaluated":
            g, a = r["guidance"], r["actual"]["value"]
            print(f"{r['metric']:<18} guided {g['low']} to {g['high']}  actual {a:.4g}  -> {r['outcome']} (accuracy {r['accuracy_score']})")
        else:
            print(f"{r['metric'] or 'n/a':<18} {r['status']}  {r['summary']}")
    m = res["mcs"]
    print(f"\nMCS: {m['score']}  ({m['grade']}, {m['confidence']} confidence)")
    for f in m["flags"]:
        print(f"  [{f['severity']}] {f['message']}")
    print("\nSee it in the dashboard: uvicorn app.main:app --port 8000  ->  http://localhost:8000")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "prepare":
        prepare(a[1], int(a[2]) if len(a) > 2 else None, int(a[3]) if len(a) > 3 else None)
    elif len(a) == 5 and a[0] == "load":
        load(a[1], int(a[2]), int(a[3]), a[4])
    else:
        print(__doc__)
