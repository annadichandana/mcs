import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import ledger, pipeline  # noqa: E402
from app.extractor import chunk_transcript, finalize_claims  # noqa: E402
from app.periods import resolve_target  # noqa: E402
from app.quotes import locate_quote  # noqa: E402
from app.reconcile import reconcile_claim  # noqa: E402
from app.scoring import compute_mcs  # noqa: E402

TRANSCRIPT = (
    "Operator: Welcome.\n"
    "Jane Doe, CFO: For the fourth quarter we expect revenue of $4.2 billion to $4.4 billion, "
    "and we\u2019ll launch the new platform by the end of Q2.\n"
    "Analyst: Will you hit 40% margins?\n"
)
FIN = {
    (2025, "Q4"): {"revenue": 4.5e9, "period_end": "2025-12-31"},
    (2024, "Q4"): {"revenue": 4.0e9},
    (2025, "Q3"): {"revenue": 4.1e9, "gross_margin": 41.0},
}


def raw_guidance(**kw):
    c = {
        "claim_type": "guidance", "speaker": "Jane Doe", "section": "prepared_remarks", "summary": "Q4 revenue guide",
        "quote": "we expect revenue of $4.2 billion to $4.4 billion", "confidence": 0.9, "metric": "revenue",
        "basis": "absolute", "value_low": 4.2, "value_high": 4.4, "qualifier": "range", "unit": "usd", "scale": "billion",
        "target_period": {"relation": "next_quarter"},
    }
    c.update(kw)
    return c


def entry_with(raws, fy=2025, q=3):
    claims = finalize_claims(raws, TRANSCRIPT, "TEST", fy, q)
    return {"ticker": "TEST", "fiscal_year": fy, "quarter": q, "call_date": "2025-10-30", "source": {}, "claims": claims}


def test_quote_location_handles_smart_quotes_and_whitespace():
    assert locate_quote(TRANSCRIPT, "we\u2019ll launch  the new platform by the end of Q2")
    assert locate_quote(TRANSCRIPT, "we expect revenue of $9 billion") is None


def test_period_resolution_rolls_fiscal_year():
    assert resolve_target({"relation": "next_quarter"}, 2025, 4) == (2026, "Q1")
    assert resolve_target({"relation": "next_year"}, 2025, 2) == (2026, "FY")
    assert resolve_target({"relation": "explicit", "fiscal_year": 2026, "period": "Q2"}, 2025, 1) == (2026, "Q2")
    assert resolve_target({"relation": "unspecified"}, 2025, 1) is None


def test_finalize_normalises_units_and_flags_bad_quotes():
    e = entry_with([raw_guidance(), raw_guidance(quote="a fabricated sentence that is not in the call")])
    good = next(c for c in e["claims"] if c["quote_verified"])
    bad = next(c for c in e["claims"] if not c["quote_verified"])
    assert good["norm_low"] == 4.2e9 and good["resolved_period"] == {"fiscal_year": 2025, "period": "Q4"}
    assert good["source"]["char_start"] > 0
    assert bad["review"]["status"] == "pending" and bad["confidence"] <= 0.4
    assert any(f["code"] == "quote_not_found" for f in bad["flags"])


def test_chunking_covers_whole_text():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(2000))
    chunks = chunk_transcript(text, size=20000, overlap=500)
    assert len(chunks) > 1 and chunks[0].startswith("line 0") and chunks[-1].rstrip().endswith("x")


def test_reconcile_above_range_is_a_mild_penalty():
    e = entry_with([raw_guidance()])
    r = reconcile_claim(e["claims"][0], e, FIN)
    assert r["status"] == "evaluated" and r["outcome"] == "above"
    assert r["target_period"] == "FY2025Q4" and 85 <= r["accuracy_score"] < 100


def test_reconcile_below_range_is_penalised_harder_than_above():
    e = entry_with([raw_guidance(value_low=5.2, value_high=5.4)])  # actual 4.5B is ~15% under the midpoint
    r = reconcile_claim(e["claims"][0], e, FIN)
    assert r["outcome"] == "below" and r["accuracy_score"] == 0.0
    e2 = entry_with([raw_guidance(value_low=4.5, value_high=4.6)])
    assert reconcile_claim(e2["claims"][0], e2, FIN)["outcome"] == "within"


