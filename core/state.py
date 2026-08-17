"""Deterministic per-table poker action state used for hint and anomaly decisions.

Pure and side-effect free apart from explicit method calls. Keeps exactly the
street-local quantities the bridge needs to compute a legal ``call`` and to
detect a suspicious ``call == 0`` while a raise is outstanding.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


def turn_identity(data: Dict[str, Any]) -> str:
    """Stable identity for one turn, including replay/reconnect copies.

    Prefers an explicit timestamp/ID from the game (initTimeStamp, turnId,
    turnID, actionId); falls back to a content hash of the turn shape so that
    reconnects re-emitting the same turn do not spawn duplicate hints.
    (Ported from legacy ``coin_autoplay._turn_id``.)
    """
    for key in ("initTimeStamp", "turnId", "turnID", "actionId"):
        value = data.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    stable = {
        "whoseTurn": data.get("whoseTurn") or data.get("userName"),
        "turnTime": data.get("turnTime"),
        "callAmount": data.get("callAmount"),
        "userTurnOptions": data.get("userTurnOptions") or {},
    }
    return "shape:" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass
class SeatState:
    seat: int
    street_contribution: int = 0
    remaining_stack: int = 0
    folded: bool = False
    all_in: bool = False


@dataclass
class StreetState:
    """Street-local snapshot; contributions reset at a proven street boundary."""

    seats: Dict[int, SeatState] = field(default_factory=dict)
    current_max: int = 0
    last_full_raise: int = 0

    def ensure_seat(self, seat: int) -> SeatState:
        seat = int(seat)
        if seat not in self.seats:
            self.seats[seat] = SeatState(seat=seat)
        return self.seats[seat]

    def contribute(self, seat: int, amount: int, *, all_in: bool = False) -> None:
        s = self.ensure_seat(seat)
        s.street_contribution += int(amount)
        s.remaining_stack = max(0, s.remaining_stack - int(amount))
        if all_in:
            s.all_in = True
        self.current_max = max(self.current_max, s.street_contribution)

    def set_raise(self, full_raise: int) -> None:
        self.last_full_raise = max(self.last_full_raise, int(full_raise))
        self.current_max = max(self.current_max, int(full_raise))

    def fold(self, seat: int) -> None:
        self.ensure_seat(seat).folded = True

    def reset_street(self) -> None:
        for s in self.seats.values():
            s.street_contribution = 0
        self.current_max = 0
        self.last_full_raise = 0

    def call_need(self, seat: int) -> int:
        """Chips the actor must add to match the current street bet."""
        return max(0, self.current_max - self.seats.get(int(seat), SeatState(seat=seat)).street_contribution)

    def active_others(self, actor_seat: int) -> Iterable[int]:
        a = int(actor_seat)
        for seat, s in self.seats.items():
            if seat != a and not s.folded and not s.all_in:
                yield seat

    def min_raise_to(self, seat: int) -> int:
        """Minimum total raise target for the actor (0 when only one can act)."""
        actor = self.seats.get(int(seat), SeatState(seat=seat))
        can_act = sum(1 for s in self.seats.values() if s.seat != int(seat) and not s.folded and not s.all_in)
        if can_act <= 0:
            return 0
        put = actor.street_contribution
        return max(0, self.current_max + max(1, self.last_full_raise) - put)

    def max_call_or_raise_to(self, seat: int) -> int:
        """Maximum the actor can put in without going all-in past a cover."""
        actor = self.seats.get(int(seat), SeatState(seat=seat))
        covers = [
            self.seats[o].street_contribution + self.seats[o].remaining_stack
            for o in self.active_others(seat)
        ]
        put = actor.street_contribution
        best = max(covers) if covers else put
        return max(0, best - put)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "current_max": self.current_max,
            "last_full_raise": self.last_full_raise,
            "seats": {
                str(k): {
                    "street_contribution": v.street_contribution,
                    "remaining_stack": v.remaining_stack,
                    "folded": v.folded,
                    "all_in": v.all_in,
                }
                for k, v in sorted(self.seats.items())
            },
        }


@dataclass
class TableState:
    """Immutable-facing per-table state with a monotonic hand generation."""

    device_id: str
    table_id: str
    generation: int = 1
    hand_id: Optional[str] = None
    street: int = 0
    street_state: StreetState = field(default_factory=StreetState)
    user_turn_options: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None

    def new_hand(self, hand_id: str) -> None:
        self.hand_id = str(hand_id)
        self.generation += 1
        self.street = 0
        self.street_state = StreetState()
        self.user_turn_options = {}
        self.source = None

    def new_street(self, street: int) -> None:
        self.street = int(street)
        self.street_state.reset_street()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "table_id": self.table_id,
            "generation": self.generation,
            "hand_id": self.hand_id,
            "street": self.street,
            "source": self.source,
            "street_state": self.street_state.snapshot(),
            "user_turn_options": dict(self.user_turn_options),
        }
