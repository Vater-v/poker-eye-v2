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
    "normalize_fuel_threshold",
]
