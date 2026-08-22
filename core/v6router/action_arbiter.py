from __future__ import annotations

import math
import os
import random
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Iterable, Optional, Protocol


HUMANIZE_CLAMPED = "HUMANIZE_CLAMPED"


class RandomSource(Protocol):
    def random(self) -> float: ...

    def uniform(self, a: float, b: float) -> float: ...


class ActionState(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ActionArbiterConfig:
    """Humanisation policy for one Coin player/device.

    ``deadline_safety_margin`` is subtracted from Coin's absolute turn deadline.
    The regular gap and optional deep-pot delay are soft: both yield to that safe
    deadline.  Values are seconds and can be overridden without changing the hook
    or backend wire schemas.
    """

    # Same-table think time. Cross-table is a physical device switch.
    inter_action_gap_min: float = 0.85
    inter_action_gap_max: float = 2.20
    cross_table_gap_min: Optional[float] = 1.20
    cross_table_gap_max: Optional[float] = 2.80
    deadline_safety_margin: float = 0.55
    deep_pot_threshold_bb: float = 60.0
    deep_pot_probability: float = 0.0
    deep_pot_delay_min: float = 2.0
    deep_pot_delay_max: float = 4.0

    def __post_init__(self) -> None:
        if self.inter_action_gap_min < 0:
            raise ValueError("inter_action_gap_min must be non-negative")
        if self.inter_action_gap_max < self.inter_action_gap_min:
            raise ValueError("inter_action_gap_max must be >= inter_action_gap_min")
        if self.cross_table_gap_min is not None and self.cross_table_gap_min < 0:
            raise ValueError("cross_table_gap_min must be non-negative")
        if self.cross_table_gap_max is not None and self.cross_table_gap_max < 0:
            raise ValueError("cross_table_gap_max must be non-negative")
        if ((self.cross_table_gap_min is None) != (self.cross_table_gap_max is None)):
            raise ValueError("cross-table gap min/max must both be set or both be None")
        if (self.cross_table_gap_min is not None
                and self.cross_table_gap_max is not None
                and self.cross_table_gap_max < self.cross_table_gap_min):
            raise ValueError("cross_table_gap_max must be >= cross_table_gap_min")
        if self.deadline_safety_margin < 0:
            raise ValueError("deadline_safety_margin must be non-negative")
        if self.deep_pot_threshold_bb < 0:
            raise ValueError("deep_pot_threshold_bb must be non-negative")
        if not 0.0 <= self.deep_pot_probability <= 1.0:
            raise ValueError("deep_pot_probability must be in [0, 1]")
        if self.deep_pot_delay_min < 0:
            raise ValueError("deep_pot_delay_min must be non-negative")
        if self.deep_pot_delay_max < self.deep_pot_delay_min:
            raise ValueError("deep_pot_delay_max must be >= deep_pot_delay_min")

    @classmethod
    def from_env(cls) -> "ActionArbiterConfig":
        def number(name: str, default: float) -> float:
            raw = os.getenv(name)
            return default if raw is None or not raw.strip() else float(raw)

        return cls(
            inter_action_gap_min=number("POKER_ACTION_GAP_MIN_SECONDS", 0.85),
            inter_action_gap_max=number("POKER_ACTION_GAP_MAX_SECONDS", 2.20),
            cross_table_gap_min=number("POKER_CROSS_TABLE_GAP_MIN_SECONDS", 1.20),
            cross_table_gap_max=number("POKER_CROSS_TABLE_GAP_MAX_SECONDS", 2.80),
            deadline_safety_margin=number(
                "POKER_ACTION_DEADLINE_SAFETY_MARGIN_SECONDS", 0.55
            ),
            deep_pot_threshold_bb=number("POKER_DEEP_POT_THRESHOLD_BB", 60.0),
            deep_pot_probability=number("POKER_DEEP_POT_PROBABILITY", 0.0),
            deep_pot_delay_min=number("POKER_DEEP_POT_DELAY_MIN_SECONDS", 2.0),
            deep_pot_delay_max=number("POKER_DEEP_POT_DELAY_MAX_SECONDS", 4.0),
        )


PREFOLD_BYPASS_REMAINING_SECONDS = 3.0


def sample_human_gap(rng: RandomSource, low: float, high: float) -> float:
    """Log-uniform gap so most waits are short but not a flat 0.5s click."""
    lo = max(0.05, float(low))
    hi = max(lo, float(high))
    if hi <= lo:
        return lo
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def should_bypass_action_gap(
    *,
    fallback: bool = False,
    extra_urgent: bool = False,
    retry: bool = False,
    prefold: bool = False,
    remaining_seconds: Optional[float] = None,
) -> bool:
    """Failsafe/retry/extra fire now. Prefold only skips the gap near the clock."""
    if fallback or extra_urgent or retry:
        return True
    if not prefold:
        return False
    if remaining_seconds is None:
        return False
    try:
        left = float(remaining_seconds)
    except (TypeError, ValueError):
        return False
    return left <= PREFOLD_BYPASS_REMAINING_SECONDS


@dataclass(frozen=True)
class ActionOffer:
    """A backend action that is ready to be placed on Coin's send path."""

    action_id: str
    table_id: int
    ready_at: float
    deadline_at: Optional[float]
    pot_bb: float = 0.0
    supports_delayed_send: bool = True
    retry: bool = False
    bypass_gap: bool = False

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("action_id is required")
        if int(self.table_id) <= 0:
            raise ValueError("table_id must be positive")
        if not math.isfinite(float(self.ready_at)):
            raise ValueError("ready_at must be finite")
        if self.deadline_at is not None and not math.isfinite(float(self.deadline_at)):
            raise ValueError("deadline_at must be finite or None")


@dataclass(frozen=True)
class ActionAudit:
    at: float
    action_id: str
    table_id: int
    reason: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class ActionRecord:
    action_id: str
    table_id: int
    ready_at: float
    deadline_at: Optional[float]
    pot_bb: float
    supports_delayed_send: bool
    retry: bool
    bypass_gap: bool
    fifo_sequence: int
    first_seen_at: float
    state: ActionState = ActionState.QUEUED
    sampled_gap_seconds: Optional[float] = None
    deep_pot_evaluated: bool = False
    deep_pot_selected: bool = False
    sampled_deep_pot_delay_seconds: Optional[float] = None
    scheduled_at: Optional[float] = None
    dispatched_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    cancellation_reason: str = ""
    reasons: list[str] = field(default_factory=list)
    audit: list[ActionAudit] = field(default_factory=list)

    @property
    def safe_deadline_at(self) -> Optional[float]:
        # The value depends on the owning arbiter's config; exposed on DispatchPlan.
        return self.deadline_at


@dataclass(frozen=True)
class DispatchPlan:
    action_id: str
    table_id: int
    scheduled_at: float
    dispatch_delay_seconds: float
    safe_deadline_at: Optional[float]
    sampled_gap_seconds: Optional[float]
    sampled_deep_pot_delay_seconds: Optional[float]
    reasons: tuple[str, ...]
    retry: bool


class ActionArbiter:
    """Serialize all Coin action injections for exactly one player/device.

    Offers may arrive concurrently from isolated per-table backend sessions.  The
    arbiter keeps one global EDF queue, using insertion order for equal deadlines.
    A plan reserves an *actual send timestamp*, so several v3 hook responses can be
    returned quickly without scheduling simultaneous Coin injections.
    """

    def __init__(
        self,
        device_id: str,
        *,
        config: Optional[ActionArbiterConfig] = None,
        rng: Optional[RandomSource] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device_id = str(device_id)
        self.config = config or ActionArbiterConfig.from_env()
        self._rng: RandomSource = rng or random.Random()
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, ActionRecord] = {}
        self._audit: list[ActionAudit] = []
        self._sequence = 0
        self._last_scheduled_at: Optional[float] = None
        self._last_scheduled_table_id: Optional[int] = None

    @property
    def audit(self) -> tuple[ActionAudit, ...]:
        with self._lock:
            return tuple(self._audit)

    @property
    def last_scheduled_at(self) -> Optional[float]:
        with self._lock:
            return self._last_scheduled_at

    @property
    def last_scheduled_table_id(self) -> Optional[int]:
        with self._lock:
            return self._last_scheduled_table_id

    def _gap_range(self, table_id: int) -> tuple[float, float, bool]:
        cross = (
            self._last_scheduled_table_id is not None
            and int(self._last_scheduled_table_id) != int(table_id)
        )
        if (cross and self.config.cross_table_gap_min is not None
                and self.config.cross_table_gap_max is not None):
            return (
                float(self.config.cross_table_gap_min),
                float(self.config.cross_table_gap_max),
                True,
            )
        return (
            float(self.config.inter_action_gap_min),
            float(self.config.inter_action_gap_max),
            cross,
        )

    def record(self, action_id: str) -> Optional[ActionRecord]:
        """Return a detached snapshot suitable for diagnostics/tests."""

        with self._lock:
            row = self._records.get(str(action_id))
            if row is None:
                return None
            return replace(row, reasons=list(row.reasons), audit=list(row.audit))

    def queued_action_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                row.action_id
                for row in sorted(
                    (value for value in self._records.values() if value.state == ActionState.QUEUED),
                    key=self._edf_key,
                )
            )

    def offer(self, offer: ActionOffer) -> ActionRecord:
        """Insert once, or refresh transport facts without re-rolling randomness."""

        now = float(self._clock())
        with self._lock:
            existing = self._records.get(offer.action_id)
            if existing is not None:
                if existing.state == ActionState.QUEUED:
                    # A reconnect may rediscover the same logical action with a new
                    # websocket.  Preserve all samples/FIFO position and never extend
                    # its deadline; only tighten it when newer evidence is safer.
                    existing.supports_delayed_send = bool(offer.supports_delayed_send)
                    existing.bypass_gap = bool(existing.bypass_gap or offer.bypass_gap)
                    existing.pot_bb = max(existing.pot_bb, float(offer.pot_bb))
                    existing.ready_at = min(existing.ready_at, float(offer.ready_at))
                    # extraTimer / failsafe / prefold: a future HOST_DELAY_PLANNED
                    # must not keep the send at 0/0 attempts until Coin sits out.
                    if existing.bypass_gap and existing.scheduled_at is not None:
                        existing.scheduled_at = None
                    if offer.deadline_at is not None:
                        existing.deadline_at = (
                            float(offer.deadline_at)
                            if existing.deadline_at is None
                            else min(existing.deadline_at, float(offer.deadline_at))
                        )
                return replace(
                    existing,
                    reasons=list(existing.reasons),
                    audit=list(existing.audit),
                )

            self._sequence += 1
            record = ActionRecord(
                action_id=str(offer.action_id),
                table_id=int(offer.table_id),
                ready_at=float(offer.ready_at),
                deadline_at=(
                    None if offer.deadline_at is None else float(offer.deadline_at)
                ),
                pot_bb=max(0.0, float(offer.pot_bb)),
                supports_delayed_send=bool(offer.supports_delayed_send),
                retry=bool(offer.retry),
                bypass_gap=bool(offer.bypass_gap),
                fifo_sequence=self._sequence,
                first_seen_at=now,
            )
            self._records[record.action_id] = record
            self._emit(record, now, "QUEUED", {
                "ready_at": record.ready_at,
                "deadline_at": record.deadline_at,
                "pot_bb": record.pot_bb,
                "fifo_sequence": record.fifo_sequence,
                "retry": record.retry,
            })
            return replace(record, reasons=list(record.reasons), audit=list(record.audit))

    def cancel(self, action_id: str, reason: str, *, at: Optional[float] = None) -> bool:
        """Cancel a queued or app-scheduled action idempotently."""

        now = float(self._clock() if at is None else at)
        with self._lock:
            record = self._records.get(str(action_id))
            if record is None or record.state == ActionState.CANCELLED:
                return False
            record.state = ActionState.CANCELLED
            record.cancelled_at = now
            record.cancellation_reason = str(reason or "STALE")
            self._emit(record, now, record.cancellation_reason, {"cancelled": True})
            return True

    def cancel_table(self, table_id: int, reason: str = "TABLE_CLOSED") -> int:
        now = float(self._clock())
        count = 0
        with self._lock:
            for record in self._records.values():
                if record.table_id != int(table_id) or record.state == ActionState.CANCELLED:
                    continue
                record.state = ActionState.CANCELLED
                record.cancelled_at = now
                record.cancellation_reason = str(reason)
                self._emit(record, now, str(reason), {"cancelled": True})
                count += 1
        return count

    def cancel_missing(
        self,
        active_action_ids: Iterable[str],
        *,
        table_id: Optional[int] = None,
        reason: str = "STALE",
    ) -> int:
        """Reconcile bridge state after manual action, turn change, or reset."""

        active = {str(value) for value in active_action_ids}
        now = float(self._clock())
        count = 0
        with self._lock:
            for record in self._records.values():
                if record.state == ActionState.CANCELLED:
                    continue
                if table_id is not None and record.table_id != int(table_id):
                    continue
                if record.action_id in active:
                    continue
                record.state = ActionState.CANCELLED
                record.cancelled_at = now
                record.cancellation_reason = str(reason)
                self._emit(record, now, str(reason), {"cancelled": True})
                count += 1
        return count

    def dispatch_next(
        self,
        eligible_action_ids: Optional[Iterable[str]] = None,
        *,
        now: Optional[float] = None,
    ) -> Optional[DispatchPlan]:
        """Reserve the next actual Coin-send timestamp.

        If the selected transport cannot schedule a future callback (legacy hook or
        retry), it remains queued until a dummy arrives at/after the planned time.
        """

        current = float(self._clock() if now is None else now)
        eligible = None if eligible_action_ids is None else {
            str(value) for value in eligible_action_ids
        }
        with self._lock:
            self._cancel_expired_locked(current)
            queued = sorted(
                (record for record in self._records.values() if record.state == ActionState.QUEUED),
                key=self._edf_key,
            )
            if not queued:
                return None

            # A dummy on table B must still be allowed to carry B's failsafe
            # when the EDF head is an ineligible table-A action. Blocking the
            # head used to burn Coin's clock, then DEADLINE_EXPIRED dropped
            # the CHECK/FOLD that should have been sent.
            # A host-planned future CHECK (Eye delay 4.5s) also must not block
            # another table that can send now — that wait is how we fall into
            # extraTimer with 0/0 attempts and Coin sit-out.
            for record in queued:
                if eligible is not None and record.action_id not in eligible:
                    continue
                if record.scheduled_at is not None and record.scheduled_at > current:
                    if record.bypass_gap:
                        record.scheduled_at = None
                    else:
                        continue

                if record.scheduled_at is not None:
                    scheduled = current
                    safe_deadline = (
                        None if record.deadline_at is None
                        else record.deadline_at - self.config.deadline_safety_margin
                    )
                    reasons = tuple(
                        reason for reason in record.reasons if reason == HUMANIZE_CLAMPED
                    )
                    record.state = ActionState.DISPATCHED
                    record.scheduled_at = scheduled
                    record.dispatched_at = current
                    self._last_scheduled_at = scheduled
                    self._last_scheduled_table_id = int(record.table_id)
                    self._emit(record, current, "DISPATCHED", {
                        "scheduled_at": scheduled,
                        "delay_seconds": 0.0,
                        "safe_deadline_at": safe_deadline,
                        "sampled_gap_seconds": record.sampled_gap_seconds,
                        "sampled_deep_pot_delay_seconds": record.sampled_deep_pot_delay_seconds,
                        "reasons": reasons,
                    })
                    return DispatchPlan(
                        action_id=record.action_id,
                        table_id=record.table_id,
                        scheduled_at=scheduled,
                        dispatch_delay_seconds=0.0,
                        safe_deadline_at=safe_deadline,
                        sampled_gap_seconds=record.sampled_gap_seconds,
                        sampled_deep_pot_delay_seconds=record.sampled_deep_pot_delay_seconds,
                        reasons=reasons,
                        retry=record.retry,
                    )

                baseline = max(current, record.ready_at)
                if record.bypass_gap:
                    gap_floor = baseline
                    if record.sampled_gap_seconds is None:
                        record.sampled_gap_seconds = 0.0
                elif self._last_scheduled_at is None:
                    gap_floor = baseline
                else:
                    if record.sampled_gap_seconds is None:
                        gap_min, gap_max, cross_table = self._gap_range(record.table_id)
                        record.sampled_gap_seconds = float(sample_human_gap(
                            self._rng, gap_min, gap_max,
                        ))
                        self._emit(record, current, "GAP_SAMPLED", {
                            "seconds": record.sampled_gap_seconds,
                            "cross_table": cross_table,
                            "previous_table_id": self._last_scheduled_table_id,
                        })
                    gap_floor = max(
                        baseline,
                        self._last_scheduled_at + record.sampled_gap_seconds,
                    )

                if record.bypass_gap:
                    record.deep_pot_evaluated = True
                    record.sampled_deep_pot_delay_seconds = 0.0
                if not record.deep_pot_evaluated:
                    record.deep_pot_evaluated = True
                    idle_deep_pot = (
                        len(queued) == 1
                        and record.pot_bb > self.config.deep_pot_threshold_bb
                    )
                    if idle_deep_pot:
                        record.deep_pot_selected = (
                            float(self._rng.random()) < self.config.deep_pot_probability
                        )
                        if record.deep_pot_selected:
                            record.sampled_deep_pot_delay_seconds = float(
                                self._rng.uniform(
                                    self.config.deep_pot_delay_min,
                                    self.config.deep_pot_delay_max,
                                )
                            )
                            self._emit(record, current, "DEEP_POT_DELAY_SAMPLED", {
                                "seconds": record.sampled_deep_pot_delay_seconds,
                                "pot_bb": record.pot_bb,
                            })

                desired = gap_floor + float(record.sampled_deep_pot_delay_seconds or 0.0)
                safe_deadline = (
                    None
                    if record.deadline_at is None
                    else record.deadline_at - self.config.deadline_safety_margin
                )
                reasons: list[str] = []
                scheduled = desired
                if safe_deadline is not None and desired > safe_deadline:
                    serial_floor = baseline
                    if self._last_scheduled_at is not None:
                        serial_floor = max(
                            serial_floor,
                            math.nextafter(self._last_scheduled_at, math.inf),
                        )
                    if serial_floor > safe_deadline:
                        scheduled = current
                        reasons.append("DEADLINE_FORCE")
                        if "DEADLINE_FORCE" not in record.reasons:
                            record.reasons.append("DEADLINE_FORCE")
                        self._emit(record, current, "DEADLINE_FORCE", {
                            "desired_at": desired,
                            "safe_deadline_at": safe_deadline,
                            "scheduled_at": scheduled,
                        })
                    else:
                        scheduled = safe_deadline
                        reasons.append(HUMANIZE_CLAMPED)
                        if HUMANIZE_CLAMPED not in record.reasons:
                            record.reasons.append(HUMANIZE_CLAMPED)
                        self._emit(record, current, HUMANIZE_CLAMPED, {
                            "desired_at": desired,
                            "scheduled_at": scheduled,
                            "safe_deadline_at": safe_deadline,
                            "sampled_gap_seconds": record.sampled_gap_seconds,
                            "sampled_deep_pot_delay_seconds": record.sampled_deep_pot_delay_seconds,
                        })

                if not record.supports_delayed_send and scheduled > current:
                    record.scheduled_at = scheduled
                    self._emit(record, current, "HOST_DELAY_PLANNED", {
                        "scheduled_at": scheduled,
                        "safe_deadline_at": safe_deadline,
                    })
                    continue

                record.state = ActionState.DISPATCHED
                record.scheduled_at = scheduled
                record.dispatched_at = current
                self._last_scheduled_at = scheduled
                self._last_scheduled_table_id = int(record.table_id)
                self._emit(record, current, "DISPATCHED", {
                    "scheduled_at": scheduled,
                    "delay_seconds": max(0.0, scheduled - current),
                    "safe_deadline_at": safe_deadline,
                    "sampled_gap_seconds": record.sampled_gap_seconds,
                    "sampled_deep_pot_delay_seconds": record.sampled_deep_pot_delay_seconds,
                    "reasons": tuple(reasons),
                })
                return DispatchPlan(
                    action_id=record.action_id,
                    table_id=record.table_id,
                    scheduled_at=scheduled,
                    dispatch_delay_seconds=max(0.0, scheduled - current),
                    safe_deadline_at=safe_deadline,
                    sampled_gap_seconds=record.sampled_gap_seconds,
                    sampled_deep_pot_delay_seconds=record.sampled_deep_pot_delay_seconds,
                    reasons=tuple(reasons),
                    retry=record.retry,
                )
            return None

    @staticmethod
    def _edf_key(record: ActionRecord) -> tuple[float, int]:
        deadline = math.inf if record.deadline_at is None else record.deadline_at
        return deadline, record.fifo_sequence

    def _cancel_expired_locked(self, now: float) -> None:
        # Never drop a queued send because Coin's clock is already tight.
        # dispatch_next force-sends instead of DEADLINE_EXPIRED cancel.
        return

    def _emit(
        self,
        record: ActionRecord,
        at: float,
        reason: str,
        detail: dict[str, object],
    ) -> None:
        event = ActionAudit(
            at=float(at),
            action_id=record.action_id,
            table_id=record.table_id,
            reason=str(reason),
            detail=dict(detail),
        )
        record.audit.append(event)
        self._audit.append(event)
