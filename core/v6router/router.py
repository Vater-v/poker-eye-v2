from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import json
import math
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from .accounts import (
    AccountLease,
    AccountPool,
    AccountPoolExhausted,
    AccountProbeDeferred,
    AccountState,
)
from .action_arbiter import ActionArbiter, ActionOffer, ActionState, DispatchPlan
from .db_recent import store as db_recent_store
from .fuel import fuel_health, fuel_reason_code, normalize_fuel_threshold
from .history_ledger import attach_session_docs, attach_session_profit
from ..verified_v1.bombpot_support import detect_double_board, stale_double_board_should_drop
from ..verified_v1.coin_action_wire import (
    build_game_leave_seat_packet,
    build_game_quit_table_packet,
    build_game_user_action_packet,
)


def hero_sitout_for_watchdog(bridge: Any) -> bool:
    """Sitout for policy/sit-in is only the live bridge latch.

    Coin timeout sets ``hero_sitting_out`` from game.sitout sitOutMap or a
    missed deal. Between-hands ``isPlaying=false`` after a failsafe is not
    sitout — that OR with ``cc_miss_streak`` produced 29/41 false cases.
    """
    return bool(getattr(bridge, "hero_sitting_out", False))


@dataclass
class TableLogic:
    """Per-table trainer logic that can be swapped between hands.

    ``cc_timeout_seconds`` is consulted on the next hero-turn CC wait.
    """

    revision: str = ""
    cc_timeout_seconds: Optional[float] = None

    @classmethod
    def from_value(cls, value: Any) -> "TableLogic":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            timeout = value.get("cc_timeout_seconds")
            return cls(
                revision=str(value.get("revision") or value.get("rev") or ""),
                cc_timeout_seconds=(
                    None if timeout is None else float(timeout)
                ),
            )
        return cls(revision=str(value or ""))


def swap_logic_between_hands(bridge: Any, logic: Any) -> bool:
    """Install updated trainer logic between hands without dropping the Coin seat.

    Returns False while a hand is live so the next call can retry after reset_data.
    Never enqueues leave or standup. Applies ``TableLogic.cc_timeout_seconds``
    onto the live bridge so the next CC wait uses the new ceiling.
    """
    if getattr(bridge, "current_hand", None) is not None:
        return False
    state = getattr(bridge, "state", None)
    if isinstance(state, dict) and str(state.get("hand_id") or "").strip():
        return False
    sitting = bool(getattr(bridge, "hero_sitting", False))
    departing = bool(getattr(bridge, "hero_departing", False))
    parsed = TableLogic.from_value(logic)
    setattr(bridge, "table_logic", parsed)
    setattr(bridge, "_logic", parsed)
    if parsed.cc_timeout_seconds is not None:
        bridge.cc_timeout_seconds = max(2.0, float(parsed.cc_timeout_seconds))
    bridge.hero_sitting = sitting
    bridge.hero_departing = departing
    return True


MAX_FRAME_BYTES = 20_000_000
LOW_STACK_EXIT_BB = 79.0
DEAD_TABLE_SECONDS = 120.0


def in_play_enough_to_lease(routed: "RoutedEvent") -> bool:
    """True when Coin evidence says we are at a live table, even without game_init."""

    cmd = routed.command
    if cmd in {
        "game.game_init",
        "game.pre_hand_start_info",
        "game.user_turn",
        "game.dealer_cards",
        "game.dealer_chat_action",
    }:
        return True
    data = routed.data if isinstance(routed.data, dict) else {}
    if cmd == "game.take_Seat":
        return bool(data.get("seatId") or data.get("isSeated") is True)
    if cmd in {"game.player_info", "game.hole_cards"}:
        cards = data.get("playerCards") or data.get("holeCards") or []
        if cards:
            return True
        try:
            chips = float(data.get("userChips") or data.get("chips") or 0)
        except (TypeError, ValueError):
            chips = 0.0
        return chips > 0 or bool(data.get("isPlaying") or data.get("isSeated"))
    if cmd in {"game.seatInfo", "game.seat"}:
        # Live Coin seatInfo is seatResponseDataList (same as _seat_rows_for_stack_guard).
        seats = (
            data.get("seatResponseDataList")
            or data.get("seats")
            or data.get("seatList")
            or []
        )
        if isinstance(seats, list) and any(
            isinstance(row, dict) and (row.get("userChips") or row.get("userName") or row.get("seatId"))
            for row in seats
        ):
            return True
        try:
            chips = float(data.get("userChips") or 0)
        except (TypeError, ValueError):
            chips = 0.0
        return chips > 0 or bool(data.get("seatId") or data.get("isSeated"))
    if cmd == "game.game_alldata":
        source = data.get("gameInitResponseData")
        if not isinstance(source, dict):
            source = data
        return bool(source.get("gameId") or source.get("handId"))
    return False


