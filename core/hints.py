"""Hint lifecycle watchdog: exactly one outstanding hint, bounded timeouts.

Mirrors the proven invariant from the legacy ``BackendHintWatchdog``: never
replace or duplicate an unresolved backend hint. A newer hint for the same table
is remembered as a successor and only promoted after the current hint finishes or
recycles; a second consecutive timeout witnesses a stale decision and triggers
recycle rather than an unbounded hold.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Tuple


class HintPhase(str, Enum):
    IDLE = "IDLE"
    OUTSTANDING = "OUTSTANDING"
    TIMEOUT_ONCE = "TIMEOUT_ONCE"
    RECYCLED = "RECYCLED"


@dataclass(frozen=True)
class HintTransition:
    event: str  # hint.armed, hint.timeout_once, hint.recycled, hint.finished
    table_id: Optional[str]
    hand_id: Optional[str]
    message: str


@dataclass
class OutstandingHint:
    table_id: str
    hand_id: Optional[str]
    created_at: float
    timeout_count: int = 0


class HintWatchdog:
    """One outstanding hint per table; stale decisions are rejected, not duplicated."""

    def __init__(self, timeout: float = 10.0, *, clock=time.monotonic) -> None:
        if timeout <= 0:
            raise ValueError("hint watchdog timeout must be positive")
        self.timeout = float(timeout)
        self._clock = clock
        self._outstanding: Dict[str, OutstandingHint] = {}
        self._successor: Dict[str, OutstandingHint] = {}

    def observe_hint(self, table_id: str, hand_id: Optional[str], now: float) -> Tuple[bool, Tuple[HintTransition, ...]]:
        """Register a new hint. Returns (accepted, transitions)."""
        table_id = str(table_id)
        cur = self._outstanding.get(table_id)
        if cur is None:
            self._outstanding[table_id] = OutstandingHint(table_id, hand_id, now)
            return True, (HintTransition("hint.armed", table_id, hand_id, "hint armed"),)
        # Never replace or duplicate an unresolved hint; remember only the first successor.
        self._successor.setdefault(
            table_id, OutstandingHint(table_id, hand_id, now)
        )
        return False, (HintTransition("hint.held", table_id, hand_id, "held newer hint while one outstanding"),)

    def observe_finish(self, table_id: str) -> Tuple[HintTransition, ...]:
        table_id = str(table_id)
        cur = self._outstanding.pop(table_id, None)
        succ = self._successor.pop(table_id, None)
        transitions: list[HintTransition] = []
        if cur is not None:
            transitions.append(HintTransition("hint.finished", table_id, cur.hand_id, "hint lifecycle completed"))
        if succ is not None:
            self._outstanding[table_id] = succ
            transitions.append(HintTransition("hint.promoted", table_id, succ.hand_id, "promoted held successor"))
        return tuple(transitions)

    def poll(self, now: float) -> Tuple[HintTransition, ...]:
        transitions: list[HintTransition] = []
        for table_id in list(self._outstanding.keys()):
            cur = self._outstanding[table_id]
            if now - cur.created_at < self.timeout:
                continue
            cur.timeout_count += 1
            if cur.timeout_count == 1:
                transitions.append(HintTransition(
                    "hint.timeout_once", table_id, cur.hand_id,
                    "SCAction timeout 1/2; holding newer RoundHint frames",
                ))
            else:
                transitions.append(HintTransition(
                    "hint.recycled", table_id, cur.hand_id,
                    "second consecutive timeout; recycling hint",
                ))
                del self._outstanding[table_id]
                succ = self._successor.pop(table_id, None)
                if succ is not None:
                    self._outstanding[table_id] = succ
                    transitions.append(HintTransition("hint.promoted", table_id, succ.hand_id, "promoted held successor"))
        return tuple(transitions)

    def outstanding(self) -> Dict[str, OutstandingHint]:
        return dict(self._outstanding)
