"""Device-serialized CC schedule with exactly three explicit send attempts."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
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