def _finite_nonnegative(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _forward(event: dict[str, Any], **extra: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {"id": event.get("id", ""), "action": "forward"}
    answer.update(extra)
    return answer


def _lp_pack(value: dict[str, Any]) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


async def _lp_read(reader: asyncio.StreamReader) -> Optional[bytes]:
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    length = struct.unpack(">I", header)[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise ValueError(f"invalid hook frame size {length}")
    return await reader.readexactly(length)


@dataclass(frozen=True)
class RoutedEvent:
    event: dict[str, Any]
    payload: Any
    raw: bytes
    command: str
    direction: str
    room_id: Optional[int]
    table_ids: tuple[int, ...]
    config_id: int = 0
    websocket_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    decoded_body: bytes = b""

    @property
    def is_dummy(self) -> bool:
        return self.direction == "out" and self.command == "lobby.dummy"

    @property
    def is_game(self) -> bool:
        return self.command.startswith("game.")


def _decode_event(event: dict[str, Any]) -> RoutedEvent:
    # Import lazily: supervisor model/selftests must stay importable without adding
    # the bridge root to sys.path.  Production launches from ready_v6 and resolves it.
    from ..verified_v1.coin_bridge_live import cmd_room_data, decode_hook_payload

    payload, raw = decode_hook_payload(event)
    command, room_id, data = cmd_room_data(payload)
    decoded_table_id = 0
    decoded_body = b""
    raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else b""
    want_body = (not command) or (b"GameRoomProperties" in raw_bytes)
    if want_body and event.get("text") is not True:
        try:
            from ..verified_v1 import coin_ppp_bridge as core

            decoded = core.decode_coin_events([event])
            if decoded:
                decoded_body = bytes(decoded[0].body or b"")
                decoded_table_id = int(decoded[0].table_id or 0)
                if not data and isinstance(decoded[0].data, dict):
                    data = decoded[0].data
        except Exception:
            pass
    direction = str(event.get("direction") or "").lower()
    table_ids: list[int] = []

    def add(value: Any) -> None:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return
        if number > 0 and number not in table_ids:
            table_ids.append(number)

    if isinstance(data, dict):
        for key in ("tableId", "gameTableId", "tableID", "tid"):
            add(data.get(key))
        nested_init = data.get("gameInitResponseData")
        if isinstance(nested_init, dict):
            for key in ("tableId", "gameTableId", "tableID", "tid"):
                add(nested_init.get(key))
        # lobby.join_game_table is the first truthful table allocation in Coin.
        for row in data.get("tablesToJoin") or ():
            if not isinstance(row, dict):
                continue
            for key in ("tableId", "gameTableId", "tableID", "tid"):
                add(row.get(key))
            name = str(row.get("tableName") or "")
            match = re.search(r"(\d{6,})\s*$", name)
            if match:
                add(match.group(1))
    add(decoded_table_id)
    if decoded_body:
        try:
            from ..verified_v1 import coin_ppp_bridge as core

            for value in core.printable_strings(decoded_body):
                match = re.search(r"\b(\d{6,8})\s*$", str(value))
                if match:
                    add(match.group(1))
        except Exception:
            pass
    try:
        config_id = int(data.get("configId") or 0) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        config_id = 0
    if not config_id and isinstance(data, dict):
        for row in data.get("tablesToJoin") or ():
            if not isinstance(row, dict):
                continue
            props = row.get("roomProperties")
            if not isinstance(props, dict):
                continue
            try:
                config_id = int(props.get("configId") or props.get("id") or 0)
            except (TypeError, ValueError):
                config_id = 0
            if config_id:
                break
    return RoutedEvent(
        event=event,
        payload=payload,
        raw=raw,
        command=command,
        direction=direction,
        room_id=room_id,
        table_ids=tuple(table_ids),
        config_id=config_id,
        websocket_id=str(event.get("ws_id") or ""),
        data=data if isinstance(data, dict) else {},
        decoded_body=decoded_body,
    )


@dataclass(frozen=True)
class RouterSeed:
    events: tuple[dict[str, Any], ...]
    requested_table_id: int = 0
    requested_config_id: int = 0


@dataclass(frozen=True)
class RouterObservation:
    kind: str
    device_id: str
    table_id: Optional[int] = None
    account_id: Optional[str] = None
    status: str = "yellow"
    reason: str = ""
    game_type: Optional[str] = None
    coin_bb: Optional[float] = None
    hand_id: Optional[str] = None
    pending: bool = False
    hand_completed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    backend_status: Optional[str] = None
    backend_message: str = ""
    backend_hash: str = ""
    fuel_quantity: Optional[float] = None
    fuel_rate_per_hand: Optional[float] = None
    fuel_reason_code: str = "FUEL_PENDING"
    fuel_sequence: int = 0
    fuel_low_threshold: float = 1500.0
    fuel_observed: bool = False


@dataclass(frozen=True)
class _LiveTableSnapshot:
    hand: str
    pending: bool
    phase: str
    coin_bb: Optional[float]
    game_type: str
    transport_up: bool
    backend_health: str
    backend_status: str
    backend_message: str
    backend_hash: str
    backend_sequence: int
    fuel_quantity: Optional[float]
    fuel_rate_per_hand: Optional[float]
    fuel_reason_code: str
    fuel_sequence: int
    fuel_low_threshold: float
    fuel_observed: bool
    fuel_updated_at: float = 0.0

    @property
    def fuel_health(self) -> str:
        return fuel_health(
            self.fuel_quantity, self.fuel_reason_code, self.fuel_low_threshold
        )

    @property
    def health(self) -> str:
        if not self.transport_up or self.backend_health == "red" or self.fuel_health == "red":
            return "red"
        if self.backend_health == "yellow" or self.fuel_health == "yellow" or self.pending or self.phase in {
            "offline", "pending", "leaving",
        }:
            return "yellow"
        return "green"

    @property
    def reason(self) -> str:
        if not self.transport_up:
            return "backend transport disconnected"
        pid_changed = self.backend_message.upper() == "PID_CHANGED"
        if self.backend_health != "green" and not pid_changed:
            parts = [f"backend {self.backend_status or 'UNKNOWN'}"]
            if self.backend_message:
                parts.append(self.backend_message)
            if self.backend_hash:
                parts.append(f"hash={self.backend_hash}")
            return " ".join(parts)
        if self.fuel_health != "green":
            if self.fuel_quantity is None:
                return f"fuel telemetry unavailable ({self.fuel_reason_code})"
            return (
                f"fuel {self.fuel_quantity:.1f} F below "
                f"{self.fuel_low_threshold:.1f} F ({self.fuel_reason_code})"
            )
        if pid_changed:
            suffix = f" hash={self.backend_hash}" if self.backend_hash else ""
            return f"backend PID_CHANGED (normal player-id assignment){suffix}"
        if self.backend_message or self.backend_hash:
            parts = [f"backend {self.backend_status or 'UNKNOWN'}"]
            if self.backend_message:
                parts.append(self.backend_message)
            if self.backend_hash:
                parts.append(f"hash={self.backend_hash}")
            return " ".join(parts)
        return f"phase={self.phase}; backend={self.backend_status or 'NORMAL'}"

    @property
    def backend_detail(self) -> dict[str, Any]:
        return {
            "backend_health": self.backend_health,
            "backend_status": self.backend_status,
            "backend_message": self.backend_message,
            "backend_hash": self.backend_hash,
            "backend_sequence": self.backend_sequence,
            "fuel_quantity": self.fuel_quantity,
            "fuel_rate_per_hand": self.fuel_rate_per_hand,
            "fuel_reason_code": self.fuel_reason_code,
            "fuel_sequence": self.fuel_sequence,
            "fuel_low_threshold": self.fuel_low_threshold,
            "fuel_observed": self.fuel_observed,
            "fuel_health": self.fuel_health,
            "fuel_unit": "F",
            "fuel_rate_unit": "F/hand",
        }

    @property
    def fuel_observation(self) -> dict[str, Any]:
        return {
            "fuel_quantity": self.fuel_quantity,
            "fuel_rate_per_hand": self.fuel_rate_per_hand,
            "fuel_reason_code": self.fuel_reason_code,
            "fuel_sequence": self.fuel_sequence,
            "fuel_low_threshold": self.fuel_low_threshold,
            "fuel_observed": self.fuel_observed,
        }


ObservationSink = Callable[[RouterObservation], None]


class TableSession(Protocol):
    device_id: str
    table_id: int
    account_id: str

    async def handle_event(self, event: dict[str, Any]) -> tuple[dict[str, Any], Optional[int]]: ...

    def action_claim(self, event: dict[str, Any]) -> Optional[tuple[int, float, float]]: ...

    async def close(self, *, crashed: bool = False, reason: str = "closed") -> None: ...


class TableSessionFactory(Protocol):
    async def create(
        self, device_id: str, table_id: int, seed: RouterSeed
    ) -> TableSession: ...


class LiveTableSession:
    """One mutable bridge and one direct backend stream for exactly one table."""

    def __init__(
        self,
        *,
        device_id: str,
        table_id: int,
        lease: AccountLease,
        bridge: Any,
        proxy: Any,
        accounts: AccountPool,
        observation_sink: ObservationSink,
        crash_quarantine_seconds: float,
        fuel_low_threshold: float = 1500.0,
    ) -> None:
        self.device_id = str(device_id)
        self.table_id = int(table_id)
        self.account_id = lease.account_id
        self._lease = lease
        self._bridge = bridge
        self._proxy = proxy
        self._accounts = accounts
        raw_sink = observation_sink or (lambda _obs: None)

        def _safe_sink(observation: RouterObservation) -> None:
            # Operator/ledger bugs must not crash the table session. FrozenInstanceError
            # on hand_started used to close the live seat and empty the console fleet.
            try:
                raw_sink(observation)
            except Exception:
                return

        self._sink = _safe_sink
        self._quarantine = float(crash_quarantine_seconds)
        self._fuel_low_threshold = normalize_fuel_threshold(fuel_low_threshold)
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_hand = ""
        self._last_action_delay_ms = 0
        self._hint_action = ""
        self._hint_amount = None
        self._candidate_hand = ""
        self._started: set[str] = set()
        self._completed: set[str] = set()
        self._stack_guard: dict[str, Any] = {}
        self._low_stack_exit_request: Optional[dict[str, Any]] = None
        self._stack_guard_saw_positive = False
        self._backend_red_since = 0.0
        self._arbiter_cancellations: collections.deque[tuple[str, str]] = collections.deque()
        self._arbiter_dispatch_context: dict[str, dict[str, Any]] = {}
        self.play_enabled = True
        self._logic = None
        self._pending_logic = None
        self._monitor = asyncio.create_task(
            self._monitor_state(), name=f"table-monitor-{device_id}-{table_id}"
        )

    def swap_logic_between_hands(self, logic: Any) -> bool:
        """Apply now if between hands; otherwise keep queued for reset_data."""
        self._pending_logic = logic
        ok = swap_logic_between_hands(self._bridge, logic)
        if ok:
            self._logic = getattr(self._bridge, "table_logic", logic)
            self._pending_logic = None
        return ok

    def _apply_pending_table_logic(self) -> bool:
        pending = self._pending_logic
        if pending is None:
            return False
        return self.swap_logic_between_hands(pending)

    async def handle_event(
        self, event: dict[str, Any]
    ) -> tuple[dict[str, Any], Optional[int]]:
        if self._closed:
            return _forward(event, router_error="table session closed"), None
        async with self._lock:
            routed = _decode_event(event)
            data = routed.data
            if routed.command in {"game.game_init", "game.pre_hand_start_info", "game.game_alldata"}:
                nested = data.get("gameInitResponseData")
                source = nested if isinstance(nested, dict) else data
                hand = str(source.get("gameId") or source.get("handId") or "")
                if hand:
                    if self._last_hand and self._last_hand != hand:
                        self._emit_hand_completed(self._last_hand, "next hand")
                    self._candidate_hand = hand
                    self._arm_stack_guard(hand, source)
            if routed.command == "game.user_turn":
                hero = str(
                    self._bridge.identity.get("user_name")
                    or self._bridge.state.get("user_name")
                    or ""
                )
                if hero and str(data.get("whoseTurn") or data.get("userName") or "") == hero:
                    hand = self._candidate_hand or str(self._bridge.state.get("hand_id") or "")
                    if hand:
                        self._emit_hand_started(hand)
            if routed.command in {"game.hole_cards", "game.player_info"}:
                cards = data.get("playerCards") or data.get("holeCards") or []
                if cards:
                    hand = self._candidate_hand or str(self._bridge.state.get("hand_id") or "")
                    if hand:
                        self._emit_hand_started(hand)
            # Never derive hand completion from a transiently empty bridge snapshot.
            # Coin emits explicit atomic boundaries; process settlement before
            # publishing completion so accounting/game type remain available.
            complete_before_bridge = (
                routed.command == "game.reset_data" and bool(self._last_hand)
            )
            hand_before_bridge = self._last_hand
            decision = await self._bridge.handle_event(event)
            self._observe_stack_guard(routed)
            if routed.command == "game.cumulativeWinnerInfo" and self._last_hand:
                self._emit_hand_completed(
                    self._last_hand, "Coin cumulativeWinnerInfo"
                )
            elif complete_before_bridge and hand_before_bridge:
                self._emit_hand_completed(hand_before_bridge, "Coin reset_data")
            if routed.command == "game.reset_data":
                self._apply_pending_table_logic()
            return decision

    @staticmethod
    def _positive_float(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    def _arm_stack_guard(self, hand_id: str, source: dict[str, Any]) -> None:
        hand_id = str(hand_id or "")
        if not hand_id:
            return
        coin_bb = self._positive_float(
            source.get("bbAmount")
            if source.get("bbAmount") is not None
            else source.get("bigBlind")
        )
        if coin_bb is None:
            coin_bb = self._positive_float(source.get("bigBlindAmount"))
        if coin_bb is None:
            profile = getattr(self._bridge, "active_money_profile", None)
            coin_bb = self._positive_float(getattr(profile, "coin_big_blind", None))
        previous = self._stack_guard
        if str(previous.get("hand_id") or "") == hand_id:
            if previous.get("coin_bb") is None and coin_bb is not None:
                previous["coin_bb"] = coin_bb
            return
        self._stack_guard = {
            "hand_id": hand_id,
            "coin_bb": coin_bb,
            "checked": False,
        }
        self._stack_guard_saw_positive = False

    def _seat_rows_for_stack_guard(self, routed: RoutedEvent) -> list[dict[str, Any]]:
        data = routed.data
        rows: Any = None
        if routed.command == "game.seatInfo":
            rows = data.get("seatResponseDataList")
        elif routed.command == "game.game_alldata":
            seats = data.get("seatInfoRsponseData") or data.get("seatInfoResponseData")
            if isinstance(seats, dict):
                rows = seats.get("seatResponseDataList")
        elif routed.command in {"game.seat", "game.reserve_Seat", "game.reserve_seat"}:
            rows = [data]
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, dict)]

    def _observe_stack_guard(self, routed: RoutedEvent) -> None:
        guard = self._stack_guard
        if not guard or guard.get("checked") or self._low_stack_exit_request is not None:
            return
        rows = self._seat_rows_for_stack_guard(routed)
        if not rows:
            return
        identity = getattr(self._bridge, "identity", {}) or {}
        state = getattr(self._bridge, "state", {}) or {}
        try:
            hero_id = int(identity.get("user_id") or state.get("user_id") or 0)
        except (TypeError, ValueError):
            hero_id = 0
        hero_name = str(identity.get("user_name") or state.get("user_name") or "")
        hero = None
        for row in rows:
            try:
                row_user_id = int(row.get("userId") or 0)
            except (TypeError, ValueError, OverflowError):
                row_user_id = 0
            if (hero_id and row_user_id == hero_id) or (
                hero_name and str(row.get("userName") or "") == hero_name
            ):
                hero = row
                break
        if hero is None:
            return
        stack = self._positive_float(hero.get("userChips"))
        if stack is None:
            stack = self._positive_float(hero.get("buyinAmount"))
        if stack is None:
            # Literal zero is a bust only after this table has shown a real stack.
            # take_Seat often has userChips=0 and chips only in buyinAmount later.
            try:
                if float(hero.get("userChips")) == 0.0 and getattr(
                    self, "_stack_guard_saw_positive", False
                ):
                    stack = 0.0
            except (TypeError, ValueError, OverflowError):
                pass
        coin_bb = self._positive_float(guard.get("coin_bb"))
        if coin_bb is None:
            profile = getattr(self._bridge, "active_money_profile", None)
            coin_bb = self._positive_float(getattr(profile, "coin_big_blind", None))
            if coin_bb is not None:
                guard["coin_bb"] = coin_bb
        if stack is None or coin_bb is None:
            return
        stack_bb = max(0.0, float(stack) / float(coin_bb))
        if stack_bb > 0.05:
            self._stack_guard_saw_positive = True
        elif not getattr(self, "_stack_guard_saw_positive", False):
            return
        guard["checked"] = True
        if stack_bb >= LOW_STACK_EXIT_BB:
            return
        request = {
            "hand_id": str(guard.get("hand_id") or self._candidate_hand or ""),
            "stack": float(stack),
            "coin_bb": float(coin_bb),
            "stack_bb": float(stack_bb),
            "threshold_bb": LOW_STACK_EXIT_BB,
        }
        self._low_stack_exit_request = request
        self._sink(
            RouterObservation(
                "low_stack_exit", self.device_id, self.table_id, self.account_id,
                "yellow", f"stack {stack_bb:.2f} BB below {LOW_STACK_EXIT_BB:g} BB",
                self._snapshot().game_type or None, float(coin_bb),
                request["hand_id"] or None, False, detail=dict(request),
            )
        )

    def take_low_stack_exit_request(self) -> Optional[dict[str, Any]]:
        request = self._low_stack_exit_request
        self._low_stack_exit_request = None
        return dict(request) if request is not None else None

    def take_dead_table_request(self) -> Optional[dict[str, Any]]:
        """Drop a PokerEYE-dead table only after the Coin tab is actually gone.

        Sitting, observing, or any live table phase stays in the console until
        a real close (quit / TABLE CLOSED / operator). Backend red alone is not
        a close.
        """
        bridge = getattr(self, "_bridge", None)
        if bool(getattr(bridge, "hero_sitting", False)):
            self._backend_red_since = 0.0
            return None
        if bool(getattr(bridge, "context_active", False)):
            self._backend_red_since = 0.0
            return None
        snap = self._snapshot()
        phase = str(snap.phase or "").strip().lower()
        if phase not in {"offline", "closed", ""}:
            self._backend_red_since = 0.0
            return None
        now = time.monotonic()
        red = str(snap.backend_health or "").lower() == "red"
        if red:
            if self._backend_red_since <= 0:
                self._backend_red_since = now
            if now - self._backend_red_since >= DEAD_TABLE_SECONDS:
                return {
                    "reason": "dead table 120s",
                    "backend_message": str(snap.backend_message or ""),
                    "phase": str(snap.phase or ""),
                }
        else:
            self._backend_red_since = 0.0
        return None

    def action_claim(self, event: dict[str, Any]) -> Optional[tuple[int, float, float]]:
        """Return FIFO key only when this session can use this exact websocket.

        The first component is priority (fresh=0, retry=1), followed by FIFO time.
        """
        now = time.monotonic()
        ack = self._bridge.pending_action_ack
        if ack:
            retry_at = float(ack.get("retry_at") or float("inf"))
            held = bool(
                getattr(self._bridge, "ack_refresh_blocks_retry", lambda _ack: False)(ack)
            )
            if held:
                ack = None
            elif now < retry_at or int(ack.get("retries") or 0) >= max(0, int(getattr(self._bridge, "action_max_attempts", 3)) - 1):
                ack = None
            elif ack.get("ws_id") and ack.get("ws_id") != event.get("ws_id"):
                ack = None
            elif ack.get("url") and ack.get("url") != event.get("url"):
                ack = None
        if ack:
            # Retry is redundancy. Any fresh unsent decision on this websocket has
            # priority because it still has a finite backend lifetime/deadline.
            return 1, float(ack.get("at") or 0.0), float(ack.get("retry_at") or now)
        pending = self._bridge.autoplay.pending
        if not pending:
            return None
        if pending.get("ws_id") and pending.get("ws_id") != event.get("ws_id"):
            return None
        if pending.get("url") and pending.get("url") != event.get("url"):
            return None
        due = float(pending.get("due") or now)
        schedule_supported = bool(event.get("schedule_send")) or int(event.get("v") or 0) >= 3
        if not schedule_supported and now < due:
            return None
        created = due - max(0, int(pending.get("delay_ms") or 0)) / 1000.0
        return 0, created, due

    def _action_id(self, row: dict[str, Any], *, retry: bool = False) -> str:
        raw = row.get("raw")
        digest = hashlib.sha256(bytes(raw or b"")).hexdigest()[:24]
        hand = str(row.get("hand_id") or self._bridge.state.get("hand_id") or "-")
        room = str(row.get("room") or "-")
        turn = str(row.get("turn_id") or "-")
        if retry:
            # Retry identifiers MUST be attempt-specific.  ActionArbiter keeps
            # DISPATCHED records, so reusing one "retry" id would suppress 3/3.
            attempt = 2 + int(row.get("retries") or 0)
            key = f"_arbiter_retry_action_id_{attempt}"
            existing = str(row.get(key) or "")
            if existing:
                return existing
            action_id = f"{self.table_id}:retry{attempt}:{hand}:{room}:{turn}:{digest}"
            row[key] = action_id
            return action_id
        existing = str(row.get("_arbiter_action_id") or "")
        if existing:
            return existing
        action_id = f"{self.table_id}:action:{hand}:{room}:{turn}:{digest}"
        row["_arbiter_action_id"] = action_id
        return action_id

    def _pot_bb(self) -> float:
        """Current gross contribution ledger expressed in table big blinds."""

        try:
            big_blind = int(self._bridge._wire_big_blind())
            if big_blind <= 0:
                return 0.0
            pot = sum(
                max(0, int(value))
                for value in self._bridge.hand_contrib.values()
            )
            return max(0.0, float(pot) / float(big_blind))
        except Exception:
            return 0.0

    @staticmethod
    def _matches_action_socket(row: dict[str, Any], event: dict[str, Any]) -> bool:
        # Native HMN1 carries an explicit target ws_id in the command.  Any outgoing
        # lobby dummy may therefore act as a harmless arbitration clock; it no longer
        # has to be the same websocket as the game.user_turn.
        if int(event.get("v") or 0) >= 6:
            return True
        if row.get("ws_id") and row.get("ws_id") != event.get("ws_id"):
            return False
        if row.get("url") and row.get("url") != event.get("url"):
            return False
        return True

    def arbitration_action_ids(self) -> set[str]:
        result: set[str] = set()
        with self._bridge.autoplay.lock:
            pending = self._bridge.autoplay.pending
            if pending:
                result.add(self._action_id(pending))
        ack = self._bridge.pending_action_ack
        if ack:
            original = str(ack.get("_arbiter_action_id") or "")
            if original:
                result.add(original)
            for key, value in ack.items():
                if str(key).startswith("_arbiter_retry_action_id_") and value:
                    result.add(str(value))
        return result

    def action_offers(self, event: dict[str, Any]) -> tuple[ActionOffer, ...]:
        """Describe all actions this exact dummy websocket can carry."""

        if getattr(self, "play_enabled", True) is False:
            return ()
        now = time.monotonic()
        offers: list[ActionOffer] = []
        with self._bridge.autoplay.lock:
            pending = self._bridge.autoplay.pending
            if pending and self._matches_action_socket(pending, event):
                room = int(pending.get("room") or 0)
                hand_matches = (
                    not pending.get("hand_id")
                    or str(pending.get("hand_id"))
                    == str(self._bridge.state.get("hand_id") or "")
                )
                turn = self._bridge.autoplay.turn_by_room.get(room) or {}
                pending_turn = str(pending.get("turn_id") or "")
                current_turn = str(turn.get("_turn_id") or "")
                turn_matches = not pending_turn or not current_turn or pending_turn == current_turn
                if not hand_matches or not turn_matches:
                    action_id = self._action_id(pending)
                    self._bridge.autoplay.pending = None
                    self._arbiter_cancellations.append(
                        (action_id, "STALE_HAND" if not hand_matches else "TURN_CHANGED")
                    )
                else:
                    action_id = self._action_id(pending)
                    if pending.get("extra_urgent"):
                        ready_at = now
                        pending["_arbiter_ready_at"] = now
                        pending["due"] = now
                    else:
                        ready_at = float(
                            pending.setdefault(
                                "_arbiter_ready_at", float(pending.get("due") or now)
                            )
                        )
                    deadline = pending.get("turn_deadline_at")
                    offers.append(
                        ActionOffer(
                            action_id=action_id,
                            table_id=self.table_id,
                            ready_at=ready_at,
                            deadline_at=(None if deadline is None else float(deadline)),
                            pot_bb=self._pot_bb(),
                            # Humanisation stays host-side and therefore cancellable.
                            # Claim a later dummy at/after the reserved timestamp
                            # instead of handing the app an uncancellable 6-12s timer.
                            supports_delayed_send=False,
                            bypass_gap=bool(
                                pending.get("fallback")
                                or pending.get("prefold")
                                or pending.get("extra_urgent")
                            ),
                        )
                    )

        # A fully exhausted 3/3 action no longer produces an offer, so report
        # its final one-second confirmation expiry here while dummies still flow.
        reporter=getattr(self._bridge,"report_action_exhausted_if_due",None)
        if callable(reporter):
            reporter(now)
        ack = self._bridge.pending_action_ack
        if ack:
            retry_at = float(ack.get("retry_at") or float("inf"))
            same_hand = str(ack.get("hand_id") or "") == str(
                self._bridge.state.get("hand_id") or ""
            )
            held = bool(
                getattr(self._bridge, "ack_refresh_blocks_retry", lambda _ack: False)(ack)
            )
            if (
                now >= retry_at
                and not held
                and int(ack.get("retries") or 0) < max(0, int(getattr(self._bridge, "action_max_attempts", 3)) - 1)
                and same_hand
                and self._matches_action_socket(ack, event)
            ):
                deadline = ack.get("turn_deadline_at")
                offers.append(
                    ActionOffer(
                        action_id=self._action_id(ack, retry=True),
                        table_id=self.table_id,
                        ready_at=retry_at,
                        deadline_at=(None if deadline is None else float(deadline)),
                        pot_bb=self._pot_bb(),
                        # LiveCoinBridge retries are immediate; a legacy/current hook
                        # dummy must wait until arbitration says the gap has elapsed.
                        supports_delayed_send=False,
                        retry=True,
                        bypass_gap=True,
                    )
                )
        return tuple(offers)

    def prepare_action_dispatch(self, plan: DispatchPlan) -> bool:
        """Apply one arbiter reservation to the bridge's existing action record."""

        with self._bridge.autoplay.lock:
            pending = self._bridge.autoplay.pending
            if pending and self._action_id(pending) == plan.action_id:
                self._arbiter_dispatch_context[plan.action_id] = {
                    "turn_id": str(pending.get("turn_id") or ""),
                    "turn_deadline_at": pending.get("turn_deadline_at"),
                }
                pending.setdefault("_arbiter_original_due", pending.get("due"))
                pending["due"] = float(plan.scheduled_at)
                pending["_arbiter_scheduled_at"] = float(plan.scheduled_at)
                pending["_arbiter_gap_seconds"] = plan.sampled_gap_seconds
                pending["_arbiter_deep_pot_delay_seconds"] = (
                    plan.sampled_deep_pot_delay_seconds
                )
                pending["_arbiter_reasons"] = list(plan.reasons)
                return True
        ack = self._bridge.pending_action_ack
        if ack and self._action_id(ack, retry=True) == plan.action_id:
            ack["retry_at"] = float(plan.scheduled_at)
            ack["_arbiter_retry_scheduled_at"] = float(plan.scheduled_at)
            ack["_arbiter_retry_gap_seconds"] = plan.sampled_gap_seconds
            ack["_arbiter_retry_reasons"] = list(plan.reasons)
            return True
        return False

    def finalize_action_dispatch(
        self, plan: DispatchPlan, decision: dict[str, Any]
    ) -> bool:
        if decision.get("action") not in {"replace", "schedule_send"}:
            return False
        ack = self._bridge.pending_action_ack
        if ack:
            context = self._arbiter_dispatch_context.pop(plan.action_id, {})
            if plan.retry:
                attempt = 1 + int(ack.get("retries") or 0)
                ack[f"_arbiter_retry_action_id_{attempt}"] = plan.action_id
            else:
                ack["_arbiter_action_id"] = plan.action_id
                ack["turn_id"] = str(context.get("turn_id") or "")
                ack["turn_deadline_at"] = context.get("turn_deadline_at")
            ack["_arbiter_scheduled_at"] = float(plan.scheduled_at)
            target_ws=str(ack.get("ws_id") or decision.get("ws_id") or "")
            if target_ws:
                decision["ws_id"]=target_ws
                try: decision["_ws_u32"]=int(target_ws,16)
                except ValueError: pass
            target_channel=str(ack.get("channel_id") or decision.get("_target_channel_id") or "")
            if target_channel:
                decision["_target_channel_id"]=target_channel
            meta=dict(decision.get("_operator_action") or {})
            meta.update({
                "table_id":self.table_id,
                "action":str(ack.get("action") or meta.get("action") or ""),
                "amount":ack.get("display_amount"),
                "attempt":(1 + int(ack.get("retries") or 0)) if plan.retry else 1,
                "max_attempts":int(getattr(self._bridge, "action_max_attempts", 3)),
                "token":str(decision.get("token") or ack.get("token") or ""),
                "queue_delay_ms":max(0,int(round(float(plan.dispatch_delay_seconds)*1000.0))),
            })
            decision["_operator_action"]=meta
            self._last_action_delay_ms = int(meta["queue_delay_ms"])
        return True

    def cancel_arbitration_action(self, action_id: str) -> None:
        with self._bridge.autoplay.lock:
            pending = self._bridge.autoplay.pending
            if pending and self._action_id(pending) == str(action_id):
                self._bridge.autoplay.pending = None

    def drain_arbitration_cancellations(self) -> tuple[tuple[str, str], ...]:
        rows = tuple(self._arbiter_cancellations)
        self._arbiter_cancellations.clear()
        return rows


    def _ledger_detail(self) -> dict[str, Any]:
        bridge = self._bridge
        nick = str(
            bridge.identity.get("user_name")
            or bridge.state.get("user_name")
            or ""
        )
        try:
            hero_seat = int(bridge.state.get("hero_seat") or 0)
        except (TypeError, ValueError):
            hero_seat = 0
        try:
            scale = float(getattr(bridge, "scale", 100) or 100)
        except (TypeError, ValueError):
            scale = 100.0
        if scale <= 0:
            scale = 100.0
        profit_chips = 0
        if hero_seat > 0:
            try:
                profit_chips = int((getattr(bridge, "session_profit", {}) or {}).get(hero_seat - 1, 0) or 0)
            except (TypeError, ValueError):
                profit_chips = 0
        stack = 0.0
        seat_map = getattr(bridge, "seat_map", {}) or {}
        raw = seat_map.get(hero_seat)
        if raw is None:
            raw = seat_map.get(str(hero_seat))
        if isinstance(raw, dict):
            try:
                stack = float(raw.get("userChips") if raw.get("userChips") not in (None, "") else raw.get("buyinAmount") or 0)
            except (TypeError, ValueError):
                stack = 0.0
        return {
            "nickname": nick,
            "stack": stack,
            "table_profit": round(profit_chips / scale, 4),
        }

    def _emit_hand_started(self, hand: str) -> None:
        hand = str(hand or "")
        if not hand or hand in self._started:
            return
        self._started.add(hand)
        self._last_hand = hand
        snapshot = self._snapshot()
        self._sink(
            RouterObservation(
                "hand_started", self.device_id, self.table_id, self.account_id,
                snapshot.health, snapshot.reason, snapshot.game_type or None,
                snapshot.coin_bb, hand, snapshot.pending,
                detail={**snapshot.backend_detail, **self._ledger_detail()},
                backend_status=snapshot.backend_status,
                backend_message=snapshot.backend_message,
                backend_hash=snapshot.backend_hash,
                **snapshot.fuel_observation,
            )
        )

    def _emit_hand_completed(self, hand: str, reason: str) -> None:
        hand = str(hand or "")
        if not hand or hand in self._completed:
            return
        if hand not in self._started:
            return
        self._completed.add(hand)
        snapshot = self._snapshot()
        observation_reason = (
            snapshot.reason
            if snapshot.health != "green" or snapshot.backend_health != "green"
            else reason
        )
        self._sink(
            RouterObservation(
                "hand_completed", self.device_id, self.table_id, self.account_id,
                snapshot.health, observation_reason, snapshot.game_type or None,
                snapshot.coin_bb, hand, False, True,
                detail={**snapshot.backend_detail, **self._ledger_detail()},
                backend_status=snapshot.backend_status,
                backend_message=snapshot.backend_message,
                backend_hash=snapshot.backend_hash,
                **snapshot.fuel_observation,
            )
        )
        if self._last_hand == hand:
            self._last_hand = ""
        if self._candidate_hand == hand:
            self._candidate_hand = ""
        self._apply_pending_table_logic()

    def _snapshot(self) -> _LiveTableSnapshot:
        bridge = self._bridge
        hand = str(bridge.state.get("hand_id") or "")
        pending = bool(bridge.autoplay.pending or bridge.pending_action_ack)
        phase = str(bridge.lifecycle_phase or "starting")
        profile = bridge.active_money_profile
        coin_bb = float(profile.coin_big_blind) if profile is not None else None
        game_type = ""
        context = bridge.current_hand or bridge.context_hand
        if context is not None:
            props = getattr(getattr(context, "room", None), "props", {}) or {}
            try:
                mini = int(props.get("miniGameTypeId"))
            except (TypeError, ValueError):
                mini = 0
            game_type = str(
                props.get("_gameTypeLabel")
                or {1: "NLH", 2: "PLO", 17: "PLO5", 20: "PLO6"}.get(mini, "")
                or ""
            )
            raw = str(props.get("gameType") or props.get("variant") or "")
            if not game_type:
                game_type = "NLH" if raw.upper() in {"", "RING", "CASH"} else raw
        transport_up = bool(bridge.eye_w and not bridge.eye_w.is_closing())
        proxy_status = getattr(self._proxy, "backend_status_snapshot", None)
        if proxy_status is None:
            backend_health = "green" if transport_up else "red"
            backend_status = "NORMAL" if transport_up else "ERROR"
            backend_message = "" if transport_up else "backend transport disconnected"
            backend_hash = ""
            backend_sequence = 0
        else:
            backend_health = str(getattr(proxy_status, "health", "yellow") or "yellow").lower()
            if backend_health not in {"red", "yellow", "green"}:
                backend_health = "yellow"
            backend_status = str(getattr(proxy_status, "status", "") or "")
            backend_message = str(getattr(proxy_status, "message", "") or "")
            backend_hash = str(getattr(proxy_status, "hash", "") or "")
            backend_sequence = int(getattr(proxy_status, "sequence", 0) or 0)
        proxy_fuel = getattr(self._proxy, "backend_fuel_snapshot", None)
        if proxy_fuel is None:
            fuel_quantity = None
            fuel_rate_per_hand = None
            source_fuel_reason = "FUEL_UNAVAILABLE"
            fuel_sequence = 0
            fuel_observed = False
            fuel_updated_at = 0.0
        else:
            fuel_quantity = _finite_nonnegative(getattr(proxy_fuel, "quantity", None))
            fuel_rate_per_hand = _finite_nonnegative(
                getattr(proxy_fuel, "rate_per_hand", None)
            )
            source_fuel_reason = str(
                getattr(proxy_fuel, "reason_code", "") or "FUEL_UNAVAILABLE"
            )
            fuel_sequence = int(getattr(proxy_fuel, "sequence", 0) or 0)
            fuel_observed = True
            fuel_updated_at = float(getattr(proxy_fuel, "updated_at", 0.0) or 0.0)
        fuel_low_threshold = normalize_fuel_threshold(
            getattr(self, "_fuel_low_threshold", 1500.0)
        )
        normalized_fuel_reason = fuel_reason_code(
            fuel_quantity, source_fuel_reason, fuel_low_threshold
        )
        return _LiveTableSnapshot(
            hand=hand,
            pending=pending,
            phase=phase,
            coin_bb=coin_bb,
            game_type=game_type,
            transport_up=transport_up,
            backend_health=backend_health,
            backend_status=backend_status,
            backend_message=backend_message,
            backend_hash=backend_hash,
            backend_sequence=backend_sequence,
            fuel_quantity=fuel_quantity,
            fuel_rate_per_hand=fuel_rate_per_hand,
            fuel_reason_code=normalized_fuel_reason,
            fuel_sequence=fuel_sequence,
            fuel_low_threshold=fuel_low_threshold,
            fuel_observed=fuel_observed,
            fuel_updated_at=fuel_updated_at,
        )

    async def _monitor_state(self) -> None:
        previous: Optional[_LiveTableSnapshot] = None
        try:
            while not self._closed:
                snapshot = self._snapshot()
                # History hands start on hero hole cards / hero user_turn, not
                # every table gameId that happens to sit in the snapshot.
                # ``bridge.state[hand_id]`` is temporarily empty while a cold
                # snapshot is reconstructed or between Coin street packets.  It is
                # not a settlement signal.  Completion is emitted only from
                # cumulativeWinnerInfo, reset_data, or a distinct next hand.
                if snapshot != previous:
                    self._sink(
                        RouterObservation(
                            "table_update", self.device_id, self.table_id,
                            self.account_id, snapshot.health, snapshot.reason,
                            snapshot.game_type or None, snapshot.coin_bb,
                            snapshot.hand or None, snapshot.pending,
                            detail=snapshot.backend_detail,
                            backend_status=snapshot.backend_status,
                            backend_message=snapshot.backend_message,
                            backend_hash=snapshot.backend_hash,
                            **snapshot.fuel_observation,
                        )
                    )
                    previous = snapshot
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass

    async def close(self, *, crashed: bool = False, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        graceful_error = ""
        try:
            async with self._lock:
                # Let already accepted Coin events reach the protocol worker first.
                self._bridge.manual_action_event.set()
                with self._bridge.autoplay.lock:
                    self._bridge.autoplay.pending = None
                protocol_queue = getattr(self._bridge, "protocol_queue", None)
                if protocol_queue is not None:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(protocol_queue.join(), 2.0)
                finish = self._bridge.state.get("_pending_finish_hint")
                if finish:
                    await self._bridge.finish_hint(finish)
                if self._bridge.context_active:
                    await self._bridge.leave_table_context(
                        f"router-{reason}", send_request=True, emit_stand_request=True
                    )
        except Exception as exc:
            graceful_error = f"{type(exc).__name__}: {exc}"
            crashed = True
        finally:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
            for task_name in ("protocol_task", "eye_reader_task", "leave_timeout_task"):
                task = getattr(self._bridge, task_name, None)
                if task and not task.done():
                    task.cancel()
            writer = getattr(self._bridge, "eye_w", None)
            if writer and not writer.is_closing():
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            with contextlib.suppress(Exception):
                await self._proxy.close()
            released = self._accounts.release(
                self._lease.owner,
                self._lease.token,
                quarantine_seconds=self._quarantine if crashed else 0.0,
            )
            if released is not None:
                self._sink(
                    RouterObservation(
                        "account_released", self.device_id, self.table_id,
                        self.account_id, "red" if crashed else "green", reason,
                        detail={
                            "released_account": self.account_id,
                            "quarantined_seconds": self._quarantine if crashed else 0.0,
                        },
                    )
                )
            self._sink(
                RouterObservation(
                    "table_close", self.device_id, self.table_id, self.account_id,
                    "red" if crashed else "green", reason,
                    detail={"crashed": crashed, "graceful_error": graceful_error},
                )
            )


class LiveTableSessionFactory:
    def __init__(
        self,
        *,
        accounts: AccountPool,
        credential_file: Path,
        backend_host: str,
        backend_port: int,
        observation_sink: ObservationSink,
        telemetry: Any = None,
        crash_quarantine_seconds: float = 60.0,
        frame_delay: float = 0.01,
        connect_timeout: float = 15.0,
        probe_attempts_per_table: int = 8,
        probe_backoff_seconds: float = 0.5,
        fuel_low_threshold: float = 1500.0,
        auto_register_rejected: bool = False,
        registration_android_id: str = "",
        login_reject_quarantine_seconds: float = 900.0,
        account_provisioner: Any = None,
    ) -> None:
        self.accounts = accounts
        self.credential_file = Path(credential_file)
        self.backend_host = str(backend_host)
        self.backend_port = int(backend_port)
        self.observation_sink = observation_sink
        self.telemetry = telemetry
        self.crash_quarantine_seconds = float(crash_quarantine_seconds)
        self.frame_delay = float(frame_delay)
        self.connect_timeout = float(connect_timeout)
        self.probe_attempts_per_table = max(1, int(probe_attempts_per_table))
        self.probe_backoff_seconds = max(0.05, float(probe_backoff_seconds))
        self.fuel_low_threshold = normalize_fuel_threshold(fuel_low_threshold)
        # Legacy experiment: a second CSLogin with the original Android identity.
        # Live testing proved SCLogin=false is not an account-creation signal, so
        # this path is OFF by default and only retained behind an explicit env flag.
        self.auto_register_rejected = bool(auto_register_rejected)
        self.registration_android_id = str(registration_android_id or "").strip()
        self.login_reject_quarantine_seconds = max(1.0, float(login_reject_quarantine_seconds))
        self.account_provisioner = account_provisioner
        self._registration_lock = asyncio.Lock()

    async def create(self, device_id: str, table_id: int, seed: RouterSeed) -> LiveTableSession:
        from ..verified_v1.coin_bridge_live import LiveCoinBridge
        from ..verified_v1.eye_direct_proxy import (
            DEFAULT_ANDROID_ID,
            BackendLoginRejected,
            DirectBackendProxy,
            DirectBackendSlot,
            backend_android_id,
        )

        owner = f"device/{device_id}/table/{int(table_id)}"
        rejected = 0
        last_wait_log = 0.0
        while True:
            try:
                lease = self.accounts.acquire(owner)
            except AccountProbeDeferred as exc:
                # The pool is expandable; another table is merely validating a
                # suffix right now. Wait without turning this table into NO_SLOT.
                await asyncio.sleep(exc.retry_after)
                continue
            except AccountPoolExhausted:
                if self.account_provisioner is not None:
                    self.observation_sink(
                        RouterObservation(
                            "account_registering", device_id, table_id,
                            status="yellow",
                            reason="no free PokerEYE account; creating a new panel login",
                            pending=True,
                            detail={"free_count": self.accounts.free_count, "source": "panel"},
                        )
                    )
                    try:
                        async with self._registration_lock:
                            created = await asyncio.to_thread(self.account_provisioner)
                        account_id = getattr(created, "account_id", None) or str(created)
                        self.accounts.register_validated(
                            account_id, source="panel-loginAccountReq"
                        )
                        self.observation_sink(
                            RouterObservation(
                                "account_registered", device_id, table_id, account_id,
                                "green",
                                "new PokerEYE account created; logging in",
                                pending=True,
                                detail={"source": "panel"},
                            )
                        )
                    except Exception as exc:
                        self.observation_sink(
                            RouterObservation(
                                "account_registration_failed", device_id, table_id,
                                status="yellow",
                                reason=f"panel create failed: {type(exc).__name__}",
                                pending=True,
                                detail={"error": str(exc)[:200], "source": "panel"},
                            )
                        )
                        await asyncio.sleep(max(0.5, self.probe_backoff_seconds))
                    continue
                now = time.monotonic()
                if now - last_wait_log >= 2.0:
                    last_wait_log = now
                    self.observation_sink(
                        RouterObservation(
                            "account_waiting", device_id, table_id,
                            status="yellow",
                            reason="no free PokerEYE slot; waiting for a released account",
                            pending=True,
                            detail={"free_count": self.accounts.free_count},
                        )
                    )
                await asyncio.sleep(max(0.25, self.probe_backoff_seconds))
                continue

            probing = self.accounts.state_for(lease.account_id) == AccountState.PROBING
            if probing:
                self.observation_sink(
                    RouterObservation(
                        "account_probing", device_id, table_id, lease.account_id,
                        "yellow", "validating backend account login", pending=True,
                        detail={"account_state": AccountState.PROBING.value},
                    )
                )

            slot_android_id = backend_android_id(lease.account_id)
            registration_android_id = self.registration_android_id or DEFAULT_ANDROID_ID
            proxy = None
            bridge = None

            proxy_logger = None
            if self.telemetry is not None:
                proxy_logger = lambda tag, message, account_id=lease.account_id: self.telemetry.backend_log(
                    device_id, table_id, account_id, tag, message
                )
                self.telemetry.bind_table(
                    device_id,
                    table_id,
                    lease.account_id,
                    seed_events=seed.events,
                )

            def diagnostic_sink(tag, message, detail=None, account_id=lease.account_id):
                clean_tag = str(tag or "bridge")
                clean_message = " ".join(str(message or "").split())[:800]
                if clean_tag in {"state_error", "protocol_event_error"}:
                    severity = "red"
                elif clean_tag in {"cc_timeout", "cc_mapping_error", "hint_error"}:
                    severity = "yellow"
                else:
                    severity = "green"
                clean_detail = {"tag": clean_tag}
                if isinstance(detail, dict):
                    clean_detail.update(detail)
                self.observation_sink(
                    RouterObservation(
                        "bridge_diag", device_id, table_id, account_id,
                        severity, f"{clean_tag}: {clean_message}",
                        detail=clean_detail,
                    )
                )
                if self.telemetry is not None:
                    self.telemetry.backend_log(
                        device_id, table_id, account_id, clean_tag, clean_message
                    )

            def make_slot(android_id: str) -> DirectBackendSlot:
                return DirectBackendSlot(
                    account_id=lease.account_id,
                    credential_file=self.credential_file,
                    host=self.backend_host,
                    port=self.backend_port,
                    android_id=str(android_id),
                )

            async def close_attempt(
                candidate_proxy: Optional[DirectBackendProxy],
                candidate_bridge: Any,
            ) -> None:
                # Startup failures happen before a LiveTableSession owns cleanup.
                # Tear down the local bridge generation explicitly so rejected
                # login attempts cannot leave reader/protocol tasks behind and
                # occupy a remote PokerEYE slot.
                if candidate_bridge is not None:
                    for task_name in ("protocol_task", "eye_reader_task", "leave_timeout_task"):
                        task = getattr(candidate_bridge, task_name, None)
                        if task and not task.done():
                            task.cancel()
                    writer = getattr(candidate_bridge, "eye_w", None)
                    if writer and not writer.is_closing():
                        writer.close()
                        with contextlib.suppress(Exception):
                            await writer.wait_closed()
                if candidate_proxy is not None:
                    with contextlib.suppress(Exception):
                        await candidate_proxy.close()

            async def open_live(android_id: str, *, stage: str) -> tuple[DirectBackendProxy, LiveCoinBridge]:
                candidate_proxy: Optional[DirectBackendProxy] = None
                candidate_bridge: Optional[LiveCoinBridge] = None
                self.observation_sink(
                    RouterObservation(
                        "account_connecting", device_id, table_id, lease.account_id,
                        "yellow", "opening dedicated PokerEYE backend stream",
                        pending=True,
                        detail={
                            "account_state": (
                                AccountState.PROBING.value if probing else AccountState.LEASED.value
                            ),
                            "backend_slot_id": str(android_id),
                            "login_stage": stage,
                        },
                    )
                )
                try:
                    candidate_proxy = DirectBackendProxy(
                        make_slot(android_id), logger=proxy_logger
                    )
                    eye_host, eye_port = await candidate_proxy.start()
                    candidate_bridge = LiveCoinBridge(
                        eye_host,
                        eye_port,
                        frame_delay=self.frame_delay,
                        diagnostic_sink=diagnostic_sink,
                    )
                    candidate_bridge.seed_router_context(
                        seed.events,
                        requested_table_id=seed.requested_table_id or int(table_id),
                        requested_config_id=seed.requested_config_id,
                    )
                    await asyncio.wait_for(
                        candidate_bridge.ensure_eye(), timeout=self.connect_timeout
                    )
                    await candidate_proxy.wait_backend_ready(self.connect_timeout)
                    return candidate_proxy, candidate_bridge
                except BaseException:
                    await close_attempt(candidate_proxy, candidate_bridge)
                    raise

            async def provision_with_original_identity() -> None:
                # There is no separate registration RPC in the recovered APK.
                # A short CSLogin using the original app's stable Android identity
                # is the provisioning attempt. Do not attach a Coin bridge or send
                # table traffic; successful SCLogin is sufficient.
                registration_proxy: Optional[DirectBackendProxy] = None
                registration_writer: Optional[asyncio.StreamWriter] = None
                try:
                    registration_proxy = DirectBackendProxy(
                        make_slot(registration_android_id), logger=proxy_logger
                    )
                    eye_host, eye_port = await registration_proxy.start()
                    _reader, registration_writer = await asyncio.open_connection(
                        eye_host, eye_port
                    )
                    await registration_proxy.wait_backend_ready(self.connect_timeout)
                finally:
                    if registration_writer is not None and not registration_writer.is_closing():
                        registration_writer.close()
                        with contextlib.suppress(Exception):
                            await registration_writer.wait_closed()
                    if registration_proxy is not None:
                        with contextlib.suppress(Exception):
                            await registration_proxy.close()

            try:
                try:
                    proxy, bridge = await open_live(slot_android_id, stage="runtime")
                except BackendLoginRejected as first_reject:
                    if not self.auto_register_rejected:
                        raise

                    self.observation_sink(
                        RouterObservation(
                            "account_registering", device_id, table_id, lease.account_id,
                            "yellow",
                            "login rejected; trying original-client auto-provision",
                            pending=True,
                            detail={
                                "account_state": (
                                    AccountState.PROBING.value if probing else AccountState.LEASED.value
                                ),
                                "registration_android_id": registration_android_id,
                            },
                        )
                    )
                    try:
                        async with self._registration_lock:
                            await provision_with_original_identity()
                            self.observation_sink(
                                RouterObservation(
                                    "account_registered",
                                    device_id,
                                    table_id,
                                    lease.account_id,
                                    "green",
                                    "auto-provision login accepted; verifying isolated slot",
                                    pending=True,
                                    detail={"registration_android_id": registration_android_id},
                                )
                            )
                            proxy, bridge = await open_live(
                                slot_android_id, stage="post_register_verify"
                            )
                    except BackendLoginRejected as second_reject:
                        self.observation_sink(
                            RouterObservation(
                                "account_registration_failed",
                                device_id,
                                table_id,
                                lease.account_id,
                                "yellow",
                                "auto-provision rejected; trying next available suffix",
                                pending=True,
                                detail={
                                    "initial_error": str(first_reject),
                                    "registration_error": str(second_reject),
                                },
                            )
                        )
                        raise second_reject from first_reject

                if proxy is None or bridge is None:
                    raise RuntimeError("backend startup completed without a live stream")
                if self.accounts.confirm(owner, lease.token) is None:
                    raise RuntimeError("backend account lease disappeared during login")
                session = LiveTableSession(
                    device_id=device_id,
                    table_id=table_id,
                    lease=lease,
                    bridge=bridge,
                    proxy=proxy,
                    accounts=self.accounts,
                    observation_sink=self.observation_sink,
                    crash_quarantine_seconds=self.crash_quarantine_seconds,
                    fuel_low_threshold=self.fuel_low_threshold,
                )

                async def release_on_recovery_exhaustion(reason: str) -> None:
                    await session.close(crashed=True, reason=str(reason))

                # Lease-safe fallback for the short interval before the owning
                # DeviceIngressRouter installs its stronger remove-slot callback.
                proxy.bind_bridge(
                    bridge,
                    recovery_exhausted_callback=release_on_recovery_exhaustion,
                )
                self.observation_sink(
                    RouterObservation(
                        "account_leased", device_id, table_id, lease.account_id,
                        "yellow", "backend login validated; stream ready", pending=True,
                        detail={"account_state": AccountState.LEASED.value},
                    )
                )
                return session
            except BackendLoginRejected as exc:
                await close_attempt(proxy, bridge)
                # SCLogin=false is authoritative for this attempt, but live
                # evidence shows the same suffix can be accepted later (agent
                # slot full, transient deny). Never permanently destroy it here.
                self.accounts.release(
                    owner,
                    lease.token,
                    quarantine_seconds=self.login_reject_quarantine_seconds,
                    reason=str(exc),
                )
                rejected += 1
                if self.telemetry is not None:
                    self.telemetry.close_table(
                        device_id,
                        table_id,
                        reason=f"backend account {lease.account_id} temporarily rejected",
                        crashed=False,
                    )
                unbounded_probe = bool(self.accounts.auto_expand_unbounded)
                self.observation_sink(
                    RouterObservation(
                        "account_quarantined", device_id, table_id, lease.account_id,
                        "yellow", "backend login rejected; suffix quarantined, trying next suffix",
                        pending=True,
                        detail={
                            "account_state": AccountState.QUARANTINED.value,
                            "probe_attempt": rejected,
                            "probe_attempt_limit": (
                                None if unbounded_probe else self.probe_attempts_per_table
                            ),
                            "probe_unbounded": unbounded_probe,
                            "auto_register_attempted": self.auto_register_rejected,
                            "retry_seconds": self.login_reject_quarantine_seconds,
                            "backend_reject": str(exc)[:500],
                        },
                    )
                )
                if not unbounded_probe and rejected >= self.probe_attempts_per_table:
                    raise AccountPoolExhausted(
                        "no usable backend account in bounded probe budget after "
                        "temporary login rejections"
                    ) from exc
                await asyncio.sleep(
                    min(5.0, self.probe_backoff_seconds * (2 ** min(rejected - 1, 4)))
                )
                continue
            except asyncio.CancelledError:
                await close_attempt(proxy, bridge)
                self.accounts.release(
                    owner,
                    lease.token,
                    quarantine_seconds=self.probe_backoff_seconds if probing else 0.0,
                    reason="table startup cancelled",
                )
                if self.telemetry is not None:
                    self.telemetry.close_table(
                        device_id, table_id, reason="table startup cancelled", crashed=False
                    )
                raise
            except Exception as exc:
                await close_attempt(proxy, bridge)
                self.accounts.release(
                    owner,
                    lease.token,
                    quarantine_seconds=self.crash_quarantine_seconds,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                self.observation_sink(
                    RouterObservation(
                        "account_quarantined", device_id, table_id, lease.account_id,
                        "red", "backend transport/protocol failure; suffix quarantined",
                        detail={"account_state": AccountState.QUARANTINED.value},
                    )
                )
                if self.telemetry is not None:
                    self.telemetry.close_table(
                        device_id,
                        table_id,
                        reason=f"backend session failed: {type(exc).__name__}",
                        crashed=True,
                    )
                raise


@dataclass
class _SessionSlot:
    table_id: int
    created_order: int
    buffer: collections.deque[dict[str, Any]]
    session: Optional[TableSession] = None
    start_task: Optional[asyncio.Task[Any]] = None
    close_task: Optional[asyncio.Task[Any]] = None
    failed: bool = False
    startup_attempts: int = 0
    startup_error: str = ""
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    seed: Optional[RouterSeed] = None


class DeviceIngressRouter:
    """Demultiplex one emulator hook stream before any mutable bridge state.

    There is no event fan-out.  A game event has one table owner, a dummy has one
    pending action owner, and global lobby/config facts are retained as immutable
    seed history for sessions created later.
    """

    def __init__(
        self,
        device_id: str,
        session_factory: TableSessionFactory,
        *,
        observation_sink: Optional[ObservationSink] = None,
        global_history_limit: int = 512,
        orphan_limit_per_room: int = 128,
        max_orphan_rooms: int = 128,
        max_provisional_tables: int = 64,
        max_table_slots: int = 64,
        session_buffer_limit: int = 512,
        max_inflight: int = 128,
        close_grace_seconds: float = 15.0,
        closing_tombstone_seconds: float = 15.0,
        provisional_timeout: float = 900.0,
        startup_max_attempts: int = 3,
        startup_backoff_base: float = 0.25,
        startup_backoff_max: float = 2.0,
        startup_attempt_timeout: float = 12.0,
        startup_stale_seconds: float = 45.0,
        telemetry: Any = None,
        action_arbiter: Optional[ActionArbiter] = None,
        navigation_handler: Optional[
            Callable[[dict[str, Any]], dict[str, Any]]
        ] = None,
        automation: Any = None,
        history_ledger: Any = None,
    ) -> None:
        if int(startup_max_attempts) < 1:
            raise ValueError("startup_max_attempts must be positive")
        if float(startup_backoff_base) < 0:
            raise ValueError("startup_backoff_base cannot be negative")
        if float(startup_backoff_max) < float(startup_backoff_base):
            raise ValueError("startup_backoff_max must be >= startup_backoff_base")
        if float(startup_attempt_timeout) <= 0:
            raise ValueError("startup_attempt_timeout must be positive")
        if float(startup_stale_seconds) <= 0:
            raise ValueError("startup_stale_seconds must be positive")
        self.device_id = str(device_id)
        self.session_factory = session_factory
        self._sink = observation_sink or (lambda _event: None)
        self._history: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=max(8, int(global_history_limit))
        )
        self._sticky_identity_event: Optional[dict[str, Any]] = None
        self._sticky_context_events: collections.OrderedDict[str, dict[str, Any]] = (
            collections.OrderedDict()
        )
        self._room_to_table: dict[int, int] = {}
        self._config_by_table: dict[int, int] = {}
        # One Coin game websocket may multiplex several SmartFox rooms.  Preserve
        # the many-to-many fact and use ws_id alone only while it is unambiguous.
        self._ws_to_tables: dict[str, set[int]] = {}
        # SmartFox emits a low-level GameRoomProperties frame with one concrete
        # table id immediately before (or during) joining a game room. Keep that
        # identity only briefly so a trainer restart in the middle of a hand can
        # re-bind the first room event even when game_init is not replayed.
        # A websocket may multiplex several tables, so conflicting recent hints
        # are marked ambiguous and are never guessed.
        self._ws_table_hints: dict[str, tuple[int, float]] = {}
        self._ws_table_hint_timeout = 5.0
        self._orphans: dict[int, collections.deque[dict[str, Any]]] = {}
        self._orphan_limit = max(1, int(orphan_limit_per_room))
        self._max_orphan_rooms = max(1, int(max_orphan_rooms))
        self._max_provisional_tables = max(1, int(max_provisional_tables))
        self._max_table_slots = max(1, int(max_table_slots))
        self._session_buffer_limit = max(8, int(session_buffer_limit))
        self._sessions: dict[int, _SessionSlot] = {}
        self._closing_tables: dict[int, Optional[int]] = {}
        self._closing_rooms: dict[int, int] = {}
        self._tombstone_tasks: dict[int, asyncio.Task[Any]] = {}
        self._provisional: dict[int, dict[str, Any]] = {}
        self._provisional_tasks: dict[int, asyncio.Task[Any]] = {}
        self._join_intents: collections.deque[tuple[int, int, float]] = collections.deque(maxlen=64)
        self._recent_closed: dict[int, float] = {}
        self._counter = 0
        self._lock = asyncio.Lock()
        self._inflight = asyncio.Semaphore(max(4, int(max_inflight)))
        self._server: Optional[asyncio.AbstractServer] = None
        self._close_grace = max(0.1, float(close_grace_seconds))
        self._tombstone_seconds = max(1.0, float(closing_tombstone_seconds))
        self._provisional_timeout = max(1.0, float(provisional_timeout))
        self._startup_max_attempts = int(startup_max_attempts)
        self._startup_backoff_base = float(startup_backoff_base)
        self._startup_backoff_max = float(startup_backoff_max)
        self._startup_attempt_timeout = float(startup_attempt_timeout)
        self._startup_stale_seconds = float(startup_stale_seconds)
        self._telemetry = telemetry
        # One player/device arbiter; per-table backend hint computation remains parallel.
        self._action_arbiter = action_arbiter or ActionArbiter(self.device_id)
        self._navigation_handler = navigation_handler
        self.automation = automation
        self.history_ledger = history_ledger
        if self.automation is not None and self._navigation_handler is None:
            self._navigation_handler = self.automation.handle
        self._watchdog_task: Optional[asyncio.Task[Any]] = None
        # Unsupported table policy is per physical-device session.  A table that
        # advertises/enters any DOUBLE BOARD variant is never leased again during
        # this router lifetime.  Exit commands remain independent from business.
        self._unsupported_tables: dict[int, str] = {}
        self._unsupported_room_reasons: dict[int, str] = {}
        self._unsupported_exit: dict[int, dict[str, Any]] = {}
        self._unsupported_close_tasks: dict[int, asyncio.Task[Any]] = {}
        self._forced_sequence = 0
        self._closed = False

    def start_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watchdog_task = loop.create_task(
            self._watchdog_loop(), name=f"auto-watch-{self.device_id}"
        )

    def _navigation_decision(self, event: dict[str, Any]) -> dict[str, Any]:
        if self._navigation_handler is None:
            return _forward(event)
        try:
            value = dict(self._navigation_handler(event) or {})
        except Exception as exc:
            self._sink(
                RouterObservation(
                    "warning",
                    self.device_id,
                    status="yellow",
                    reason=f"navigation observation: {type(exc).__name__}: {exc}",
                )
            )
            return _forward(event, navigation_error=type(exc).__name__)
        if value.get("id", event.get("id", "")) != event.get("id", ""):
            self._sink(
                RouterObservation(
                    "warning",
                    self.device_id,
                    status="yellow",
                    reason="navigation returned a mismatched hook event id",
                )
            )
            return _forward(event, navigation_error="event_id_mismatch")
        return value or _forward(event)

    def _record_telemetry(
        self,
        table_id: int,
        routed: RoutedEvent,
        decision: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._telemetry is None:
            return
        self._telemetry.coin_event(
            self.device_id,
            int(table_id),
            routed.event,
            command=routed.command,
            direction=routed.direction,
            room_id=routed.room_id,
            data=routed.data,
            decision=decision,
        )

    @property
    def active_table_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._sessions))

    def table_has_live_session(self, table_id: int) -> bool:
        slot = self._sessions.get(int(table_id))
        return slot is not None and slot.session is not None

    async def start(self, host: str, port: int) -> tuple[str, int]:
        if self._server is not None:
            sockets = self._server.sockets or ()
            bound = sockets[0].getsockname() if sockets else (host, port)
            return str(bound[0]), int(bound[1])
        self._server = await asyncio.start_server(
            self._client, str(host), int(port), backlog=256, reuse_address=True
        )
        socket = (self._server.sockets or ())[0]
        bound = socket.getsockname()
        self._sink(
            RouterObservation(
                "router_ready", self.device_id, status="green",
                reason=f"hook listener {bound[0]}:{bound[1]}",
                detail={"ingress_port": int(bound[1])},
            )
        )
        return str(bound[0]), int(bound[1])

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not self._closed:
                raw = await _lp_read(reader)
                if raw is None:
                    break
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("hook event is not an object")
                except Exception as exc:
                    decision = _forward({}, router_error=f"bad_json: {exc}")
                else:
                    decision, finish = await self.handle_event(event)
                    # The owning bridge schedules Finish itself for app-side delayed
                    # sends.  Immediate legacy injection still needs this callback.
                    if finish:
                        slot = await self._slot_for_finish(finish)
                        if slot and slot.session:
                            asyncio.create_task(slot.session._bridge.finish_hint(finish)) if isinstance(slot.session, LiveTableSession) else None
                writer.write(_lp_pack(decision))
                await writer.drain()
        except Exception as exc:
            self._sink(
                RouterObservation(
                    "warning", self.device_id, status="yellow",
                    reason=f"hook client: {type(exc).__name__}: {exc}",
                )
            )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _slot_for_finish(self, table_id: int) -> Optional[_SessionSlot]:
        async with self._lock:
            return self._sessions.get(int(table_id))

    @staticmethod
    def _is_seed_safe(routed: RoutedEvent) -> bool:
        if routed.command == "lobby.dummy" or routed.is_game:
            return False
        if routed.command:
            return routed.command.startswith(("lobby.", "login.", "user."))
        # SmartFox login is a low-level a=12 packet with no extension command.
        # Retain only a proven identity/config fact; arbitrary Centrifugo traffic
        # must not flood the bounded seed history.
        return bool(
            (
                routed.data.get("userName")
                and "sessionId" in routed.data
                and routed.data.get("userId") is not None
            )
            or b"GameRoomProperties" in routed.decoded_body
        )

    def _matching_intent(self, routed: RoutedEvent, table_id: int) -> tuple[int, int]:
        requested_table = 0
        requested_config = int(self._config_by_table.get(int(table_id), 0))
        # Newest matching intent wins; consume only that one.
        selected = -1
        for index in range(len(self._join_intents) - 1, -1, -1):
            tid, config, _created = self._join_intents[index]
            if tid and tid != table_id:
                continue
            if requested_config and config and requested_config != config:
                continue
            if routed.config_id and config and routed.config_id != config:
                continue
            selected = index
            requested_table, requested_config = tid, config
            break
        if selected >= 0:
            del self._join_intents[selected]
        return requested_table or table_id, requested_config or routed.config_id

    async def _ensure_slot_locked(self, table_id: int, routed: RoutedEvent) -> _SessionSlot:
        table_id = int(table_id)
        slot = self._sessions.get(table_id)
        if slot is not None:
            return slot
        self._counter += 1
        requested_table, requested_config = self._matching_intent(routed, table_id)
        slot = _SessionSlot(
            table_id=table_id,
            created_order=self._counter,
            buffer=collections.deque(maxlen=self._session_buffer_limit),
        )
        self._sessions[table_id] = slot
        provisional = self._provisional.pop(table_id, None) or {}
        timeout_task = self._provisional_tasks.pop(table_id, None)
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
        combined = [
            *([self._sticky_identity_event] if self._sticky_identity_event else []),
            *self._sticky_context_events.values(),
            *self._history,
        ]
        history_rows: list[dict[str, Any]] = []
        seen_seed: set[str] = set()
        for row in combined:
            key = str(row.get("id") or "") or hashlib.sha256(
                str(row).encode("utf-8", "replace")
            ).hexdigest()
            if key in seen_seed:
                continue
            seen_seed.add(key)
            history_rows.append(row)
        history = tuple(history_rows)
        seed = RouterSeed(history, requested_table, requested_config)
        slot.seed = seed
        slot.start_task = asyncio.create_task(
            self._start_session(slot, seed),
            name=f"start-table-{self.device_id}-{table_id}",
        )
        self._sink(
            RouterObservation(
                "table_open", self.device_id, table_id, status="yellow",
                reason="backend session starting",
                game_type=provisional.get("game_type"),
                coin_bb=provisional.get("coin_bb"),
                pending=True,
            )
        )
        return slot

    @staticmethod
    def _coin_table_meta(routed: RoutedEvent, table_id: int) -> tuple[Optional[str], Optional[float]]:
        props: dict[str, Any] = {}
        data = routed.data
        for row in data.get("tablesToJoin") or ():
            if not isinstance(row, dict):
                continue
            name = str(row.get("tableName") or "")
            if not re.search(rf"\b{int(table_id)}\s*$", name):
                continue
            candidate = row.get("roomProperties")
            if isinstance(candidate, dict):
                props = candidate
            break
        if not props:
            props = data
        try:
            mini = int(props.get("miniGameTypeId") or props.get("minigamesId") or data.get("miniGameType") or 0)
        except (TypeError, ValueError):
            mini = 0
        game_type = {1: "NLH", 2: "PLO", 17: "PLO5", 20: "PLO6"}.get(mini)
        try:
            raw_bb = props.get("bigBlind", data.get("bigblind"))
            coin_bb = float(raw_bb) if raw_bb is not None else None
        except (TypeError, ValueError):
            coin_bb = None
        return game_type, coin_bb

    def _mark_provisional_locked(self, table_id: int, routed: RoutedEvent, reason: str) -> None:
        table_id = int(table_id)
        if table_id in self._sessions:
            return
        previous = self._provisional.get(table_id)
        if previous is None and len(self._provisional) >= self._max_provisional_tables:
            oldest = min(
                self._provisional,
                key=lambda candidate: float(
                    self._provisional[candidate].get("updated") or 0.0
                ),
            )
            self._remove_provisional_locked(oldest, "pending capacity evicted")
        first = previous is None
        changed = first or previous.get("reason") != reason
        game_type, coin_bb = self._coin_table_meta(routed, table_id)
        self._provisional[table_id] = {
            "room_id": routed.room_id,
            "config_id": routed.config_id,
            "updated": time.monotonic(),
            "reason": reason,
            "game_type": game_type or (previous or {}).get("game_type"),
            "coin_bb": coin_bb if coin_bb is not None else (previous or {}).get("coin_bb"),
        }
        old = self._provisional_tasks.pop(table_id, None)
        if old and not old.done():
            old.cancel()
        self._provisional_tasks[table_id] = asyncio.create_task(
            self._expire_provisional(table_id),
            name=f"pending-timeout-{self.device_id}-{table_id}",
        )
        if changed:
            self._sink(
                RouterObservation(
                    "table_pending", self.device_id, table_id, status="yellow",
                    reason=reason,
                    game_type=self._provisional[table_id].get("game_type"),
                    coin_bb=self._provisional[table_id].get("coin_bb"),
                    pending=True,
                    detail={"provisional": True, "account_leased": False},
                )
            )

    async def _expire_provisional(self, table_id: int) -> None:
        try:
            await asyncio.sleep(self._provisional_timeout)
            async with self._lock:
                row = self._provisional.pop(int(table_id), None)
                self._provisional_tasks.pop(int(table_id), None)
            if row is not None:
                self._sink(
                    RouterObservation(
                        "table_close", self.device_id, int(table_id), status="yellow",
                        reason="pending/waitlist timeout",
                        detail={"provisional": True, "account_leased": False},
                    )
                )
        except asyncio.CancelledError:
            pass

    def _remove_provisional_locked(self, table_id: int, reason: str) -> None:
        table_id = int(table_id)
        row = self._provisional.pop(table_id, None)
        task = self._provisional_tasks.pop(table_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if row is not None:
            self._sink(
                RouterObservation(
                    "table_close", self.device_id, table_id, status="green",
                    reason=reason,
                    detail={"provisional": True, "account_leased": False},
                )
            )

    def _orphan_queue_locked(
        self, room_id: int
    ) -> collections.deque[dict[str, Any]]:
        """Return a bounded pre-admission queue without allowing room-id floods."""

        room_id = int(room_id)
        queue = self._orphans.get(room_id)
        if queue is not None:
            return queue
        while len(self._orphans) >= self._max_orphan_rooms:
            oldest_room = next(iter(self._orphans))
            self._orphans.pop(oldest_room, None)
        queue = collections.deque(maxlen=self._orphan_limit)
        self._orphans[room_id] = queue
        return queue

    async def _start_session(self, slot: _SessionSlot, seed: RouterSeed) -> None:
        for attempt in range(1, self._startup_max_attempts + 1):
            slot.startup_attempts = attempt
            try:
                session = await asyncio.wait_for(
                    self.session_factory.create(self.device_id, slot.table_id, seed),
                    timeout=self._startup_attempt_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                async with self._lock:
                    active = (
                        not self._closed
                        and self._sessions.get(slot.table_id) is slot
                    )
                    if active:
                        slot.startup_error = error
                        slot.failed = attempt >= self._startup_max_attempts
                if not active:
                    return
                if attempt >= self._startup_max_attempts:
                    self._sink(
                        RouterObservation(
                            "table_start_failed", self.device_id, slot.table_id, status="red",
                            reason=(
                                "backend session startup exhausted "
                                f"after {attempt} attempts: {error}"
                            ),
                            detail={
                                "startup_attempt": attempt,
                                "startup_attempt_limit": self._startup_max_attempts,
                                "startup_exhausted": True,
                            },
                        )
                    )
                    # A failed startup is not an active table slot. Keeping it in
                    # _sessions creates a fake capacity limit and a permanent
                    # "open" row in the console.
                    await self.close_table(
                        slot.table_id,
                        crashed=True,
                        reason=f"backend startup failed: {error}",
                    )
                    return
                delay = min(
                    self._startup_backoff_max,
                    self._startup_backoff_base * (2 ** (attempt - 1)),
                )
                self._sink(
                    RouterObservation(
                        "warning", self.device_id, slot.table_id, status="yellow",
                        reason=(
                            f"backend session startup attempt {attempt}/"
                            f"{self._startup_max_attempts} failed; retry in {delay:.3f}s: {error}"
                        ),
                        pending=True,
                        detail={
                            "startup_attempt": attempt,
                            "startup_attempt_limit": self._startup_max_attempts,
                            "startup_retry_seconds": delay,
                        },
                    )
                )
                if delay:
                    await asyncio.sleep(delay)
                continue

            async with self._lock:
                if self._closed or self._sessions.get(slot.table_id) is not slot:
                    stale = True
                    replay: list[dict[str, Any]] = []
                else:
                    stale = False
                    slot.session = session
                    slot.failed = False
                    slot.startup_error = ""
                    replay = list(slot.buffer)
                    slot.buffer.clear()
            if stale:
                await session.close(reason="stale-start")
                return
            if isinstance(session, LiveTableSession):
                async def recovery_exhausted(
                    reason: str,
                    *,
                    table_id: int = slot.table_id,
                    expected: LiveTableSession = session,
                ) -> None:
                    async with self._lock:
                        current = self._sessions.get(table_id)
                        owns_slot = current is slot and current.session is expected
                    if owns_slot:
                        await self.close_table(
                            table_id, crashed=True, reason=str(reason)
                        )
                    else:
                        await expected.close(crashed=True, reason=str(reason))

                # Factory installs a lease-safe fallback before returning. Once
                # the session belongs to this router, replace it with full table
                # teardown so neither the red slot nor its account lease remains.
                session._proxy.bind_bridge(
                    session._bridge,
                    recovery_exhausted_callback=recovery_exhausted,
                )
            for event in replay:
                # Buffered decisions were already answered FORWARD.  Replay only
                # state-bearing events; never claim/schedule a historical dummy.
                try:
                    routed = _decode_event(event)
                    if not routed.is_dummy:
                        await session.handle_event(event)
                except Exception as exc:
                    self._sink(
                        RouterObservation(
                            "warning", self.device_id, slot.table_id,
                            session.account_id, "yellow",
                            f"buffer replay: {type(exc).__name__}: {exc}",
                        )
                    )
            return

    def _record_ws_table_hint_locked(self, routed: RoutedEvent) -> None:
        # This is deliberately narrower than generic decoded table-id evidence.
        # In the supplied Coin capture an incoming GameRoomProperties frame is a
        # transport-level acknowledgement for exactly one joined table. Global
        # room-set snapshots contain many ids and therefore never enter here.
        if (
            routed.command
            or routed.direction != "in"
            or not routed.websocket_id
            or len(routed.table_ids) != 1
            or b"GameRoomProperties" not in routed.decoded_body
        ):
            return
        table_id = int(routed.table_ids[0])
        now = time.monotonic()
        previous = self._ws_table_hints.get(routed.websocket_id)
        if (
            previous is not None
            and now - float(previous[1]) <= self._ws_table_hint_timeout
            and int(previous[0]) not in {0, table_id}
        ):
            # One websocket can multiplex rooms. Two different fresh hints mean
            # there is no safe table owner until an explicit room/table packet.
            self._ws_table_hints[routed.websocket_id] = (0, now)
            return
        self._ws_table_hints[routed.websocket_id] = (table_id, now)

    def _recent_ws_table_hint_locked(self, routed: RoutedEvent) -> int:
        if not routed.websocket_id:
            return 0
        row = self._ws_table_hints.get(routed.websocket_id)
        if row is None:
            return 0
        table_id, observed = int(row[0]), float(row[1])
        if time.monotonic() - observed > self._ws_table_hint_timeout:
            self._ws_table_hints.pop(routed.websocket_id, None)
            return 0
        return table_id if table_id > 0 else 0

    def _late_owner_locked(self, routed: RoutedEvent) -> tuple[int, str]:
        # These frames are table-state-bearing and are safe to queue once owner
        # identity is recovered. Only game_init/pre_hand/user_turn/full snapshot
        # are admission edges below; e.g. hole_cards can bind+buffer without
        # prematurely leasing a backend account.
        recovery_commands = {
            "game.game_init",
            "game.wait_list_data",
            "game.game_alldata",
            "game.pre_hand_start_info",
            "game.user_turn",
            "game.hole_cards",
            "game.player_info",
            "game.seatInfo",
            "game.seat",
            "game.take_Seat",
            "game.potInfo",
            "game.dealer_cards",
        }
        if (
            not routed.is_game
            or routed.room_id is None
            or routed.command not in recovery_commands
        ):
            return 0, ""

        # Normal join path already knows the concrete table from
        # lobby.join_game_table. If there is exactly one still-unadmitted table,
        # a state-bearing game room can only belong to it. Never guess when two
        # provisional tables exist.
        candidates = [
            int(table_id)
            for table_id in self._provisional
            if int(table_id) not in self._closing_tables
            and int(table_id) not in self._sessions
        ]
        if len(candidates) == 1:
            auto = self.automation
            # join_game is in flight: the next game packet may belong to the
            # table that has not been allocated yet. Binding it to the older
            # provisional splits one Coin table into two trainer sessions.
            if auto is not None and bool(getattr(auto, "_joining", False)):
                return 0, ""
            return candidates[0], "single provisional table"

        # Full trainer restart may have lost the lobby allocation entirely. The
        # low-level GameRoomProperties acknowledgement survives on the Coin wire
        # and gives one short-lived exact table identity for this websocket.
        hinted = self._recent_ws_table_hint_locked(routed)
        if hinted and hinted not in self._closing_tables:
            # One hint is evidence for one room join. Consume it as soon as it
            # supplies ownership so it cannot bleed into a later room on the same
            # multiplexed websocket.
            self._ws_table_hints.pop(routed.websocket_id, None)
            return hinted, "recent GameRoomProperties table hint"
        if routed.websocket_id and routed.websocket_id not in self._ws_to_tables:
            auto = self.automation
            if auto is not None and bool(getattr(auto, "_joining", False)):
                return 0, ""
            unadmitted = [
                int(table_id)
                for table_id in self._provisional
                if int(table_id) not in self._closing_tables
                and int(table_id) not in self._sessions
            ]
            if len(unadmitted) == 1:
                return unadmitted[0], "new game websocket for one unadmitted table"
        return 0, ""

    def _bind_locked(self, routed: RoutedEvent, table_id: int) -> None:
        if routed.room_id is not None and routed.is_game:
            self._room_to_table[int(routed.room_id)] = int(table_id)
        if routed.websocket_id and routed.is_game:
            self._ws_to_tables.setdefault(routed.websocket_id, set()).add(int(table_id))
            hint = self._ws_table_hints.get(routed.websocket_id)
            if hint is not None and int(hint[0]) == int(table_id):
                self._ws_table_hints.pop(routed.websocket_id, None)

    def _owner_locked(self, routed: RoutedEvent) -> int:
        # A bound SmartFox room is stronger than a table id embedded in a global
        # splash/advertisement packet.  The live capture contains exactly that case.
        if routed.room_id is not None:
            table = self._room_to_table.get(int(routed.room_id), 0)
            if table:
                return table
        # dealer_cards is board/splash — never claim owner from its table_ids.
        binding_commands = {
            "game.game_init",
            "game.wait_list_data",
            "game.game_alldata",
            "game.pre_hand_start_info",
            "game.user_turn",
            "game.hole_cards",
            "game.player_info",
            "game.take_Seat",
            "game.seatInfo",
            "game.seat",
            "game.dealer_chat_action",
        }
        if len(routed.table_ids) == 1 and routed.command in binding_commands:
            return routed.table_ids[0]
        late_table, late_reason = self._late_owner_locked(routed)
        if late_table:
            self._sink(
                RouterObservation(
                    "warning", self.device_id, late_table, status="yellow",
                    reason=f"late table owner recovered: {late_reason}",
                    pending=late_table not in self._sessions,
                    detail={
                        "room_id": routed.room_id,
                        "command": routed.command,
                        "websocket_id": routed.websocket_id,
                        "late_owner_recovery": True,
                    },
                )
            )
            return late_table
        # Never use websocket fallback for an explicitly scoped but not-yet-bound
        # room.  The live 3-table capture shows two rooms sharing one ws; guessing
        # here would contaminate the older session before wait_list_data arrives.
        if routed.is_game and routed.room_id is None and routed.websocket_id:
            owners = self._ws_to_tables.get(routed.websocket_id, set())
            if len(owners) == 1:
                return next(iter(owners))
        return 0

    @property
    def unsupported_table_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._unsupported_tables))

    async def _watchdog_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(2.0)
                await self._watchdog_tick()
        except asyncio.CancelledError:
            return

    async def _watchdog_tick(self) -> None:
        dead_closes: list[tuple[int, str]] = []
        async with self._lock:
            for table_id, slot in list(self._sessions.items()):
                session = slot.session
                if not isinstance(session, LiveTableSession):
                    continue
                try:
                    dead = session.take_dead_table_request()
                except Exception:
                    dead = None
                if dead:
                    dead_closes.append(
                        (int(table_id), str(dead.get("reason") or "dead table 120s"))
                    )
        for table_id, reason in dead_closes:
            await self.close_table(int(table_id), crashed=True, reason=reason)
        auto = self.automation
        if auto is None:
            return
        rows: list[dict[str, Any]] = []
        async with self._lock:
            live = [int(table_id) for table_id in self._sessions]
            for table_id, slot in list(self._sessions.items()):
                session = slot.session
                if not isinstance(session, LiveTableSession):
                    continue
                bridge = session._bridge
                try:
                    snap = session._snapshot()
                    coin_bb = float(snap.coin_bb or 0.0)
                    seats = getattr(bridge, "seat_map", {}) or {}
                    players = len([
                        row for row in seats.values()
                        if isinstance(row, dict) and (row.get("userId") or row.get("userName"))
                    ])
                    try:
                        hero_id = int(bridge.state.get("user_id") or 0)
                    except (TypeError, ValueError):
                        hero_id = 0
                    hero_name = str(bridge.state.get("user_name") or "")
                    try:
                        hero_seat = int(bridge.state.get("hero_seat") or 0)
                    except (TypeError, ValueError):
                        hero_seat = 0
                    hero_row = None
                    for row in seats.values():
                        if not isinstance(row, dict):
                            continue
                        try:
                            uid = int(row.get("userId") or 0)
                        except (TypeError, ValueError):
                            uid = 0
                        try:
                            seat = int(row.get("seatId") or 0)
                        except (TypeError, ValueError):
                            seat = 0
                        if (
                            (hero_id and uid == hero_id)
                            or (hero_name and str(row.get("userName") or "") == hero_name)
                            or (hero_seat and seat == hero_seat)
                        ):
                            hero_row = row
                            break
                    stack_bb: Optional[float] = None
                    stack_known = False
                    if hero_row is not None and coin_bb > 0:
                        raw_chips = hero_row.get("userChips")
                        if raw_chips in (None, "", 0, 0.0):
                            raw_chips = hero_row.get("buyinAmount")
                        parsed = _finite_nonnegative(raw_chips)
                        if parsed is not None:
                            stack_bb = float(parsed) / coin_bb
                            stack_known = parsed > 0
                    sitout = hero_sitout_for_watchdog(bridge)
                    rows.append({
                        "table_id": int(table_id),
                        "stack_bb": stack_bb,
                        "stack_known": stack_known,
                        "players": players,
                        "seated": bool(getattr(bridge, "hero_sitting", False)),
                        "sitout": sitout,
                        "hand": bool(getattr(bridge, "current_hand", None)),
                        "bb": coin_bb,
                        "streak": int(getattr(bridge, "cc_miss_streak", 0) or 0),
                        "failsafe_standup": bool(getattr(bridge, "cc_failsafe_standup", False)),
                        "room": int(bridge.context_hook_room or bridge.active_hook_room or 0),
                        "ws": str(auto.game_ws.get(int(table_id)) or self.game_ws_for(int(table_id))),
                    })
                except Exception:
                    continue
        for row in rows:
            auto.mark_stack(
                row["table_id"], row["stack_bb"], row["players"], row["seated"],
                stack_known=bool(row["stack_known"]),
            )
            auto.mark_sitout(row["table_id"], row["sitout"])
            auto.mark_hand(row["table_id"], row["hand"])
            if row["bb"]:
                auto.mark_bb(row["table_id"], row["bb"])
        play = True
        if auto is not None:
            play = bool(getattr(auto.policy, "play_enabled", True))
        async with self._lock:
            for slot in self._sessions.values():
                session = getattr(slot, "session", None)
                if isinstance(session, LiveTableSession):
                    session.play_enabled = play
                    try:
                        session._bridge.play_enabled = play
                    except Exception:
                        pass
        auto.tick(seated_tables=len(rows), live_table_ids=live)
        for table_id in list(getattr(auto, "_leave_reasons", {}) or {}):
            auto.retract_false_leave(int(table_id))
        leaves = auto.drain_leaves()
        extra: list[tuple[int, str]] = []
        standup_ids: set[int] = set()
        for row in rows:
            if not row.get("seated"):
                continue
            if int(row.get("streak") or 0) > 3 or row.get("failsafe_standup"):
                standup_ids.add(int(row["table_id"]))
                extra.append((
                    int(row["table_id"]),
                    "PokerEYE silent 3 hero turns; standup after failsafe",
                ))
        leave_ids = {int(tid) for tid, _reason in list(leaves) + extra}
        for row in rows:
            if not row.get("seated") or not row.get("sitout"):
                continue
            tid = int(row["table_id"])
            if tid in standup_ids or tid in leave_ids:
                continue
            auto.request_sit_in(
                tid, room=int(row.get("room") or 0), ws_id=str(row.get("ws") or ""),
            )
        async with self._lock:
            for table_id in list(self._unsupported_exit):
                self._abort_false_policy_exit_locked(int(table_id))
            for table_id, reason in list(leaves) + extra:
                if int(table_id) not in self._sessions:
                    # Unrecognized Coin tabs still occupy the 5-slot cap. Do not
                    # pretend they closed — that used to refill into a 6th join.
                    if not auto._leaving_all:
                        auto.note_table_closed(int(table_id), "already-gone")
                    continue
                match = next((row for row in rows if int(row["table_id"]) == int(table_id)), None)
                room = int((match or {}).get("room") or 0)
                ws_id = str((match or {}).get("ws") or "")
                if auto._leaving_all or str(reason).startswith("operator leave-all"):
                    kind = "leave_all"
                elif "silent 3" in reason:
                    kind = "cc_streak"
                else:
                    kind = "policy"
                self._queue_policy_leave_locked(
                    int(table_id), room=room, ws_id=ws_id, kind=kind, reason=reason,
                )
        releases: list[int] = []
        async with self._lock:
            now = time.monotonic()
            for tid, state in list(self._unsupported_exit.items()):
                if not state.get("wait_ui"):
                    continue
                ui = getattr(auto, "_ui_last", {}) or {}
                # A live-table dump (closed=false) is not "tab gone". That invert
                # ripped the Eye session while Coin still showed the felt, then
                # auto sat us back. Only a confirmed UI leave closes the row.
                if self.ui_leave_confirmed(ui):
                    waiting = sorted(
                        (float(row.get("wait_ui_until") or 0), int(item))
                        for item, row in self._unsupported_exit.items()
                        if row.get("wait_ui")
                    )
                    if waiting:
                        releases.append(waiting[0][1])
                    break
                until = float(state.get("wait_ui_until") or 0.0)
                if until and now > until:
                    state["wait_ui"] = False
                    state["wait_ui_until"] = now + 90.0
                    # Manual: do not re-arm a device-global LEAVE HUD.
                    # That hamburged a sibling live ring.
        for tid in releases:
            await self.close_table(int(tid), reason="UI leave confirmed")

    def _checkfold_raw_locked(self, table_id: int, room: int) -> tuple[str, bytes]:
        """Backend CHECK if this table listed it, else FOLD. Always a packet, never GUI."""
        opts: dict[str, Any] = {}
        slot = self._sessions.get(int(table_id))
        session = getattr(slot, "session", None) if slot is not None else None
        if isinstance(session, LiveTableSession):
            turns = getattr(session._bridge.autoplay, "turn_by_room", {}) or {}
            turn = turns.get(int(room)) or {}
            if isinstance(turn, dict):
                opts = turn.get("userTurnOptions") or {}
        if str(3) in opts or 3 in opts:
            return "CHECK", build_game_user_action_packet(int(room), 3, 0.0)
        return "FOLD", build_game_user_action_packet(int(room), 7, 0.0)

    def _arm_db_checkfold_locked(self, table_id: int) -> None:
        """Put CHECK/FOLD extra_urgent on the live bridge so the next dummy sends."""
        slot = self._sessions.get(int(table_id))
        session = getattr(slot, "session", None) if slot is not None else None
        if not isinstance(session, LiveTableSession):
            return
        bridge = session._bridge
        room = 0
        try:
            room = int(bridge.context_hook_room or bridge.active_hook_room or 0)
        except (TypeError, ValueError):
            room = 0
        if room <= 0:
            try:
                room = int((self._unsupported_exit.get(int(table_id)) or {}).get("room") or 0)
            except (TypeError, ValueError):
                room = 0
        if room <= 0:
            return
        name, raw = self._checkfold_raw_locked(int(table_id), room)
        now = time.monotonic()
        turn = {}
        try:
            turn = (bridge.autoplay.turn_by_room.get(int(room)) or {}) if room else {}
        except Exception:
            turn = {}
        if not isinstance(turn, dict):
            turn = {}
        with bridge.autoplay.lock:
            bridge.autoplay.pending = {
                "due": now,
                "raw": raw,
                "room": int(room),
                "ws_id": turn.get("_ws_id") or self._ws_for_table_locked(int(table_id)),
                "url": turn.get("_url"),
                "channel_id": turn.get("_channel_id"),
                "action": name,
                "delay_ms": 0,
                "fallback": True,
                "extra_urgent": True,
                "_arbiter_ready_at": now,
                "hand_id": str(bridge.state.get("hand_id") or ""),
                "turn_id": str(turn.get("_turn_id") or ""),
            }

    def _unstick_db_checkfold_locked(self, table_id: int) -> None:
        state = self._unsupported_exit.get(int(table_id))
        if not state:
            return
        state["next_at"] = 0.0
        inflight = state.get("inflight") or {}
        if not inflight:
            return
        token, stage = next(iter(inflight.items()))
        name = str(stage.get("stage") or "")
        if name not in {"CHECK", "FOLD", "CHECKFOLD"}:
            return
        inflight.pop(token, None)
        state["awaiting_coin"] = False
        queue = state.setdefault("queue", collections.deque())
        queue.appendleft(dict(stage))

    def _release_stale_double_board_locked(self, table_id: int) -> bool:
        """A new ordinary hand must not inherit Warning: DB from the previous one."""
        table_id = int(table_id)
        marked = table_id in self._unsupported_tables
        state = self._unsupported_exit.get(table_id)
        kind = str((state or {}).get("kind") or "")
        if kind and kind != "unsupported":
            return False
        room = 0
        try:
            room = int((state or {}).get("room") or 0)
        except (TypeError, ValueError):
            room = 0
        if room:
            self._unsupported_room_reasons.pop(room, None)
        for rid, owner in list(self._room_to_table.items()):
            if int(owner) == table_id:
                self._unsupported_room_reasons.pop(int(rid), None)
        if not marked and not state:
            return False
        self._unsupported_tables.pop(table_id, None)
        self._unsupported_exit.pop(table_id, None)
        slot = self._sessions.get(table_id)
        session = getattr(slot, "session", None) if slot is not None else None
        bridge = getattr(session, "_bridge", None) if session is not None else None
        if bridge is not None:
            try:
                bridge.hero_departing = False
            except Exception:
                pass
            try:
                pending = getattr(getattr(bridge, "autoplay", None), "pending", None)
                if (
                    isinstance(pending, dict)
                    and pending.get("fallback")
                    and str(pending.get("action") or "").upper() in {"CHECK", "FOLD", "CHECKFOLD"}
                ):
                    bridge.autoplay.pending = None
            except Exception:
                pass
        task = self._unsupported_close_tasks.pop(table_id, None)
        if task is not None:
            task.cancel()
        self._sink(
            RouterObservation(
                "unsupported_table",
                self.device_id,
                table_id,
                status="green",
                reason="DOUBLE BOARD mark dropped; current hand has no second board",
                detail={
                    "unsupported": "",
                    "warning": "",
                    "hud": {"text": "", "clear": True, "sticky": False, "leave": False},
                },
            )
        )
        return True

    def game_ws_for(self, table_id: int) -> str:
        owners = [ws for ws, tables in self._ws_to_tables.items() if int(table_id) in tables]
        return owners[0] if len(owners) == 1 else ""

    def _ws_for_table_locked(self, table_id: int) -> str:
        unique = self.game_ws_for(int(table_id))
        if unique:
            return unique
        for websocket, owners in self._ws_to_tables.items():
            if int(table_id) in owners:
                return str(websocket)
        auto = self.automation
        if auto is not None:
            return str(auto.game_ws.get(int(table_id)) or "")
        return ""

    def _definite_table_locked(self, routed: RoutedEvent) -> int:
        """Destructive identity: bound SmartFox room, or this packet's only table id.

        Never guess from a multiplexed websocket. That is how DOUBLE BOARD
        evidence and a manual quit of an unfocused tab closed the live table.
        """
        if routed.room_id is not None:
            bound = int(self._room_to_table.get(int(routed.room_id), 0) or 0)
            if bound > 0:
                return bound
        if len(routed.table_ids) != 1:
            return 0
        table_id = int(routed.table_ids[0])
        if table_id <= 0:
            return 0
        if table_id not in self._sessions and table_id not in self._provisional:
            return 0
        if routed.room_id is not None:
            bound = int(self._room_to_table.get(int(routed.room_id), 0) or 0)
            if bound and bound != table_id:
                return 0
        return table_id

    @staticmethod
    def ui_leave_confirmed(ui: Any) -> bool:
        """True only after the janitor taps Coin's leave confirm."""
        return str((ui or {}).get("tap") or "") == "confirm-exit-table"

    def _queue_policy_leave_locked(
        self, table_id: int, *, room: int, ws_id: str, kind: str, reason: str,
    ) -> bool:
        table_id = int(table_id)
        if table_id in self._recent_closed and self._recent_closed[table_id] > time.monotonic():
            return False
        if table_id in self._unsupported_exit:
            return False
        if int(room) <= 0:
            room = next(
                (int(rid) for rid, owner in self._room_to_table.items() if int(owner) == table_id),
                0,
            )
        bound = int(self._room_to_table.get(int(room), 0) or 0) if int(room) else 0
        if bound and bound != table_id:
            return False
        if int(room) <= 0:
            return False
        self._recent_closed[table_id] = time.monotonic() + 120.0
        event = {"id": f"auto-leave-{table_id}", "ws_id": ws_id}
        routed = RoutedEvent(
            event=event, payload=None, raw=b"", command="game.leave_Seat",
            direction="out", room_id=int(room) or None, table_ids=(int(table_id),),
            websocket_id=str(ws_id or ""), data={},
        )
        before = int(table_id) in self._unsupported_exit
        self._ensure_unsupported_exit_locked(
            int(table_id), routed, reset=True, exit_kind=str(kind or "policy"),
            detail={"reason": reason},
        )
        if int(table_id) not in self._unsupported_exit:
            return False
        if str(kind) != "leave_all":
            self._action_arbiter.cancel_table(int(table_id), "POLICY_LEAVE")
        if table_id not in self._unsupported_close_tasks:
            self._unsupported_close_tasks[int(table_id)] = asyncio.create_task(
                self._close_unsupported_after(int(table_id)),
                name=f"policy-close-{self.device_id}-{table_id}",
            )
        if not before:
            self._sink(
                RouterObservation(
                    "automation_leave", self.device_id, int(table_id), status="yellow",
                    reason=str(reason or "policy leave"),
                    detail={"kind": kind, "reason": reason},
                )
            )
        return True

    def _ensure_unsupported_exit_locked(
        self, table_id: int, routed: RoutedEvent, *, reset: bool = False,
        exit_kind: str = "unsupported", detail: Optional[dict[str, Any]] = None,
    ) -> None:
        table_id = int(table_id)
        room = int(routed.room_id) if routed.room_id is not None else 0
        state = self._unsupported_exit.get(table_id)
        if state is not None and str(state.get("kind") or "unsupported") != str(exit_kind):
            current = str(state.get("kind") or "unsupported")
            incoming = str(exit_kind or "unsupported")
            # DOUBLE BOARD failsafe outranks a policy standup. Otherwise
            # CHECK/FOLD never starts because leave_Seat is already queued.
            if incoming == "unsupported" and current != "unsupported":
                self._unsupported_exit.pop(table_id, None)
                state = None
            elif state.get("queue") or state.get("inflight"):
                return
            else:
                self._unsupported_exit.pop(table_id, None)
                state = None
        if state is not None:
            if routed.websocket_id and routed.is_game:
                owners = self._ws_to_tables.get(routed.websocket_id, set())
                if int(table_id) in owners or not owners:
                    state["ws_id"] = routed.websocket_id
            if room:
                state["room"] = room
            if not reset:
                return
            # A fresh game_init after the previous forced exit is a re-entry
            # attempt. Start a new best-effort FOLD/STANDUP/LEAVE sequence.
            if state.get("queue") or state.get("inflight"):
                return

        if room <= 0:
            return
        self._forced_sequence += 1
        # Server quit_table is forbidden. CHECK/FOLD, then leave_Seat.
        # The Coin tab is closed only by the APK janitor (hamburger → Leave).
        # leave-all: play the hand (CC stays), then standup, then UI close,
        # then the next table. Do not CHECK/FOLD-steal that dummy.
        in_hand = True
        recent = bool((detail or {}).get("recent"))
        if str(exit_kind) == "unsupported":
            slot = self._sessions.get(table_id)
            session = getattr(slot, "session", None) if slot is not None else None
            bridge = getattr(session, "_bridge", None) if session is not None else None
            if recent:
                in_hand = False
            elif bridge is not None:
                sitting = bool(getattr(bridge, "hero_sitting", False))
                hand = getattr(bridge, "current_hand", None) or getattr(bridge, "context_hand", None)
                in_hand = sitting and bool(hand)
        if str(exit_kind) == "leave_all" or (str(exit_kind) == "unsupported" and not in_hand):
            stages = collections.deque([
                {
                    "stage": "STANDUP",
                    "raw": build_game_leave_seat_packet(room),
                    "attempt": 1,
                },
            ])
        else:
            stages = collections.deque([
                {
                    "stage": "CHECKFOLD",
                    "attempt": 1,
                },
                {
                    "stage": "STANDUP",
                    "raw": build_game_leave_seat_packet(room),
                    "attempt": 1,
                },
            ])
        self._unsupported_exit[table_id] = {
            "room": room,
            "ws_id": routed.websocket_id,
            "queue": stages,
            "inflight": {},
            "generation": self._forced_sequence,
            "next_at": 0.0,
            "kind": str(exit_kind or "unsupported"),
            "detail": dict(detail or {}),
        }
        slot = self._sessions.get(table_id)
        session = getattr(slot, "session", None) if slot is not None else None
        if isinstance(session, LiveTableSession) and str(exit_kind) != "leave_all":
            bridge = session._bridge
            try:
                now = time.monotonic()
                action = ""
                with bridge.autoplay.lock:
                    pending = bridge.autoplay.pending
                    action = str((pending or {}).get("action") or "").upper()
                    if pending and action in {"CHECK", "FOLD"}:
                        pending["due"] = now
                        pending["delay_ms"] = 0
                        pending["extra_urgent"] = True
                        pending["fallback"] = True
                        pending["_arbiter_ready_at"] = now
                    else:
                        bridge.autoplay.pending = None
                if action not in {"CHECK", "FOLD"}:
                    bridge.pending_action_ack = None
                bridge.hero_departing = True
            except Exception:
                pass

    def _mark_unsupported_locked(
        self, table_id: int, routed: RoutedEvent, reason: str
    ) -> None:
        table_id = int(table_id)
        first = table_id not in self._unsupported_tables
        if not first:
            # Do not restart CHECK/FOLD → standup on a table already in the
            # double-board exit. One sequence per table.
            self._bind_locked(routed, table_id)
            return
        self._unsupported_tables.setdefault(table_id, str(reason or "DOUBLE BOARD"))
        if routed.room_id is not None:
            self._unsupported_room_reasons[int(routed.room_id)] = str(
                reason or "DOUBLE BOARD"
            )
        self._bind_locked(routed, table_id)
        self._ensure_unsupported_exit_locked(
            table_id,
            routed,
            reset=(routed.command == "game.game_init"),
        )
        self._action_arbiter.cancel_table(table_id, "UNSUPPORTED_DOUBLE_BOARD")
        if self.automation is not None:
            self.automation._join_block_until = max(
                float(getattr(self.automation, "_join_block_until", 0.0) or 0.0),
                time.monotonic() + 45.0,
            )
            self.automation._status = "db-exit"
        if first:
            try:
                db_recent_store().remember(table_id, device_id=self.device_id, reason=str(reason or "DOUBLE BOARD"))
            except Exception:
                pass
            self._sink(
                RouterObservation(
                    "unsupported_table",
                    self.device_id,
                    table_id,
                    status="red",
                    reason="DOUBLE BOARD",
                    detail={
                        "unsupported": "DOUBLE_BOARD",
                        "warning": "DB",
                        "blacklisted_for_session": True,
                        "evidence": str(reason or "DOUBLE BOARD")[:240],
                        "hud": {
                            "text": "Warning: DB",
                            "sticky": True,
                            "tone": "red",
                            "leave": False,
                        },
                    },
                )
            )
        if table_id in self._sessions and table_id not in self._unsupported_close_tasks:
            self._unsupported_close_tasks[table_id] = asyncio.create_task(
                self._close_unsupported_after(table_id),
                name=f"unsupported-close-{self.device_id}-{table_id}",
            )

    async def _close_unsupported_after(self, table_id: int) -> None:
        try:
            # Give the native FOLD/STANDUP/LEAVE sequence time to receive local
            # send ACKs and the Coin quit ACK.  The account lease is still bounded.
            await asyncio.sleep(0.8)
            if int(table_id) in self._sessions:
                state = self._unsupported_exit.get(int(table_id)) or {}
                kind = str(state.get("kind") or "unsupported")
                # Policy/low-stack/sitout still occupy a Coin UI tab until quit ACK.
                # Closing the trainer session after 0.8s is why the console dropped
                # to 1-2 tables while Coin still showed 5 live tabs.
                if kind in {
                    "policy", "low_stack", "cc_streak", "sitout", "short_table",
                    "unsupported",
                }:
                    # Coin CHECK/FOLD + standup + quit need dummy carriers.
                    # Closing the trainer session at 0.8s dropped the failsafe.
                    return
                await self.close_table(int(table_id), reason="DOUBLE BOARD unsupported table")
        except asyncio.CancelledError:
            return
        finally:
            self._unsupported_close_tasks.pop(int(table_id), None)

    def _abort_false_policy_exit_locked(self, table_id: int) -> bool:
        """Cancel a junk/uidump standup if the table filled in."""
        table_id = int(table_id)
        state = self._unsupported_exit.get(table_id)
        if not state:
            return False
        kind = str(state.get("kind") or "")
        if kind not in {"policy", "short_table"}:
            return False
        if state.get("wait_ui"):
            return False
        inflight = state.get("inflight") or {}
        if any(str(row.get("stage") or "") == "LEAVE" for row in inflight.values()):
            return False
        auto = self.automation
        wanted = True
        if auto is not None and hasattr(auto, "retract_false_leave"):
            wanted = table_id in getattr(auto, "_leave_reasons", {})
            auto.retract_false_leave(table_id)
            wanted = table_id in getattr(auto, "_leave_reasons", {})
        if wanted:
            return False
        self._unsupported_exit.pop(table_id, None)
        slot = self._sessions.get(table_id)
        session = getattr(slot, "session", None) if slot is not None else None
        bridge = getattr(session, "_bridge", None) if session is not None else None
        if bridge is not None:
            bridge.hero_departing = False
        self._sink(
            RouterObservation(
                "automation_leave", self.device_id, table_id, status="green",
                reason="standup aborted · table is live",
                detail={"kind": kind, "aborted": True},
            )
        )
        return True

    def _exit_blocks_other_play_locked(self) -> bool:
        return False

    def _forced_exit_decision_locked(
        self, routed: RoutedEvent, table_id: int
    ) -> dict[str, Any]:
        if self._abort_false_policy_exit_locked(int(table_id)):
            return _forward(routed.event)
        state = self._unsupported_exit.get(int(table_id))
        if not state:
            # Preserve the legacy DOUBLE BOARD marker for the rare unsupported
            # packet that could not yet be bound to a room/ws.
            return _forward(
                routed.event, router_unsupported=True, router_forced_exit=False
            )
        exit_kind = str(state.get("kind") or "unsupported")

        def forced_forward() -> dict[str, Any]:
            return _forward(
                routed.event,
                router_unsupported=(exit_kind == "unsupported"),
                router_forced_exit=exit_kind,
            )

        if routed.websocket_id and routed.is_game:
            owners = self._ws_to_tables.get(routed.websocket_id, set())
            if int(table_id) in owners or not owners:
                state["ws_id"] = routed.websocket_id
        # Incoming DOUBLE BOARD evidence is not a send carrier. Wait for dummy.
        if not routed.is_dummy:
            return forced_forward()
        dummy_ws = str(routed.websocket_id or "")
        want_ws = self._ws_for_table_locked(int(table_id)) or str(state.get("ws_id") or "")
        # HMN1 v>=6 names the target websocket in the inject. Any lobby dummy
        # can carry CHECK/FOLD; requiring the game ws left the HUD burning.
        if (
            want_ws and dummy_ws and dummy_ws != want_ws
            and int(routed.event.get("v") or 0) < 6
        ):
            return forced_forward()
        now = time.monotonic()
        if state.get("inflight"):
            if state.get("awaiting_coin") and now >= float(state.get("awaiting_coin_until") or 0.0):
                # Coin did not ACK. CHECK/FOLD is often not our turn — skip to standup.
                stuck = {}
                if state.get("inflight"):
                    stuck = next(iter(state["inflight"].values()))
                name = str(stuck.get("stage") or "")
                state["inflight"] = {}
                state["awaiting_coin"] = False
                if name in {"CHECK", "FOLD", "CHECKFOLD"}:
                    retry = dict(stuck)
                    retry["attempt"] = int(stuck.get("attempt") or 1) + 1
                    if int(retry["attempt"]) <= 3:
                        queue = state.setdefault("queue", collections.deque())
                        queue.appendleft(retry)
                    state["next_at"] = 0.0
                else:
                    retry = dict(stuck)
                    retry["attempt"] = int(stuck.get("attempt") or 1) + 1
                    if int(retry["attempt"]) <= 3:
                        queue = state.setdefault("queue", collections.deque())
                        queue.appendleft(retry)
            else:
                return forced_forward()
        queue = state.get("queue")
        if not queue or now < float(state.get("next_at") or 0.0):
            return forced_forward()
        if state.get("wait_ui"):
            return forced_forward()
        ws_id = self._ws_for_table_locked(int(table_id)) or str(state.get("ws_id") or "")
        if not ws_id:
            return forced_forward()

        stage = queue.popleft()
        if str(stage.get("stage") or "") == "STANDUP" and exit_kind == "leave_all":
            slot = self._sessions.get(int(table_id))
            session = getattr(slot, "session", None) if slot is not None else None
            bridge = getattr(session, "_bridge", None) if session is not None else None
            sitting = bool(getattr(bridge, "hero_sitting", False))
            hand = getattr(bridge, "current_hand", None) or getattr(bridge, "context_hand", None)
            if sitting and hand:
                queue.appendleft(stage)
                return forced_forward()
            if bridge is not None:
                try:
                    bridge.hero_departing = True
                except Exception:
                    pass
        if str(stage.get("stage") or "") == "CHECKFOLD" or stage.get("raw") is None:
            name, raw = self._checkfold_raw_locked(int(table_id), int(state.get("room") or 0))
            stage["stage"] = name
            stage["raw"] = raw
        generation = int(state.get("generation") or 0)
        token_prefix = "lowstack" if exit_kind == "low_stack" else "unsupported"
        token = (
            f"{token_prefix}:{int(table_id)}:{generation}:"
            f"{stage['stage']}:{int(stage.get('attempt') or 1)}"
        )
        state["inflight"][token] = dict(stage)
        state["awaiting_coin"] = False
        state["next_at"] = now + 0.15
        decision = {
            "id": routed.event.get("id", ""),
            "action": "schedule_send",
            "text": False,
            "payload_b64": base64.b64encode(bytes(stage["raw"])).decode(),
            "delay_ms": 0,
            "token": token,
            "ws_id": ws_id,
            "_operator_action": {
                "table_id": int(table_id),
                "action": str(stage["stage"]),
                "amount": None,
                "attempt": int(stage.get("attempt") or 1),
                "max_attempts": 3,
                "token": token,
                "forced_exit": True,
                "forced_exit_reason": exit_kind,
            },
        }
        try:
            decision["_ws_u32"] = int(ws_id, 16)
        except (TypeError, ValueError):
            pass
        return decision

    async def handle_native_action_result(self, result: dict[str, Any]) -> bool:
        token = str(result.get("token") or "")
        if bool(result.get("ok")) and token and not token.startswith(("unsupported:", "lowstack:")):
            async with self._lock:
                slots = list(self._sessions.values())
            for slot in slots:
                session = slot.session
                if not isinstance(session, LiveTableSession):
                    continue
                ack = session._bridge.pending_action_ack
                if not ack:
                    continue
                want = str(ack.get("token") or "")
                if want and (token == want or token.startswith(want + ":retry")):
                    # Native send is "отправлено", not Coin ACK. Wait for
                    # user_action / turn-advanced before "выполнено".
                    return True
            return False
        if not token.startswith(("unsupported:", "lowstack:")):
            return False
        try:
            table_id = int(token.split(":", 3)[1])
        except (IndexError, TypeError, ValueError):
            return False
        async with self._lock:
            state = self._unsupported_exit.get(table_id)
            if not state:
                return False
            stage = (state.get("inflight") or {}).get(token)
            if not stage:
                return False
            ok = bool(result.get("ok"))
            attempt = int(stage.get("attempt") or 1)
            if not ok:
                state["inflight"].pop(token, None)
                if attempt < 3:
                    retry = dict(stage)
                    retry["attempt"] = attempt + 1
                    state["queue"].appendleft(retry)
                state["awaiting_coin"] = False
                state["next_at"] = 0.0
            else:
                # Packet left the device. Advance only on Coin ACK, not this send ACK.
                state["awaiting_coin"] = True
                state["awaiting_coin_until"] = time.monotonic() + 2.5
            exit_kind = str(state.get("kind") or "unsupported")
            self._sink(
                RouterObservation(
                    "low_stack_exit_stage" if exit_kind == "low_stack" else "unsupported_exit_stage",
                    self.device_id,
                    table_id,
                    status="green" if ok else "yellow",
                    reason=str(stage.get("stage") or "EXIT"),
                    detail={
                        "stage": str(stage.get("stage") or "EXIT"),
                        "ok": ok,
                        "attempt": attempt,
                        "max_attempts": 3,
                        "reason_code": result.get("reason_code"),
                        "forced_exit_reason": exit_kind,
                        "awaiting_coin": bool(ok),
                    },
                )
            )
            return True

    def _forced_coin_ack_locked(self, routed: RoutedEvent, table_id: int) -> str:
        """Advance CHECK/FOLD → STANDUP → LEAVE only on Coin IN ACK for that room."""
        state = self._unsupported_exit.get(int(table_id))
        if not state or routed.direction != "in":
            return ""
        inflight = state.get("inflight") or {}
        if not inflight:
            return ""
        room = int(state.get("room") or 0)
        if routed.room_id is not None and room and int(routed.room_id) != int(room):
            return ""
        token, stage = next(iter(inflight.items()))
        name = str(stage.get("stage") or "")
        cmd = str(routed.command or "")
        hit = False
        if name in {"CHECK", "FOLD", "CHECKFOLD"} and cmd == "game.user_action":
            hit = True
        elif name == "STANDUP" and cmd == "game.leave_Seat":
            hit = True
        elif name == "LEAVE" and cmd == "game.quit_table" and self._quit_succeeded(routed):
            hit = True
        if not hit:
            return ""
        inflight.pop(token, None)
        state["awaiting_coin"] = False
        state["next_at"] = 0.0
        if name == "STANDUP":
            state["wait_ui"] = False
            state["done"] = True
            state["queue"] = collections.deque()
            if str(state.get("kind") or "") == "unsupported":
                self._schedule_db_close_locked(int(table_id))
        self._sink(
            RouterObservation(
                "unsupported_exit_stage",
                self.device_id,
                int(table_id),
                status="green",
                reason=f"{name} Coin ACK",
                detail={
                    "stage": name, "ok": True, "coin_ack": True,
                    "hud": {
                        "text": "Warning: DB" if str(state.get("kind") or "") == "unsupported" else name,
                        "leave": False,
                        "sticky": str(state.get("kind") or "") == "unsupported",
                        "tone": "red" if str(state.get("kind") or "") == "unsupported" else "",
                    },
                },
            )
        )
        return name

    def _schedule_db_close_locked(self, table_id: int) -> None:
        table_id = int(table_id)
        try:
            db_recent_store().remember(table_id, device_id=self.device_id)
        except Exception:
            pass
        if table_id in self._unsupported_close_tasks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._unsupported_close_tasks[table_id] = loop.create_task(
            self.close_table(table_id, reason="DOUBLE BOARD standup"),
            name=f"db-close-{self.device_id}-{table_id}",
        )

    async def _route_dummy(self, routed: RoutedEvent) -> tuple[dict[str, Any], Optional[int]]:
        async with self._lock:
            slots = [slot for slot in self._sessions.values() if slot.session is not None]
            # Hero extra_urgent/prefold/failsafe beats STANDUP. DOUBLE BOARD
            # CHECK/FOLD is itself failsafe: a delayed Eye action on another
            # table must not leave the DB hint burning with 0 sends.
            has_play = False
            has_urgent_play = False
            now = time.monotonic()
            for slot in slots:
                session = slot.session
                if not isinstance(session, LiveTableSession):
                    continue
                for offer in session.action_offers(routed.event):
                    due = offer.bypass_gap or float(offer.ready_at) <= now + 0.05
                    if due:
                        has_play = True
                        if offer.bypass_gap:
                            has_urgent_play = True
                        break
                if has_urgent_play:
                    break
            for table_id in sorted(self._unsupported_exit):
                state = self._unsupported_exit.get(int(table_id)) or {}
                queue = state.get("queue")
                head = None
                if queue:
                    try:
                        head = queue[0]
                    except (IndexError, TypeError, KeyError):
                        head = None
                stage = str((head or {}).get("stage") or "")
                if stage in {"CHECK", "FOLD", "CHECKFOLD"}:
                    forced = self._forced_exit_decision_locked(routed, table_id)
                    if forced.get("action") != "forward":
                        return forced, None
            if not has_play:
                for table_id in sorted(self._unsupported_exit):
                    forced = self._forced_exit_decision_locked(routed, table_id)
                    if forced.get("action") != "forward":
                        return forced, None
            # Never freeze other tables. CHECK/FOLD/standup on one table must
            # not swallow dummy play on the rest of the fleet.
            legacy_candidates = [
                (claim, slot.created_order, slot)
                for slot in slots
                if not hasattr(slot.session, "action_offers")
                if (claim := slot.session.action_claim(routed.event)) is not None
            ]

        owners: dict[str, LiveTableSession] = {}
        for slot in slots:
            session = slot.session
            if not isinstance(session, LiveTableSession):
                continue
            for offer in session.action_offers(routed.event):
                self._action_arbiter.offer(offer)
                owners[offer.action_id] = session
            for action_id, reason in session.drain_arbitration_cancellations():
                self._action_arbiter.cancel(action_id, reason)

        plan = self._action_arbiter.dispatch_next(owners)
        for action_id, session in owners.items():
            record = self._action_arbiter.record(action_id)
            if record is not None and record.state == ActionState.CANCELLED:
                session.cancel_arbitration_action(action_id)
        if plan is not None:
            owner = owners.get(plan.action_id)
            if owner is None or not owner.prepare_action_dispatch(plan):
                self._action_arbiter.cancel(plan.action_id, "STALE")
                return _forward(routed.event), None
            result = await owner.handle_event(routed.event)
            if not owner.finalize_action_dispatch(plan, result[0]):
                self._action_arbiter.cancel(plan.action_id, "STALE")
            self._record_telemetry(owner.table_id, routed, result[0])
            return result

        # Compatibility path for non-production test/session factories.
        if not legacy_candidates:
            return _forward(routed.event), None
        _claim, _order, slot = min(
            legacy_candidates, key=lambda row: (row[0][0], row[0][1], row[1])
        )
        result = await slot.session.handle_event(routed.event)  # type: ignore[union-attr]
        self._record_telemetry(slot.table_id, routed, result[0])
        return result

    def _mark_closing_locked(self, table_id: int, room_id: Optional[int]) -> None:
        table_id = int(table_id)
        room = int(room_id) if room_id is not None else None
        self._closing_tables[table_id] = room
        if room is not None:
            self._closing_rooms[room] = table_id

    def _schedule_tombstone_clear_locked(self, table_id: int) -> None:
        table_id = int(table_id)
        previous = self._tombstone_tasks.pop(table_id, None)
        if previous and not previous.done():
            previous.cancel()
        self._tombstone_tasks[table_id] = asyncio.create_task(
            self._clear_tombstone_after(table_id),
            name=f"closing-tombstone-{self.device_id}-{table_id}",
        )

    async def _clear_tombstone_after(self, table_id: int) -> None:
        try:
            await asyncio.sleep(self._tombstone_seconds)
            async with self._lock:
                room = self._closing_tables.pop(int(table_id), None)
                if room is not None and self._closing_rooms.get(room) == int(table_id):
                    self._closing_rooms.pop(room, None)
                self._tombstone_tasks.pop(int(table_id), None)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _quit_succeeded(routed: RoutedEvent) -> bool:
        if routed.command != "game.quit_table" or routed.direction != "in":
            return False
        _cmd, _room, data = __import__("core.verified_v1.coin_bridge_live", fromlist=["cmd_room_data"]).cmd_room_data(routed.payload)
        if not isinstance(data, dict):
            return True
        try:
            return data.get("isSuccess") is not False and int(
                data.get("code") or data.get("errorCode") or 0
            ) == 0
        except (TypeError, ValueError):
            return data.get("isSuccess") is not False

    async def handle_event(
        self, event: dict[str, Any]
    ) -> tuple[dict[str, Any], Optional[int]]:
        if self._closed:
            return _forward(event, router_error="device router closed"), None
        # Fail open under overload; blocking RealWebSocket.send is worse than losing
        # one passive observation.  Queue/task counts remain bounded.
        try:
            await asyncio.wait_for(self._inflight.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            self._sink(
                RouterObservation(
                    "warning", self.device_id, status="yellow",
                    reason="router backpressure: hook event forwarded",
                )
            )
            return _forward(event, router_error="backpressure"), None
        try:
            routed = _decode_event(event)
            double_board, double_board_reason = detect_double_board(
                routed.payload, routed.command
            )
            if routed.is_dummy:
                # A due poker decision owns the dummy first.  Navigation may
                # consume that same carrier only when the action arbiter/session
                # forwarded it, so one hook event is replaced at most once.
                action_decision, finish = await self._route_dummy(routed)
                if action_decision.get("action") != "forward":
                    return action_decision, finish
                navigation = self._navigation_decision(event)
                if navigation.get("action") != "forward":
                    self._record_telemetry(0, routed, navigation)
                    return navigation, None
                return action_decision, finish

            # Every non-dummy hook event is a passive navigation observation.
            # Lifecycle injection is deliberately impossible on these carriers;
            # an unexpected replacement is ignored fail-open and audited.
            navigation = self._navigation_decision(event)
            if navigation.get("action") != "forward":
                self._sink(
                    RouterObservation(
                        "warning",
                        self.device_id,
                        status="yellow",
                        reason="navigation attempted injection on a non-dummy event",
                    )
                )

            async with self._lock:
                if self._is_seed_safe(routed):
                    self._history.append(dict(event))
                    try:
                        identity_id = int(routed.data.get("userId") or 0)
                    except (TypeError, ValueError):
                        identity_id = 0
                    if (
                        routed.data.get("userName")
                        and "sessionId" in routed.data
                        and routed.data.get("userId") is not None
                    ):
                        # Current Coin login may carry userId=0.  Keep this frame
                        # sticky anyway; LiveCoinBridge resolves the numeric uid
                        # from the later seat snapshot.
                        self._sticky_identity_event = dict(event)
                    if b"GameRoomProperties" in routed.decoded_body:
                        context_key = (
                            "tables:" + ",".join(str(value) for value in routed.table_ids)
                            if routed.table_ids
                            else "wire:" + hashlib.sha256(routed.decoded_body).hexdigest()
                        )
                        self._sticky_context_events[context_key] = dict(event)
                        self._sticky_context_events.move_to_end(context_key)
                        while len(self._sticky_context_events) > 128:
                            self._sticky_context_events.popitem(last=False)
                self._record_ws_table_hint_locked(routed)
                if routed.direction == "out" and routed.command in {
                    "lobby.join_game", "lobby.join_game_table"
                }:
                    tid = routed.table_ids[0] if len(routed.table_ids) == 1 else 0
                    self._join_intents.append((tid, routed.config_id, time.monotonic()))
                if routed.direction == "in" and routed.command == "lobby.join_game_table":
                    for provisional_id in routed.table_ids:
                        if routed.config_id:
                            self._config_by_table[int(provisional_id)] = routed.config_id
                        self._mark_provisional_locked(
                            provisional_id, routed, "allocated; waiting for game_init"
                        )

                table_id = self._owner_locked(routed)
                definite = self._definite_table_locked(routed)
                owner = int(definite or table_id or 0)
                if owner and stale_double_board_should_drop(
                    routed.payload, routed.command, detected=double_board
                ):
                    self._release_stale_double_board_locked(owner)
                if double_board and routed.room_id is not None:
                    self._unsupported_room_reasons[int(routed.room_id)] = str(
                        double_board_reason or "DOUBLE BOARD"
                    )
                inherited_unsupported = (
                    definite > 0
                    and routed.room_id is not None
                    and int(routed.room_id) in self._unsupported_room_reasons
                    and int(self._room_to_table.get(int(routed.room_id), definite) or 0) == definite
                )
                if definite and (double_board or inherited_unsupported):
                    self._mark_unsupported_locked(
                        definite,
                        routed,
                        double_board_reason
                        or self._unsupported_room_reasons.get(int(routed.room_id or 0), "DOUBLE BOARD"),
                    )
                    table_id = definite
                if table_id in self._unsupported_tables:
                    # Re-entry into a session-blacklisted table gets no PokerEYE
                    # lease.  Send best-effort FOLD -> STANDUP -> LEAVE directly.
                    if routed.room_id is None or self._room_to_table.get(int(routed.room_id), table_id) == table_id:
                        self._bind_locked(routed, table_id)
                    extra_timer = "extra" in str(
                        (routed.data or {}).get("timerName") or ""
                    ).lower()
                    if extra_timer or str(routed.command or "") == "game.user_turn":
                        self._arm_db_checkfold_locked(int(table_id))
                        self._unstick_db_checkfold_locked(int(table_id))
                    self._ensure_unsupported_exit_locked(
                        table_id, routed, reset=(routed.command == "game.game_init")
                    )
                    self._forced_coin_ack_locked(routed, table_id)
                    forced = self._forced_exit_decision_locked(routed, table_id)
                    self._record_telemetry(table_id, routed, forced)
                    return forced, None
                forced_state = self._unsupported_exit.get(int(table_id)) if table_id else None
                if forced_state and str(forced_state.get("kind") or "") == "low_stack":
                    self._bind_locked(routed, table_id)
                    forced = self._forced_exit_decision_locked(routed, table_id)
                    if forced.get("action") != "forward":
                        self._record_telemetry(table_id, routed, forced)
                        return forced, None
                if table_id and routed.config_id:
                    self._config_by_table[int(table_id)] = routed.config_id
                is_quit_request = (
                    routed.command == "game.quit_table" and routed.direction == "out"
                )
                if routed.command == "game.quit_table":
                    strict = self._definite_table_locked(routed)
                    if strict:
                        table_id = strict
                    else:
                        # Unfocused-tab quit arrives on the focused websocket.
                        # Guessing the owner here closed the live table.
                        table_id = 0
                if table_id and is_quit_request and table_id in self._sessions:
                    self._mark_closing_locked(table_id, routed.room_id)
                closing_table = 0
                if routed.room_id is not None:
                    closing_table = self._closing_rooms.get(int(routed.room_id), 0)
                if not closing_table:
                    closing_table = next(
                        (tid for tid in routed.table_ids if tid in self._closing_tables), 0
                    )
                if closing_table and not is_quit_request:
                    closing_slot = self._sessions.get(closing_table)
                    if self._quit_succeeded(routed) and closing_slot is not None:
                        # Route the one real ACK to its old owner; close/release once
                        # after the bridge has consumed it.
                        table_id = closing_table
                    elif self._quit_succeeded(routed):
                        self._schedule_tombstone_clear_locked(closing_table)
                        return _forward(event, router_closing=True), None
                    else:
                        return _forward(event, router_closing=True), None
                if table_id:
                    self._bind_locked(routed, table_id)
                    slot = self._sessions.get(table_id)
                    if slot is not None:
                        slot.last_seen = time.monotonic()
                    # Coin emits game_alldata for transient wait-list/preview rooms in
                    # the live multitable capture.  game_init is the admission edge
                    # that distinguishes the three genuinely opened tables.
                    # game_init is the preferred admission edge, but native
                    # injection/reconnect may attach after that one frame. Strong
                    # in-hand evidence must be allowed to recover the table instead
                    # of leaving a real table permanently unserved.
                    startable = in_play_enough_to_lease(routed)
                    if slot is None and not startable:
                        # Allocation/wait-list metadata is not admission.  The live
                        # capture contains repeated/failed rows that would otherwise
                        # leak one backend account apiece.
                        if self._quit_succeeded(routed):
                            self._remove_provisional_locked(table_id, "waitlist/entry cancelled")
                            if routed.room_id is not None:
                                self._room_to_table.pop(int(routed.room_id), None)
                                self._orphans.pop(int(routed.room_id), None)
                            return _forward(event), None
                        if routed.is_game and routed.room_id is not None:
                            room = int(routed.room_id)
                            queue = self._orphan_queue_locked(room)
                            queue.append(dict(event))
                        if routed.command == "game.wait_list_data":
                            self._mark_provisional_locked(table_id, routed, "waitlist/pending")
                        return _forward(event, router_waiting_for_game_init=True), None
                    if slot is None and startable and db_recent_store().blocked(int(table_id)):
                        self._unsupported_tables.setdefault(int(table_id), "DOUBLE BOARD recent")
                        self._ensure_unsupported_exit_locked(
                            int(table_id), routed, reset=True, exit_kind="unsupported",
                            detail={"recent": True},
                        )
                        forced = self._forced_exit_decision_locked(routed, int(table_id))
                        self._record_telemetry(int(table_id), routed, forced)
                        return forced, None
                    if slot is None:
                        if len(self._sessions) >= self._max_table_slots:
                            self._sink(
                                RouterObservation(
                                    "error", self.device_id, int(table_id), status="red",
                                    reason="per-device table-session capacity reached",
                                )
                            )
                            return _forward(event, router_error="table_capacity"), None
                        slot = await self._ensure_slot_locked(table_id, routed)
                    # Flush room-orphans before the event that supplied identity.
                    if routed.room_id is not None:
                        orphaned = self._orphans.pop(int(routed.room_id), None)
                        if orphaned:
                            slot.buffer.extend(orphaned)
                            for orphan in orphaned:
                                with contextlib.suppress(Exception):
                                    self._record_telemetry(
                                        table_id, _decode_event(orphan), _forward(orphan)
                                    )
                    if slot.session is None:
                        if not routed.is_dummy:
                            slot.buffer.append(dict(event))
                        pending_decision = _forward(event, router_pending=True)
                        self._record_telemetry(table_id, routed, pending_decision)
                        return pending_decision, None
                    session = slot.session
                else:
                    session = None
                    if routed.is_game and routed.room_id is not None:
                        room = int(routed.room_id)
                        queue = self._orphan_queue_locked(room)
                        queue.append(dict(event))

            if session is None:
                if event.get("kind") in {"ping", "health"}:
                    async with self._lock:
                        active = len(self._sessions)
                        pending = sum(
                            1 for row in self._sessions.values() if row.session is None
                        )
                    return _forward(
                        event,
                        bridge_health={
                            "ok": True,
                            "router": "multitable",
                            "tables": active,
                            "starting": pending,
                        },
                    ), None
                return _forward(event), None

            arbiter_before = (
                session.arbitration_action_ids()
                if isinstance(session, LiveTableSession)
                else set()
            )
            try:
                decision = await session.handle_event(event)
            except Exception as exc:
                await self.close_table(
                    table_id,
                    crashed=True,
                    reason=f"table session crash: {type(exc).__name__}",
                )
                raise
            if isinstance(session, LiveTableSession):
                low_stack = session.take_low_stack_exit_request()
                if low_stack is not None:
                    async with self._lock:
                        self._bind_locked(routed, table_id)
                        self._ensure_unsupported_exit_locked(
                            table_id, routed, reset=True,
                            exit_kind="low_stack", detail=low_stack,
                        )
                        self._action_arbiter.cancel_table(table_id, "LOW_STACK_EXIT")
                        if table_id not in self._unsupported_close_tasks:
                            self._unsupported_close_tasks[table_id] = asyncio.create_task(
                                self._close_unsupported_after(table_id),
                                name=f"low-stack-close-{self.device_id}-{table_id}",
                            )
                        forced = self._forced_exit_decision_locked(routed, table_id)
                    if forced.get("action") != "forward":
                        decision = (forced, None)
            cancel_reason = ""
            if routed.direction == "out" and routed.command == "game.user_action":
                cancel_reason = "MANUAL_ACTION"
            elif routed.command == "game.user_turn":
                cancel_reason = "TURN_CHANGED"
            elif routed.command in {
                "game.reset_data", "game.quit_table", "game.pre_hand_start_info",
                "lobby.join_game_table", "lobby.join_game",
            }:
                cancel_reason = "STALE"
            if cancel_reason and isinstance(session, LiveTableSession):
                arbiter_after = session.arbitration_action_ids()
                for action_id in arbiter_before - arbiter_after:
                    self._action_arbiter.cancel(action_id, cancel_reason)
            self._record_telemetry(table_id, routed, decision[0])
            if routed.command == "game.quit_table":
                if self._quit_succeeded(routed):
                    await self.close_table(table_id, reason="Coin quit ACK")
                elif routed.direction == "out":
                    async with self._lock:
                        current = self._sessions.get(table_id)
                        if current and (current.close_task is None or current.close_task.done()):
                            current.close_task = asyncio.create_task(
                                self._close_after_grace(table_id),
                                name=f"quit-grace-{self.device_id}-{table_id}",
                            )
            return decision
        except Exception as exc:
            self._sink(
                RouterObservation(
                    "error", self.device_id, status="red",
                    reason=f"route event: {type(exc).__name__}: {exc}",
                )
            )
            return _forward(event, router_error=f"{type(exc).__name__}: {exc}"), None
        finally:
            self._inflight.release()

    async def _close_after_grace(self, table_id: int) -> None:
        await asyncio.sleep(self._close_grace)
        await self.close_table(table_id, reason="Coin quit timeout")

    async def close_table(
        self, table_id: int, *, crashed: bool = False, reason: str = "closed"
    ) -> None:
        self._action_arbiter.cancel_table(int(table_id), "TABLE_CLOSED")
        task = self._unsupported_close_tasks.get(int(table_id))
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        auto = self.automation
        async with self._lock:
            self._unsupported_exit.pop(int(table_id), None)
            slot = self._sessions.pop(int(table_id), None)
            if slot is None:
                if auto is not None:
                    auto.note_table_closed(int(table_id), str(reason or "ghost"))
                return
            for room, owner in list(self._room_to_table.items()):
                if owner == int(table_id):
                    self._room_to_table.pop(room, None)
                    self._orphans.pop(room, None)
                    # SmartFox room ids are transport-scoped and can be reused after
                    # reconnect.  Keep the concrete table blacklist, but never let an
                    # old room id mark an unrelated future table as double-board.
                    self._unsupported_room_reasons.pop(int(room), None)
                    self._closing_rooms.pop(int(room), None)
            for websocket, owners in list(self._ws_to_tables.items()):
                owners.discard(int(table_id))
                if not owners:
                    self._ws_to_tables.pop(websocket, None)
            start_task = slot.start_task
            close_task = slot.close_task
            session = slot.session
        if close_task and close_task is not asyncio.current_task() and not close_task.done():
            close_task.cancel()
        if (
            session is None
            and start_task
            and start_task is not asyncio.current_task()
            and not start_task.done()
        ):
            start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await start_task
        # Return the table stack to wallet before History sees table_close.
        # Otherwise End is leftover after buy-in, or the table stack ± deltas.
        if auto is not None:
            auto.note_table_closed(int(table_id), str(reason or ""))
        if session is not None:
            await session.close(crashed=crashed, reason=reason)
        else:
            self._sink(
                RouterObservation(
                    "table_close", self.device_id, int(table_id), status="red" if crashed else "green",
                    reason=reason,
                )
            )
        async with self._lock:
            if int(table_id) in self._closing_tables:
                self._schedule_tombstone_clear_locked(int(table_id))

    async def reap_stale_startups(self) -> tuple[int, ...]:
        """Remove startup slots that can no longer become useful.

        This is deliberately limited to slots without a live session; normal idle
        poker tables are not GC'd merely because they have not produced a hand.
        """

        now = time.monotonic()
        async with self._lock:
            stale = [
                int(table_id)
                for table_id, slot in self._sessions.items()
                if slot.session is None
                and (
                    slot.failed
                    or now - float(slot.created_at or now) >= self._startup_stale_seconds
                )
            ]
        for table_id in stale:
            await self.close_table(
                table_id,
                crashed=True,
                reason="stale backend startup slot reaped",
            )
        return tuple(stale)

    async def restart_table_backend(self, table_id: int) -> bool:
        """Recycle only the PokerEYE side for one admitted table."""

        table_id = int(table_id)
        async with self._lock:
            slot = self._sessions.get(table_id)
            if slot is None:
                return False
            if slot.seed is None:
                hang = True
                seed = None
                old_session = None
                old_start = None
            else:
                hang = False
                seed = slot.seed
                old_session = slot.session
                old_start = slot.start_task
                slot.session = None
                slot.failed = False
                slot.startup_error = ""
                slot.startup_attempts = 0
                slot.created_at = time.monotonic()
                slot.last_seen = slot.created_at
                slot.start_task = None
        if hang:
            await self.close_table(
                table_id, crashed=False, reason="operator close hung startup"
            )
            return True
        if old_start and old_start is not asyncio.current_task() and not old_start.done():
            old_start.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_start
        if old_session is not None:
            await old_session.close(crashed=False, reason="operator backend reload")
        async with self._lock:
            current = self._sessions.get(table_id)
            if current is not slot:
                return False
            slot.start_task = asyncio.create_task(
                self._start_session(slot, seed),
                name=f"restart-table-{self.device_id}-{table_id}",
            )
        self._sink(
            RouterObservation(
                "table_restart", self.device_id, table_id, status="yellow",
                reason="operator requested backend reload", pending=True,
            )
        )
        return True

    async def control_snapshot(self, compact: bool = True) -> dict[str, Any]:
        async with self._lock:
            rows = list(self._sessions.values())
        tables = []
        hero_name = ""
        now = time.monotonic()
        ordered = sorted(rows, key=lambda item: item.created_order)
        for index, slot in enumerate(ordered, 1):
            session = slot.session
            row: dict[str, Any] = {
                "device_id": self.device_id,
                "table_id": int(slot.table_id),
                "table_no": int(index),
                "state": "ready" if session is not None else ("failed" if slot.failed else "starting"),
                "startup_attempts": int(slot.startup_attempts),
                "startup_error": str(slot.startup_error or ""),
                "age_seconds": max(0.0, now - float(slot.created_at or now)),
                "seen_seconds": max(0.0, now - float(slot.last_seen or now)),
                "account_id": str(getattr(session, "account_id", "") or ""),
            }
            if isinstance(session, LiveTableSession):
                snap = session._snapshot()
                bridge = session._bridge
                current_hero = str(
                    bridge.identity.get("user_name")
                    or bridge.state.get("user_name")
                    or ""
                )
                if current_hero:
                    hero_name = current_hero
                seat_map = getattr(bridge, "seat_map", {}) or {}
                seats = []
                for seat_id, raw in sorted(seat_map.items()):
                    if not isinstance(raw, dict):
                        continue
                    seats.append({
                        "seat": int(seat_id),
                        "name": str(raw.get("userName") or ""),
                        "chips": raw.get("userChips"),
                        "playing": bool(raw.get("isPlaying")),
                    })
                pending = dict(getattr(bridge.autoplay, "pending", None) or {})
                live_hint = bool(pending.get("action")) and not pending.get("_hud_done")
                if live_hint:
                    session._hint_action = str(pending.get("action") or "")
                    session._hint_amount = pending.get("display_amount")
                    try:
                        session._last_action_delay_ms = int(pending.get("delay_ms") or 0)
                    except (TypeError, ValueError):
                        session._last_action_delay_ms = 0
                    last_source = "prefold" if pending.get("prefold") else (
                        "failsafe" if pending.get("fallback") else "cc"
                    )
                    last_action = str(pending.get("action") or "")
                    if last_source == "prefold":
                        last_action = "PREFOLD"
                    elif last_source == "failsafe":
                        last_action = "FALLBACK"
                    last_amount = pending.get("display_amount")
                else:
                    session._hint_action = ""
                    session._hint_amount = None
                    session._last_action_delay_ms = 0
                    last_action = ""
                    last_amount = None
                    last_source = ""
                cards = []
                hand = getattr(bridge, "current_hand", None) or getattr(bridge, "context_hand", None)
                if hand is not None:
                    cards = list(getattr(hand, "cards", None) or [])
                max_seats = 0
                if hand is not None:
                    props = getattr(getattr(hand, "room", None), "props", {}) or {}
                    try:
                        max_seats = int(props.get("maxSize") or 0)
                    except (TypeError, ValueError):
                        max_seats = 0
                row.update(
                    phase=str(snap.phase or ""),
                    hand_id=str(snap.hand or ""),
                    pending_action=bool(snap.pending),
                    game_type=str(snap.game_type or ""),
                    backend_status=str(snap.backend_status or ""),
                    backend_health=str(snap.backend_health or ""),
                    backend_message=str(snap.backend_message or ""),
                    fuel_quantity=snap.fuel_quantity,
                    fuel_rate_per_hand=snap.fuel_rate_per_hand,
                    fuel_updated_at=float(getattr(snap, "fuel_updated_at", 0.0) or 0.0),
                    fuel_sequence=int(getattr(snap, "fuel_sequence", 0) or 0),
                    hero_name=current_hero,
                    players=len([s for s in seats if s.get("name") or s.get("chips")]),
                    max_seats=max_seats or len(seats),
                    seats=seats,
                    hero_sitting=bool(getattr(bridge, "hero_sitting", False)) or any(
                        current_hero and str(item.get("name") or "") == current_hero
                        for item in seats
                    ),
                    standup_queued=bool(
                        getattr(bridge, "hero_departing", False)
                        and getattr(bridge, "hero_sitting", False)
                    ),
                    last_action=last_action,
                    last_amount=last_amount,
                    last_action_source=last_source,
                    action_delay_ms=(
                        int(pending.get("delay_ms") or 0) if live_hint else None
                    ),
                    hole_cards=cards,
                    hero_seat=int(bridge.state.get("hero_seat") or 0),
                )
                ledger = self.history_ledger
                enabled = bool(
                    ledger is not None
                    and getattr(self.automation, "policy", None) is not None
                    and getattr(self.automation.policy, "ledger_enabled", False)
                )
                profit = None
                if ledger is not None and enabled:
                    try:
                        profit = ledger.table_profit(self.device_id, int(slot.table_id))
                    except Exception:
                        profit = None
                row.update(attach_session_profit(row, profit, enabled=enabled))
                docs = None
                if ledger is not None and enabled:
                    try:
                        docs = ledger.session_docs(self.device_id, int(slot.table_id))
                    except Exception:
                        docs = None
                row.update(attach_session_docs(row, docs, enabled=enabled))
            if int(slot.table_id) in self._unsupported_tables or db_recent_store().blocked(int(slot.table_id)):
                row["warning"] = "DB"
                row["warning_text"] = "Warning: DB"
            tables.append(row)
        session_ids = {int(slot.table_id) for slot in ordered}
        async with self._lock:
            pending_rows = [
                (int(table_id), dict(meta))
                for table_id, meta in self._provisional.items()
                if int(table_id) not in session_ids
            ]
        for table_id, meta in pending_rows:
            tables.append({
                "device_id": self.device_id,
                "table_id": int(table_id),
                "table_no": len(tables) + 1,
                "state": "observing",
                "hero_sitting": False,
                "phase": "observe",
                "game_type": str(meta.get("game_type") or ""),
                "account_id": "",
                "startup_attempts": 0,
                "startup_error": "",
                "age_seconds": max(0.0, now - float(meta.get("updated") or now)),
                "seen_seconds": 0.0,
                "players": 0,
                "max_seats": 0,
                "seats": [],
                "hole_cards": [],
                "last_action": "",
                "pending_action": False,
            })
        live_ids = {int(row.get("table_id") or 0) for row in tables}
        if self.automation is not None:
            for table_id in sorted(self.automation._seated):
                tid = int(table_id)
                if tid <= 0 or tid in live_ids:
                    continue
                tables.append({
                    "device_id": self.device_id,
                    "table_id": tid,
                    "table_no": len(tables) + 1,
                    "state": "ready",
                    "hero_sitting": True,
                    "persisted": True,
                    "phase": "persisted",
                    "game_type": "",
                    "account_id": "",
                    "startup_attempts": 0,
                    "startup_error": "",
                    "age_seconds": 0.0,
                    "seen_seconds": 0.0,
                    "players": 0,
                    "max_seats": 0,
                    "seats": [],
                    "hole_cards": [],
                    "last_action": "",
                    "pending_action": False,
                })
                live_ids.add(tid)
        auto = self.automation.snapshot() if self.automation is not None else {}
        if compact:
            for table in tables:
                table.pop("seats", None)
            if isinstance(auto, dict):
                auto = dict(auto)
                ui = auto.get("ui")
                if isinstance(ui, dict):
                    auto["ui"] = {k: v for k, v in ui.items() if k != "rows"}
        warning = "DB" if any(str(row.get("warning") or "") == "DB" for row in tables) else ""
        return {
            "device_id": self.device_id,
            "hero_name": hero_name,
            "warning": warning,
            "tables": tables,
            "automation": auto,
        }

    async def wait_table_ready(self, table_id: int, timeout: float = 5.0) -> TableSession:
        deadline = asyncio.get_running_loop().time() + float(timeout)
        while True:
            async with self._lock:
                slot = self._sessions.get(int(table_id))
                if slot and slot.session is not None:
                    return slot.session
                failed = bool(slot and slot.failed)
            if failed:
                raise RuntimeError(f"table {table_id} session failed")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"table {table_id} did not become ready")
            await asyncio.sleep(0.01)

    async def close(self, *, crashed: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None:
            task.cancel()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        async with self._lock:
            table_ids = tuple(self._sessions)
            provisional_ids = tuple(self._provisional)
            for table_id in provisional_ids:
                self._remove_provisional_locked(table_id, "device stopped")
            tombstone_tasks = tuple(self._tombstone_tasks.values())
            self._tombstone_tasks.clear()
            self._closing_tables.clear()
            self._closing_rooms.clear()
        for task in tombstone_tasks:
            task.cancel()
        await asyncio.gather(
            *(self.close_table(tid, crashed=crashed, reason="device stopped") for tid in table_ids),
            return_exceptions=True,
        )


class RouterRuntimeHost:
    """Own all device listeners on one asyncio thread inside the supervisor.

    Keeping account allocation in this process is what prevents two emulator
    workers from accidentally leasing the same backend identity.
    """

    def __init__(
        self,
        *,
        session_factory_builder: Callable[[ObservationSink], TableSessionFactory],
        observation_sink: ObservationSink,
        telemetry: Any = None,
        navigation_register: Optional[
            Callable[[str, str, int], Callable[[dict[str, Any]], dict[str, Any]]]
        ] = None,
        navigation_unregister: Optional[Callable[[str, str, int], None]] = None,
    ) -> None:
        self._factory_builder = session_factory_builder
        self._sink = observation_sink
        self._telemetry = telemetry
        self._navigation_register = navigation_register
        self._navigation_unregister = navigation_unregister
        self._loop = asyncio.new_event_loop()
        self._routers: dict[str, DeviceIngressRouter] = {}
        self._router_identity: dict[str, tuple[str, int]] = {}
        self._thread = threading.Thread(
            target=self._run, name="coin-multitable-router", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coroutine: Awaitable[Any], timeout: float = 30.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    async def _add(
        self,
        device_id: str,
        host: str,
        port: int,
        player_key: str,
        app_generation: int,
    ) -> tuple[str, int]:
        existing = self._routers.get(str(device_id))
        if existing is not None:
            if self._router_identity.get(str(device_id)) != (
                str(player_key),
                int(app_generation),
            ):
                raise ValueError("router identity/app generation changed without teardown")
            return await existing.start(host, port)
        factory = self._factory_builder(self._sink)
        navigation_handler = None
        if self._navigation_register is not None:
            navigation_handler = self._navigation_register(
                str(device_id), str(player_key), int(app_generation)
            )
        router = DeviceIngressRouter(
            device_id,
            factory,
            observation_sink=self._sink,
            telemetry=self._telemetry,
            navigation_handler=navigation_handler,
        )
        try:
            bound = await router.start(host, port)
        except Exception:
            if self._navigation_unregister is not None and navigation_handler is not None:
                self._navigation_unregister(
                    str(device_id), str(player_key), int(app_generation)
                )
            raise
        self._routers[str(device_id)] = router
        self._router_identity[str(device_id)] = (str(player_key), int(app_generation))
        return bound

    def add_device(
        self,
        device_id: str,
        host: str,
        port: int,
        *,
        player_key: str = "",
        app_generation: int = 1,
    ) -> tuple[str, int]:
        return self._submit(
            self._add(
                str(device_id),
                str(host),
                int(port),
                str(player_key),
                int(app_generation),
            )
        )

    async def _remove(self, device_id: str, crashed: bool) -> None:
        router = self._routers.pop(str(device_id), None)
        identity = self._router_identity.pop(str(device_id), None)
        if router is not None:
            await router.close(crashed=crashed)
        if identity is not None and self._navigation_unregister is not None:
            self._navigation_unregister(str(device_id), identity[0], identity[1])

    def remove_device(self, device_id: str, *, crashed: bool = False) -> None:
        self._submit(self._remove(str(device_id), bool(crashed)))

    async def _shutdown(self) -> None:
        rows = tuple(
            (device_id, router, self._router_identity.get(device_id))
            for device_id, router in self._routers.items()
        )
        self._routers.clear()
        self._router_identity.clear()
        await asyncio.gather(
            *(router.close() for _device_id, router, _identity in rows),
            return_exceptions=True,
        )
        if self._navigation_unregister is not None:
            for device_id, _router, identity in rows:
                if identity is not None:
                    self._navigation_unregister(device_id, identity[0], identity[1])

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        with contextlib.suppress(Exception):
            self._submit(self._shutdown())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5.0)
        with contextlib.suppress(Exception):
            self._loop.close()
