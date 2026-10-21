"""Untrusted-text handling, base-rate anchoring, and the explicit no-edge skip.

Crawled titles and symbols are data. They are never copied into the instruction
channel, and strings that look like prompt overrides are dropped.
"""

from __future__ import annotations

import math
import re

_INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+|previous\s+|prior\s+)?instructions"
    r"|system\s+prompt|you\s+are\s+now|do\s+not\s+follow"
    r"|<\s*/?\s*(system|assistant|user)\s*>)",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_KEEP = re.compile(r"[^A-Za-z0-9 ._$-]+")


def redact_secrets(text: str) -> str:
    """Remove key material before hashing or storing a reasoning note."""
    return _SECRET.sub("[redacted]", text)


def sanitize_untrusted(text: str, *, limit: int = 64) -> str:
    """Return a short data token, or empty when the text tries to give orders."""
    if not isinstance(text, str) or _INJECTION.search(text):
        return ""
    cleaned = _KEEP.sub("", redact_secrets(text)).strip()
    return cleaned[:limit]


def anchor_probability(probability: float, base_rate: float, strength: float) -> float:
    """Shrink a probability toward a base rate. Strength is a pseudo-count."""
    if not 0 <= probability <= 1 or not 0 <= base_rate <= 1:
        raise ValueError("probabilities must be in [0, 1]")
    if strength < 0 or not math.isfinite(strength):
        raise ValueError("base-rate strength must be non-negative")
    if strength == 0:
        return probability
    return (probability + strength * base_rate) / (1.0 + strength)


def no_edge_skip(
    probability: float,
    reference: float,
    *,
    min_edge: float,
    cost: float = 0.0,
) -> bool:
    """True when the long-side edge does not clear the configured cost."""
    if min_edge < 0 or cost < 0:
        raise ValueError("edge and cost must be non-negative")
    return (probability - reference) <= (min_edge + cost)


_NUMERIC_FIELDS = (
    "price_usd",
    "liquidity_usd",
    "fdv_usd",
    "price_change_m5",
    "price_change_h1",
    "trades_m5",
    "trades_h1",
    "volume_h1_usd",
    "pool_age_hours",
    "top10_holder_fraction",
    "spike_acceleration",
)
_MINT = re.compile(r"^[A-Za-z0-9]{1,64}$")


def candidate_for_model(raw: dict) -> dict | None:
    """Allowlisted numeric view of one pool. Free text is not forwarded."""
    mint = raw.get("mint")
    if not isinstance(mint, str) or not _MINT.fullmatch(mint):
        return None
    view: dict[str, object] = {"mint": mint}
    symbol = sanitize_untrusted(str(raw.get("symbol") or ""))
    if symbol:
        view["symbol"] = symbol
    for key in _NUMERIC_FIELDS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(float(value)):
            continue
        view[key] = float(value)
    return view
