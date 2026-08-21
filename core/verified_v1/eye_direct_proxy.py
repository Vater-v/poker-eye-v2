#!/usr/bin/env python3
"""In-process PokerEYE backend adapter for ``coin_bridge_live``.

The live bridge already has a well-tested length-prefixed local EYE transport.
This module intentionally preserves that boundary: it exposes an ephemeral local
TCP endpoint, translates ``traffic`` frames to the recovered gRPC ``CSPacket``
stream, and translates backend ``SCAction`` messages back to the exact local
``cc`` frame consumed by the autoplay coordinator.

One instance represents one PokerEYE account slot.  The backend action does not
carry a table id, so a slot must never be shared by concurrent tables.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import hashlib
import math
import struct
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from .eye_backend_probe import (
    GRPC_METHOD,
    LoginParams,
    PacketReplayWindow,
    ReconnectDirective,
    ResilientEyeStream,
    compact_json,
    login_envelope,
    normalize_packet,
    ping_envelope,
    settings_envelope,
    reconnect_protocol_selftest,
    resilient_stream_selftest,
)


DEFAULT_ACCOUNT_ID = ""
DEFAULT_HOST = "gs.eye-panel.com"
DEFAULT_PORT = 443
DEFAULT_ANDROID_ID = "82b990b5a48d6b10"
DEFAULT_ROOM_VERSION = "76659f5fc0f5ad2d59469446388715a4"
DEFAULT_BUNDLE_ID = "PPPoker"
DEFAULT_ROOM_TYPE = "PPPoker"
DEFAULT_WORKING_MODE = "AUTO"
DEFAULT_DEVICE = " REL 28 034bbbf0516cc950055989f053210d5f"
DEFAULT_CREDENTIAL_FILE = Path("secrets/eye.agent")


def backend_android_id(account_id: str) -> str:
    """Return one stable Android-style serial per backend account.

    The recovered mobile client uses one Android identity per app instance. The
    Trainer runs several independent backend streams in one process, so sharing a
    single hard-coded serial makes those streams indistinguishable to the remote
    backend. Derive a deterministic 16-hex identity from the full account id
    instead; no credential material is involved.
    """

    value = str(account_id or "").strip()
    if not value:
        return DEFAULT_ANDROID_ID
    return hashlib.sha256(("pokereye-eye-slot:" + value).encode("utf-8")).hexdigest()[:16]


class BackendLoginRejected(ConnectionError):
    """SCLogin answered successfully at transport level but rejected this full ID."""


def _clean_backend_field(value: Any, limit: int) -> str:
    """Bound untrusted backend telemetry before it reaches logs/the GUI."""

    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_backend_preview(value: Any, limit: int = 320) -> str:
    """Serialize backend diagnostics while redacting credential-like fields."""

    sensitive = ("pass", "token", "secret", "credential", "auth", "cookie", "session", "key")

    def scrub(item: Any, depth: int = 0) -> Any:
        if depth >= 3:
            return "…"
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            for raw_key, raw_value in list(item.items())[:24]:
                key = str(raw_key)
                if any(part in key.lower() for part in sensitive):
                    out[key] = "<redacted>"
                else:
                    out[key] = scrub(raw_value, depth + 1)
            return out
        if isinstance(item, (list, tuple)):
            return [scrub(v, depth + 1) for v in list(item)[:24]]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return str(item)

    try:
        text = json.dumps(scrub(value), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(value)
    return _clean_backend_field(text, limit)


def _sc_login_rejection_detail(body: Any) -> str:
    """Keep the authoritative SCLogin=false diagnostics without leaking secrets."""

    base = "PokerEYE backend login rejected"
    if not isinstance(body, dict):
        return base
    parts: list[str] = []
    for key in ("args", "workMode", "wle", "workingMode", "message", "error", "reason"):
        value = body.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={_safe_backend_preview(value, 240)}")
    session_info = body.get("sessionInfo")
    if session_info not in (None, "", [], {}):
        # Session material may be a bearer-like blob; presence/length is enough
        # for diagnostics and avoids writing it to journal/operator logs.
        parts.append(f"sessionInfo=<present:{len(str(session_info))}>")
    return base if not parts else f"{base}: {'; '.join(parts)}"


def _backend_health(status: str, message: str) -> str:
    """Map the backend protocol status to the supervisor traffic-light model."""

    status_word = status.strip().upper()
    message_word = message.strip().upper()
    # PokerEYE reports the normal pid=0 -> assigned playing-id handshake using
    # PID_CHANGED.  It must never turn a healthy table red, even when older server
    # versions put it inside an ERROR-shaped envelope.
    if message_word == "PID_CHANGED":
        return "green"
    critical_messages = {
        "GAME_IS_BROKEN",
        "INSUFFICIENT_FUEL_BALANCE",
        "AUTH_FAILED",
        "ACCOUNT_DISABLED",
        "ACCOUNT_DELETED",
        "AUTHKEY_CHANGED",
    }
    if status_word in {"ERROR", "FAILED", "FAIL", "FATAL"} or message_word in critical_messages:
        return "red"
    if status_word in {"NORMAL", "OK", "SUCCESS", "READY"}:
        return "green"
    return "yellow"


@dataclass(frozen=True)
class BackendStatusSnapshot:
    """Sanitized, immutable status of one direct backend account stream."""

    status: str
    message: str
    hash: str
    health: str
    sequence: int
    updated_at: float

    @property
    def reason(self) -> str:
        if self.message.upper() == "PID_CHANGED":
            parts = ["backend PID_CHANGED (normal player-id assignment)"]
            if self.hash:
                parts.append(f"hash={self.hash}")
            return " ".join(parts)
        parts = [f"backend {self.status or 'UNKNOWN'}"]
        if self.message:
            parts.append(self.message)
        if self.hash:
            parts.append(f"hash={self.hash}")
        return " ".join(parts)


@dataclass(frozen=True)
class BackendFuelSnapshot:
    """Immutable fuel telemetry for exactly one direct backend account stream.

    The recovered PokerEYE DTO names the authoritative balance field ``fuelQty``
    and its UI renders that value in ``F``. ``fuelRate`` is a separate
    consumption rate rendered as ``F/hand``; it is never a remaining balance.
    """

    account_id: str
    quantity: Optional[float]
    rate_per_hand: Optional[float]
    available: bool
    reason_code: str
    sequence: int
    updated_at: float


def _json_nonnegative_number(value: Any) -> Optional[float]:
    """Accept an actual finite JSON number, never a bool or numeric string."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_backend_fuel(value: Any) -> tuple[Optional[float], Optional[float], str]:
    """Parse only the exact top-level ``SCSettingsBillingRates`` fuel fields."""

    if not isinstance(value, dict):
        return None, None, "FUEL_INVALID_PAYLOAD"
    if "fuelQty" not in value:
        return None, None, "FUEL_UNAVAILABLE"
    quantity = _json_nonnegative_number(value.get("fuelQty"))
    if quantity is None:
        return None, None, "FUEL_INVALID_QUANTITY"
    if "fuelRate" not in value:
        return quantity, None, "FUEL_RATE_UNAVAILABLE"
    rate = _json_nonnegative_number(value.get("fuelRate"))
    if rate is None:
        return quantity, None, "FUEL_INVALID_RATE"
    return quantity, rate, "FUEL_AVAILABLE"


