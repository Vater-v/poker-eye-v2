"""Device-serialized CC schedule with exactly three explicit send attempts and human-like delay calculator."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import random
import threading
import time
import uuid


class ActionStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    FAILED = "FAILED"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"


@dataclass
class Action:
    device_id: str
    table_id: str
    generation: int
    command: str
    amount: int | float
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempt: int = 0
    status: ActionStatus = ActionStatus.PENDING
    uncertain: bool = False
    turn_id: str | None = None


class ActionScheduler:
    """A device has one active CC action; stale ACKs cannot settle a new generation."""
    MAX_ATTEMPTS = 3
    def __init__(self, monotonic=time.monotonic) -> None:
        self._clock = monotonic
        self._lock = threading.Lock()
        self._active: dict[str, Action] = {}

    def create(self, action: Action) -> bool:
        with self._lock:
            if action.device_id in self._active:
                return False
            self._active[action.device_id] = action
            return True

    def next_attempt(self, device_id: str) -> tuple[Action, float] | None:
        with self._lock:
            action = self._active.get(device_id)
            if not action or action.status not in {ActionStatus.PENDING, ActionStatus.SENT}:
                return None
            if action.uncertain or action.attempt >= self.MAX_ATTEMPTS:
                action.status = ActionStatus.NEEDS_OPERATOR if action.uncertain else ActionStatus.FAILED
                return None
            action.attempt += 1
            action.status = ActionStatus.SENT
            # Caller applies computed human delay for first attempt; retries are exactly +1 second.
            return action, (0.0 if action.attempt == 1 else 1.0)

    def acknowledge(self, device_id: str, correlation_id: str, generation: int) -> bool:
        with self._lock:
            action = self._active.get(device_id)
            if not action or action.correlation_id != correlation_id or action.generation != generation:
                return False
            action.status = ActionStatus.ACKED
            del self._active[device_id]
            return True

    def timeout_unknown(self, device_id: str, correlation_id: str) -> bool:
        with self._lock:
            action = self._active.get(device_id)
            if not action or action.correlation_id != correlation_id:
                return False
            action.uncertain = True
            action.status = ActionStatus.NEEDS_OPERATOR
            # Release the device slot: a fresh turn (new generation) must be
            # allowed to start; stale ACKs cannot settle it (generation check).
            del self._active[device_id]
            return True

    def finish_failed(self, device_id: str) -> Action | None:
        with self._lock:
            action = self._active.get(device_id)
            if not action:
                return None
            if action.attempt < self.MAX_ATTEMPTS and not action.uncertain:
                return None
            action.status = ActionStatus.NEEDS_OPERATOR if action.uncertain else ActionStatus.FAILED
            return self._active.pop(device_id)

    def active(self, device_id: str) -> Action | None:
        with self._lock:
            return self._active.get(device_id)

    def has_active(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._active


# --- Human-like delay calculator ----------------------------------------
@dataclass(frozen=True)
class HumanDelay:
    """Samples human-like action delays; extra delay probability for 60 BB+ pots.

    All delays are bounded by a ``max_delay_ms`` (turn deadline minus margin).
    """

    MAX_DELAY_MS: int = 15_000
    TURN_DEADLINE_MARGIN_MS: int = 750
    BIG_POT_THRESHOLD_BB: float = 60.0
    BIG_POT_EXTRA_PROBABILITY: float = 0.3
    BIG_POT_EXTRA_MS_MIN: int = 500
    BIG_POT_EXTRA_MS_MAX: int = 2_000

    @staticmethod
    def compute(
        cc_delay_ms: int,
        *,
        turn_time_s: float = 0.0,
        observed_at: float | None = None,
        now: float | None = None,
        current_pot_bb: float = 0.0,
        rng: random.Random | None = None,
    ) -> int:
        """Return the human-like delay in whole milliseconds for a CC action.

        * ``cc_delay_ms`` — the CC's ``delay`` field, clamped to [0, MAX_DELAY_MS].
        * ``turn_time_s`` — the Coin turn timer in seconds (0 means unknown/instant).
        * ``observed_at`` — monotonic clock when the turn was observed.
        * ``now`` — current monotonic clock.
        * ``current_pot_bb`` — pot size in big blinds (0 means unknown).
        """
        rng = rng or random.Random()
        requested = max(0, min(HumanDelay.MAX_DELAY_MS, int(cc_delay_ms)))
        # Cap by turn deadline if available.
        if turn_time_s > 0 and observed_at is not None and now is not None:
            turn_deadline_at = observed_at + turn_time_s
            remaining_ms = max(0, int(round((turn_deadline_at - now) * 1000)) - HumanDelay.TURN_DEADLINE_MARGIN_MS)
        else:
            remaining_ms = HumanDelay.MAX_DELAY_MS
        delay_ms = min(requested, remaining_ms)

        # Big-pot extra delay.
        if current_pot_bb >= HumanDelay.BIG_POT_THRESHOLD_BB and rng.random() < HumanDelay.BIG_POT_EXTRA_PROBABILITY:
            extra = rng.randint(HumanDelay.BIG_POT_EXTRA_MS_MIN, HumanDelay.BIG_POT_EXTRA_MS_MAX)
            delay_ms = min(delay_ms + extra, remaining_ms)

        return delay_ms
