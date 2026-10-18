"""FastAPI application: REST API + static frontend."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ledger, llm, pipeline
from .config import settings
from .fmp import FMPClient, FMPError

app = FastAPI(title="Earnings Call Intelligence & Management Credibility System")
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

_pool = ThreadPoolExecutor(max_workers=2)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def fmp_client() -> FMPClient | None:
    return FMPClient() if settings.fmp_api_key else None


def run_job(fn, *args) -> str:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "status": "running", "log": [], "result": None, "error": None}
    with _jobs_lock:
        _jobs[jid] = job

    def log(msg: str) -> None:
        job["log"].append(msg)

    def work() -> None:
        try:
            job["result"] = fn(*args, log)
            job["status"] = "done"
        except Exception as e:  # surface any failure to the UI
            job["error"] = f"{type(e).__name__}: {e}"
            job["status"] = "error"

    _pool.submit(work)
    return jid


# ------------------------------------------------------------------ models
class PipelineReq(BaseModel):
    ticker: str
    last_n: int = Field(6, ge=1, le=20)
    periods: list[dict] | None = None          # [{"fiscal_year": 2025, "quarter": 3}]
    check_commitments: bool = True
    force: bool = False


class AnalyzeReq(BaseModel):
    check_commitments: bool = True
    force: bool = False


class ManualReq(BaseModel):
    ticker: str
    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    call_date: str = ""
    transcript: str = Field(min_length=500)


class ReviewReq(BaseModel):
    action: str
    edits: dict | None = None
    actual_override: float | None = None
    note: str = ""


# ------------------------------------------------------------------ routes
@app.get("/api/config")
def config():
    return {"anthropic": llm.available(), "llm_backend": llm.backend(), "fmp": bool(settings.fmp_api_key), "model": settings.model}


@app.get("/api/companies")
def companies():
    out = []
    for t in ledger.list_tickers():
        res = ledger.load_results(t) or {}
        mcs = res.get("mcs") or {}
        entries = ledger.load_all(t)
        out.append({
            "ticker": t, "calls": len(entries), "claims": sum(len(e["claims"]) for e in entries),
            "score": mcs.get("score"), "grade": mcs.get("grade"), "confidence": mcs.get("confidence"),
            "needs_review": mcs.get("needs_human_review", True), "flags": len(mcs.get("flags", [])),
            "updated_at": res.get("updated_at"),
        })
    return out


@app.get("/api/companies/{ticker}")
def company(ticker: str):
    try:
        t = ledger.norm_ticker(ticker)
    except ValueError as e:
        raise HTTPException(400, str(e))
    entries = ledger.load_all(t)
    if not entries:
        raise HTTPException(404, f"No ledger for {t}")
    res = ledger.load_results(t) or {}
    return {
        "ticker": t,
        "calls": [{"fiscal_year": e["fiscal_year"], "quarter": e["quarter"], "call_date": e.get("call_date"),
                   "claims": len(e["claims"]), "source": e["source"]} for e in entries],
        "results": res,
    }


@app.get("/api/companies/{ticker}/ledger/{fy}/{q}")
def ledger_entry(ticker: str, fy: int, q: int):
    e = ledger.load_entry(ticker, fy, q)
    if not e:
        raise HTTPException(404, "Ledger entry not found")
    return e


@app.get("/api/companies/{ticker}/transcript/{fy}/{q}")
def transcript_snippet(ticker: str, fy: int, q: int, start: int = 0, end: int = 0, pad: int = 600):
    got = ledger.load_transcript(ticker, fy, q)
    if not got:
        raise HTTPException(404, "Transcript not stored")
    text = got[0]
    a, b = max(0, start - pad), min(len(text), end + pad)
    return {"before": text[a:start], "match": text[start:end], "after": text[end:b]}


@app.post("/api/pipeline")
def start_pipeline(req: PipelineReq):
    if not llm.available():
        raise HTTPException(400, "No Claude backend: set ANTHROPIC_API_KEY or install the Claude Code CLI (`claude`) and log in.")
    try:
        t = ledger.norm_ticker(req.ticker)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def job(log):
        fmp = fmp_client()
        if req.periods:
            periods = [(int(p["fiscal_year"]), int(p["quarter"])) for p in req.periods]
        elif fmp is None:
            raise RuntimeError("FMP_API_KEY is not configured; upload transcripts manually instead.")
        else:
            periods = pipeline.select_periods(t, fmp, req.last_n)
            log(f"{len(periods)} transcript(s) selected for {t}")
        if not periods:
            raise RuntimeError(f"No transcripts found for {t}. Check the ticker and that your FMP plan includes transcripts.")
        for fy, q in periods:
            pipeline.ingest_period(t, fy, q, fmp, req.force, log)
        return pipeline.analyze(t, fmp, req.check_commitments, req.force, log)["mcs"]

    return {"job_id": run_job(job)}


@app.post("/api/ingest/manual")
def ingest_manual(req: ManualReq):
    if not llm.available():
        raise HTTPException(400, "No Claude backend: set ANTHROPIC_API_KEY or install the Claude Code CLI (`claude`) and log in.")
    try:
        t = ledger.norm_ticker(req.ticker)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def job(log):
        ledger.save_transcript(t, req.fiscal_year, req.quarter, req.transcript, req.call_date, "manual")
        pipeline.ingest_period(t, req.fiscal_year, req.quarter, None, True, log)
        return pipeline.analyze(t, fmp_client(), False, False, log)["mcs"]

    return {"job_id": run_job(job)}


@app.post("/api/companies/{ticker}/analyze")
def analyze(ticker: str, req: AnalyzeReq):
    try:
        t = ledger.norm_ticker(ticker)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if req.check_commitments and not llm.available():
        raise HTTPException(400, "No Claude backend: set ANTHROPIC_API_KEY or install the Claude Code CLI (`claude`) and log in.")
    return {"job_id": run_job(lambda log: pipeline.analyze(t, fmp_client(), req.check_commitments, req.force, log)["mcs"])}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    j = _jobs.get(jid)
    if not j:
        raise HTTPException(404, "Unknown job")
    return j


@app.get("/api/review-queue")
def review_queue(ticker: str | None = None):
    return pipeline.review_queue(ledger.norm_ticker(ticker) if ticker else None)


@app.post("/api/review/{ticker}/{claim_id}")
def review(ticker: str, claim_id: str, req: ReviewReq):
    try:
        t = ledger.norm_ticker(ticker)
        pipeline.apply_review(t, claim_id, req.action, req.edits, req.actual_override, req.note)
    except KeyError:
        raise HTTPException(404, "Claim not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # deterministic recompute only (no Claude calls; cached commitment assessments are reused)
    return pipeline.analyze(t, fmp_client(), check_commitments=False)["mcs"]


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
