from __future__ import annotations

import math
from typing import Optional


DEFAULT_FUEL_LOW_THRESHOLD = 1500.0
FUEL_UNIT = "F"
FUEL_RATE_UNIT = "F/hand"

_FUEL_CRITICAL_CODES = frozenset({
    "FUEL_EXHAUSTED",
    "FUEL_AUTH_CRITICAL",
    "FUEL_UNAVAILABLE_CRITICAL",
})


def normalize_fuel_threshold(value: object) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("fuel low threshold must be a positive finite number") from exc
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("fuel low threshold must be a positive finite number")
    return threshold


def fuel_reason_code(
    quantity: Optional[float],
    source_reason: str,
    threshold: float,
) -> str:
    reason = str(source_reason or "").strip().upper()
    if reason in _FUEL_CRITICAL_CODES:
        return reason
    if quantity is None:
        return reason if reason.startswith("FUEL_") else "FUEL_UNAVAILABLE"
    if quantity < normalize_fuel_threshold(threshold):
        return "FUEL_LOW"
    if reason in {"FUEL_BACKEND_WARNING", "FUEL_INVALID_QUANTITY", "FUEL_INVALID_PAYLOAD"}:
        return reason
    return reason if reason.startswith("FUEL_") else "FUEL_AVAILABLE"


def latest_fuel_reading(rows: list) -> tuple[Optional[float], Optional[float]]:
    """Pick the newest fuelQty. Never sum across devices or tables.

    Recency is ``fuel_updated_at`` then ``fuel_sequence``. Equal recency keeps
    the later row so two devices reporting 5000 then 5642 yield 5642.
    """
    best: Optional[tuple[float, int, float, Optional[float]]] = None
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        raw = row.get("fuel_quantity")
        if raw is None:
            continue
        try:
            quantity = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(quantity) or quantity < 0:
            continue
        recency = 0.0
        for key in ("fuel_updated_at", "fuel_sequence"):
            try:
                recency = float(row.get(key) or 0)
            except (TypeError, ValueError):
                recency = 0.0
            if recency:
                break
        rate: Optional[float] = None
        raw_rate = row.get("fuel_rate_per_hand")
        if raw_rate is not None:
            try:
                parsed = float(raw_rate)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None and math.isfinite(parsed):
                rate = parsed
        cand = (recency, index, quantity, rate)
        if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] > best[1]):
            best = cand
    if best is None:
        return None, None
    return best[2], best[3]


def fuel_health(quantity: Optional[float], reason_code: str, threshold: float) -> str:
    reason = fuel_reason_code(quantity, reason_code, threshold)
    if reason in _FUEL_CRITICAL_CODES:
        return "red"
    if quantity is None or reason in {"FUEL_LOW", "FUEL_BACKEND_WARNING"}:
        return "yellow"
    return "green"


__all__ = [
    "DEFAULT_FUEL_LOW_THRESHOLD",
    "FUEL_RATE_UNIT",
    "FUEL_UNIT",
    "fuel_health",
    "fuel_reason_code",
    "latest_fuel_reading",
    "normalize_fuel_threshold",
]
