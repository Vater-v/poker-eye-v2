"""Per-device business orchestration: hint -> CC -> action attempts -> ACK -> ledger.

This module implements the v2 action lifecycle end-to-end without any I/O of its
own: all sends go through injected callbacks (``eye_sender`` for hint frames and
``device_sender`` for action frames), so the whole loop is testable with fakes.

Contract implemented here:
- one in-flight CC action per device (enforced by ActionScheduler),
- exactly three explicit send attempts (calculated delay, +1 s, +1 s),
- uncertain result never retried blindly (reconcile first, else NEEDS_OPERATOR),
- call=0 anomaly guard applied through ``policy.decide_cc_action``,
- hint watchdog: exactly one outstanding hint, bounded timeouts,
- idempotent hint finish (FinishRoundHintRSP emitted once),
- ledger finalize exactly once per action key,
- a broken table/action never blocks the others.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
from .actions import Action, ActionScheduler, HumanDelay
from .anomalies import AnomalyVerdict
from .hints import HintWatchdog
from .ledger import Ledger, LedgerStatus
from .policy import decide_cc_action
from .state import TableState, turn_identity


@dataclass
class BridgeEvent:
    event: str
    table_id: Optional[str] = None
    device_id: Optional[str] = None
    message: str = ""
    severity: str = "INFO"
    fields: Dict[str, Any] = field(default_factory=dict)


class BridgeContext:
    """One device's business loop. Callbacks are injected (no sockets here)."""

    def __init__(
        self,
        device_id: str,
        *,
        ledger: Ledger,
        eye_sender: Callable[[Dict[str, Any]], bool],
        device_sender: Callable[[str, Dict[str, Any]], bool],
        on_event: Optional[Callable[[BridgeEvent], None]] = None,
        chip_scale: int = 100,
        watchdog_timeout: float = 10.0,
        action_ack_timeout: float = 2.5,
    ) -> None:
        self.device_id = device_id
        self.ledger = ledger
        self.eye_sender = eye_sender
        self.device_sender = device_sender
        self.on_event = on_event
        self.chip_scale = chip_scale
        self.ack_timeout = action_ack_timeout
        self.scheduler = ActionScheduler()
        self.watchdog = HintWatchdog(timeout=watchdog_timeout)
        self.tables: Dict[str, TableState] = {}
        self.seen_turns: Dict[str, str] = {}  # table_id -> turn_id
        self._pending_finish_hint: Dict[str, bool] = {}  # table_id -> hint outstanding
        self._lock = threading.Lock()

    # -- events -----------------------------------------------------------
    def _emit(self, event: str, *, table_id: Optional[str] = None, message: str = "",
              severity: str = "INFO", **fields: Any) -> None:
        if self.on_event:
            try:
                self.on_event(BridgeEvent(event, table_id, self.device_id, message, severity, fields))
            except Exception:
                pass

    # -- table state ------------------------------------------------------
    def table(self, table_id: str) -> TableState:
        table_id = str(table_id)
        with self._lock:
            existing = self.tables.get(table_id)
            if existing is None:
                existing = TableState(self.device_id, table_id)
                self.tables[table_id] = existing
            return existing

    # -- hint request -----------------------------------------------------
    def on_hero_turn(
        self,
        table_id: str,
        turn: Dict[str, Any],
        *,
        hand_id: Optional[str] = None,
        user_turn_options: Optional[Dict[str, Any]] = None,
        turn_time_s: float = 0.0,
        observed_at: Optional[float] = None,
        pot_bb: float = 0.0,
        hint_frame: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """A hero turn arrived from the device. Returns True if a hint was sent."""
        table_id = str(table_id)
        tid = turn_identity(turn)
        state = self.table(table_id)
        with self._lock:
            last = self.seen_turns.get(table_id)
            if last == tid:
                return False  # reconnect replay of the same turn: no duplicate hint
            self.seen_turns[table_id] = tid
        state.user_turn_options = dict(user_turn_options or {})
        if hand_id and state.hand_id != hand_id:
            state.new_hand(hand_id)
            state.user_turn_options = dict(user_turn_options or {})  # new_hand resets them
        state.source = "coin.turn"
        now = time.monotonic()
        accepted, transitions = self.watchdog.observe_hint(table_id, hand_id, now)
        if not accepted:
            for t in transitions:
                self._emit("hint.held", table_id=table_id, message=t.message, severity="WARN")
            return False
        frame = hint_frame or self._build_hint_frame(table_id, hand_id, turn, turn_time_s)
        ok = self.eye_sender(frame)
        if not ok:
            self._emit("hint.send_failed", table_id=table_id, message="EYE channel unavailable", severity="WARN")
            return False
        with self._lock:
            self._pending_finish_hint[table_id] = True
        self._emit("hint.requested", table_id=table_id, message=f"hint for {table_id}", turn_id=tid,
                   hand_id=hand_id, call_need=state.street_state.call_need(self._hero_seat(state)),
                   pot_bb=pot_bb)
        return True

    def _build_hint_frame(self, table_id: str, hand_id: Optional[str], turn: Dict[str, Any], turn_time_s: float) -> Dict[str, Any]:
        return {
            "data": "",
            "msg": json_dumps({
                "timestamp": int(time.time() * 1000),
                "cmd": "pb.RoundHintMultipleTableREQ",
                "direction": "ClientToServer",
                "data": "",
                "location": "TABLE",
                "seq": 0,
                "table_id": table_id,
                "hand_id": hand_id,
                "turn_time": turn_time_s,
            }),
            "packageName": "com.lein.pppoker.android",
            "tag": "traffic",
        }

    @staticmethod
    def _hero_seat(state: TableState) -> int:
        # v2 keeps seats as observed; caller passes the hero seat with actions.
        # The bridge uses the smallest registered seat as default identity only
        # for diagnostics; the real hero seat comes from turn options.
        return 1

    # -- CC received ------------------------------------------------------
    def on_cc(self, table_id: str, cc: Dict[str, Any], *, hero_seat: int,
              seq: Optional[int] = None, source: Optional[str] = None,
              raw_frame: Optional[bytes] = None) -> Optional[str]:
        """An EYE cc arrived. Returns the correlation_id if an action was scheduled."""
        table_id = str(table_id)
        state = self.table(table_id)
        state.source = "eye.cc"
        decision = decide_cc_action(
            cc,
            table_state=state,
            hero_seat=hero_seat,
            user_turn_options=state.user_turn_options,
            chip_scale=self.chip_scale,
            source=source,
            seq=seq,
            raw_frame=raw_frame,
        )
        if decision.status == "state_gap_check":
            self._emit("state.gap", table_id=table_id, severity="WARN",
                       message=decision.reason,
                       anomaly=decision.anomaly.diagnostics if decision.anomaly else None)
            # Prefer CHECK exactly once through the normal scheduler.
        elif decision.status == "needs_operator":
            self._emit("action.needs_operator", table_id=table_id, severity="WARN", message=decision.reason)
            self._finalize_hint(table_id, None)
            return None
        elif decision.status == "error":
            self._emit("action.mapping_error", table_id=table_id, severity="ERROR", message=decision.reason)
            self._finalize_hint(table_id, None)
            return None
        if decision.action is None:
            return None
        action = Action(
            device_id=self.device_id,
            table_id=table_id,
            generation=state.generation,
            command=decision.action.name,
            amount=decision.action.bet_amount,
        )
        if not self.scheduler.create(action):
            self._emit("action.skipped", table_id=table_id, severity="WARN",
                       message="another CC is already in flight for this device")
            self._finalize_hint(table_id, None)
            return None
        delay_ms = HumanDelay.compute(
            int(cc.get("delay") or 0),
            turn_time_s=float(state.user_turn_options.get("turnTime") or 0.0),
            current_pot_bb=float(cc.get("pot_bb") or 0.0),
        )
        self._emit("action.created", table_id=table_id, message=f"scheduled {decision.action.name}",
                   correlation_id=action.correlation_id, generation=action.generation,
                   delay_ms=delay_ms, command=decision.action.name, amount=decision.action.bet_amount)
        threading.Thread(
            target=self._attempt_loop, args=(action, delay_ms, state.user_turn_options),
            daemon=True, name=f"action-{action.correlation_id[:8]}",
        ).start()
        return action.correlation_id

    # -- attempts ---------------------------------------------------------
    def _attempt_loop(self, action: Action, first_delay_ms: int, options: Dict[str, Any]) -> None:
        delays = [first_delay_ms / 1000.0, 1.0, 1.0]
        for attempt in range(1, ActionScheduler.MAX_ATTEMPTS + 1):
            delay = delays[attempt - 1]
            if attempt > 1:
                # The scheduler owns the attempt counter; ask it for the next one.
                scheduled = self.scheduler.next_attempt(self.device_id)
                if scheduled is None:
                    break
                _a, extra = scheduled
                delay = extra or 0.0
            else:
                self.scheduler.next_attempt(self.device_id)
            if delay > 0:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    # Poll ACK while waiting; an early ACK settles the action.
                    if not self.scheduler.has_active(self.device_id):
                        return
                    time.sleep(min(0.05, deadline - time.monotonic()))
            if not self.scheduler.has_active(self.device_id):
                return
            ok = self.device_sender(action.table_id, {
                "type": "action",
                "correlation_id": action.correlation_id,
                "generation": action.generation,
                "device_id": self.device_id,
                "command": action.command,
                "amount": action.amount,
                "attempt": attempt,
                "options": options,
            })
            self._emit("action.attempt_sent", table_id=action.table_id,
                       message=f"attempt {attempt}/3 {action.command}",
                       correlation_id=action.correlation_id, attempt=attempt, sent=ok)
            if not ok:
                self._emit("action.send_failed", table_id=action.table_id, severity="WARN",
                           message=f"attempt {attempt}: device channel unavailable",
                           correlation_id=action.correlation_id)
            # Wait for the ACK (bounded).
            deadline = time.monotonic() + self.ack_timeout
            while time.monotonic() < deadline:
                if not self.scheduler.has_active(self.device_id):
                    return  # acknowledged already
                time.sleep(0.05)
        # Exhausted all attempts without an ACK.
        finished = self.scheduler.finish_failed(self.device_id)
        if finished is not None and finished is action:
            self._finalize(action, LedgerStatus.FAILED, "3 attempts exhausted without ACK")

    # -- ACK --------------------------------------------------------------
    def on_ack(self, correlation_id: str, *, generation: Optional[int] = None,
               status: str = "ok") -> bool:
        action = self.scheduler.active(self.device_id)
        if action is None or action.correlation_id != correlation_id:
            self._emit("action.stale_ack", message="stale/unknown ACK rejected", correlation_id=correlation_id, severity="WARN")
            return False
        if generation is not None and generation != action.generation:
            self._emit("action.stale_ack", message="ACK from an older generation rejected",
                       correlation_id=correlation_id, severity="WARN")
            return False
        ok = self.scheduler.acknowledge(self.device_id, correlation_id, action.generation)
        if not ok:
            return False
        final = LedgerStatus.SUCCESS if status == "ok" else LedgerStatus.FAILED
        self._finalize(action, final, f"acked: {status}")
        return True

    def on_action_uncertain(self, correlation_id: str) -> bool:
        """The result of an action is unknown (e.g. connection loss mid-send).

        Never retry blindly: mark uncertain -> NEEDS_OPERATOR, finalize ledger,
        and release the hint so the table can reconcile on fresh state.
        """
        action = self.scheduler.active(self.device_id)
        if action is None or action.correlation_id != correlation_id:
            return False
        if self.scheduler.timeout_unknown(self.device_id, correlation_id):
            self._finalize(action, LedgerStatus.NEEDS_OPERATOR, "uncertain result; reconciliation required")
            return True
        return False

    # -- finalization -----------------------------------------------------
    def _finalize(self, action: Action, status: LedgerStatus, reason: str) -> None:
        key = f"{action.device_id}:{action.table_id}:{action.generation}:{action.command}:{action.correlation_id}"
        self.ledger.finalize(key, status, action=action.command, amount=action.amount, extra={"reason": reason})
        self._emit("action.finalized", table_id=action.table_id,
                   message=f"{action.command} {status.value}: {reason}",
                   severity="INFO" if status == LedgerStatus.SUCCESS else "WARN",
                   correlation_id=action.correlation_id, status=status.value)
        self._finalize_hint(action.table_id, action)

    def _finalize_hint(self, table_id: str, action: Optional[Action]) -> None:
        """Emit FinishRoundHintRSP exactly once per outstanding hint (idempotent)."""
        with self._lock:
            if not self._pending_finish_hint.get(table_id):
                return
            self._pending_finish_hint[table_id] = False
        transitions = self.watchdog.observe_finish(table_id)
        for t in transitions:
            self._emit(t.event, table_id=table_id, message=t.message)
        self.eye_sender({
            "data": "",
            "msg": json_dumps({
                "timestamp": int(time.time() * 1000),
                "cmd": "pb.FinishRoundHintRSP",
                "direction": "ClientToServer",
                "data": "",
                "location": "TABLE",
                "seq": 0,
                "table_id": table_id,
            }),
            "packageName": "com.lein.pppoker.android",
            "tag": "traffic",
        })

    # -- polling ----------------------------------------------------------
    def poll(self, now: Optional[float] = None) -> None:
        """Advance the hint watchdog; emits timeout/recycle transitions."""
        now = now if now is not None else time.monotonic()
        for transition in self.watchdog.poll(now):
            self._emit(transition.event, table_id=transition.table_id,
                       message=transition.message, severity="WARN")

    def close(self) -> None:
        pass


def json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))
