"""Fiscal-period helpers. Periods are (fiscal_year:int, period:str) with period in Q1..Q4 or FY."""
from __future__ import annotations

VALID_PERIODS = {"Q1", "Q2", "Q3", "Q4", "FY"}


def period_key(fy: int, period: str) -> str:
    return f"FY{fy}{period}"


def call_key(fy: int, q: int) -> str:
    return f"FY{fy}Q{q}"


def idx(fy: int, period: str) -> int:
    """Monotonic index so periods can be compared. FY sorts with Q4."""
    q = 4 if period == "FY" else int(period[1])
    return fy * 4 + q


def next_quarter(fy: int, q: int) -> tuple[int, int]:
    return (fy + 1, 1) if q == 4 else (fy, q + 1)


def prev_quarter(fy: int, q: int) -> tuple[int, int]:
    return (fy - 1, 4) if q == 1 else (fy, q - 1)


def resolve_target(tp: dict | None, call_fy: int, call_q: int) -> tuple[int, str] | None:
    """Turn Claude's relative period description into an explicit (fy, period)."""
    if not tp:
        return None
    rel = tp.get("relation")
    if rel == "current_quarter":
        return call_fy, f"Q{call_q}"
    if rel == "next_quarter":
        fy, q = next_quarter(call_fy, call_q)
        return fy, f"Q{q}"
    if rel == "quarter_after_next":
        fy, q = next_quarter(*next_quarter(call_fy, call_q))
        return fy, f"Q{q}"
    if rel == "current_year":
        return call_fy, "FY"
    if rel == "next_year":
        return call_fy + 1, "FY"
    if rel == "explicit":
        fy, per = tp.get("fiscal_year"), tp.get("period")
        if isinstance(fy, int) and per in VALID_PERIODS:
            return fy, per
    return None
