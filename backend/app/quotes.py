"""Citation integrity: every extracted quote must be locatable verbatim in the source transcript."""
from __future__ import annotations

import re

# Length-preserving folds so character offsets stay valid against the original text.
_TR = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ", "\u2026": ".",
})


def fold(s: str) -> str:
    return s.translate(_TR)


def locate_quote(transcript: str, quote: str) -> tuple[int, int] | None:
    """Return (start, end) offsets of quote in transcript, or None if it is not a verbatim match."""
    if not quote:
        return None
    words = fold(quote).strip().strip("\"'").split()
    if len(words) < 3:
        return None
    pattern = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pattern, fold(transcript), re.IGNORECASE)
    return (m.start(), m.end()) if m else None