def test_yoy_growth_and_margin_and_pending_and_missing():
    g = raw_guidance(basis="yoy_growth_pct", value_low=10, value_high=14, unit="percent", scale=None)
    e = entry_with([g])
    r = reconcile_claim(e["claims"][0], e, FIN)  # 4.5/4.0 -> +12.5%
    assert r["outcome"] == "within" and round(r["actual"]["value"], 1) == 12.5

    m = raw_guidance(metric="gross_margin", basis="level_pct", value_low=40, value_high=40, qualifier="point",
                     unit="percent", scale=None, target_period={"relation": "current_quarter"})
    e = entry_with([m])
    assert reconcile_claim(e["claims"][0], e, FIN)["outcome"] == "above"  # 41 vs 40 +/-0.5

    fut = raw_guidance(target_period={"relation": "explicit", "fiscal_year": 2027, "period": "Q1"})
    e = entry_with([fut])
    assert reconcile_claim(e["claims"][0], e, FIN)["status"] == "pending"

    gap = raw_guidance(target_period={"relation": "explicit", "fiscal_year": 2024, "period": "Q2"})
    e = entry_with([gap])
    r = reconcile_claim(e["claims"][0], e, FIN)
    assert r["status"] == "missing_actual" and r["flags"][0]["severity"] == "medium"


def test_human_override_beats_fmp():
    e = entry_with([raw_guidance()])
    e["claims"][0]["actual_override"] = 4.3e9
    r = reconcile_claim(e["claims"][0], e, FIN)
    assert r["outcome"] == "within" and r["actual"]["source"] == "human_override"


def test_mcs_flags_thin_data_and_shrinks_toward_neutral():
    e = entry_with([raw_guidance(value_low=4.5, value_high=4.6)])
    recon = [reconcile_claim(e["claims"][0], e, FIN)]
    mcs = compute_mcs("TEST", [e], recon, [])
    assert mcs["components"]["forecast_accuracy"]["raw"] == 100.0
    assert 50 < mcs["components"]["forecast_accuracy"]["score"] < 100
    codes = {f["code"] for f in mcs["flags"]}
    assert {"few_observations", "few_calls", "no_commitment_data"} <= codes
    assert mcs["confidence"] == "low" and mcs["needs_human_review"] and mcs["provisional"]


def test_mcs_commitments_and_no_data():
    e = entry_with([raw_guidance(value_low=4.5, value_high=4.6)])
    commits = [{"claim_id": f"c{i}", "call_period": "FY2025Q3", "status": s, "confidence": 0.9}
               for i, s in enumerate(["delivered", "partially_delivered", "not_delivered", "no_evidence", "pending"])]
    mcs = compute_mcs("TEST", [e], [], commits)
    assert mcs["components"]["commitment_delivery"]["n"] == 3
    assert mcs["components"]["commitment_delivery"]["unassessed"] == 1
    assert any(f["code"] == "no_forecast_data" for f in mcs["flags"])
    empty = compute_mcs("TEST", [], [], [])
    assert empty["score"] is None and empty["needs_human_review"]


def test_review_queue_and_edit_flow():
    bad = raw_guidance(quote="a fabricated sentence that is not in the call")
    e = entry_with([bad])
    ledger.save_entry(e)
    q = pipeline.review_queue("TEST")
    assert len(q) == 1 and q[0]["reasons"][0]["code"] == "quote_not_found"
    cid = q[0]["claim_id"]
    pipeline.apply_review("TEST", cid, "edit", {"value_low": 4.5, "value_high": 4.6}, None, "fixed")
    c = ledger.load_entry("TEST", 2025, 3)["claims"][0]
    assert c["norm_low"] == 4.5e9 and c["review"]["status"] == "edited" and pipeline.review_queue("TEST") == []
    pipeline.apply_review("TEST", cid, "reject", None, None, "")
    assert ledger.load_entry("TEST", 2025, 3)["claims"][0]["review"]["status"] == "rejected"
