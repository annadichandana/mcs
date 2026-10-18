"""Guidance vs actual: compare quantitative management guidance with reported FMP financials."""
from __future__ import annotations

from datetime import date

from .periods import idx, period_key, prev_quarter

SUPPORTED = {
    "revenue", "gross_profit", "gross_margin", "operating_income", "operating_margin",
    "net_income", "net_margin", "eps", "ebitda", "capex", "free_cash_flow",
}
PCT_METRICS = {"gross_margin", "operating_margin", "net_margin"}
NEUTRAL_METRICS = {"capex"}           # above/below guidance is not "good" or "bad" per se
POINT_TOL_REL = 0.01                  # +/-1% band around a point estimate (absolute levels)
POINT_TOL_PTS = 0.5                   # +/-0.5 percentage points (growth rates and margins)
FULL_PENALTY_REL = 0.10               # a 10% miss -> accuracy score 0
BEAT_WEIGHT = 0.5                     # overshoot is penalised at half the rate of a shortfall


def _val(fin: dict, fy: int, per: str, metric: str):
    return (fin.get((fy, per)) or {}).get(metric)


def compute_actual(fin: dict, fy: int, per: str, metric: str, basis: str):
    """Return (actual, note) where actual may be None."""
    if basis in ("absolute", "level_pct"):
        return _val(fin, fy, per, metric), None
    cur = _val(fin, fy, per, metric)
    if basis == "yoy_growth_pct":
        prev = _val(fin, fy - 1, per, metric)
    elif basis == "qoq_growth_pct":
        if per == "FY":
            return None, "qoq growth is undefined for a full year"
        pfy, pq = prev_quarter(fy, int(per[1]))
        prev = _val(fin, pfy, f"Q{pq}", metric)
    else:
        return None, f"unsupported basis {basis}"
    if cur is None or prev in (None, 0):
        return None, None
    return (cur / abs(prev) - 1) * 100, None


def _bounds(c: dict):
    lo, hi, q = c.get("norm_low"), c.get("norm_high"), c.get("qualifier")
    if lo is not None and hi is not None:
        return lo, hi, "range" if lo != hi else "point"
    if q == "at_least" and lo is not None:
        return lo, None, "at_least"
    if q == "at_most":
        return None, (hi if hi is not None else lo), "at_most"
    p = lo if lo is not None else hi
    return p, p, "point"


def _result_base(c: dict, entry: dict) -> dict:
    return {
        "claim_id": c["claim_id"],
        "ticker": entry["ticker"],
        "call_period": f"FY{entry['fiscal_year']}Q{entry['quarter']}",
        "call_date": entry.get("call_date", ""),
        "metric": c.get("metric"),
        "basis": c.get("basis"),
        "unit": c.get("unit"),
        "summary": c.get("summary"),
        "speaker": c.get("speaker"),
        "update_type": c.get("update_type"),
        "claim_confidence": c.get("confidence"),
        "target_period": None,
        "guidance": None,
        "actual": None,
        "outcome": None,
        "deviation_pct": None,
        "accuracy_score": None,
        "status": None,
        "flags": [],
    }


def reconcile_claim(c: dict, entry: dict, fin: dict, today: date | None = None) -> dict:
    today = today or date.today()
    r = _result_base(c, entry)
    flag = lambda code, msg, sev: r["flags"].append({"code": code, "message": msg, "severity": sev})

    if not c.get("quantitative"):
        r["status"] = "qualitative"
        return r
    metric = c["metric"]
    basis = c.get("basis") or "absolute"
    if metric in PCT_METRICS and basis in ("absolute", "level_pct"):
        basis = "level_pct"
    r["basis"] = basis
    if metric not in SUPPORTED or (basis == "level_pct" and metric not in PCT_METRICS) \
            or (metric in PCT_METRICS and basis != "level_pct"):
        r["status"] = "unverifiable_metric"
        flag("unverifiable_metric", f"'{metric}' on basis '{basis}' cannot be checked against FMP data.", "low")
        return r
    rp = c.get("resolved_period")
    if not rp:
        r["status"] = "unresolved_period"
        flag("missing_target_period", "Target period could not be resolved.", "medium")
        return r
    fy, per = rp["fiscal_year"], rp["period"]
    r["target_period"] = period_key(fy, per)

    lo, hi, kind = _bounds(c)
    pct_like = basis != "absolute"
    if kind == "point":
        tol = POINT_TOL_PTS if pct_like else abs(lo) * POINT_TOL_REL
        glo, ghi = lo - tol, hi + tol
    else:
        glo, ghi = lo, hi
    r["guidance"] = {"low": lo, "high": hi, "kind": kind, "band_low": glo, "band_high": ghi}

    # actual (human override beats FMP)
    override = c.get("actual_override")
    if override is not None:
        actual, source, period_end = float(override), "human_override", ""
    else:
        actual, note = compute_actual(fin, fy, per, metric, basis)
        source, period_end = "FMP financial statements", (fin.get((fy, per)) or {}).get("period_end", "")
        if actual is None:
            known = [idx(*k) for k in fin]
            if known and idx(fy, per) > max(known):
                r["status"] = "pending"
            else:
                r["status"] = "missing_actual"
                flag("missing_actual", note or f"No reported {metric} found for {period_key(fy, per)} in FMP.", "medium")
            return r
    r["actual"] = {"value": actual, "source": source, "period_end": period_end}

    # outcome
    if glo is not None and actual < glo:
        outcome, dist = "below", glo - actual
    elif ghi is not None and actual > ghi:
        outcome, dist = "above", actual - ghi
    else:
        outcome, dist = "within", 0.0
    mid = ((lo + hi) / 2) if (lo is not None and hi is not None) else (lo if lo is not None else hi)
    if basis == "absolute":
        rel = dist / abs(mid) if mid else 0.0
    elif basis == "level_pct":
        rel = dist / 10.0
    else:
        rel = dist / max(abs(mid), 5.0)
    weight = BEAT_WEIGHT if (outcome == "above" and metric not in NEUTRAL_METRICS and kind != "at_most") else 1.0
    r["outcome"] = outcome
    r["deviation_pct"] = round((rel if outcome == "above" else -rel) * 100, 2) if outcome != "within" else 0.0
    r["accuracy_score"] = round(max(0.0, 100.0 - 100.0 * rel * weight / FULL_PENALTY_REL), 1)
    r["status"] = "evaluated"
    if c.get("hedged"):
        flag("hedged_language", "Management hedged this guidance; consider before penalising.", "low")
    if (c.get("confidence") or 0) < 0.6:
        flag("low_confidence_claim", "Underlying claim extraction confidence is low.", "medium")
    return r


def reconcile_entries(entries: list[dict], fin: dict) -> list[dict]:
    out = []
    for e in entries:
        for c in e["claims"]:
            if c["claim_type"] != "guidance" or c.get("review", {}).get("status") == "rejected":
                continue
            out.append(reconcile_claim(c, e, fin))
    return out
