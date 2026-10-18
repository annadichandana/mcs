"""Test the system on ONE earnings call.

Put this file in earnings-credibility/backend/ (next to the `app` folder) and run:

    python try_one.py MSFT              # second-newest call (its guidance can already be checked against actuals)
    python try_one.py MSFT 2025 3       # a specific fiscal year / quarter
    python try_one.py MSFT --no-claude-analysis   # only list available calls, spend nothing

Uses ~1 Claude request (API key OR the Claude Code CLI). Needs FMP_API_KEY in the project .env file.
"""
import sys

from app import ledger, llm, pipeline
from app.config import settings
from app.extractor import extract_claims
from app.fmp import FMPClient, FMPError


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    list_only = "--no-claude-analysis" in sys.argv
    ticker = ledger.norm_ticker(args[0] if args else "MSFT")

    if not settings.fmp_api_key:
        sys.exit("FMP_API_KEY missing in .env")
    if not list_only and not llm.available():
        sys.exit("No Claude backend: set ANTHROPIC_API_KEY in .env or install the Claude Code CLI and log in")

    fmp = FMPClient()
    try:
        dates = fmp.transcript_dates(ticker)
    except FMPError as e:
        sys.exit(f"FMP error: {e}")
    if not dates:
        sys.exit(f"No transcripts found for {ticker}. Your FMP plan may not include transcripts.")

    print(f"Available calls for {ticker}: " + ", ".join(f"FY{d['fiscal_year']}Q{d['quarter']}" for d in dates[:8]))
    if list_only:
        return

    if len(args) >= 3:
        fy, q = int(args[1]), int(args[2])
    else:
        pick = dates[1] if len(dates) > 1 else dates[0]
        fy, q = pick["fiscal_year"], pick["quarter"]
    print(f"\nUsing FY{fy} Q{q}")

    t = fmp.transcript(ticker, fy, q)
    if not t:
        sys.exit("Transcript not available for that period.")
    ledger.save_transcript(ticker, fy, q, t["content"], t["date"], "fmp")
    print(f"Transcript: {len(t['content']):,} characters, call date {t['date']}")

    print("Extracting claims with Claude (this can take a minute)...")
    entry = extract_claims(t["content"], ticker, fy, q, t["date"])
    ledger.save_entry(entry)
    claims = entry["claims"]

    kinds = {}
    for c in claims:
        kinds[c["claim_type"]] = kinds.get(c["claim_type"], 0) + 1
    print(f"\n{len(claims)} claims saved: {kinds}")
    print(f"Quotes verified verbatim: {sum(c['quote_verified'] for c in claims)}/{len(claims)}")
    print(f"Flagged for review: {sum(c['review']['status'] == 'pending' for c in claims)}")
    print(f"Ledger file: {ledger.ledger_path(ticker, fy, q)}")

    print("\n--- Guidance and commitments ---")
    for c in claims:
        if c["claim_type"] not in ("guidance", "commitment"):
            continue
        vals = ""
        if c.get("value_low") is not None:
            vals = f" [{c.get('metric')}: {c['value_low']}-{c['value_high']} {c.get('scale') or ''} {c.get('unit') or ''}]"
        period = c.get("resolved_period")
        per = f" -> FY{period['fiscal_year']} {period['period']}" if period else ""
        ok = "OK " if c["quote_verified"] else "?? "
        print(f"{ok}{c['claim_type']:<10} {c['summary']}{vals}{per}")
        print(f'      "{c["quote"]}"')

    print("\nComparing guidance with reported actuals (commitment checks skipped)...")
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
    print("\nOpen the web app (uvicorn app.main:app --port 8000) to see this in the dashboard.")


if __name__ == "__main__":
    main()
