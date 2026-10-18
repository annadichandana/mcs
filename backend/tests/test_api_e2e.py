"""End-to-end: HTTP API -> pipeline -> ledger -> reconcile -> commitments -> MCS, with Claude and FMP mocked."""
import os
import tempfile
import time
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from app import commitments, extractor, main  # noqa: E402
from app.config import settings  # noqa: E402

Q3 = ("Operator: Welcome to the call.\n"
      "Pat Lee, CFO: For the fourth quarter we expect revenue of $4.2 billion to $4.4 billion. "
      "We will open the Singapore data center by the end of Q4. Supply constraints remain a risk.\n")
Q4 = ("Operator: Welcome.\nPat Lee, CFO: We opened the Singapore data center in December, on schedule.\n")
FIN = {(2025, "Q4"): {"revenue": 4.3e9, "period_end": "2025-12-31"}, (2025, "Q3"): {"revenue": 4.0e9}}


class FakeFMP:
    def transcript_dates(self, s):
        return [{"fiscal_year": 2025, "quarter": 4, "date": "2026-01-30"}, {"fiscal_year": 2025, "quarter": 3, "date": "2025-10-30"}]

    def transcript(self, s, y, q):
        return {"content": {(2025, 3): Q3, (2025, 4): Q4}.get((y, q)), "date": f"{y}-{q}"} if (y, q) in {(2025, 3), (2025, 4)} else None

    def financials(self, s):
        return FIN


def tool_msg(name, payload):
    return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", name=name, input=payload)])


class FakeClaude:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        name = kw["tools"][0]["name"]
        user = kw["messages"][0]["content"]
        if name == "record_claims":
            if "Pat Lee, CFO: We opened" in user and "fiscal 2025 Q3" not in user:
                return tool_msg(name, {"claims": []})
            g = {"claim_type": "guidance", "speaker": "Pat Lee", "role": "CFO", "section": "prepared_remarks",
                 "summary": "Q4 revenue guidance", "quote": "we expect revenue of $4.2 billion to $4.4 billion", "confidence": 0.95,
                 "metric": "revenue", "basis": "absolute", "value_low": 4.2, "value_high": 4.4, "qualifier": "range", "unit": "usd",
                 "scale": "billion", "target_period": {"relation": "next_quarter"}}
            c = {"claim_type": "commitment", "speaker": "Pat Lee", "role": "CFO", "section": "prepared_remarks",
                 "summary": "Open the Singapore data center", "quote": "We will open the Singapore data center by the end of Q4",
                 "confidence": 0.9, "deadline_text": "by the end of Q4", "target_period": {"relation": "next_quarter"}}
            bad = {"claim_type": "risk", "speaker": "Pat Lee", "section": "prepared_remarks", "summary": "Supply risk",
                   "quote": "Supply chains will definitely collapse next year", "confidence": 0.8, "target_period": {"relation": "unspecified"}}
            return tool_msg(name, {"claims": [g, c, bad]})
        return tool_msg(name, {"assessments": [{
            "claim_id": "TST-FY2025Q3-002", "status": "delivered", "evidence_period": "FY2025Q4",
            "evidence_quote": "We opened the Singapore data center in December, on schedule", "rationale": "Opened on time.", "confidence": 0.9}]})


def test_full_flow(monkeypatch):
    settings.anthropic_api_key = "test"
    monkeypatch.setattr(extractor, "get_client", lambda: FakeClaude())
    monkeypatch.setattr(commitments, "get_client", lambda: FakeClaude())
    monkeypatch.setattr(main, "fmp_client", lambda: FakeFMP())
    c = TestClient(main.app)

    jid = c.post("/api/pipeline", json={"ticker": "tst", "last_n": 2}).json()["job_id"]
    for _ in range(100):
        job = c.get(f"/api/jobs/{jid}").json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done", job

    d = c.get("/api/companies/TST").json()
    assert [x["quarter"] for x in d["calls"]] == [3, 4]
    res = d["results"]
    rec = res["reconciliation"][0]
    assert rec["status"] == "evaluated" and rec["outcome"] == "within" and rec["accuracy_score"] == 100.0
    cm = res["commitments"][0]
    assert cm["status"] == "delivered" and cm["evidence_verified"]
    mcs = res["mcs"]
    assert mcs["score"] and mcs["needs_human_review"] and mcs["confidence"] == "low"

    # fabricated quote surfaces in the review queue and in the ledger JSON
    q = c.get("/api/review-queue?ticker=TST").json()
    assert len(q) == 1 and q[0]["reasons"][0]["code"] == "quote_not_found"
    led = c.get("/api/companies/TST/ledger/2025/3").json()
    assert all("quote_verified" in x for x in led["claims"]) and led["source"]["transcript_sha256"]

    snip = c.get("/api/companies/TST/transcript/2025/3", params={"start": led["claims"][0]["source"]["char_start"], "end": led["claims"][0]["source"]["char_end"]}).json()
    assert "4.2 billion" in snip["match"]

    # rejecting it clears the queue and raises the evidence-quality component
    before = mcs["components"]["evidence_quality"]["score"]
    after = c.post(f"/api/review/TST/{q[0]['claim_id']}", json={"action": "reject"}).json()
    assert c.get("/api/review-queue?ticker=TST").json() == []
    assert after["components"]["evidence_quality"]["score"] > before
    assert c.get("/api/companies").json()[0]["ticker"] == "TST"