@dataclass(frozen=True)
class HintWatchdogTransition:
    """One deterministic liveness edge emitted by :class:`BackendHintWatchdog`."""

    health: str
    status: str
    message: str
    request_recovery: bool = False


class BackendHintWatchdog:
    """Keep exactly one backend hint outstanding and reject stale decisions.

    ``LiveCoinBridge`` waits ten seconds for ``SCAction``.  Once that deadline has
    passed, delivering the old action is unsafe because the next hero turn may
    already own the bridge's CC waiter.  A second unanswered hero hint is held
    locally (never sent into the unresolved backend latch); if it also reaches the
    deadline, the owning direct session must be recycled.
    """

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("hint watchdog timeout must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.reset()

    def reset(self) -> None:
        self.outstanding_since: Optional[float] = None
        self.blocked_since: Optional[float] = None
        self.outstanding_expired = False
        self.blocked_expired = False
        self.action_delivered = False
        self.recovery_pending = False
        self.consecutive_timeouts = 0

    def poll(self, now: float) -> tuple[HintWatchdogTransition, ...]:
        transitions: list[HintWatchdogTransition] = []
        now = float(now)
        if (
            self.outstanding_since is not None
            and not self.outstanding_expired
            and not self.action_delivered
            and now - self.outstanding_since >= self.timeout_seconds
        ):
            self.outstanding_expired = True
            self.consecutive_timeouts = 1
            transitions.append(HintWatchdogTransition(
                "yellow", "WARNING",
                "SCAction timeout 1/2; holding newer RoundHint frames",
            ))
        if (
            self.outstanding_expired
            and self.blocked_since is not None
            and not self.blocked_expired
            and now - self.blocked_since >= self.timeout_seconds
        ):
            self.blocked_expired = True
            self.consecutive_timeouts = 2
            self.recovery_pending = True
            transitions.append(HintWatchdogTransition(
                "red", "ERROR",
                "SCAction timeout 2/2; direct backend session is silent",
                True,
            ))
        return tuple(transitions)

    def observe_hint(
        self, now: float
    ) -> tuple[bool, tuple[HintWatchdogTransition, ...]]:
        transitions = self.poll(now)
        if self.recovery_pending:
            return False, transitions
        if self.outstanding_since is None:
            self.outstanding_since = float(now)
            return True, transitions
        # Never replace or duplicate an unresolved backend hint.  Remember only
        # the first held successor; it is the second consecutive timeout witness.
        if self.blocked_since is None:
            self.blocked_since = float(now)
        return False, transitions

    def observe_action(
        self, now: float
    ) -> tuple[bool, tuple[HintWatchdogTransition, ...]]:
        transitions = self.poll(now)
        accepted = bool(
            self.outstanding_since is not None
            and not self.outstanding_expired
            and not self.action_delivered
            and not self.recovery_pending
        )
        if accepted:
            self.action_delivered = True
        return accepted, transitions

    def observe_finish(self) -> tuple[HintWatchdogTransition, ...]:
        # During controlled recovery Finish is the first cleanup frame; it must
        # not cancel the already-latched recycle request.
        if self.recovery_pending:
            self.outstanding_since = None
            self.blocked_since = None
            self.action_delivered = False
            return ()
        recovered = self.consecutive_timeouts > 0
        self.reset()
        if recovered:
            return (HintWatchdogTransition(
                "green", "NORMAL", "hint lifecycle completed without recycle"
            ),)
        return ()


def _lp_pack(value: dict[str, Any]) -> bytes:
    raw = compact_json(value).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


async def _lp_read(reader: asyncio.StreamReader) -> Optional[bytes]:
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    size = struct.unpack(">I", header)[0]
    if not 0 < size <= 20_000_000:
        raise ValueError(f"bad direct-proxy frame size {size}")
    return await reader.readexactly(size)


def _work_mode(value: Any) -> Optional[str]:
    """Extract the backend mode without depending on one response DTO version."""
    if isinstance(value, dict):
        for key in ("workMode", "workingMode"):
            if key in value:
                mode = value[key]
                if isinstance(mode, str) and mode:
                    return mode.lower()
                if isinstance(mode, int):
                    return {0: "man", 1: "auto", 2: "vip"}.get(mode, str(mode))
        for nested in value.values():
            found = _work_mode(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _work_mode(nested)
            if found is not None:
                return found
    return None


def hook_game_mode(extracted: Optional[str] = None) -> str:
    """Always auto. Eye APK WorkModeDto ordinal 0=man, 1=auto, 2=vip.

    The decompiled client only forwards SCLogin.workMode onto the hook
    ``game_mode`` frame. There is no CS to change it. We still send ``auto``
    so every live slot plays as auto even when the backend body says man/0.
    """
    return "auto"


def hook_game_type(extracted: Optional[str] = None) -> str:
    """Always PPPoker. Empty CSSettings.game_type is the panel '-' column."""
    return DEFAULT_ROOM_TYPE


def hook_working_mode(extracted: Optional[str] = None) -> str:
    """CSSettings.working_mode uses GameMode.name(): MANUAL / AUTO / VIP."""
    return DEFAULT_WORKING_MODE


def hook_game_mode_frame(backend_body: Any = None) -> dict[str, str]:
    """Length-prefixed hook payload for one login/billing cycle."""
    extracted = _work_mode(backend_body) if backend_body is not None else None
    return {
        "tag": "game_mode",
        "msg": "",
        "data": hook_game_mode(extracted),
        "packageName": "com.lein.pppoker.android",
    }


@dataclass(frozen=True)
class DirectBackendSlot:
    """Frozen identity for exactly one backend stream/table slot."""

    account_id: str = DEFAULT_ACCOUNT_ID
    credential_file: Path = DEFAULT_CREDENTIAL_FILE
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    android_id: str = DEFAULT_ANDROID_ID
    room_version: str = DEFAULT_ROOM_VERSION
    bundle_id: str = DEFAULT_BUNDLE_ID
    device: str = DEFAULT_DEVICE

    def credential(self) -> str:
        value = self.credential_file.read_text(encoding="utf-8-sig").strip()
        if not value:
            raise RuntimeError(f"empty PokerEYE credential file: {self.credential_file}")
        return value


class DirectBackendProxy:
    """Local LP-to-gRPC adapter used by one ``LiveCoinBridge`` instance."""

    def __init__(
        self,
        slot: DirectBackendSlot | None = None,
        *,
        listen_host: str = "127.0.0.1",
        ping_interval: float = 2.5,
        connect_timeout: float = 12.0,
        login_timeout: float = 15.0,
        hint_timeout: float = 10.0,
        recovery_max_attempts: int = 3,
        recovery_backoff_base: float = 0.25,
        recovery_backoff_max: float = 2.0,
        logger: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.slot = slot or DirectBackendSlot()
        self.listen_host = listen_host
        self.ping_interval = ping_interval
        self.connect_timeout = connect_timeout
        self.login_timeout = login_timeout
        self.logger = logger or (lambda _tag, _message: None)
        self.server: Optional[asyncio.AbstractServer] = None
        self.port = 0
        self._sessions: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._active_client: Optional[asyncio.StreamWriter] = None
        self._accept_gate = asyncio.Event()
        self._accept_gate.set()
        self._session_idle = asyncio.Event()
        self._session_idle.set()
        self._recycle_lock = asyncio.Lock()
        self._bridge_recovery_lock = asyncio.Lock()
        self._recovery_task: Optional[asyncio.Task[Any]] = None
        self._bound_bridge: Any = None
        self._recovery_in_progress = False

        self.recovery_max_attempts = max(1, int(recovery_max_attempts))
        self.recovery_backoff_base = max(0.0, float(recovery_backoff_base))
        self.recovery_backoff_max = max(
            self.recovery_backoff_base, float(recovery_backoff_max)
        )
        self._recovery_attempt = 0
        self._recovery_exhausted_callback: Optional[Callable[[str], Any]] = None
        self._next_seq = 0
        self._first_login = True
        self._replay = PacketReplayWindow()
        self._hint_watchdog = BackendHintWatchdog(hint_timeout)
        self.recovery_requested = asyncio.Event()
        self._forwarded_commands: dict[str, int] = {}
        self._reseed_state = "ready"
        self.backend_ready = asyncio.Event()
        self.backend_login_ok: Optional[bool] = None
        self.backend_error = ""
        self._backend_status_sequence = 0
        self._backend_status = BackendStatusSnapshot(
            status="PENDING",
            message="connecting",
            hash="",
            health="yellow",
            sequence=0,
            updated_at=time.monotonic(),
        )
        self._backend_fuel_sequence = 0
        self._backend_fuel = BackendFuelSnapshot(
            account_id=self.slot.account_id,
            quantity=None,
            rate_per_hand=None,
            available=False,
            reason_code="FUEL_PENDING",
            sequence=0,
            updated_at=time.monotonic(),
        )

    @property
    def address(self) -> tuple[str, int]:
        if not self.server or not self.port:
            raise RuntimeError("direct backend proxy has not been started")
        return self.listen_host, self.port

    @property
    def backend_status_snapshot(self) -> BackendStatusSnapshot:
        return self._backend_status

    @property
    def backend_fuel_snapshot(self) -> BackendFuelSnapshot:
        return self._backend_fuel

    def _record_backend_fuel(
        self,
        value: Any = None,
        *,
        reason_code: Optional[str] = None,
        preserve_values: bool = False,
    ) -> BackendFuelSnapshot:
        if preserve_values:
            quantity = self._backend_fuel.quantity
            rate = self._backend_fuel.rate_per_hand
            parsed_reason = self._backend_fuel.reason_code
        else:
            quantity, rate, parsed_reason = parse_backend_fuel(value)
        clean_reason = _clean_backend_field(reason_code or parsed_reason, 64).upper()
        if not clean_reason.startswith("FUEL_"):
            clean_reason = "FUEL_INVALID_REASON"
        self._backend_fuel_sequence += 1
        self._backend_fuel = BackendFuelSnapshot(
            account_id=self.slot.account_id,
            quantity=quantity,
            rate_per_hand=rate,
            available=quantity is not None,
            reason_code=clean_reason,
            sequence=self._backend_fuel_sequence,
            updated_at=time.monotonic(),
        )
        return self._backend_fuel

    def _record_backend_status(
        self,
        status: Any,
        message: Any = "",
        hash_value: Any = "",
        *,
        health: Optional[str] = None,
    ) -> BackendStatusSnapshot:
        clean_status = _clean_backend_field(status, 32).upper()
        clean_message = _clean_backend_field(message, 256)
        clean_hash = _clean_backend_field(hash_value, 96)
        clean_health = str(health or _backend_health(clean_status, clean_message)).lower()
        if clean_health not in {"red", "yellow", "green"}:
            clean_health = "yellow"
        self._backend_status_sequence += 1
        self._backend_status = BackendStatusSnapshot(
            status=clean_status,
            message=clean_message,
            hash=clean_hash,
            health=clean_health,
            sequence=self._backend_status_sequence,
            updated_at=time.monotonic(),
        )
        return self._backend_status

    def _apply_hint_transitions(
        self, transitions: tuple[HintWatchdogTransition, ...]
    ) -> None:
        for transition in transitions:
            self._record_backend_status(
                transition.status, transition.message, health=transition.health
            )
            self.logger("BACKEND", transition.message)
            if transition.request_recovery:
                self.recovery_requested.set()

    def forwarded_command_count(self, command: str) -> int:
        return int(self._forwarded_commands.get(str(command), 0))

    async def wait_command_forwarded(
        self, command: str, after_count: int, timeout: float = 2.0
    ) -> None:
        """Wait until the old stream consumed a cleanup command in FIFO order."""

        deadline = asyncio.get_running_loop().time() + max(0.01, float(timeout))
        while self.forwarded_command_count(command) <= int(after_count):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"backend did not consume cleanup {command}")
            await asyncio.sleep(0.01)

    def _allow_during_hand_reseed(self, command: str) -> bool:
        """Quarantine the remainder of the hand that triggered a clean recycle."""

        if self._reseed_state == "ready":
            return True
        safe_session_commands = {
            "pb.UserLoginREQ", "pb.UserLoginRSP", "pb.EnterRoomREQ", "pb.EnterRoomRSP",
            "pb.SitDownREQ", "pb.SitDownBRC", "pb.SitDownRSP", "pb.TotalBuyinBRC",
            "pb.HeartBeatREQ", "pb.HeartBeatRSP", "pb.StandUpBRC",
        }
        if self._reseed_state == "waiting":
            if command == "pb.DealerInfoRSP":
                self._reseed_state = "replaying"
                self.logger("RECOVERY", "next full hand preamble detected")
                return True
            return command in safe_session_commands
        if command == "pb.HandCardRSP":
            self._reseed_state = "ready"
            self._record_backend_status(
                "NORMAL", "backend hand context reseeded", health="green"
            )
            self.logger("RECOVERY", "full hand context reseeded; hints enabled")
        return True

    def bind_bridge(
        self,
        bridge: Any,
        *,
        recovery_exhausted_callback: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """Attach the logical bridge and optional table-owner recovery callback.

        v6 ``DeviceIngressRouter`` installs a lease-safe callback twice:
        first a session-level fallback, then a stronger table-slot teardown
        callback after the session is owned by the router.  Rebinding the SAME
        bridge therefore updates the callback without spawning another recovery
        task.  Binding a different bridge to one backend slot remains forbidden.
        """

        if self._bound_bridge is not None and self._bound_bridge is not bridge:
            raise RuntimeError("direct backend slot is already bound to another bridge")

        self._bound_bridge = bridge
        if recovery_exhausted_callback is not None:
            self._recovery_exhausted_callback = recovery_exhausted_callback

        if self._recovery_task is not None and not self._recovery_task.done():
            return

        self._recovery_task = asyncio.create_task(
            self._bridge_recovery_loop(bridge),
            name=f"backend-recovery-{self.slot.account_id}",
        )

    def _discard_actions_for_recovery_exhaustion(self, bridge: Any) -> None:
        """Make it impossible for a stale backend action to leak after exhaustion."""

        manual_event = getattr(bridge, "manual_action_event", None)
        if manual_event is not None:
            manual_event.set()

        autoplay = getattr(bridge, "autoplay", None)
        autoplay_lock = getattr(autoplay, "lock", None)
        if autoplay_lock is not None:
            with autoplay_lock:
                autoplay.pending = None
        elif autoplay is not None and hasattr(autoplay, "pending"):
            autoplay.pending = None

        if hasattr(bridge, "pending_action_ack"):
            bridge.pending_action_ack = None
        try:
            if autoplay is not None:
                autoplay.schedule_failsafe(
                    getattr(bridge, "state", {}) or {},
                    reason="RECOVERY_EXHAUSTED",
                )
        except Exception:
            pass

        for task in tuple(getattr(bridge, "schedule_finish_tasks", {}).values()):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        getattr(bridge, "schedule_finish_tasks", {}).clear()

        clear_cc = getattr(bridge, "_clear_cc_queue", None)
        if clear_cc is not None:
            try:
                clear_cc()
            except Exception:
                pass

        self._replay.clear()
        self._hint_watchdog.reset()

    async def _bridge_recovery_loop(self, bridge: Any) -> None:
        """Retry silent-backend recovery serially, then escalate exactly once."""

        try:
            while not self._closed:
                await self.recovery_requested.wait()
                if self._closed:
                    return

                recovered = False
                last_error = ""
                attempts = self.recovery_max_attempts

                for attempt in range(1, attempts + 1):
                    if self._closed:
                        return
                    self._recovery_attempt = attempt
                    # ``recover_bridge`` checks this bit itself.  A failed
                    # generation may clear it, so re-arm it before every retry.
                    self.recovery_requested.set()
                    try:
                        recovered = bool(await self.recover_bridge(bridge))
                        if recovered:
                            last_error = ""
                            break
                        last_error = "recover_bridge returned false"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        recovered = False
                        last_error = f"{type(exc).__name__}: {exc}"
                        self._recovery_in_progress = False

                    if attempt < attempts:
                        delay = min(
                            self.recovery_backoff_max,
                            self.recovery_backoff_base * (2 ** (attempt - 1)),
                        )
                        self._record_backend_status(
                            "RECOVERING",
                            f"backend recovery retry {attempt}/{attempts}: {last_error}",
                            health="yellow",
                        )
                        self.logger(
                            "RECOVERY",
                            f"attempt {attempt}/{attempts} failed: {last_error}",
                        )
                        if delay > 0.0:
                            await asyncio.sleep(delay)

                if recovered:
                    self._recovery_attempt = 0
                    self.recovery_requested.clear()
                    # Real ``recover_bridge`` already resets this while recycling;
                    # this also makes test/future alternative recovery functions
                    # obey the same invariant.
                    self._hint_watchdog.recovery_pending = False
                    continue

                reason = f"backend recovery exhausted after {attempts} attempts"
                if last_error:
                    reason += f": {last_error}"

                self._recovery_attempt = 0
                self._recovery_in_progress = False
                self.recovery_requested.clear()
                self._discard_actions_for_recovery_exhaustion(bridge)
                self._record_backend_status("ERROR", reason, health="red")
                self.logger("RECOVERY", reason)

                callback = self._recovery_exhausted_callback
                if callback is not None and not self._closed:
                    try:
                        result = callback(reason)
                        if inspect.isawaitable(result):
                            await result
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.logger(
                            "RECOVERY",
                            "recovery exhaustion callback failed: "
                            f"{type(exc).__name__}: {exc}",
                        )
        except asyncio.CancelledError:
            pass


    async def recycle_backend_session(self, reason: str) -> None:
        """Retire one poisoned gRPC/local generation without changing Coin state."""

        async with self._recycle_lock:
            self._recovery_in_progress = True
            self._record_backend_status("RECOVERING", reason, health="red")
            self._accept_gate.clear()
            try:
                writer = self._active_client
                if writer is not None and not writer.is_closing():
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                await asyncio.wait_for(self._session_idle.wait(), timeout=5.0)
                # An unresolved hint must never be replayed into the fresh stream.
                self._replay.clear()
                self._hint_watchdog.reset()
                self._reseed_state = "waiting"
                # The retry loop owns this latch. One recycle attempt is not the
                # final recovery outcome; keep it set until reconnect succeeds
                # or the bounded recovery budget is exhausted.
                self.backend_ready.clear()
                self.backend_login_ok = None
                self.backend_error = ""
            finally:
                self._accept_gate.set()

    async def recover_bridge(self, bridge: Any) -> bool:
        """Finish/leave the poisoned session, reconnect, then replay current table.

        The bridge's logical Coin/table/hand model is deliberately retained.  A
        fresh local TCP generation invokes its existing wire resync path, which
        replays UserLogin, EnterRoom and (when seated) SitDown for this table only.
        """

        async with self._bridge_recovery_lock:
            if not (self.recovery_requested.is_set() or self._hint_watchdog.recovery_pending):
                return False

            async def perform() -> None:
                self._recovery_in_progress = True
                self._record_backend_status(
                    "RECOVERING", "silent backend cleanup: Finish -> StandUp -> Leave",
                    health="red",
                )
                self.logger("RECOVERY", "silent backend cleanup started")

                manual_event = getattr(bridge, "manual_action_event", None)
                if manual_event is not None:
                    manual_event.set()
                autoplay = getattr(bridge, "autoplay", None)
                autoplay_lock = getattr(autoplay, "lock", None)
                if autoplay_lock is not None:
                    with autoplay_lock:
                        autoplay.pending = None
                if hasattr(bridge, "pending_action_ack"):
                    bridge.pending_action_ack = None
                for task in tuple(getattr(bridge, "schedule_finish_tasks", {}).values()):
                    if task is not asyncio.current_task() and not task.done():
                        task.cancel()
                getattr(bridge, "schedule_finish_tasks", {}).clear()
                clear_cc = getattr(bridge, "_clear_cc_queue", None)
                if clear_cc is not None:
                    clear_cc()

                pending = getattr(bridge, "state", {}).get("_pending_finish_hint")
                if pending is not None:
                    await bridge.finish_hint(pending)

                from . import coin_ppp_bridge as core

                if (
                    bool(getattr(bridge, "context_active", False))
                    and bool(getattr(bridge, "hero_sitting", False))
                    and getattr(bridge, "state", {}).get("hero_seat")
                ):
                    await bridge.eye_send_cmd("pb.StandUpREQ", b"")
                leave_before = self.forwarded_command_count("pb.LeaveRoomREQ")
                if bool(getattr(bridge, "context_active", False)):
                    leave = core.p_int(1, 0) + core.p_int(2, 0) + core.p_int(3, 0)
                    await bridge.eye_send_cmd("pb.LeaveRoomREQ", leave)
                    # forward_local is FIFO: observing Leave also proves the preceding
                    # Finish and StandUp were handed to the old gRPC stream.
                    await self.wait_command_forwarded("pb.LeaveRoomREQ", leave_before)

                await self.recycle_backend_session(
                    "reconnecting after two consecutive SCAction timeouts"
                )
                generation = int(getattr(bridge, "eye_generation", 0) or 0)
                writer = getattr(bridge, "eye_w", None)
                invalidate = getattr(bridge, "_invalidate_eye_generation", None)
                if invalidate is not None and generation:
                    invalidate(generation, writer)
                await bridge.ensure_eye()
                await self.wait_backend_ready(self.login_timeout)
                self._recovery_in_progress = False
                self._record_backend_status(
                    "WARNING", "backend recovered; waiting for next full hand",
                    health="yellow",
                )
                self.logger("RECOVERY", "backend reconnected; current table re-admitted")

            try:
                hint_lock = getattr(bridge, "hint_lock", None)
                if hint_lock is None:
                    await perform()
                else:
                    async with hint_lock:
                        await perform()
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._recovery_in_progress = False
                self.recovery_requested.clear()
                self._record_backend_status(
                    "ERROR",
                    f"silent backend recovery failed: {type(exc).__name__}: {exc}",
                    health="red",
                )
                self.logger(
                    "RECOVERY", f"failed: {type(exc).__name__}: {exc}"
                )
                return False

    async def start(self) -> tuple[str, int]:
        if self.server:
            return self.address
        # Validate the configured slot before accepting the bridge connection.  The
        # credential value itself is deliberately neither retained here nor logged.
        if not self.slot.credential_file.is_file():
            raise FileNotFoundError(f"PokerEYE credential file not found: {self.slot.credential_file}")
        self.server = await asyncio.start_server(self._accept, self.listen_host, 0, backlog=4)
        sockets = self.server.sockets or []
        if not sockets:
            raise RuntimeError("direct backend proxy did not acquire a listening socket")
        self.port = int(sockets[0].getsockname()[1])
        self.logger("DIRECT", f"local adapter ready {self.listen_host}:{self.port}; account={self.slot.account_id}")
        return self.address

    async def close(self) -> None:
        self._closed = True
        self._accept_gate.set()
        recovery_task = self._recovery_task
        self._recovery_task = None
        if (
            recovery_task is not None
            and recovery_task is not asyncio.current_task()
            and not recovery_task.done()
        ):
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = list(self._sessions)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sessions.clear()

    async def wait_backend_ready(self, timeout: Optional[float] = None) -> None:
        """Wait until SCLogin explicitly accepts this account stream."""
        await asyncio.wait_for(
            self.backend_ready.wait(),
            timeout=self.login_timeout if timeout is None else float(timeout),
        )
        if self.backend_login_ok is False:
            raise BackendLoginRejected(
                self.backend_error or "PokerEYE backend login rejected"
            )
        if self.backend_login_ok is not True:
            raise RuntimeError(
                self.backend_error or "PokerEYE backend transport failed before SCLogin"
            )

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._sessions.add(task)
        owns_slot = False
        try:
            await self._accept_gate.wait()
            if self._closed:
                return
            if self._active_client is not None and not self._active_client.is_closing():
                self.logger("DIRECT", "rejected second local bridge client for this account slot")
                return
            self._active_client = writer
            owns_slot = True
            self._session_idle.clear()
            await self._session(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.backend_error = f"{type(exc).__name__}: {exc}"
            # None means infrastructure/transport failure before an authoritative
            # SCLogin result. Do not turn missing grpc/network/TLS into INVALID.
            self.backend_login_ok = None
            self.backend_ready.set()
            self._record_backend_status("ERROR", self.backend_error)
            self.logger("DIRECT", f"backend slot disconnected: {type(exc).__name__}: {exc}")
        finally:
            try:
                if not writer.is_closing():
                    writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if task:
                self._sessions.discard(task)
            if owns_slot and self._active_client is writer:
                self._active_client = None
                self._session_idle.set()

    async def _session(self, local_reader: asyncio.StreamReader, local_writer: asyncio.StreamWriter) -> None:
        try:
            import grpc
        except ImportError as exc:
            raise RuntimeError("grpcio is required for --direct-backend") from exc

        # Every replacement stream must prove a fresh billing snapshot. Do not
        # keep displaying a previous connection's balance during reconnect.
        self._record_backend_fuel(reason_code="FUEL_PENDING")

        if self._recovery_in_progress:
            self._record_backend_status(
                "RECOVERING", "connecting replacement backend stream", health="red"
            )
        else:
            self._record_backend_status("PENDING", "connecting")
        target = f"{self.slot.host}:{self.slot.port}"
        metadata = (("x-android-id", self.slot.android_id), ("x-host", target))
        options = (
            ("grpc.max_receive_message_length", 32 * 1024 * 1024),
            ("grpc.max_send_message_length", 32 * 1024 * 1024),
        )
        channel = grpc.aio.secure_channel(target, grpc.ssl_channel_credentials(), options=options)
        stream: Optional[ResilientEyeStream] = None
        backend_guard: Optional[asyncio.Task[Any]] = None
        local_forwarder: Optional[asyncio.Task[Any]] = None
        pinger: Optional[asyncio.Task[Any]] = None
        hint_watchdog: Optional[asyncio.Task[Any]] = None
        sent_packets = 0

        async def send_local(tag: str, data: Any) -> None:
            if tag == "game_mode":
                frame = hook_game_mode_frame(data if isinstance(data, dict) else {"workMode": data})
                local_writer.write(_lp_pack(frame))
                await local_writer.drain()
                return
            value = data if isinstance(data, str) else compact_json(data)
            local_writer.write(_lp_pack({
                "tag": tag,
                "msg": "",
                "data": value,
                "packageName": "com.lein.pppoker.android",
            }))
            await local_writer.drain()

        async def on_backend_message(message_type: str, body: Any) -> None:
            if message_type == "SCLogin":
                accepted = bool(body.get("loginSuccess")) if isinstance(body, dict) else False
                self.backend_login_ok = accepted
                self.backend_error = "" if accepted else _sc_login_rejection_detail(body)
                self._recovery_in_progress = False
                self._record_backend_status(
                    "NORMAL" if accepted else "ERROR",
                    "login accepted" if accepted else self.backend_error,
                )
                self.backend_ready.set()
                await send_local("game_mode", body if isinstance(body, dict) else {})
                if accepted and stream is not None:
                    asyncio.create_task(
                        stream.send_wire(
                            settings_envelope(
                                game_type=hook_game_type(),
                                working_mode=hook_working_mode(),
                            )
                        ),
                        name="eye-cs-settings",
                    )
                self.logger(
                    "DIRECT",
                    "backend login accepted" if accepted else self.backend_error,
                )
            elif message_type == "SCAction" and isinstance(body, dict):
                # Preserve the backend JSON contract exactly.  Existing CC
                # scheduling/ACK logic remains the sole action executor.
                accepted, transitions = self._hint_watchdog.observe_action(time.monotonic())
                self._apply_hint_transitions(transitions)
                if accepted:
                    await send_local("cc", body)
                    self.logger(
                        "DIRECT",
                        "SCAction "
                        f"type={body.get('type')} subtype={body.get('subtype')} "
                        f"amount={body.get('amount')} delay={body.get('delay')}ms "
                        f"lifetime={body.get('lifetime')}ms",
                    )
                else:
                    self.logger(
                        "DIRECT",
                        "ignored stale SCAction without one live outstanding RoundHint",
                    )
            elif message_type == "SCSettingsBillingRates":
                fuel = self._record_backend_fuel(body)
                quantity = "-" if fuel.quantity is None else f"{fuel.quantity:.1f} F"
                rate = "-" if fuel.rate_per_hand is None else f"{fuel.rate_per_hand:.2f} F/hand"
                self.logger(
                    "FUEL",
                    f"{fuel.reason_code} quantity={quantity} rate={rate}",
                )
                await send_local("game_mode", body if isinstance(body, dict) else {})
            elif message_type == "SCStatus" and isinstance(body, dict):
                status = str(body.get("status") or "")
                message = str(body.get("message") or "")
                hash_value = str(body.get("hash") or "")
                snapshot = self._record_backend_status(status, message, hash_value)
                message_word = message.upper()
                if message_word == "INSUFFICIENT_FUEL_BALANCE":
                    self._record_backend_fuel(
                        reason_code="FUEL_EXHAUSTED", preserve_values=True
                    )
                elif message_word in {"AUTH_FAILED", "AUTHKEY_CHANGED"}:
                    self._record_backend_fuel(
                        reason_code="FUEL_AUTH_CRITICAL", preserve_values=True
                    )
                elif message_word == "YOUR_FUEL_WILL_RUN_OUT_SOON":
                    self._record_backend_fuel(
                        reason_code="FUEL_BACKEND_WARNING", preserve_values=True
                    )
                if message.upper() == "PID_CHANGED":
                    self.logger("DIRECT", "PID_CHANGED received; normal playing-id assignment")
                    return
                await send_local(
                    "backend_status",
                    {"status": snapshot.status, "message": snapshot.message, "hash": snapshot.hash},
                )
                if snapshot.health != "green" or snapshot.message:
                    self.logger("BACKEND", snapshot.reason.removeprefix("backend "))

        async def on_reconnect(directive: ReconnectDirective, replay_count: int) -> None:
            # PID_CHANGED is the normal pid=0 -> actual-player-id transition.
            # Do not surface it to the bridge as a backend error.
            detail = directive.details or "backend requested reconnect"
            if detail.upper() == "PID_CHANGED":
                self._record_backend_status("NORMAL", "PID_CHANGED")
            else:
                self._record_backend_status("WARNING", detail)
            self.logger(
                "DIRECT",
                f"{detail}: reconnect in {directive.interval_ms}ms; replay={replay_count}; local bridge kept open",
            )

        async def forward_local() -> None:
            nonlocal sent_packets
            while not self._closed:
                raw = await _lp_read(local_reader)
                if raw is None:
                    return
                try:
                    outer = json.loads(raw)
                except json.JSONDecodeError:
                    self.logger("DIRECT", "ignored malformed local JSON frame")
                    continue
                if not isinstance(outer, dict) or outer.get("tag") != "traffic":
                    # ``broadcast`` is a UI-only EYE feature and has no backend
                    # protocol equivalent.  It is intentionally not fabricated.
                    continue
                message = outer.get("msg")
                if not isinstance(message, str):
                    continue
                try:
                    packet = normalize_packet(json.loads(message))
                except Exception as exc:
                    self.logger("DIRECT", f"ignored malformed traffic frame: {exc}")
                    continue
                command = str(packet.get("cmd") or "")
                if not self._allow_during_hand_reseed(command):
                    if command == "pb.RoundHintMultipleTableRSP":
                        self.logger(
                            "RECOVERY",
                            "held current-hand RoundHint until a full hand preamble",
                        )
                    continue
                if command == "pb.RoundHintMultipleTableRSP":
                    forward, transitions = self._hint_watchdog.observe_hint(time.monotonic())
                    self._apply_hint_transitions(transitions)
                    if not forward:
                        self.logger(
                            "DIRECT",
                            "held RoundHint while prior backend hint is unresolved",
                        )
                        continue
                elif command == "pb.FinishRoundHintRSP":
                    self._apply_hint_transitions(self._hint_watchdog.observe_finish())
                if (
                    self._hint_watchdog.recovery_pending
                    and command not in {
                        "pb.FinishRoundHintRSP",
                        "pb.StandUpREQ",
                        "pb.LeaveRoomREQ",
                        "pb.HeartBeatREQ",
                        "pb.HeartBeatRSP",
                    }
                ):
                    # Freeze the poisoned generation until explicit cleanup; the
                    # logical bridge keeps observing Coin and is replayed later.
                    continue
                packet["timestamp"] = int(time.time() * 1000)
                packet["seq"] = self._next_seq
                self._next_seq += 1
                assert stream is not None
                await stream.send_packet(packet)
                sent_packets += 1
                self._forwarded_commands[command] = self.forwarded_command_count(command) + 1

        async def ping_loop() -> None:
            while not self._closed:
                await asyncio.sleep(self.ping_interval)
                assert stream is not None
                await stream.send_wire(ping_envelope())

        async def hint_watchdog_loop() -> None:
            while not self._closed:
                await asyncio.sleep(0.1)
                self._apply_hint_transitions(
                    self._hint_watchdog.poll(time.monotonic())
                )

        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=self.connect_timeout)
            method = channel.stream_stream(
                GRPC_METHOD,
                request_serializer=lambda value: value,
                response_deserializer=lambda value: value,
            )
            credential = self.slot.credential()
            login = LoginParams(
                device_id=self.slot.account_id,
                password=credential,
                version=self.slot.room_version,
                lang="en",
                bundle_identifier=self.slot.bundle_id,
                device=self.slot.device,
                serial=self.slot.android_id,
                reset_seq=self._first_login,
                geo="",
            )
            initial_login_wire = login_envelope(login)
            reconnect_login_wire = login_envelope(replace(login, reset_seq=False))
            credential = None
            login = None

            stream = ResilientEyeStream(
                lambda: method(metadata=metadata, compression=grpc.Compression.Gzip, wait_for_ready=True),
                lambda reset_seq: initial_login_wire if reset_seq else reconnect_login_wire,
                initial_reset_seq=self._first_login,
                login_timeout=self.login_timeout,
                replay=self._replay,
                on_message=on_backend_message,
                on_reconnect=on_reconnect,
            )
            await stream.start()
            self._first_login = False

            backend_guard = asyncio.create_task(stream.wait_terminated())
            local_forwarder = asyncio.create_task(forward_local())
            pinger = asyncio.create_task(ping_loop())
            hint_watchdog = asyncio.create_task(hint_watchdog_loop())
            done, _pending = await asyncio.wait(
                {backend_guard, local_forwarder, pinger, hint_watchdog},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for finished in done:
                error = finished.exception()
                if error:
                    raise error
        finally:
            for task in (local_forwarder, pinger, hint_watchdog, backend_guard):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (local_forwarder, pinger, hint_watchdog, backend_guard) if task),
                return_exceptions=True,
            )
            if stream is not None:
                await stream.close()
            await channel.close()
            self.logger("DIRECT", f"backend session closed packets={sent_packets}")


def protocol_selftest() -> None:
    assert _work_mode({"workMode": "AUTO"}) == "auto"
    assert _work_mode({"billing": {"workingMode": 1}}) == "auto"
    assert _work_mode({"workingMode": "VIP"}) == "vip"
    assert _work_mode({"workMode": 0}) == "man"
    assert hook_game_mode("man") == "auto"
    assert hook_game_mode_frame({"workMode": 0})["data"] == "auto"
    assert hook_game_mode_frame({"workMode": "MAN"})["data"] == "auto"
    assert parse_backend_fuel({"fuelQty": 7169.6, "fuelRate": 0.25}) == (
        7169.6, 0.25, "FUEL_AVAILABLE"
    )
    assert parse_backend_fuel({"fuelQty": "7169.6", "fuelRate": 0.25}) == (
        None, None, "FUEL_INVALID_QUANTITY"
    )
    value = {"tag": "cc", "data": compact_json({"type": "RAISE", "amount": 1.25})}
    wire = _lp_pack(value)
    assert struct.unpack(">I", wire[:4])[0] == len(wire) - 4
    assert json.loads(wire[4:]) == value
    reconnect_protocol_selftest()
    asyncio.run(resilient_stream_selftest())


if __name__ == "__main__":
    protocol_selftest()
    print("DIRECT BACKEND PROXY SELFTEST PASS")
