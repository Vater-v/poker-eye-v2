"""Production v2 runtime: native device ingress + verified v6 multitable router.

Runtime invariants:
- one authenticated TCP connection per physical Android process/device;
- passive Coin frames are one-way telemetry: Trainer sends no ``forward`` reply;
- logical isolation is v6 ``DeviceIngressRouter``: SmartFox room/table before state;
- one verified backend account per admitted real table (``game.game_init`` edge);
- device-level v6 ``ActionArbiter`` serializes actions across tables;
- transport disconnect does not tear down active table/backend sessions immediately.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import hashlib
import hmac
import json
import http.server
import os
import queue
import socket
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .logging import SessionLogger
from .coin_capture import RawCoinCaptureManager
from .v6router.accounts import AccountPool
from .v6router.action_arbiter import ActionArbiter
from .v6router.automation import AutomationStore
from .v6router.router import (
    DeviceIngressRouter,
    LiveTableSession,
    LiveTableSessionFactory,
    RouterObservation,
    _decode_event,
)

PROTOCOL_VERSION = 2
TRANSPORT_MAX_FRAME = 20_000_000
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 19037
DEFAULT_PUBLIC_HOST = "37.192.228.101"
MAX_DEVICES = 30
MAX_TABLES_PER_DEVICE = 8
MAX_TOTAL_TABLES = MAX_DEVICES * MAX_TABLES_PER_DEVICE
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 19101

NATIVE_MAGIC = b"HMN1"
NATIVE_VERSION = 1
NATIVE_WS_FRAME = 0x01
NATIVE_HEARTBEAT = 0x02
NATIVE_ACTION_RESULT = 0x03
NATIVE_COMMAND = 0x81
NATIVE_HEARTBEAT_ACK = 0x82

BUILD_ID_FILE = Path(__file__).resolve().parents[1] / "BUILD_ID"
try:
    BUILD_ID = BUILD_ID_FILE.read_text(encoding="utf-8").strip() or "dev-unversioned"
except OSError:
    BUILD_ID = "dev-unversioned"


def _short(value: Any, limit: int = 220) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _read_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("peer disconnected")
        buf.extend(chunk)
    return bytes(buf)


def recv_raw_frame(sock: socket.socket) -> bytes:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if not 0 < size <= TRANSPORT_MAX_FRAME:
        raise ValueError(f"invalid transport frame size {size}")
    return _read_exact(sock, size)


def send_raw_frame(sock: socket.socket, raw: bytes) -> None:
    if not raw or len(raw) > TRANSPORT_MAX_FRAME:
        raise ValueError("transport reply too large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def recv_json_frame(sock: socket.socket) -> Dict[str, Any]:
    value = json.loads(recv_raw_frame(sock).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("transport frame must be JSON object")
    return value


def send_json_frame(sock: socket.socket, value: Dict[str, Any]) -> None:
    send_raw_frame(
        sock,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def direct_proof(secret: bytes, device_id: str, transport_id: str) -> str:
    material = f"{device_id}|{transport_id}|trainer|{PROTOCOL_VERSION}".encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def parse_native_message(raw: bytes) -> tuple[str, Dict[str, Any]]:
    if len(raw) < 8 or raw[:4] != NATIVE_MAGIC:
        raise ValueError("bad native magic")
    typ = raw[4]
    version = raw[5]
    if version != NATIVE_VERSION:
        raise ValueError(f"bad native version {version}")

    if typ == NATIVE_HEARTBEAT:
        if len(raw) != 16:
            raise ValueError("bad native heartbeat")
        return "heartbeat", {"sequence": struct.unpack_from("!Q", raw, 8)[0]}

    if typ == NATIVE_ACTION_RESULT:
        if len(raw) < 16:
            raise ValueError("bad native action result")
        flags = struct.unpack_from("!H", raw, 6)[0]
        ws_u32 = struct.unpack_from("!I", raw, 8)[0]
        token_len = struct.unpack_from("!H", raw, 12)[0]
        reason_code = raw[14]
        if len(raw) != 16 + token_len:
            raise ValueError("bad native action result length")
        token = raw[16:].decode("utf-8", errors="replace")
        return "action_result", {
            "ok": bool(flags & 0x01),
            "ws_id": f"{ws_u32:08x}",
            "_ws_u32": ws_u32,
            "token": token,
            "reason_code": int(reason_code),
        }

    if typ != NATIVE_WS_FRAME or len(raw) < 28:
        raise ValueError(f"bad native message type {typ}")

    seq = struct.unpack_from("!Q", raw, 8)[0]
    ws_u32 = struct.unpack_from("!I", raw, 16)[0]
    direction = raw[20]
    payload_len = struct.unpack_from("!I", raw, 24)[0]
    if payload_len != len(raw) - 28:
        raise ValueError("bad native payload length")
    payload = raw[28:]
    return "ws_message", {
        "type": "ws_message",
        "kind": "ws_message",
        "v": 6,
        "async": True,
        "schedule_send": True,
        "id": str(seq),
        "direction": "out" if direction else "in",
        "text": False,
        "url": "",
        "ws_id": f"{ws_u32:08x}",
        "_ws_u32": ws_u32,
        "_raw": payload,
    }


def native_heartbeat_ack(sequence: int) -> bytes:
    return (
        NATIVE_MAGIC
        + bytes([NATIVE_HEARTBEAT_ACK, NATIVE_VERSION])
        + b"\x00\x00"
        + struct.pack("!Q", int(sequence))
    )


def native_command(decision: Dict[str, Any], fallback_ws_u32: int) -> Optional[bytes]:
    cancel = str(decision.get("cancel_schedule") or "")
    action = str(decision.get("action") or "forward")
    send_action = action in {"schedule_send", "replace"}
    if not cancel and not send_action:
        return None

    ws_raw = decision.get("_ws_u32")
    if ws_raw is None:
        ws_text = str(decision.get("ws_id") or "")
        try:
            ws_u32 = int(ws_text, 16) if ws_text else int(fallback_ws_u32)
        except ValueError:
            ws_u32 = int(fallback_ws_u32)
    else:
        ws_u32 = int(ws_raw)

    delay_ms = int(decision.get("delay_ms") or 0) if action == "schedule_send" else 0
    token = str(decision.get("token") or decision.get("id") or "") if send_action else ""
    payload = b""
    if send_action:
        encoded = str(decision.get("payload_b64") or "")
        if not encoded:
            return None
        payload = base64.b64decode(encoded)

    token_b = token.encode("utf-8")
    cancel_b = cancel.encode("utf-8")
    if len(token_b) > 65535 or len(cancel_b) > 65535:
        raise ValueError("native action token too long")
    flags = (0x01 if cancel_b else 0) | (0x02 if payload else 0)
    header = (
        NATIVE_MAGIC
        + bytes([NATIVE_COMMAND, NATIVE_VERSION])
        + struct.pack(
            "!HIIHHI",
            flags,
            ws_u32 & 0xFFFFFFFF,
            max(0, min(delay_ms, 15000)),
            len(token_b),
            len(cancel_b),
            len(payload),
        )
    )
    return header + token_b + cancel_b + payload



class TrafficMeter:
    """Ingress-only traffic proof that can never decode or block Coin frames.

    This meter intentionally does *not* call ``_decode_event``.  It runs in the
    socket reader thread, before the production router, so any parser/backend work
    here can stall the entire HMN1 connection after the first frame.  Rich decoded
    diagnostics belong to the router observations; this class only proves that raw
    frames keep arriving from Android.
    """

    def __init__(self, emit=None, *, window_seconds: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._emit = emit
        self._window_seconds = max(0.25, float(window_seconds))

    def observe(self, device_id: str, event: Dict[str, Any]) -> None:
        raw = event.get("_raw")
        size = len(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else 0
        direction = str(event.get("direction") or "?").lower()
        now = time.monotonic()
        payload_first = None
        if size:
            try:
                payload_first = int(raw[0])
            except Exception:
                payload_first = None

        emitted = None
        with self._lock:
            row = self._rows.setdefault(
                device_id,
                {
                    "last_print": now,
                    "frames": 0,
                    "bytes": 0,
                    "ws": set(),
                    "directions": collections.Counter(),
                    "last_first": None,
                },
            )
            row["frames"] += 1
            row["bytes"] += size
            if event.get("ws_id"):
                row["ws"].add(str(event.get("ws_id")))
            row["directions"][direction] += 1
            if payload_first is not None:
                row["last_first"] = payload_first

            elapsed = now - row["last_print"]
            if elapsed >= self._window_seconds:
                emitted = {
                    "device_id": device_id,
                    "frames": int(row["frames"]),
                    "bytes": int(row["bytes"]),
                    "ws": len(row["ws"]),
                    "incoming": int(row["directions"].get("in", 0)),
                    "outgoing": int(row["directions"].get("out", 0)),
                    "last_first": row["last_first"],
                    "window_s": elapsed,
                }
                row["last_print"] = now
                row["frames"] = 0
                row["bytes"] = 0
                row["ws"].clear()
                row["directions"].clear()

        # Never call logger code while holding the meter lock.
        if emitted is not None and self._emit is not None:
            try:
                self._emit(**emitted)
            except Exception:
                pass


class OperatorConsole:
    """Minimal Russian operator view; all technical detail stays in run logs."""

    ACTION_RU = {
        "CHECK": "CHECK",
        "CALL": "CALL",
        "RAISE": "RAISE",
        "FOLD": "FOLD",
    }
    CANCEL_RU = {
        "manual-action": "сделан ручной ход",
        "turn-advanced-before-due": "ход уже сменился",
        "game.reset_data": "раздача сброшена",
        "game.quit_table": "выход со стола",
        "game.pre_hand_start_info": "началась новая раздача",
        "lobby.join_game_table": "переход на другой стол",
        "lobby.join_game": "переход на другой стол",
    }
    NATIVE_FAIL_RU = {
        1: "целевой игровой сокет уже закрыт",
        2: "не удалось подготовить пакет в приложении",
        3: "CoinPoker отклонил локальную отправку",
        4: "ошибка JNI при локальной отправке",
    }
    VALUE_RU = {
        "ACE": "A", "KING": "K", "QUEEN": "Q", "JACK": "J", "TEN": "10",
        "NINE": "9", "EIGHT": "8", "SEVEN": "7", "SIX": "6", "FIVE": "5",
        "FOUR": "4", "THREE": "3", "TWO": "2",
    }
    SUIT_RU = {"SPADES": "♠", "HEARTS": "♥", "DIAMONDS": "♦", "CLUBS": "♣"}

    def __init__(
        self, logger: SessionLogger, account_count: int, *, accounts_dynamic: bool = False
    ) -> None:
        self.logger = logger
        self.account_count = int(account_count)
        self.accounts_dynamic = bool(accounts_dynamic)
        self._lock = threading.RLock()
        self._device_no: dict[str, int] = {}
        self._device_online: set[str] = set()
        self._device_seen: set[str] = set()
        self._tables: dict[tuple[str, int], int] = {}
        self._next_table: dict[str, int] = collections.defaultdict(int)
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._latest_by_table: dict[tuple[str, int], dict[str, Any]] = {}
        self._seated: set[tuple[str, int]] = set()
        self._last_cards_hand: set[tuple[str, int, str]] = set()
        self._backend_state: dict[tuple[str, int], str] = {}
        self._device_down_generation: dict[str, int] = collections.defaultdict(int)
        self._device_down_announced: set[str] = set()
        self._device_down_grace_seconds = 5.0
        self._hands_table: dict[tuple[str, int], int] = collections.defaultdict(int)
        self._hands_device: dict[str, int] = collections.defaultdict(int)
        self._hands_by_type_device: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
        self._hands_total = 0
        self._completed_hands: set[tuple[str, int, str]] = set()
        self._table_account: dict[tuple[str, int], str] = {}
        self._device_nick: dict[str, str] = {}

    def _clock(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _line(self, text: str, event: str, *, severity: str = "INFO", **fields: Any) -> None:
        line = f"{self._clock()}  {text}"
        print(line, flush=True)
        self.logger.emit(event, severity=severity, message=line, operator=True, **fields)

    def technical(self, event: str, *, severity: str = "INFO", **fields: Any) -> None:
        self.logger.emit(event, severity=severity, **fields)

    def _device(self, device_id: str) -> int:
        with self._lock:
            if device_id not in self._device_no:
                self._device_no[device_id] = len(self._device_no) + 1
            return self._device_no[device_id]

    def _table(self, device_id: str, table_id: int) -> int:
        key = (str(device_id), int(table_id))
        with self._lock:
            existing = self._tables.get(key)
            if existing is not None:
                return int(existing)
            used = {int(number) for (owner, _tid), number in self._tables.items() if owner == str(device_id)}
            number = 1
            while number in used:
                number += 1
            self._tables[key] = number
            return number

    def _label(self, device_id: str, table_id: int) -> str:
        d = self._device(device_id)
        t = self._table(device_id, table_id)
        return f"Стол {t}" if len(self._device_no) <= 1 else f"Устройство {d} · Стол {t}"

    @staticmethod
    def _amount(value: Any) -> str:
        if value is None:
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if abs(number) < 1e-9:
            return ""
        text = f"{number:.8f}".rstrip("0").rstrip(".")
        return f" {text}"

    def _action_text(self, meta: dict[str, Any]) -> str:
        action = self.ACTION_RU.get(str(meta.get("action") or "").upper(), str(meta.get("action") or "ACTION").upper())
        return action + self._amount(meta.get("amount"))

    def _cards(self, cards: Any) -> str:
        if not isinstance(cards, list):
            return str(cards or "?")
        out = []
        for row in cards:
            if not isinstance(row, dict):
                out.append(str(row))
                continue
            value = self.VALUE_RU.get(str(row.get("value") or "").upper(), str(row.get("value") or "?"))
            suit = self.SUIT_RU.get(str(row.get("suit") or "").upper(), str(row.get("suit") or ""))
            out.append(f"{value}{suit}")
        return " ".join(out) or "?"

    @staticmethod
    def _game_type_label(value: Any) -> str:
        text = str(value or "").upper().replace(" ", "")
        aliases = {"PLO": "PLO4", "OMAHA": "PLO4", "HOLDEM": "NLH", "HOLD'EM": "NLH"}
        return aliases.get(text, text or "OTHER")

    def _session_hands_text(self, device_id: str) -> str:
        counter = self._hands_by_type_device.get(str(device_id), collections.Counter())
        order = ("NLH", "PLO4", "PLO5", "PLO6")
        parts = [f"{name}: {int(counter.get(name, 0))}" for name in order]
        extras = sorted((name, count) for name, count in counter.items() if name not in order and count)
        parts.extend(f"{name}: {int(count)}" for name, count in extras)
        return " · ".join(parts)

    def fleet_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows: dict[str, dict[str, Any]] = {}
            for device_id, number in self._device_no.items():
                counter = self._hands_by_type_device.get(device_id) or collections.Counter()
                nick = str(self._device_nick.get(device_id) or "")
                rows[device_id] = {
                    "device_no": int(number),
                    "nick": nick,
                    "hands": int(self._hands_device.get(device_id, 0)),
                    "hands_by_type": {str(name): int(count) for name, count in counter.items() if count},
                    "session_hands": self._session_hands_text(device_id),
                    "online": device_id in self._device_online,
                }
            return rows

    def _reset_device_session_locked(self, device_id: str) -> None:
        device_id = str(device_id)
        self._hands_device[device_id] = 0
        self._hands_by_type_device[device_id].clear()
        self._device_nick.pop(device_id, None)
        for key in [key for key in self._hands_table if key[0] == device_id]:
            self._hands_table.pop(key, None)
        self._completed_hands = {row for row in self._completed_hands if row[0] != device_id}
        self._last_cards_hand = {row for row in self._last_cards_hand if row[0] != device_id}
        self._seated = {row for row in self._seated if row[0] != device_id}
        for mapping in (self._latest_by_table, self._backend_state, self._table_account):
            for key in [key for key in mapping if key[0] == device_id]:
                mapping.pop(key, None)
        for key in [key for key in self._tables if key[0] == device_id]:
            self._tables.pop(key, None)
        self._next_table[device_id] = 0

    def ready(self) -> None:
        account_text = f"{self.account_count}+ · автоподбор" if self.accounts_dynamic else str(self.account_count)
        self._line(
            f"PokerEye готов · build {BUILD_ID} · PokerEYE аккаунтов: {account_text} · запись PCAP: включена",
            "operator.ready",
        )
        self._line("Ожидание устройства и столов…", "operator.waiting")

    def device_up(self, device_id: str) -> None:
        number = self._device(device_id)
        with self._lock:
            was_seen = device_id in self._device_seen
            was_online = device_id in self._device_online
            was_announced_down = device_id in self._device_down_announced
            if was_announced_down:
                self._reset_device_session_locked(device_id)
            self._device_seen.add(device_id)
            self._device_online.add(device_id)
            self._device_down_announced.discard(device_id)
            # Invalidate every pending delayed-offline callback.
            self._device_down_generation[device_id] += 1
        if was_online:
            self.technical(
                "operator.device_up_suppressed",
                device_id=device_id, device_no=number, reason="already-online",
            )
            return
        if was_seen and not was_announced_down:
            # Fast reconnects are transport detail, not an operator event.
            self.technical(
                "operator.device_reconnect_fast",
                device_id=device_id, device_no=number,
            )
            return
        text = (
            f"● Устройство {number}: связь восстановлена"
            if was_announced_down
            else f"● Устройство {number} подключено"
        )
        self._line(text, "operator.device_up", device_id=device_id, device_no=number)

    def device_down(self, device_id: str, reason: str) -> None:
        number = self._device(device_id)
        with self._lock:
            if device_id not in self._device_online:
                self.technical(
                    "operator.device_down_suppressed",
                    severity="WARN", device_id=device_id, reason=reason,
                )
                return
            self._device_online.discard(device_id)
            self._device_down_generation[device_id] += 1
            generation = self._device_down_generation[device_id]

        self.technical(
            "operator.device_down_pending",
            severity="WARN", device_id=device_id, reason=reason,
            grace_seconds=self._device_down_grace_seconds,
        )

        def announce_if_still_down() -> None:
            with self._lock:
                if self._device_down_generation.get(device_id) != generation:
                    return
                if device_id in self._device_online:
                    return
                if device_id in self._device_down_announced:
                    return
                self._device_down_announced.add(device_id)
            session_hands = self._session_hands_text(device_id)
            self._line(
                f"● Устройство {number}: связь потеряна · руки за сессию: {session_hands}",
                "operator.device_down", severity="WARN",
                device_id=device_id, reason=reason, session_hands=session_hands,
            )

        timer = threading.Timer(self._device_down_grace_seconds, announce_if_still_down)
        timer.daemon = True
        timer.start()


    def command_sent(self, device_id: str, meta: dict[str, Any]) -> None:
        if not meta:
            return
        table_id = int(meta.get("table_id") or 0)
        if table_id <= 0:
            return
        token = str(meta.get("token") or "")
        attempt = max(1, int(meta.get("attempt") or 1))
        maximum = max(attempt, int(meta.get("max_attempts") or 3))
        label = self._label(device_id, table_id)
        text = self._action_text(meta)
        with self._lock:
            if token:
                self._pending[(device_id, token)] = dict(meta)
            self._latest_by_table[(device_id, table_id)] = dict(meta)
        if meta.get("forced_exit"):
            # The policy line already describes the evacuation; keep individual
            # successful FOLD/STANDUP/LEAVE sends in technical.log only.
            self.technical(
                "operator.forced_exit_command", device_id=device_id, table_id=table_id,
                action=meta.get("action"), attempt=attempt, token=token,
                forced_exit_reason=meta.get("forced_exit_reason"),
            )
            return
        # Keep the operator view compact: the local native ACK below is the
        # meaningful fact.  Command dispatch intent stays in technical.log.
        self.technical(
            "operator.action_command", device_id=device_id, table_id=table_id,
            action=meta.get("action"), amount=meta.get("amount"), attempt=attempt,
            max_attempts=maximum, token=token,
        )

    def native_result(self, device_id: str, result: dict[str, Any]) -> None:
        token = str(result.get("token") or "")
        with self._lock:
            meta = dict(self._pending.pop((device_id, token), None) or {})
        if not meta:
            self.technical("native.action_result.unmatched", device_id=device_id, **result)
            return
        table_id = int(meta.get("table_id") or 0)
        label = self._label(device_id, table_id)
        text = self._action_text(meta)
        attempt = int(meta.get("attempt") or 1)
        maximum = int(meta.get("max_attempts") or 3)
        if meta.get("forced_exit"):
            stage=str(meta.get("action") or "").upper()
            stage_text={
                "FOLD":"FOLD",
                "STANDUP":"встали со стола",
                "LEAVE":"команда выхода отправлена",
            }.get(stage,stage)
            if result.get("ok"):
                # Initial policy line describes the full evacuation and the final
                # table_close confirms exit. Successful intermediate stages stay technical.
                forced_reason=str(meta.get("forced_exit_reason") or "unsupported")
                self.technical(
                    "operator.forced_exit_stage", device_id=device_id, table_id=table_id,
                    stage=stage, attempt=attempt, token=token, ok=True,
                    forced_exit_reason=forced_reason,
                )
            else:
                reason=self.NATIVE_FAIL_RU.get(int(result.get("reason_code") or 0),"локальная отправка не удалась")
                suffix=" — повторим" if attempt < maximum else ""
                self._line(
                    f"{label}: ✗ {stage_text} · {reason}{suffix}",
                    "operator.unsupported_exit_failed", severity="WARN", device_id=device_id, table_id=table_id,
                    stage=stage, attempt=attempt, token=token, reason_code=result.get("reason_code"),
                )
            return
        if result.get("ok"):
            self._line(
                f"{label}: ✓ {text} отправлено · попытка {attempt}/{maximum}",
                "operator.action_sent", device_id=device_id, table_id=table_id,
                action=meta.get("action"), amount=meta.get("amount"), attempt=attempt, token=token,
            )
        else:
            reason = self.NATIVE_FAIL_RU.get(int(result.get("reason_code") or 0), "локальная отправка не удалась")
            self._line(
                f"{label}: ✗ {text} не отправлен · попытка {attempt}/{maximum} · {reason}",
                "operator.action_send_failed", severity="WARN", device_id=device_id, table_id=table_id,
                action=meta.get("action"), amount=meta.get("amount"), attempt=attempt, token=token,
                reason_code=result.get("reason_code"),
            )

    def observation(self, observation: RouterObservation) -> None:
        kind = observation.kind
        device = observation.device_id
        table = int(observation.table_id or 0)
        detail = dict(observation.detail or {})
        if table > 0:
            label = self._label(device, table)
        else:
            label = f"Устройство {self._device(device)}"

        if kind == "account_connecting" and table > 0:
            if observation.account_id:
                self._table_account[(device, table)] = str(observation.account_id)
            stage = str(detail.get("login_stage") or "runtime")
            suffix = " · проверка после регистрации" if stage == "post_register_verify" else ""
            self._line(
                f"{label}: PokerEYE {observation.account_id or 'аккаунт'} — подключение{suffix}",
                "operator.eye_connecting",
                device_id=device, table_id=table, account_id=observation.account_id,
                login_stage=stage,
            )
        elif kind == "account_waiting" and table > 0:
            self._line(
                f"{label}: слотов PokerEYE не хватает — ждём освобождения и логинимся снова",
                "operator.eye_slot_wait", severity="WARN",
                device_id=device, table_id=table,
            )
        elif kind == "account_registering" and table > 0:
            if str(detail.get("source") or "") == "panel":
                self._line(
                    f"{label}: слотов нет — регистрируем новый PokerEYE аккаунт",
                    "operator.eye_account_registering", severity="WARN",
                    device_id=device, table_id=table,
                )
            else:
                self._line(
                    f"{label}: логин отклонён — пробуем зарегистрировать {observation.account_id or 'аккаунт'}",
                    "operator.eye_account_registering", severity="WARN",
                    device_id=device, table_id=table, account_id=observation.account_id,
                )
        elif kind == "account_registered" and table > 0:
            if str(detail.get("source") or "") == "panel":
                self._line(
                    f"{label}: зарегистрирован {observation.account_id} — логинимся",
                    "operator.eye_account_registered",
                    device_id=device, table_id=table, account_id=observation.account_id,
                )
            else:
                self._line(
                    f"{label}: регистрация принята — проверяем обычный слот",
                    "operator.eye_account_registered",
                    device_id=device, table_id=table, account_id=observation.account_id,
                )
        elif kind == "account_registration_failed" and table > 0:
            self._line(
                f"{label}: регистрация не принята — ищем следующий аккаунт",
                "operator.eye_account_registration_failed", severity="WARN",
                device_id=device, table_id=table, account_id=observation.account_id,
            )
        elif kind == "account_leased" and table > 0:
            self._backend_state[(device, table)] = "green"
            if observation.account_id:
                self._table_account[(device, table)] = str(observation.account_id)
            self._line(f"◆ {label}: PokerEYE готов", "operator.table_ready", device_id=device, table_id=table)
        elif kind in {"account_invalid", "account_quarantined"} and table > 0:
            reject = _short(detail.get("backend_reject") or observation.reason, 180)
            suffix = f" · {reject}" if reject else ""
            self._line(f"{label}: PokerEYE аккаунт недоступен — ищем другой{suffix}", "operator.eye_account_retry", severity="WARN", device_id=device, table_id=table)
        elif kind == "warning" and table > 0 and detail.get("startup_attempt"):
            attempt=int(detail.get("startup_attempt") or 1); maximum=int(detail.get("startup_attempt_limit") or 3)
            self._line(f"{label}: подключение PokerEYE — попытка {attempt}/{maximum}", "operator.eye_start_retry", severity="WARN", device_id=device, table_id=table, attempt=attempt, max_attempts=maximum)
        elif kind == "table_restart" and table > 0:
            self._line(
                f"{label}: перезапускаем PokerEYE backend",
                "operator.table_restart", severity="WARN",
                device_id=device, table_id=table,
            )
        elif kind == "table_start_failed" and table > 0:
            self._line(
                f"{label}: PokerEYE не поднялся — слот освобождён · {_short(observation.reason, 100)}",
                "operator.table_start_failed", severity="ERROR",
                device_id=device, table_id=table,
            )
        elif kind == "table_update" and table > 0:
            key=(device, table)
            health=str(detail.get("backend_health") or "").lower()
            message=_short(observation.backend_message or detail.get("backend_message") or observation.reason, 100)
            code=message.upper().strip()
            # Idle-table backend watchdog noise is useful technically but is not an
            # operator error and was seen in the supplied NLB/PLO6 run between hands.
            if "NO_TRAFFIC_FROM_ROOM" in code:
                self.technical("operator.eye_idle_suppressed", device_id=device, table_id=table, backend_message=message)
            else:
                previous=self._backend_state.get(key, "")
                if health and health != previous:
                    self._backend_state[key]=health
                    if health == "red":
                        if "GAME_IS_BROKEN" in code:
                            self._line(f"{label}: PokerEYE: ошибка раздачи, восстанавливаем", "operator.eye_hand_recovery", severity="WARN", device_id=device, table_id=table)
                        else:
                            self._line(f"{label}: PokerEYE ошибка{(' — ' + message) if message else ''}", "operator.eye_error", severity="WARN", device_id=device, table_id=table)
                    elif health == "green" and previous in {"red", "yellow"}:
                        self._line(f"{label}: PokerEYE восстановлен", "operator.eye_recovered", device_id=device, table_id=table)
        elif kind == "table_close" and table > 0:
            crashed = bool(detail.get("crashed")) or str(observation.status).lower() == "red"
            unsupported="DOUBLE BOARD" in str(observation.reason or "").upper()
            table_hands=int(self._hands_table.get((device, table), 0))
            session_hands=self._session_hands_text(device)
            if unsupported:
                text=f"◆ {label}: вышли со стола · DOUBLE BOARD остаётся в чёрном списке · сессия: {session_hands}"
            else:
                state_text="закрыт из-за ошибки" if crashed else "вышли со стола"
                text=f"◆ {label}: {state_text} · сессия: {session_hands}"
            self._line(
                text,
                "operator.table_close", severity="WARN" if crashed else "INFO",
                device_id=device, table_id=table, reason=observation.reason,
                hands_table=table_hands, hands_device=int(self._hands_device.get(device,0)),
                hands_total=self._hands_total,
            )
            self._seated.discard((device, table))
            self._latest_by_table.pop((device, table), None)
            with self._lock:
                self._tables.pop((device, table), None)
        elif kind == "hand_started" and table > 0:
            hand = str(observation.hand_id or "?")
            self._line(f"◆ {label}: новая раздача #{hand}", "operator.hand_started", device_id=device, table_id=table, hand_id=hand)
        elif kind == "hand_completed" and table > 0:
            hand=str(observation.hand_id or "")
            key=(device, table, hand)
            game_type=self._game_type_label(observation.game_type)
            if key not in self._completed_hands:
                self._completed_hands.add(key)
                self._hands_table[(device, table)] += 1
                self._hands_device[device] += 1
                self._hands_by_type_device[device][game_type] += 1
                self._hands_total += 1
            table_hands=int(self._hands_table[(device, table)])
            device_hands=int(self._hands_device[device])
            session_hands=self._session_hands_text(device)
            self._line(
                f"◆ {label}: раздача завершена · сессия: {session_hands}",
                "operator.hand_completed", device_id=device, table_id=table,
                hand_id=observation.hand_id, game_type=game_type, hands_table=table_hands,
                hands_device=device_hands, hands_total=self._hands_total, session_hands=session_hands,
            )
        elif kind == "bridge_diag" and table > 0:
            tag = str(detail.get("tag") or "")
            if tag == "identity_name":
                nick = str(detail.get("name") or "").strip()
                if nick:
                    self._device_nick[device] = nick
                    self.technical("operator.identity_name", device_id=device, table_id=table, name=nick)
            elif tag == "seated":
                key = (device, table)
                if key not in self._seated:
                    self._seated.add(key)
                    self._line(f"◆ {label}: сели за стол", "operator.seated", device_id=device, table_id=table, seat=detail.get("seat"))
            elif tag == "cards":
                hand = str(detail.get("hand_id") or "")
                key = (device, table, hand)
                if key not in self._last_cards_hand:
                    self._last_cards_hand.add(key)
                    self._line(f"{label}: наши карты — {self._cards(detail.get('cards'))}", "operator.cards", device_id=device, table_id=table, hand_id=hand)
            elif tag == "joined_mid_hand":
                self._line(
                    f"{label}: неполная текущая раздача — включён страховочный ход",
                    "operator.mid_hand_fallback", severity="WARN",
                    device_id=device, table_id=table,
                )
            elif tag == "mid_hand_recovered":
                self._line(
                    f"{label}: текущая раздача восстановлена — продолжаем",
                    "operator.mid_hand_recovered", device_id=device, table_id=table,
                )
            elif tag == "hero_turn":
                self._line(f"{label}: наш ход", "operator.hero_turn", device_id=device, table_id=table)
            elif tag == "cc_received":
                meta = {
                    "table_id": table,
                    "action": str(detail.get("action") or "").upper() or None,
                    "amount": detail.get("amount"),
                }
                if meta["action"]:
                    self._latest_by_table[(device, table)] = dict(meta)
                self.technical(
                    "operator.cc_received", device_id=device, table_id=table,
                    action=meta["action"], amount=meta["amount"],
                )
            elif tag == "standup_queued":
                self._line(
                    f"{label}: стендап после этой руки — доигрываем раздачу",
                    "operator.standup_queued", device_id=device, table_id=table,
                    hand_id=detail.get("hand_id"),
                )
            elif tag == "prefold_ready":
                meta = {
                    "table_id": table,
                    "action": "FOLD",
                    "amount": None,
                    "attempt": 1,
                    "max_attempts": 3,
                }
                self._latest_by_table[(device, table)] = dict(meta)
                hand = str(detail.get("hand") or "")
                where = " · ".join(
                    part for part in (
                        str(detail.get("position") or ""),
                        str(detail.get("facing") or ""),
                        f"{detail.get('players')} игроков" if detail.get("players") else "",
                        hand,
                    ) if part
                )
                self._line(
                    f"{label}: префолд FOLD{(' · ' + where) if where else ''}",
                    "operator.prefold_ready", device_id=device, table_id=table,
                    action="FOLD", hand=hand, rule_id=detail.get("rule_id"),
                )
            elif tag == "action_ready":
                meta = {
                    "table_id": table,
                    "action": detail.get("action"),
                    "amount": detail.get("amount"),
                    "attempt": int(detail.get("attempt") or 1),
                    "max_attempts": int(detail.get("max_attempts") or 3),
                }
                self._latest_by_table[(device, table)] = dict(meta)
                amount_text = self._amount(meta.get("amount")).strip()
                self._line(
                    f"{label}: ACTION {str(meta.get('action') or 'ACTION').upper()}"
                    + (f" · AMOUNT {amount_text}" if amount_text else ""),
                    "operator.action_ready", device_id=device, table_id=table,
                    action=meta["action"], amount=meta["amount"],
                )
            elif tag == "fallback_ready":
                meta = {
                    "table_id": table,
                    "action": str(detail.get("action") or "FOLD").upper(),
                    "amount": None,
                    "attempt": int(detail.get("attempt") or 1),
                    "max_attempts": int(detail.get("max_attempts") or 3),
                }
                self._latest_by_table[(device, table)] = dict(meta)
                why = str(detail.get("reason") or "").upper()
                prefix = "PokerEYE не ответил" if why == "CC_TIMEOUT" else "подсказка недоступна"
                self._line(
                    f"{label}: {prefix} — страховочный {self._action_text(meta)}",
                    "operator.fallback_ready", severity="WARN",
                    device_id=device, table_id=table,
                    action=meta["action"], reason=why,
                )
            elif tag == "action_retry":
                meta = dict(self._latest_by_table.get((device, table)) or {})
                attempt = int(detail.get("attempt") or max(2, int(meta.get("attempt") or 1) + 1))
                maximum = int(detail.get("max_attempts") or 3)
                meta.update({
                    "table_id": table,
                    "action": detail.get("action") or meta.get("action"),
                    "amount": detail.get("amount", meta.get("amount")),
                    "attempt": attempt,
                    "max_attempts": maximum,
                })
                self._latest_by_table[(device, table)] = meta
                self._line(
                    f"{label}: подтверждения нет — повтор, попытка {attempt}/{maximum}",
                    "operator.action_retry", severity="WARN",
                    device_id=device, table_id=table, action=meta.get("action"),
                    amount=meta.get("amount"), attempt=attempt, max_attempts=maximum,
                )
            elif tag == "action_confirmed":
                meta = dict(self._latest_by_table.get((device, table)) or {})
                meta.update({
                    "action": detail.get("action") or meta.get("action"),
                    "amount": detail.get("amount", meta.get("amount")),
                })
                self._line(
                    f"{label}: ✓ {self._action_text(meta)} выполнено",
                    "operator.action_confirmed", device_id=device, table_id=table,
                    action=meta.get("action"), amount=meta.get("amount"),
                    attempt=detail.get("attempt"),
                )
            elif tag == "action_cancelled":
                reason = self.CANCEL_RU.get(str(detail.get("reason") or ""), str(detail.get("reason") or "отменено"))
                self._line(
                    f"{label}: действие отменено · {reason}",
                    "operator.action_cancelled", severity="WARN",
                    device_id=device, table_id=table, reason=detail.get("reason"),
                )
            elif tag == "sitout_detected":
                self._line(
                    f"{label}: ситаут — выходим и сядем снова по правилам auto",
                    "operator.sitout", severity="WARN",
                    device_id=device, table_id=table,
                )
            elif tag == "cc_streak_standup":
                self._line(
                    f"{label}: PokerEYE молчит 3 руки подряд — встаём",
                    "operator.cc_streak_standup", severity="ERROR",
                    device_id=device, table_id=table, streak=detail.get("streak"),
                )
                self.logger.error(
                    "cc_streak_standup",
                    message="PokerEYE produced no CC for 3 consecutive hero hands",
                    device_id=device, table_id=table, streak=detail.get("streak"),
                )
            elif tag == "cc_timeout":
                self._line(
                    f"{label}: ⚠ PokerEYE не ответил — включаем CHECK/FOLD страховку",
                    "operator.eye_timeout", severity="WARN",
                    device_id=device, table_id=table,
                )
                self.logger.error(
                    "cc_timeout",
                    message="PokerEYE did not return CC for a required hero turn",
                    device_id=device, table_id=table,
                    telemetry=detail.get("telemetry"),
                    timeout_s=detail.get("timeout_s"),
                )
            elif tag == "cc_mapping_error":
                self._line(
                    f"{label}: ⚠ решение PokerEYE не удалось применить — включаем CHECK/FOLD страховку",
                    "operator.mapping_error", severity="WARN",
                    device_id=device, table_id=table,
                )
                self.logger.error(
                    "cc_mapping_error",
                    message=str(observation.reason or "CC mapping failed"),
                    device_id=device, table_id=table,
                    telemetry=detail.get("telemetry"),
                )
            elif tag in {"state_error", "protocol_event_error"}:
                stage = "игрового состояния" if tag == "state_error" else "очереди протокола"
                command = str(detail.get("command") or "-")
                count = int(detail.get("count") or 1)
                self._line(
                    f"{label}: ⚠ внутренняя ошибка {stage} · {command} · повтор {count}",
                    f"operator.{tag}", severity="ERROR",
                    device_id=device, table_id=table, command=command,
                    error_type=detail.get("error_type"), count=count,
                )
                self.logger.error(
                    tag,
                    message=str(detail.get("error") or observation.reason or tag),
                    device_id=device, table_id=table, command=command,
                    error_type=detail.get("error_type"), count=count,
                    telemetry=detail.get("telemetry"),
                )
            elif tag == "hint_error":
                self._line(
                    f"{label}: ⚠ подсказка не построена — включаем CHECK/FOLD страховку",
                    "operator.hint_error", severity="WARN",
                    device_id=device, table_id=table,
                )
                self.logger.error(
                    "hint_error",
                    message=str(observation.reason or "hint failed"),
                    device_id=device, table_id=table,
                    telemetry=detail.get("telemetry"),
                )
            elif tag == "failsafe_unavailable":
                self._line(
                    f"{label}: ✗ нет подсказки и CHECK/FOLD сейчас недоступны",
                    "operator.failsafe_unavailable", severity="ERROR",
                    device_id=device, table_id=table,
                )
                self.logger.error(
                    "failsafe_unavailable",
                    message=str(observation.reason or "no legal technical fallback"),
                    device_id=device, table_id=table,
                    telemetry=detail.get("telemetry"),
                )
            elif tag == "action_exhausted":
                meta = dict(self._latest_by_table.get((device, table)) or {})
                meta.update({
                    "action": detail.get("action") or meta.get("action"),
                    "amount": detail.get("amount", meta.get("amount")),
                })
                maximum = int(detail.get("max_attempts") or 3)
                self._line(
                    f"{label}: ✗ {self._action_text(meta)} не подтверждено после {maximum}/{maximum}",
                    "operator.action_exhausted", severity="ERROR",
                    device_id=device, table_id=table,
                    action=meta.get("action"), amount=meta.get("amount"),
                    max_attempts=maximum,
                )
                self.logger.error(
                    "action_exhausted",
                    message="Coin did not confirm action after all attempts",
                    device_id=device, table_id=table,
                    action=meta.get("action"), amount=meta.get("amount"),
                    max_attempts=maximum, telemetry=detail.get("telemetry"),
                )
            elif tag == "late_cc_ignored":
                self.technical(
                    "operator.late_cc_ignored", severity="WARN",
                    device_id=device, table_id=table,
                )
        elif kind == "low_stack_exit" and table > 0:
            stack_bb=float(detail.get("stack_bb") or 0.0)
            threshold=float(detail.get("threshold_bb") or 79.0)
            self._line(
                f"{label}: стек {stack_bb:.1f} BB < {threshold:g} BB на старте раздачи — FOLD → встаём → выходим",
                "operator.low_stack_exit", severity="WARN", device_id=device, table_id=table,
                hand_id=observation.hand_id, stack_bb=stack_bb, threshold_bb=threshold,
            )
        elif kind == "unsupported_table" and table > 0:
            self._line(
                f"{label}: DOUBLE BOARD — FOLD → встаём → выходим · стол заблокирован до перезапуска",
                "operator.unsupported_table", severity="WARN", device_id=device, table_id=table,
            )
        elif kind in {"unsupported_exit_stage", "low_stack_exit_stage"}:
            # Native action_result prints the human send result. Keep router stage
            # accounting in technical logs only to avoid duplicate console lines.
            self.technical("forced_exit.stage", device_id=device, table_id=table or None, kind=kind, **detail)
        elif kind == "error":
            self._line(f"{label}: ошибка — {_short(observation.reason, 120)}", "operator.error", severity="ERROR", device_id=device, table_id=table or None)
        elif str(kind).startswith("automation"):
            self._line(
                f"{label}: {_short(observation.reason, 160)}",
                f"operator.{kind}",
                severity="WARN" if str(observation.status).lower() == "yellow" else "INFO",
                device_id=device, table_id=table or None,
            )

class RouterService:
    """Own the exact v6 device routers on one asyncio thread."""

    def __init__(
        self,
        *,
        accounts: AccountPool,
        credential_file: Path,
        backend_host: str,
        backend_port: int,
        observation_sink,
        technical_sink=None,
        account_provisioner=None,
    ) -> None:
        self.accounts = accounts
        self.observation_sink = observation_sink
        self.technical_sink = technical_sink or (lambda *_args, **_kwargs: None)
        self.factory = LiveTableSessionFactory(
            accounts=accounts,
            credential_file=credential_file,
            backend_host=backend_host,
            backend_port=backend_port,
            observation_sink=observation_sink,
            telemetry=None,
            frame_delay=0.004,
            # A real backend login in the supplied run completes in a few
            # seconds. Bound dead slots aggressively so one bad account cannot
            # occupy a table forever. Panel create + SCLogin needs more room.
            connect_timeout=8.0,
            probe_attempts_per_table=8,
            probe_backoff_seconds=0.35,
            auto_register_rejected=os.getenv("POKEREYE_ACCOUNT_AUTOREGISTER", "0").strip().lower()
            not in {"0", "false", "no", "off"},
            registration_android_id=os.getenv("POKEREYE_REGISTRATION_ANDROID_ID", "").strip(),
            login_reject_quarantine_seconds=float(
                os.getenv("POKEREYE_LOGIN_REJECT_QUARANTINE_SECONDS", "900")
            ),
            account_provisioner=account_provisioner,
        )
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="v6-router-host", daemon=True)
        self.thread.start()
        self._routers: dict[str, DeviceIngressRouter] = {}
        self._connected: set[str] = set()
        self._last_seen: dict[str, float] = {}
        self._device_labels: dict[str, str] = {}
        self.fleet_provider = None
        self.automation_store = AutomationStore()
        self._reaper = asyncio.run_coroutine_threadsafe(self._reaper_loop(), self.loop)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _router(self, device_id: str) -> DeviceIngressRouter:
        router = self._routers.get(device_id)
        if router is None:
            from .v6router.automation import DeviceAutomation

            automation = DeviceAutomation(
                device_id,
                store=self.automation_store,
                sink=lambda event, text="", severity="INFO": self.observation_sink(
                    RouterObservation(
                        kind=str(event),
                        device_id=device_id,
                        status="green" if severity == "INFO" else "yellow",
                        reason=str(text or ""),
                        detail={"automation": True},
                    )
                ),
            )
            router = DeviceIngressRouter(
                device_id,
                self.factory,
                observation_sink=self.observation_sink,
                action_arbiter=ActionArbiter(device_id),
                global_history_limit=512,
                orphan_limit_per_room=128,
                max_orphan_rooms=128,
                max_provisional_tables=64,
                max_table_slots=MAX_TABLES_PER_DEVICE,
                session_buffer_limit=512,
                max_inflight=128,
                close_grace_seconds=15.0,
                closing_tombstone_seconds=15.0,
                provisional_timeout=30.0,
                startup_max_attempts=3,
                startup_backoff_base=0.25,
                startup_backoff_max=1.0,
                startup_attempt_timeout=35.0,
                startup_stale_seconds=90.0,
                automation=automation,
            )
            self._routers[device_id] = router
            router.start_watchdog()
        return router

    async def _handle(self, device_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        self._last_seen[device_id] = time.monotonic()
        router = await self._router(device_id)
        decision, finish = await router.handle_event(event)

        # v6 synchronous ingress explicitly finished immediate legacy injections
        # before returning them. Native transport is also push-based, so preserve
        # that ordering before the action command is emitted to Android.
        if finish:
            slot = await router._slot_for_finish(int(finish))
            if slot and isinstance(slot.session, LiveTableSession):
                await slot.session._bridge.finish_hint(int(finish))

        answer = dict(decision or {})
        answer.setdefault("id", event.get("id"))
        answer.setdefault("ws_id", event.get("ws_id"))
        answer.setdefault("_ws_u32", event.get("_ws_u32"))
        answer.setdefault("action", "forward")
        return answer

    def handle(self, device_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        future = asyncio.run_coroutine_threadsafe(self._handle(device_id, event), self.loop)
        try:
            return future.result(timeout=4.0)
        except Exception as exc:
            future.cancel()
            self.technical_sink("router.slow_or_error", severity="WARN", device_id=device_id, error_type=type(exc).__name__, error=_short(exc))
            return {
                "id": event.get("id"),
                "ws_id": event.get("ws_id"),
                "_ws_u32": event.get("_ws_u32"),
                "action": "forward",
            }

    async def _action_result(self, device_id: str, result: Dict[str, Any]) -> bool:
        router = self._routers.get(device_id)
        if router is None:
            return False
        return await router.handle_native_action_result(result)

    def action_result(self, device_id: str, result: Dict[str, Any]) -> bool:
        future = asyncio.run_coroutine_threadsafe(
            self._action_result(device_id, dict(result)), self.loop
        )
        try:
            return bool(future.result(timeout=1.0))
        except Exception:
            future.cancel()
            return False

    def operator_error(self, device_id: str, event: str, detail: Dict[str, Any]) -> None:
        self.technical_sink(
            f"runtime.{event}",
            severity="ERROR",
            device_id=device_id,
            **dict(detail or {}),
        )

    def transport_up(self, device_id: str, device_label: str = "") -> None:
        def mark() -> None:
            self._connected.add(device_id)
            self._last_seen[device_id] = time.monotonic()
            if str(device_label or "").strip():
                self._device_labels[device_id] = str(device_label).strip()
        self.loop.call_soon_threadsafe(mark)

    def transport_down(self, device_id: str) -> None:
        def mark() -> None:
            self._connected.discard(device_id)
            self._last_seen[device_id] = time.monotonic()
        self.loop.call_soon_threadsafe(mark)

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(10.0)
                now = time.monotonic()
                for router in tuple(self._routers.values()):
                    reaped = await router.reap_stale_startups()
                    for table_id in reaped:
                        self.technical_sink(
                            "table.startup_reaped",
                            severity="WARN",
                            device_id=router.device_id,
                            table_id=table_id,
                        )
                stale = [
                    device_id
                    for device_id, seen in self._last_seen.items()
                    if device_id not in self._connected and now - seen > 180.0
                ]
                for device_id in stale:
                    router = self._routers.pop(device_id, None)
                    self._last_seen.pop(device_id, None)
                    self._device_labels.pop(device_id, None)
                    if router is not None:
                        self.technical_sink("device.expired", severity="WARN", device_id=device_id)
                        await router.close(crashed=True)
        except asyncio.CancelledError:
            return

    async def _control_snapshot(self) -> dict[str, Any]:
        devices = []
        fleet = {}
        provider = self.fleet_provider
        if callable(provider):
            try:
                fleet = dict(provider() or {})
            except Exception:
                fleet = {}
        for device_id, router in sorted(self._routers.items()):
            row = await router.control_snapshot()
            stats = fleet.get(device_id) or {}
            nick = str(row.get("hero_name") or stats.get("nick") or "")
            row["connected"] = device_id in self._connected
            row["device_label"] = self._device_labels.get(device_id, "")
            row["device_no"] = stats.get("device_no")
            row["hero_name"] = nick
            row["display_name"] = nick or row.get("device_label") or f"Устройство {stats.get('device_no') or '?'}"
            row["hands"] = int(stats.get("hands") or 0)
            row["hands_by_type"] = dict(stats.get("hands_by_type") or {})
            row["session_hands"] = str(stats.get("session_hands") or "")
            devices.append(row)
        accounts = [
            {
                "account_id": row.account_id,
                "state": row.state.value,
                "owner": row.owner,
                "validated": row.validated,
                "suffix": row.suffix,
                "attempts": row.attempts,
                "retry_in": round(row.retry_in, 3),
                "last_error": row.last_error,
            }
            for row in self.accounts.state_snapshot()
        ]
        return {
            "ok": True,
            "build": BUILD_ID,
            "patch": "MTABLE-20260818-A",
            "devices": devices,
            "accounts": accounts,
            "connected_devices": len(self._connected),
            "max_tables_per_device": MAX_TABLES_PER_DEVICE,
        }

    def control_snapshot(self) -> dict[str, Any]:
        future = asyncio.run_coroutine_threadsafe(self._control_snapshot(), self.loop)
        return dict(future.result(timeout=3.0))

    async def _control_close_table(self, device_id: str, table_id: int) -> bool:
        router = self._routers.get(str(device_id))
        if router is None:
            return False
        await router.close_table(int(table_id), reason="operator console close")
        return True

    def control_close_table(self, device_id: str, table_id: int) -> bool:
        future = asyncio.run_coroutine_threadsafe(
            self._control_close_table(device_id, table_id), self.loop
        )
        return bool(future.result(timeout=5.0))

    async def _control_restart_table(self, device_id: str, table_id: int) -> bool:
        router = self._routers.get(str(device_id))
        if router is None:
            return False
        return bool(await router.restart_table_backend(int(table_id)))

    def control_restart_table(self, device_id: str, table_id: int) -> bool:
        future = asyncio.run_coroutine_threadsafe(
            self._control_restart_table(device_id, table_id), self.loop
        )
        return bool(future.result(timeout=8.0))

    async def _control_reset_device(self, device_id: str) -> bool:
        device_id = str(device_id)
        router = self._routers.pop(device_id, None)
        if router is None:
            return False
        await router.close(crashed=False)
        self.technical_sink("device.operator_reset", severity="WARN", device_id=device_id)
        return True

    def control_reset_device(self, device_id: str) -> bool:
        future = asyncio.run_coroutine_threadsafe(
            self._control_reset_device(device_id), self.loop
        )
        return bool(future.result(timeout=8.0))

    async def _control_auto(self, device_id: str, policy: dict[str, Any], *, apply: bool) -> dict[str, Any]:
        router = await self._router(str(device_id))
        auto = router.automation
        if auto is None:
            return {"ok": False, "error": "no automation"}
        # apply means "take this policy now", not "force auto on". The console
        # already puts the intended enabled flag in the body; overwriting it
        # with True is why toggling auto off still opened tables.
        enable = None
        if isinstance(policy, dict) and "enabled" in policy:
            enable = bool(policy.get("enabled"))
        saved = auto.apply_policy(policy, enable=enable)
        if apply:
            await router._watchdog_tick()
        return {"ok": True, "policy": saved.public(), "automation": auto.snapshot()}

    def control_auto(self, device_id: str, policy: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
        future = asyncio.run_coroutine_threadsafe(
            self._control_auto(device_id, dict(policy or {}), apply=apply), self.loop
        )
        return dict(future.result(timeout=5.0))

    async def _control_leave_all(self, device_id: str, *, gradual: bool) -> dict[str, Any]:
        router = self._routers.get(str(device_id))
        if router is None or router.automation is None:
            return {"ok": False, "error": "device offline"}
        auto = router.automation
        async with router._lock:
            table_ids = set(int(table_id) for table_id in router._sessions)
        table_ids.update(int(tid) for tid in auto._tabs)
        table_ids.update(int(tid) for tid in auto._seated)
        queued = auto.schedule_leave_all(sorted(tid for tid in table_ids if tid > 0), gradual=gradual)
        await router._watchdog_tick()
        return {"ok": True, "queued": queued, "gradual": bool(gradual), "status": auto._status}

    def control_leave_all(self, device_id: str, *, gradual: bool = False) -> dict[str, Any]:
        future = asyncio.run_coroutine_threadsafe(
            self._control_leave_all(device_id, gradual=gradual), self.loop
        )
        return dict(future.result(timeout=5.0))

    async def _close_all(self) -> None:
        routers = list(self._routers.values())
        self._routers.clear()
        for router in routers:
            await router.close(crashed=False)

    def close(self) -> None:
        if not self.thread.is_alive():
            return
        try:
            self._reaper.cancel()
            future = asyncio.run_coroutine_threadsafe(self._close_all(), self.loop)
            future.result(timeout=10.0)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3.0)


class TrainerControlServer:
    """Loopback-only JSON control plane for the operator web console."""

    def __init__(
        self,
        router_service: RouterService,
        *,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
    ) -> None:
        self.router_service = router_service
        self.host = str(host)
        self.port = int(port)
        self.httpd: Optional[http.server.ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> int:
        if self.httpd is not None:
            return int(self.httpd.server_address[1])
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "PokerEyeControl/1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _reply(self, status: int, value: Dict[str, Any]) -> None:
                raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Dict[str, Any]:
                try:
                    size = min(65536, max(0, int(self.headers.get("Content-Length") or 0)))
                    raw = self.rfile.read(size) if size else b"{}"
                    value = json.loads(raw.decode("utf-8"))
                    return value if isinstance(value, dict) else {}
                except Exception:
                    return {}

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] == "/snapshot":
                    try:
                        self._reply(200, owner.router_service.control_snapshot())
                    except Exception as exc:
                        self._reply(500, {"ok": False, "error": f"{type(exc).__name__}: {_short(exc)}"})
                    return
                if self.path.split("?", 1)[0] == "/health":
                    self._reply(200, {"ok": True, "build": BUILD_ID, "patch": "MTABLE-20260818-A"})
                    return
                self._reply(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:
                body = self._body()
                path = self.path.split("?", 1)[0]
                try:
                    if path == "/table/close":
                        ok = owner.router_service.control_close_table(
                            str(body.get("device_id") or ""), int(body.get("table_id") or 0)
                        )
                    elif path == "/table/restart":
                        ok = owner.router_service.control_restart_table(
                            str(body.get("device_id") or ""), int(body.get("table_id") or 0)
                        )
                    elif path == "/device/reset":
                        ok = owner.router_service.control_reset_device(
                            str(body.get("device_id") or "")
                        )
                    elif path == "/device/auto":
                        value = owner.router_service.control_auto(
                            str(body.get("device_id") or ""),
                            dict(body.get("policy") or body),
                            apply=bool(body.get("apply")),
                        )
                        self._reply(200 if value.get("ok") else 404, value)
                        return
                    elif path == "/device/leave-all":
                        value = owner.router_service.control_leave_all(
                            str(body.get("device_id") or ""),
                            gradual=bool(body.get("gradual")),
                        )
                        self._reply(200 if value.get("ok") else 404, value)
                        return
                    else:
                        self._reply(404, {"ok": False, "error": "not_found"})
                        return
                except Exception as exc:
                    self._reply(500, {"ok": False, "error": f"{type(exc).__name__}: {_short(exc)}"})
                    return
                self._reply(200 if ok else 404, {"ok": bool(ok)})

        self.httpd = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            daemon=True,
            name="pokereye-control",
        )
        self.thread.start()
        return int(self.httpd.server_address[1])

    def stop(self) -> None:
        httpd = self.httpd
        self.httpd = None
        if httpd is not None:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        thread = self.thread
        self.thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


class NativeIngressServer:
    """Authenticated HMN1 endpoint with multiple process channels per device.

    CoinPoker may run networking WebSockets in more than one Android process. A
    physical device therefore owns one v6 router/action arbiter but may have
    several simultaneous native TCP channels. Channels never evict each other.
    """

    def __init__(
        self,
        secret: bytes,
        router_service: RouterService,
        meter: TrafficMeter,
        capture: Optional[RawCoinCaptureManager] = None,
        operator: Optional[OperatorConsole] = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.secret = secret
        self.router_service = router_service
        self.meter = meter
        self.capture = capture
        self.operator = operator
        self.host = host
        self.port = int(port)
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._all: set[socket.socket] = set()
        self._channels: dict[str, socket.socket] = {}
        self._channel_send_locks: dict[str, threading.Lock] = {}
        self._device_channels: dict[str, set[str]] = collections.defaultdict(set)
        self._traffic_online: set[str] = set()
        self._channel_seq = 0

    def count(self) -> int:
        """Physical devices with at least one authenticated process channel."""
        with self._lock:
            return sum(1 for rows in self._device_channels.values() if rows)

    def channel_count(self, device_id: Optional[str] = None) -> int:
        with self._lock:
            if device_id is not None:
                return len(self._device_channels.get(str(device_id), ()))
            return len(self._channels)

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # On Windows SO_REUSEADDR can let a stale PokerEye process keep the same
        # production port alive.  That makes new Android connections land on an
        # older process while the freshly opened console waits on a different run.
        # Production must have exactly one owner of :19037.
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise OSError(
                exc.errno,
                f"PokerEye ingress port {self.host}:{self.port} is already in use; "
                "close the old PokerEye process before starting this one",
            ) from exc
        sock.listen(256)
        sock.settimeout(0.25)
        self._server = sock
        self.port = int(sock.getsockname()[1])
        threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name="native-ingress-accept",
        ).start()
        return self.port

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._all.add(conn)
            threading.Thread(
                target=self._handle,
                args=(conn, addr),
                daemon=True,
                name=f"native-ingress-{addr[0]}-{addr[1]}",
            ).start()

    @staticmethod
    def _valid_transport_id(device_id: str, transport_id: str) -> bool:
        legacy = f"{device_id}-native"
        return transport_id == legacy or transport_id.startswith(legacy + "-")

    def _register_channel(
        self, device_id: str, conn: socket.socket
    ) -> tuple[str, bool]:
        with self._lock:
            rows = self._device_channels.get(device_id)
            first_for_device = not rows
            if first_for_device and self.count() >= MAX_DEVICES:
                raise RuntimeError("device_capacity")
            self._channel_seq += 1
            channel_id = f"{device_id}#p{self._channel_seq}"
            self._channels[channel_id] = conn
            self._channel_send_locks[channel_id] = threading.Lock()
            self._device_channels[device_id].add(channel_id)
            return channel_id, first_for_device

    def _remove_channel(
        self, device_id: str, channel_id: str, conn: socket.socket
    ) -> bool:
        """Return True only when this was the final channel for the device."""
        with self._lock:
            self._all.discard(conn)
            if self._channels.get(channel_id) is conn:
                self._channels.pop(channel_id, None)
                self._channel_send_locks.pop(channel_id, None)
            rows = self._device_channels.get(device_id)
            if rows is not None:
                rows.discard(channel_id)
                if not rows:
                    self._device_channels.pop(device_id, None)
                    return True
            return False

    def _send_channel(self, channel_id: str, raw: bytes) -> bool:
        with self._lock:
            conn = self._channels.get(channel_id)
            lock = self._channel_send_locks.get(channel_id)
        if conn is None or lock is None:
            return False
        try:
            with lock:
                send_raw_frame(conn, raw)
            return True
        except (ConnectionError, OSError):
            return False

    def _mark_first_traffic(self, device_id: str) -> bool:
        first = False
        with self._lock:
            if device_id not in self._traffic_online:
                self._traffic_online.add(device_id)
                first = True
        if first and self.operator is not None:
            self.operator.device_up(device_id)
        return first

    def _action_delivery_failed(
        self,
        device_id: str,
        decision: Dict[str, Any],
        *,
        target_channel: str,
    ) -> None:
        meta = dict(decision.get("_operator_action") or {})
        result = {
            "ok": False,
            "token": str(decision.get("token") or meta.get("token") or ""),
            "ws_id": str(decision.get("ws_id") or ""),
            "_ws_u32": int(decision.get("_ws_u32") or 0),
            "reason_code": 1,
            "target_channel": target_channel,
        }
        if result["token"]:
            try:
                self.router_service.action_result(device_id, result)
            except Exception:
                pass
            if self.operator is not None:
                self.operator.native_result(device_id, result)
        if self.operator is not None:
            self.operator.technical(
                "action.target_channel_missing",
                severity="ERROR",
                device_id=device_id,
                target_channel=target_channel,
                token=result["token"],
                ws_id=result["ws_id"],
            )

    def _route_worker(
        self,
        device_id: str,
        channel_id: str,
        work_queue: "queue.Queue[tuple[str, Dict[str, Any]] | None]",
        stop_event: threading.Event,
    ) -> None:
        """Process router/backend work away from the TCP reader.

        Coin telemetry is one-way.  Reading the HMN1 stream must therefore never
        wait for protobuf/SmartFox decoding, backend startup, or action arbitration.
        A blocked router used to stop recv_raw_frame after the first Coin frame;
        Android kept writing into TCP until the connection eventually reset.
        """
        while not stop_event.is_set() or not work_queue.empty():
            try:
                item = work_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                work_queue.task_done()
                break

            kind, message = item
            try:
                if kind == "action_result":
                    if hasattr(self.router_service, "action_result"):
                        self.router_service.action_result(device_id, message)
                    if self.operator is not None:
                        self.operator.native_result(device_id, message)
                    continue

                route_started = time.monotonic()
                decision = self.router_service.handle(device_id, message)
                route_elapsed = time.monotonic() - route_started
                if route_elapsed >= 0.5 and self.operator is not None:
                    self.operator.technical(
                        "router.ingress_slow",
                        severity="WARN",
                        device_id=device_id,
                        channel_id=channel_id,
                        ws_id=message.get("ws_id"),
                        direction=message.get("direction"),
                        elapsed_ms=round(route_elapsed * 1000.0, 1),
                    )

                command = native_command(
                    decision, int(message.get("_ws_u32") or 0)
                )
                if command is None:
                    continue

                target_channel = str(
                    decision.get("_target_channel_id") or channel_id
                )
                meta = dict(decision.get("_operator_action") or {})
                if self.operator is not None and meta:
                    self.operator.command_sent(device_id, meta)

                if not self._send_channel(target_channel, command):
                    self._action_delivery_failed(
                        device_id, decision, target_channel=target_channel
                    )
            except Exception as exc:
                if self.operator is not None:
                    self.operator.technical(
                        "router.worker_error",
                        severity="ERROR",
                        device_id=device_id,
                        channel_id=channel_id,
                        error_type=type(exc).__name__,
                        error=_short(exc, 1000),
                    )
            finally:
                work_queue.task_done()

    def _handle(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        device_id = ""
        transport_id = ""
        channel_id = ""
        first_for_device = False
        reason = "closed"
        route_stop = threading.Event()
        route_queue: "queue.Queue[tuple[str, Dict[str, Any]] | None]" = queue.Queue(
            maxsize=32768
        )
        route_thread: Optional[threading.Thread] = None
        route_drops = 0
        heartbeat_count = 0
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(5.0)
            hello = recv_json_frame(conn)
            device_id = str(hello.get("device_id") or "")
            transport_id = str(hello.get("table_id") or "")
            apk_build_id = str(hello.get("build_id") or "legacy-unversioned")
            device_label = _short(hello.get("device_label") or "", 96)
            if self.operator is not None:
                self.operator.technical(
                    "transport.hello",
                    device_id=device_id,
                    transport_id=transport_id,
                    peer=f"{addr[0]}:{addr[1]}",
                    native=bool(hello.get("native_mux")),
                    apk_build_id=apk_build_id,
                    trainer_build_id=BUILD_ID,
                    build_match=(apk_build_id == BUILD_ID),
                    device_label=device_label or None,
                )

            if hello.get("type") != "direct_hello" or hello.get("version") != PROTOCOL_VERSION:
                send_json_frame(conn, {"type": "error", "error": "invalid_direct_hello"})
                raise ValueError("invalid_direct_hello")
            if not hello.get("native_mux"):
                send_json_frame(conn, {"type": "error", "error": "native_mux_required"})
                raise ValueError("native_mux_required")
            if not device_id or not transport_id or device_id.lower() == "unknown":
                send_json_frame(conn, {"type": "error", "error": "invalid_identity"})
                raise ValueError("invalid_identity")
            if not self._valid_transport_id(device_id, transport_id):
                send_json_frame(conn, {"type": "error", "error": "invalid_transport_id"})
                raise ValueError("invalid_transport_id")
            supplied = str(hello.get("proof") or "")
            expected = direct_proof(self.secret, device_id, transport_id)
            if not hmac.compare_digest(supplied, expected):
                send_json_frame(conn, {"type": "error", "error": "bad_proof"})
                raise ValueError("bad_proof")

            if apk_build_id != BUILD_ID and self.operator is not None:
                self.operator.technical(
                    "transport.build_mismatch",
                    severity="WARN",
                    device_id=device_id,
                    apk_build_id=apk_build_id,
                    trainer_build_id=BUILD_ID,
                    allowed=True,
                )

            try:
                channel_id, first_for_device = self._register_channel(device_id, conn)
            except RuntimeError as exc:
                if str(exc) == "device_capacity":
                    send_json_frame(
                        conn,
                        {
                            "type": "error",
                            "error": "device_capacity",
                            "max_devices": MAX_DEVICES,
                            "max_tables_per_device": MAX_TABLES_PER_DEVICE,
                            "max_total_tables": MAX_TOTAL_TABLES,
                        },
                    )
                    raise ValueError("device_capacity")
                raise

            send_json_frame(
                conn,
                {
                    "type": "welcome",
                    "version": PROTOCOL_VERSION,
                    "device_id": device_id,
                    "table_id": transport_id,
                    "native_mux": True,
                    "devices": self.count(),
                    "process_channels": self.channel_count(device_id),
                    "channel_id": channel_id,
                    "build_id": BUILD_ID,
                },
            )
            conn.settimeout(35.0)

            if first_for_device:
                try:
                    self.router_service.transport_up(device_id, device_label)
                except TypeError:
                    # Compatibility with test/legacy router shims that predate
                    # the optional human device label argument.
                    self.router_service.transport_up(device_id)
                if self.operator is not None:
                    self.operator.device_up(device_id)
            if self.operator is not None:
                self.operator.technical(
                    "transport.channel_up",
                    device_id=device_id,
                    transport_id=transport_id,
                    channel_id=channel_id,
                    peer=f"{addr[0]}:{addr[1]}",
                    channels=self.channel_count(device_id),
                )

            route_thread = threading.Thread(
                target=self._route_worker,
                args=(device_id, channel_id, route_queue, route_stop),
                daemon=True,
                name=f"native-router-{channel_id}",
            )
            route_thread.start()

            while not self._stop.is_set():
                try:
                    raw = recv_raw_frame(conn)
                except socket.timeout:
                    continue

                kind, message = parse_native_message(raw)
                if kind == "heartbeat":
                    heartbeat_count += 1
                    if not self._send_channel(
                        channel_id,
                        native_heartbeat_ack(int(message["sequence"])),
                    ):
                        raise ConnectionError("heartbeat ack failed")
                    if heartbeat_count == 1 and self.operator is not None:
                        self.operator.technical(
                            "transport.heartbeat_ok",
                            device_id=device_id,
                            transport_id=transport_id,
                            channel_id=channel_id,
                            peer=f"{addr[0]}:{addr[1]}",
                        )
                    continue

                if kind == "action_result":
                    message["_channel_id"] = channel_id
                    message["_transport_id"] = transport_id
                    try:
                        route_queue.put_nowait(("action_result", message))
                    except queue.Full:
                        route_drops += 1
                        if self.operator is not None and route_drops in {1, 10, 100, 1000}:
                            self.operator.technical(
                                "router.queue_drop",
                                severity="ERROR",
                                device_id=device_id,
                                channel_id=channel_id,
                                dropped=route_drops,
                                queue_size=route_queue.qsize(),
                                kind="action_result",
                            )
                    continue

                # The server-side channel is the process-scoped routing key.  The
                # Android wire format remains compact and backwards compatible.
                message["_channel_id"] = channel_id
                message["_transport_id"] = transport_id
                first_traffic = self._mark_first_traffic(device_id)
                if first_traffic and self.operator is not None:
                    raw_payload = message.get("_raw")
                    raw_size = len(raw_payload) if isinstance(
                        raw_payload, (bytes, bytearray, memoryview)
                    ) else 0
                    first_byte = int(raw_payload[0]) if raw_size else None
                    self.operator.technical(
                        "transport.first_ws_frame",
                        device_id=device_id,
                        transport_id=transport_id,
                        channel_id=channel_id,
                        ws_id=message.get("ws_id"),
                        direction=message.get("direction"),
                        bytes=raw_size,
                        first=first_byte,
                    )

                # Capture raw Coin bytes before any decoder/router/backend work.
                if self.capture is not None:
                    self.capture.observe(device_id, message)
                self.meter.observe(device_id, message)

                try:
                    route_queue.put_nowait(("ws_message", message))
                except queue.Full:
                    route_drops += 1
                    if self.operator is not None and route_drops in {1, 10, 100, 1000}:
                        self.operator.technical(
                            "router.queue_drop",
                            severity="ERROR",
                            device_id=device_id,
                            channel_id=channel_id,
                            dropped=route_drops,
                            queue_size=route_queue.qsize(),
                            ws_id=message.get("ws_id"),
                            direction=message.get("direction"),
                        )

        except (ConnectionError, EOFError, OSError, ValueError, json.JSONDecodeError) as exc:
            reason = type(exc).__name__
            if self.operator is not None:
                self.operator.technical(
                    "transport.disconnect",
                    severity="WARN",
                    device_id=device_id or None,
                    transport_id=transport_id or None,
                    channel_id=channel_id or None,
                    peer=f"{addr[0]}:{addr[1]}",
                    error_type=type(exc).__name__,
                    error=_short(exc, 500),
                )
        except Exception as exc:
            reason = f"RuntimeError:{type(exc).__name__}"
            if self.operator is not None:
                self.operator.technical(
                    "transport.handler_exception",
                    severity="ERROR",
                    device_id=device_id or None,
                    transport_id=transport_id or None,
                    channel_id=channel_id or None,
                    peer=f"{addr[0]}:{addr[1]}",
                    error_type=type(exc).__name__,
                    error=_short(exc, 1000),
                )
                try:
                    self.operator.logger.error(
                        "transport_handler_exception",
                        message="native ingress handler raised an unexpected runtime exception",
                        device_id=device_id or None,
                        transport_id=transport_id or None,
                        channel_id=channel_id or None,
                        peer=f"{addr[0]}:{addr[1]}",
                        error_type=type(exc).__name__,
                        error=_short(exc, 1000),
                    )
                except Exception:
                    pass
        finally:
            route_stop.set()
            if route_thread is not None:
                try:
                    route_queue.put_nowait(None)
                except queue.Full:
                    pass
                route_thread.join(timeout=0.5)
                if route_thread.is_alive() and self.operator is not None:
                    self.operator.technical(
                        "router.worker_detached",
                        severity="WARN",
                        device_id=device_id or None,
                        channel_id=channel_id or None,
                        queued=route_queue.qsize(),
                    )

            last_for_device = False
            if device_id and channel_id:
                last_for_device = self._remove_channel(device_id, channel_id, conn)
            else:
                with self._lock:
                    self._all.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

            if last_for_device and device_id:
                self.router_service.transport_down(device_id)
                had_traffic = False
                with self._lock:
                    if device_id in self._traffic_online:
                        self._traffic_online.discard(device_id)
                        had_traffic = True
                if had_traffic and self.operator is not None:
                    self.operator.device_down(device_id, reason)
            elif device_id and channel_id and self.operator is not None:
                self.operator.technical(
                    "transport.channel_down",
                    severity="WARN",
                    device_id=device_id,
                    transport_id=transport_id,
                    channel_id=channel_id,
                    remaining_channels=self.channel_count(device_id),
                    reason=reason,
                )

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

        with self._lock:
            conns = tuple(self._all)
            self._all.clear()
            self._channels.clear()
            self._channel_send_locks.clear()
            self._device_channels.clear()
            self._traffic_online.clear()

        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


class ProductionTrainer:
    def __init__(
        self,
        *,
        secret: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        public_host: str = DEFAULT_PUBLIC_HOST,
        log_dir: str = "logs",
        account_file: str = "config/backend_accounts.local.json",
        credential_file: str = "secrets/eye.agent",
        backend_host: str = "gs.eye-panel.com",
        backend_port: int = 443,
    ) -> None:
        self.secret = secret.encode("utf-8")
        self.host = host
        self.port = int(port)
        self.public_host = public_host
        self.logger = SessionLogger(log_dir)
        self._stop = threading.Event()
        account_path = Path(account_file)
        data = json.loads(account_path.read_text(encoding="utf-8-sig"))
        # POKEREYE_SAFE_ACCOUNT_BOOTSTRAP_V2
        account_rows = [
            row for row in (data.get("accounts") or [])
            if isinstance(row, dict) and str(row.get("account_id") or "").strip()
        ]
        account_ids = [str(row.get("account_id") or "").strip() for row in account_rows]
        validated_accounts = [
            str(row.get("account_id") or "").strip()
            for row in account_rows
            if bool(row.get("validated"))
            and str(row.get("state") or "").upper() not in {"INVALID", "QUARANTINED"}
        ]
        if not account_ids:
            raise RuntimeError("PokerEYE account registry is empty")

        credential = Path(credential_file)
        if not credential.is_file() or not credential.read_text(encoding="utf-8-sig").strip():
            raise RuntimeError("PokerEYE credential missing/empty")

        account_base = str(data.get("base") or "").strip()
        if not account_base:
            first_account = account_ids[0]
            account_base = first_account.rsplit("-", 1)[0] if "-" in first_account else ""
        autogrow = bool(account_base) and os.getenv("POKEREYE_ACCOUNT_AUTOGROW", "0").strip().lower() in {"1", "true", "yes", "on"}
        pool = AccountPool(
            account_ids,
            dynamic_base=account_base or None,
            registry_path=account_path if account_base else None,
            profile=str(data.get("profile") or "PPPoker"),
            auto_expand_unbounded=autogrow,
        )
        self.account_count = len(validated_accounts)
        self.operator = OperatorConsole(
            self.logger, self.account_count, accounts_dynamic=bool(account_base)
        )

        def capture_open(device_id: str, path: Path) -> None:
            try:
                shown = path.relative_to(Path.cwd())
            except ValueError:
                shown = path
            self.logger.emit(
                "capture.open",
                device_id=device_id,
                path=str(path),
                format="pcap-DLT_USER0-HMR1",
            )

        def capture_drop(count: int) -> None:
            self.logger.emit(
                "capture.drop",
                severity="WARN",
                dropped=count,
            )

        self.capture = RawCoinCaptureManager(
            self.logger.directory / "captures",
            on_open=capture_open,
            on_drop=capture_drop,
        )
        self.meter = TrafficMeter(
            lambda **fields: self.logger.emit("traffic.window", **fields)
        )
        from .verified_v1.eye_panel_admin import client_from_env

        panel = client_from_env(credential.read_text(encoding="utf-8-sig").strip())
        self.router_service = RouterService(
            accounts=pool,
            credential_file=credential,
            backend_host=backend_host,
            backend_port=backend_port,
            observation_sink=self._observation,
            technical_sink=self.operator.technical,
            account_provisioner=None if panel is None else panel.create_account,
        )
        self.router_service.fleet_provider = self.operator.fleet_snapshot
        self.server = NativeIngressServer(
            self.secret,
            self.router_service,
            self.meter,
            self.capture,
            self.operator,
            host=host,
            port=port,
        )
        self.control = TrainerControlServer(self.router_service)
        # Android APK uses exactly one configured public HMN1 endpoint.
        # There is intentionally no implicit localhost/adb-reverse fallback.

    def _observation(self, observation: RouterObservation) -> None:
        kind = observation.kind
        severity = "ERROR" if kind in {"error", "table_start_failed"} else (
            "WARN" if kind in {
                "warning", "account_invalid", "account_quarantined", "table_restart",
                "account_registering", "account_registration_failed", "account_waiting",
            } else "INFO"
        )
        reason = _short(observation.reason)
        noisy_table = (
            kind == "table_update"
            and str(observation.status or "").lower() != "red"
            and (
                "FUEL_PENDING" in reason
                or "NO_TRAFFIC_FROM_ROOM" in reason.upper()
                or reason.startswith("phase=")
            )
        )
        noisy_diag = kind == "bridge_diag" and str((observation.detail or {}).get("tag") or "") in {
            "hint_sent", "turn_refresh", "identity_uid",
        }
        if not noisy_table and not noisy_diag:
            self.logger.emit(
                f"v6.{kind}",
                severity=severity,
                device_id=observation.device_id,
                table_id=observation.table_id,
                account_id=observation.account_id,
                reason=reason,
                hand_id=observation.hand_id,
                detail=observation.detail or {},
                status=observation.status,
            )
        self.operator.observation(observation)

    def start(self) -> None:
        port = self.server.start()
        control_port = self.control.start()
        self.logger.emit(
            "trainer.ready",
            flush=True,
            build_id=BUILD_ID,
            host=self.host,
            port=port,
            public_host=self.public_host,
            transport="native HMN1 multi-process channels per physical device",
            router="verified v6 DeviceIngressRouter",
            accounts=self.account_count,
            chip_scale=100,
            control_host=CONTROL_HOST,
            control_port=control_port,
            runtime_patch="MTABLE-20260818-A",
        )
        self.operator.ready()

    def run_forever(self) -> None:
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.control.stop()
        self.server.stop()
        self.router_service.close()
        self.capture.close()
        self.logger.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="poker-eye-v2 production native trainer")
    p.add_argument("--secret", required=True)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--public-host", default=DEFAULT_PUBLIC_HOST)
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--account-file", default="config/backend_accounts.local.json")
    p.add_argument("--credential-file", default="secrets/eye.agent")
    p.add_argument("--backend-host", default="gs.eye-panel.com")
    p.add_argument("--backend-port", type=int, default=443)
    return p


def run_from_args(args: argparse.Namespace) -> None:
    trainer = ProductionTrainer(
        secret=args.secret,
        host=args.host,
        port=args.port,
        public_host=args.public_host,
        log_dir=args.log_dir,
        account_file=args.account_file,
        credential_file=args.credential_file,
        backend_host=args.backend_host,
        backend_port=args.backend_port,
    )
    trainer.start()
    trainer.run_forever()
