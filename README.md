# Guidance Ledger: Earnings Call Intelligence & Management Credibility System

Pulls earnings call transcripts (FMP), extracts management guidance / commitments / risks with Claude, stores them as a
cited JSON ledger, compares them with reported financials (FMP), and produces a **Management Credibility Score (MCS)**
with flags for human review.

## Run it

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY and FMP_API_KEY
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
python -m pytest tests          # 12 tests (Claude and FMP are mocked)
```
Or: `docker build -t guidance-ledger . && docker run -p 8000:8000 --env-file .env -v $PWD/data:/app/data guidance-ledger`

> The FMP connector you added inside claude.ai is only available in that chat. This app calls the FMP REST API
> directly with `FMP_API_KEY`. FMP earnings transcripts require a paid plan; if your plan lacks them, use
> **Paste a transcript instead** in the UI (financial actuals still come from FMP).

## Deliverables map

| # | Deliverable | Where |
|---|---|---|
| 1 | Transcript extraction with Claude | `backend/app/extractor.py` (forced tool-use for schema-valid output, chunking, retry) |
| 2 | Structured guidance ledger + citations | `data/ledger/<TICKER>/FY2025Q3.json`, `backend/app/ledger.py` |
| 3 | Guidance vs actual | `backend/app/reconcile.py` (+ range-vs-actual view in UI) |
| 4 | Management Credibility Score + review flags | `backend/app/scoring.py`, `backend/app/pipeline.py::review_queue`, Review page |

## Pipeline

```
FMP transcript ─► Claude (record_claims tool) ─► post-processing ─► ledger JSON
                                                  • verbatim-quote check + char offsets
                                                  • unit/scale normalisation ($4.2B -> 4.2e9)
                                                  • relative period -> explicit fiscal period
FMP income statement / cash flow ─► reconcile.py ─► guided range vs actual per claim
later transcripts (next 2 calls) ─► Claude (record_assessments) ─► commitment delivery, evidence quote verified
                                                  └──────────────► scoring.py ─► MCS + flags ─► review queue
```

### Ledger entry (abridged)
```json
{"ticker":"MSFT","fiscal_year":2025,"quarter":3,"call_date":"2025-04-30",
 "source":{"provider":"FMP earning-call-transcript","transcript_sha256":"…","extractor_model":"claude-sonnet-5-5"},
 "claims":[{"claim_id":"MSFT-FY2025Q3-001","claim_type":"guidance","speaker":"Amy Hood","role":"CFO",
   "summary":"Q4 revenue guidance","quote":"…verbatim…","quote_verified":true,"source":{"char_start":10234,"char_end":10301},
   "metric":"revenue","basis":"absolute","value_low":4.2,"value_high":4.4,"unit":"usd","scale":"billion",
   "norm_low":4.2e9,"norm_high":4.4e9,"resolved_period":{"fiscal_year":2025,"period":"Q4"},
   "confidence":0.93,"flags":[],"review":{"status":"none"}}]}
```

## How the score works (`scoring.py`)

`MCS = 0.50·Forecast accuracy + 0.30·Commitment delivery + 0.20·Evidence quality` (each 0-100)

* **Forecast accuracy**: each guided figure is scored 100 if the actual lands inside the guided range (point estimates get a ±1% / ±0.5pt band).
  Outside the range the score falls linearly to 0 at a 10% miss of the midpoint (margins: 10 percentage points). Overshoots
  are penalised at half the rate of shortfalls (conservative guidance is less misleading than optimistic). Capex is neutral (both directions full weight).
  Supports revenue, gross profit, operating income, net income, EBITDA, EPS, capex, FCF, margins, and YoY/QoQ growth rates.
* **Commitment delivery**: delivered = 100, partially = 50, not delivered = 0. A status other than "no evidence" is accepted only if Claude
  supplies an evidence quote that is found verbatim in a later transcript; otherwise it is downgraded. Silence is never counted as failure.
* **Evidence quality**: share of claims with verified quotes, share of quantitative guidance verifiable against actuals, mean extraction confidence.
* Items are weighted by extraction confidence and recency (8-quarter half-life); small samples are shrunk toward 50 so one quarter can't dominate.
  Missing components are dropped and weights re-normalised.

**Flags (→ review queue, confidence "low" = provisional):** quote not found, low-confidence extraction, missing target period, missing actual,
unverifiable metric, fewer than 4 scored items, fewer than 3 calls, <50% coverage, unassessed commitments, unverified evidence quotes.

Reviewers can **approve**, **correct values / target period / actual**, or **exclude** a claim; scores recompute instantly without new Claude calls.

## API

`GET /api/config` · `GET /api/companies` · `GET /api/companies/{t}` · `GET /api/companies/{t}/ledger/{fy}/{q}` ·
`GET /api/companies/{t}/transcript/{fy}/{q}?start&end` · `POST /api/pipeline` · `POST /api/ingest/manual` ·
`POST /api/companies/{t}/analyze` · `GET /api/jobs/{id}` · `GET /api/review-queue` · `POST /api/review/{t}/{claim_id}`

Interactive docs at `/docs`.

## Known limits
* Guidance is verified only for metrics FMP reports (list above). Segment KPIs, bookings, subscribers, etc. are logged in the ledger but marked "not checkable".
* Fiscal-period matching relies on FMP's `fiscalYear`/`period` labels; companies with unusual calendars may need a reviewer correction.
* Non-USD reporters: guidance and actuals are compared in the company's reporting currency as stated on the call; check the flag if a call quotes a different currency.
* Cost: one Claude request per call for extraction (more for very long transcripts) and one per call for commitment checks. Results are cached on disk; re-runs skip finished calls unless `force` is set.
* Output is decision support for analysts, not investment advice.
