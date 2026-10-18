import json
import os
import subprocess
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

import pytest  # noqa: E402

from app import commitments, extractor, llm  # noqa: E402
from app.config import settings  # noqa: E402


def fake_run(outputs, seen):
    it = iter(outputs)

    def run(cmd, input=None, **kw):
        seen.append((cmd, input, kw))
        out = next(it)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


@pytest.fixture(autouse=True)
def cli_mode(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "cli")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(llm, "cli_path", lambda: "claude")


def test_backend_selection(monkeypatch):
    assert llm.backend() == "cli" and llm.available() and extractor.get_client() is None
    monkeypatch.setattr(settings, "llm_backend", "auto")
    assert llm.backend() == "cli"
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert llm.backend() == "api"
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(llm, "cli_path", lambda: None)
    assert llm.backend() == "none" and not llm.available()


def test_cli_parses_fenced_json_and_sends_prompt_on_stdin(monkeypatch):
    seen = []
    payload = {"claims": [{"claim_type": "risk"}]}
    out = json.dumps({"type": "result", "is_error": False, "result": "Sure!\n```json\n" + json.dumps(payload) + "\n```"})
    monkeypatch.setattr(llm.subprocess, "run", fake_run([out], seen))
    got = llm.structured(None, "SYS", "USER-TEXT", extractor.TOOL, 100)
    assert got == payload
    cmd, stdin, kw = seen[0]
    assert cmd[:4] == ["claude", "-p", "--output-format", "json"] and "USER-TEXT" in stdin and "SYS" in stdin
    assert kw["cwd"]  # runs outside the project folder


def test_cli_retries_once_then_succeeds_and_then_fails(monkeypatch):
    seen = []
    good = json.dumps({"result": '{"claims": []}'})
    monkeypatch.setattr(llm.subprocess, "run", fake_run([json.dumps({"result": "not json at all"}), good], seen))
    assert llm.structured(None, "s", "u", extractor.TOOL, 1) == {"claims": []} and len(seen) == 2
    monkeypatch.setattr(llm.subprocess, "run", fake_run([json.dumps({"result": "nope"})] * 2, []))
    with pytest.raises(llm.LLMError):
        llm.structured(None, "s", "u", extractor.TOOL, 1)


def test_cli_error_paths(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", fake_run([json.dumps({"is_error": True, "result": "Not logged in"})], []))
    with pytest.raises(llm.LLMError, match="Not logged in"):
        llm.structured(None, "s", "u", extractor.TOOL, 1)

    def boom(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="login required")
    monkeypatch.setattr(llm.subprocess, "run", boom)
    with pytest.raises(llm.LLMError, match="login required"):
        llm.structured(None, "s", "u", extractor.TOOL, 1)


def test_extract_and_commitments_work_through_cli(monkeypatch):
    T = "Pat Lee, CFO: We expect revenue of $4.2 billion to $4.4 billion next quarter and we will open the Singapore data center by Q4.\n"
    claims = {"claims": [{"claim_type": "guidance", "speaker": "Pat Lee", "section": "prepared_remarks", "summary": "rev",
                          "quote": "We expect revenue of $4.2 billion to $4.4 billion", "confidence": 0.9, "metric": "revenue",
                          "basis": "absolute", "value_low": 4.2, "value_high": 4.4, "unit": "usd", "scale": "billion",
                          "target_period": {"relation": "next_quarter"}},
                         {"claim_type": "commitment", "speaker": "Pat Lee", "section": "prepared_remarks",
                          "summary": "Open data center", "quote": "we will open the Singapore data center by Q4", "confidence": 0.9,
                          "target_period": {"relation": "next_quarter"}}]}
    assess = {"assessments": [{"claim_id": "X-FY2025Q3-002", "status": "delivered", "evidence_period": "FY2025Q4",
                               "evidence_quote": "We opened the Singapore data center in December", "rationale": "ok", "confidence": 0.9}]}
    seen = []
    monkeypatch.setattr(llm.subprocess, "run", fake_run([json.dumps({"result": json.dumps(claims)}), json.dumps({"result": json.dumps(assess)})], seen))
    entry = extractor.extract_claims(T, "X", 2025, 3, "2025-10-30")
    assert entry["source"]["extractor_model"] == "claude CLI" and all(c["quote_verified"] for c in entry["claims"])
    res = commitments.assess_entry(entry, [{"fy": 2025, "q": 4, "text": "Pat Lee: We opened the Singapore data center in December, on schedule."}])
    assert res[0]["status"] == "delivered" and res[0]["evidence_verified"]
