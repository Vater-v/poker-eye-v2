#!/usr/bin/env python3
"""Standalone PokerEYE protocol probe recovered from the Android client.

The default commands are offline-only.  ``connect`` is deliberately explicit:
it prompts for the password (or reads a named environment variable), never
accepts it on the command line, and never prints or stores CSLogin bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import getpass
import hashlib
import inspect
import json
import os
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator, Optional


GRPC_METHOD = "/Eye/SubscribeToStream"
DEFAULT_PORT = 443
DEFAULT_TWEAK_VERSION = "0.0.142"
TARGET_CLUB_ID = 3_663_333


class ProtocolError(ValueError):
    pass


def _varint(value: int) -> bytes:
    if value < 0:
        raise ProtocolError("negative protobuf length")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(buf) and shift < 70:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    raise ProtocolError("truncated/invalid protobuf varint")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_envelope(message_type: str, body: str | dict[str, Any]) -> bytes:
    """Encode EyeMessages.CSData/SCData: field 1 type, field 2 body."""
    if isinstance(body, dict):
        body = compact_json(body)
    if not isinstance(message_type, str) or not isinstance(body, str):
        raise TypeError("message_type and body must be strings/dicts")
    type_bytes = message_type.encode("utf-8")
    body_bytes = body.encode("utf-8")
    return b"\x0a" + _varint(len(type_bytes)) + type_bytes + b"\x12" + _varint(len(body_bytes)) + body_bytes


def decode_envelope(payload: bytes) -> tuple[str, str]:
    fields: dict[int, bytes] = {}
    pos = 0
    while pos < len(payload):
        tag, pos = _read_varint(payload, pos)
        field = tag >> 3
        wire = tag & 7
        if wire != 2:
            raise ProtocolError(f"unsupported envelope wire type {wire} for field {field}")
        size, pos = _read_varint(payload, pos)
        end = pos + size
        if end > len(payload):
            raise ProtocolError("truncated protobuf string")
        fields[field] = payload[pos:end]
        pos = end
    try:
        return fields[1].decode("utf-8"), fields[2].decode("utf-8")
    except (KeyError, UnicodeDecodeError) as exc:
        raise ProtocolError("Eye envelope must contain UTF-8 fields 1 and 2") from exc


def decode_response(payload: bytes) -> tuple[str, Any]:
    message_type, body = decode_envelope(payload)
    try:
        decoded: Any = json.loads(body)
    except json.JSONDecodeError:
        decoded = body
    return message_type, decoded


@dataclass(frozen=True)
class LoginParams:
    device_id: str
    password: str
    version: str
    lang: str
    bundle_identifier: str
    device: str
    serial: str
    reset_seq: bool = False
    geo: str = ""
    tweak_version: str = DEFAULT_TWEAK_VERSION
    os_name: str = "Android"

    def body(self) -> dict[str, Any]:
        # Field order follows the Kotlin serializer.  This is useful when comparing
        # exact diagnostic bytes even though JSON object order is semantically free.
        return {
            "deviceId": self.device_id,
            "password": self.password,
            "version": self.version,
            "lang": self.lang,
            "bundleIdentifier": self.bundle_identifier,
            "device": self.device,
            "serial": self.serial,
            "tweakVersion": self.tweak_version,
            "os": self.os_name,
            "resetSeq": self.reset_seq,
            "geo": self.geo,
        }


def login_envelope(params: LoginParams) -> bytes:
    return encode_envelope("CSLogin", params.body())


def ping_envelope(timestamp_ms: Optional[int] = None) -> bytes:
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return encode_envelope("CSPing", {"timestamp": int(timestamp_ms)})


RECONNECT_INTERVAL_TRAILER = "RECONNECT_INTERVAL"
REPLAY_PACKET_LIMIT = 50


@dataclass(frozen=True)
class ReconnectDirective:
    """Recoverable stream termination requested by the PokerEYE backend."""

    interval_ms: int
    details: str


@dataclass(frozen=True)
class ReplayPacket:
    serial: int
    packet: dict[str, Any]


class PacketReplayWindow:
    """Exact equivalent of EYE's 50-entry replaying ``SharedFlow``.

    EYE clears its replay cache when the incoming CSPacket PID differs from the
    last cached packet.  This is expected when the room starts with PID 0 and
    later learns the real playing id; it is not itself an error.
    """

    def __init__(self, maxlen: int = REPLAY_PACKET_LIMIT) -> None:
        if maxlen <= 0:
            raise ValueError("packet replay window must be positive")
        self._packets: collections.deque[ReplayPacket] = collections.deque(maxlen=maxlen)
        self._next_serial = 0

    def append(self, packet: dict[str, Any]) -> ReplayPacket:
        copied = dict(packet)
        pid = str(copied.get("pid") or "")
        if self._packets and str(self._packets[-1].packet.get("pid") or "") != pid:
            self._packets.clear()
        record = ReplayPacket(self._next_serial, copied)
        self._next_serial += 1
        self._packets.append(record)
        return record

    def snapshot(self) -> tuple[ReplayPacket, ...]:
        return tuple(self._packets)

    def clear(self) -> None:
        self._packets.clear()

    def __len__(self) -> int:
        return len(self._packets)


def _status_code_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.upper()
    return str(value).rsplit(".", 1)[-1].upper()


def _metadata_pair(value: Any) -> tuple[Any, Any] | None:
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return value[0], value[1]
    key = getattr(value, "key", None)
    if key is not None:
        return key, getattr(value, "value", None)
    return None


def reconnect_directive(error: BaseException) -> Optional[ReconnectDirective]:
    """Decode EYE's recoverable gRPC termination without importing grpc.

    ``grpc.aio.AioRpcError`` exposes ``code()``, ``details()`` and
    ``trailing_metadata()`` synchronously.  Keeping the decoder duck-typed also
    lets the exact reconnect contract be tested offline.
    """

    try:
        code = error.code()  # type: ignore[attr-defined]
    except Exception:
        return None
    if _status_code_name(code) != "INVALID_ARGUMENT":
        return None
    try:
        trailers = error.trailing_metadata()  # type: ignore[attr-defined]
    except Exception:
        return None
    for item in trailers or ():
        pair = _metadata_pair(item)
        if pair is None:
            continue
        raw_key, raw_value = pair
        if isinstance(raw_key, bytes):
            raw_key = raw_key.decode("ascii", "replace")
        normalized_key = str(raw_key).replace("-", "_").upper()
        if normalized_key != RECONNECT_INTERVAL_TRAILER:
            continue
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("ascii", "replace")
        try:
            interval_ms = max(0, int(str(raw_value)))
        except (TypeError, ValueError):
            # Kotlin's toLongOrNull() path falls back to zero.
            interval_ms = 0
        try:
            details = str(error.details() or "")  # type: ignore[attr-defined]
        except Exception:
            details = ""
        return ReconnectDirective(interval_ms, details)
    return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ResilientEyeStream:
    """One logical EYE session spanning backend-requested bidi reconnects.

    Only INVALID_ARGUMENT carrying RECONNECT_INTERVAL is recoverable.  A new
    bidi call logs in with resetSeq=false and replays the current PID window
    byte-for-byte at the JSON field level (including original seq/timestamp).
    """

    def __init__(
        self,
        call_factory: Callable[[], Any],
        login_wire_factory: Callable[[bool], bytes],
        *,
        initial_reset_seq: bool,
        login_timeout: float,
        replay: Optional[PacketReplayWindow] = None,
        on_message: Optional[Callable[[str, Any], Awaitable[None] | None]] = None,
        on_reconnect: Optional[Callable[[ReconnectDirective, int], Awaitable[None] | None]] = None,
    ) -> None:
        self._call_factory = call_factory
        self._login_wire_factory = login_wire_factory
        self._initial_reset_seq = bool(initial_reset_seq)
        self._login_timeout = float(login_timeout)
        self.replay = replay if replay is not None else PacketReplayWindow()
        self._on_message = on_message
        self._on_reconnect = on_reconnect
        self._ready = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._call: Any = None
        self._runner: Optional[asyncio.Task[None]] = None
        self._initial_login: Optional[asyncio.Future[bool]] = None
        self._terminal: Optional[asyncio.Future[BaseException]] = None
        self._last_replayed_serial = -1
        self._closing = False

    @property
    def runner(self) -> asyncio.Task[None]:
        if self._runner is None:
            raise RuntimeError("backend stream has not been started")
        return self._runner

    async def start(self) -> None:
        if self._runner is not None:
            return
        loop = asyncio.get_running_loop()
        self._initial_login = loop.create_future()
        self._terminal = loop.create_future()
        self._runner = asyncio.create_task(self._run(), name="eye-backend-stream")
        await self._wait_initial_login()

    async def _wait_initial_login(self) -> None:
        assert self._initial_login is not None and self._terminal is not None
        done, _ = await asyncio.wait(
            {self._initial_login, self._terminal},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._terminal in done:
            raise self._terminal.result()
        if not self._initial_login.result():
            raise PermissionError("PokerEYE backend rejected login")

    async def _wait_ready(self) -> None:
        assert self._terminal is not None
        while not self._ready.is_set():
            if self._terminal.done():
                raise self._terminal.result()
            waiter = asyncio.create_task(self._ready.wait())
            done, _ = await asyncio.wait({waiter, self._terminal}, return_when=asyncio.FIRST_COMPLETED)
            if waiter not in done:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                raise self._terminal.result()

    async def _dispatch(self, message_type: str, body: Any) -> None:
        if self._on_message is not None:
            await _maybe_await(self._on_message(message_type, body))

    async def _read_call(self, call: Any, login_result: asyncio.Future[bool]) -> BaseException:
        try:
            async for wire in call:
                message_type, body = decode_response(wire)
                if message_type == "SCLogin" and not login_result.done():
                    accepted = bool(body.get("loginSuccess")) if isinstance(body, dict) else False
                    login_result.set_result(accepted)
                await self._dispatch(message_type, body)
            return ConnectionError("PokerEYE backend stream ended without status")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return exc
        finally:
            if not login_result.done():
                login_result.set_result(False)

    async def _finish_call(self, call: Any) -> None:
        try:
            await _maybe_await(call.done_writing())
        except Exception:
            pass

    async def _run(self) -> None:
        assert self._initial_login is not None and self._terminal is not None
        reconnecting = False
        try:
            while not self._closing:
                call = self._call_factory()
                login_result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                reader = asyncio.create_task(self._read_call(call, login_result))
                failure: BaseException
                try:
                    await call.write(self._login_wire_factory(False if reconnecting else self._initial_reset_seq))
                    accepted = await asyncio.wait_for(asyncio.shield(login_result), timeout=self._login_timeout)
                    if not accepted:
                        # If login was interrupted by a status-bearing stream
                        # failure, preserve that AioRpcError and its trailers;
                        # a plain SCLogin rejection remains fatal.
                        await asyncio.sleep(0)
                        if reader.done() and not reader.cancelled():
                            raise reader.result()
                        raise PermissionError("PokerEYE backend rejected login")

                    async with self._write_lock:
                        self._call = call
                        if reconnecting:
                            replayed = self.replay.snapshot()
                            for record in replayed:
                                # Do not regenerate seq/timestamp on replay.
                                await call.write(encode_envelope("CSPacket", record.packet))
                            if replayed:
                                self._last_replayed_serial = max(row.serial for row in replayed)
                        self._ready.set()
                    if not self._initial_login.done():
                        self._initial_login.set_result(True)
                    failure = await reader
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    failure = exc
                    # grpc.aio can report a generic write-state exception just
                    # before its reader exposes the authoritative AioRpcError.
                    # Prefer the latter so RECONNECT_INTERVAL is not discarded.
                    if reconnect_directive(exc) is None and not isinstance(exc, (PermissionError, TimeoutError)):
                        try:
                            failure = await asyncio.wait_for(asyncio.shield(reader), timeout=1.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            failure = exc
                finally:
                    async with self._write_lock:
                        if self._call is call:
                            self._ready.clear()
                            self._call = None
                    if not reader.done():
                        reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                    await self._finish_call(call)

                directive = reconnect_directive(failure)
                if directive is None or self._closing:
                    raise failure
                reconnecting = True
                if self._on_reconnect is not None:
                    await _maybe_await(self._on_reconnect(directive, len(self.replay)))
                if directive.interval_ms:
                    await asyncio.sleep(directive.interval_ms / 1000.0)
        except asyncio.CancelledError:
            if not self._closing:
                raise
        except BaseException as exc:
            if not self._terminal.done():
                self._terminal.set_result(exc)
            if not self._initial_login.done():
                self._initial_login.set_result(False)
        finally:
            self._ready.clear()

    async def send_packet(self, packet: dict[str, Any]) -> None:
        record = self.replay.append(packet)
        while True:
            await self._wait_ready()
            async with self._write_lock:
                if record.serial <= self._last_replayed_serial:
                    return
                call = self._call
                if call is None or not self._ready.is_set():
                    continue
                try:
                    await call.write(encode_envelope("CSPacket", record.packet))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The reader owns status/trailer classification.  Clearing
                    # readiness keeps the local bridge alive until it reconnects.
                    self._ready.clear()

    async def send_wire(self, wire: bytes) -> None:
        """Send a non-replayed control frame such as CSPing."""
        while True:
            await self._wait_ready()
            async with self._write_lock:
                call = self._call
                if call is None or not self._ready.is_set():
                    continue
                try:
                    await call.write(wire)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._ready.clear()

    async def wait_terminated(self) -> None:
        """Wait for a non-recoverable termination and surface its cause."""
        if self._runner is None or self._terminal is None:
            raise RuntimeError("backend stream has not been started")
        await self._runner
        if self._terminal.done():
            raise self._terminal.result()

    async def close(self) -> None:
        self._closing = True
        self._ready.set()
        call = self._call
        if call is not None:
            await self._finish_call(call)
        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
        if self._runner is not None:
            await asyncio.gather(self._runner, return_exceptions=True)


class _FakeStatusCode:
    name = "INVALID_ARGUMENT"


class _FakeReconnectError(Exception):
    def code(self) -> _FakeStatusCode:
        return _FakeStatusCode()

    def details(self) -> str:
        return "PID_CHANGED"

    def trailing_metadata(self) -> tuple[tuple[str, str], ...]:
        return (("reconnect-interval", "0"),)


_FAKE_STREAM_END = object()


class _FakeBidiCall:
    """Offline grpc.aio-shaped call used only by the reconnect selftest."""

    def __init__(self, fail_after_first_packet: bool) -> None:
        self.fail_after_first_packet = fail_after_first_packet
        self.writes: list[bytes] = []
        self._responses: asyncio.Queue[Any] = asyncio.Queue()
        self._ended = False

    def __aiter__(self) -> "_FakeBidiCall":
        return self

    async def __anext__(self) -> bytes:
        value = await self._responses.get()
        if value is _FAKE_STREAM_END:
            raise StopAsyncIteration
        if isinstance(value, BaseException):
            raise value
        return value

    async def write(self, wire: bytes) -> None:
        self.writes.append(wire)
        message_type, _body = decode_response(wire)
        if message_type == "CSLogin":
            await self._responses.put(encode_envelope("SCLogin", {"loginSuccess": True}))
        elif message_type == "CSPacket" and self.fail_after_first_packet:
            self.fail_after_first_packet = False
            await self._responses.put(_FakeReconnectError("PID_CHANGED"))

    async def done_writing(self) -> None:
        if not self._ended:
            self._ended = True
            await self._responses.put(_FAKE_STREAM_END)


def reconnect_protocol_selftest() -> None:
    directive = reconnect_directive(_FakeReconnectError("PID_CHANGED"))
    assert directive == ReconnectDirective(0, "PID_CHANGED")

    window = PacketReplayWindow()
    window.append({"pid": "0", "seq": 1})
    window.append({"pid": "0", "seq": 2})
    window.append({"pid": "13765731", "seq": 3})
    assert [row.packet["seq"] for row in window.snapshot()] == [3]
    for seq in range(4, 59):
        window.append({"pid": "13765731", "seq": seq})
    assert len(window) == REPLAY_PACKET_LIMIT
    assert window.snapshot()[0].packet["seq"] == 9


async def resilient_stream_selftest() -> None:
    calls: list[_FakeBidiCall] = []
    reconnect_seen = asyncio.Event()

    def call_factory() -> _FakeBidiCall:
        call = _FakeBidiCall(fail_after_first_packet=not calls)
        calls.append(call)
        return call

    async def on_reconnect(directive: ReconnectDirective, replay_count: int) -> None:
        assert directive.details == "PID_CHANGED"
        assert replay_count == 1
        reconnect_seen.set()

    base_login = LoginParams("id", "secret", "version", "en", "PPPoker", "device", "serial", True)
    initial_wire = login_envelope(base_login)
    reconnect_wire = login_envelope(replace(base_login, reset_seq=False))
    stream = ResilientEyeStream(
        call_factory,
        lambda reset_seq: initial_wire if reset_seq else reconnect_wire,
        initial_reset_seq=True,
        login_timeout=1.0,
        on_reconnect=on_reconnect,
    )
    await stream.start()
    packet = {
        "direction": "IN",
        "type": "pb.HeartBeatRSP",
        "pid": "13765731",
        "cmd": "pb.HeartBeatRSP",
        "uid": "13765731",
        "data": "CAE=",
        "timestamp": 123456789,
        "location": "TABLE",
        "dataExtra": "",
        "seq": 77,
    }
    await stream.send_packet(packet)
    await asyncio.wait_for(reconnect_seen.wait(), timeout=1.0)
    deadline = asyncio.get_running_loop().time() + 1.0
    while (len(calls) < 2 or len(calls[1].writes) < 2) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    assert len(calls) == 2
    initial_kind, initial_body = decode_response(calls[0].writes[0])
    reconnect_kind, reconnect_body = decode_response(calls[1].writes[0])
    assert initial_kind == reconnect_kind == "CSLogin"
    assert initial_body["resetSeq"] is True
    assert reconnect_body["resetSeq"] is False
    first_packet_wire = next(wire for wire in calls[0].writes if decode_envelope(wire)[0] == "CSPacket")
    replay_packet_wire = next(wire for wire in calls[1].writes if decode_envelope(wire)[0] == "CSPacket")
    assert replay_packet_wire == first_packet_wire
    _, replay_body = decode_response(replay_packet_wire)
    assert replay_body["seq"] == 77 and replay_body["timestamp"] == 123456789
    next_packet = dict(packet, seq=78, timestamp=123456790)
    await stream.send_packet(next_packet)
    _, continued_body = decode_response(calls[1].writes[-1])
    assert continued_body["seq"] == 78 and continued_body["timestamp"] == 123456790
    await stream.close()


def _unwrap_bridge_object(value: dict[str, Any]) -> Optional[dict[str, Any]]:
    # Offline pcap decoders wrap the actual local-socket object in ``payload``.
    if isinstance(value.get("payload"), dict):
        value = value["payload"]
    if isinstance(value.get("msg"), str):
        try:
            value = json.loads(value["msg"])
        except json.JSONDecodeError as exc:
            raise ProtocolError("invalid nested bridge msg JSON") from exc
    if not isinstance(value, dict) or not value.get("cmd"):
        return None
    return value


def normalize_packet(value: dict[str, Any]) -> dict[str, Any]:
    raw = _unwrap_bridge_object(value)
    if raw is None:
        raise ProtocolError("not a traffic packet")
    direction = str(raw.get("direction") or "").upper()
    direction = {
        "SERVERTOCLIENT": "IN",
        "CLIENTTOSERVER": "OUT",
        "SERVER_TO_CLIENT": "IN",
        "CLIENT_TO_SERVER": "OUT",
    }.get(direction, direction)
    if direction not in ("IN", "OUT"):
        raise ProtocolError(f"unsupported packet direction: {direction!r}")
    cmd = str(raw.get("cmd") or "")
    data = raw.get("data", "")
    if not isinstance(data, str):
        data = compact_json(data)
    # The app duplicates cmd into CSPacketDto.type and CSPacketDto.cmd.
    return {
        "direction": direction,
        "type": cmd,
        "pid": str(raw.get("pid") or ""),
        "cmd": cmd,
        "uid": str(raw.get("uid") or ""),
        "data": data,
        "timestamp": int(raw.get("timestamp") or int(time.time() * 1000)),
        "location": str(raw.get("location") or "OTHERS"),
        "dataExtra": str(raw.get("dataExtra", raw.get("extraData", "")) or ""),
        "seq": int(raw.get("seq") or 0),
    }


def packet_envelope(value: dict[str, Any]) -> bytes:
    return encode_envelope("CSPacket", normalize_packet(value))


def iter_json_objects(path: Path) -> Iterator[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("["):
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise ProtocolError("JSON input must be an array or NDJSON")
        for item in loaded:
            if isinstance(item, dict):
                yield item
        return
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("==="):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            # Bridge console logs are allowed in the same file; only JSON records
            # participate in direct packet replay.
            continue
        if isinstance(item, dict):
            yield item


def load_packets(path: Path, start: int = 0, limit: Optional[int] = None, *, strict: bool = False) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    skipped = 0
    for item in iter_json_objects(path):
        try:
            raw = _unwrap_bridge_object(item)
        except ProtocolError:
            if strict:
                raise
            skipped += 1
            continue
        if raw is not None:
            packets.append(normalize_packet(raw))
    if skipped:
        print(f"[WARN] skipped {skipped} malformed/truncated packet record(s)", file=sys.stderr)
    if start < 0:
        raise ProtocolError("packet start must be non-negative")
    return packets[start:] if limit is None else packets[start:start + max(0, limit)]


def packet_manifest(packets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(packets)
    by_cmd: dict[str, int] = {}
    for row in rows:
        cmd = str(row.get("cmd") or "")
        by_cmd[cmd] = by_cmd.get(cmd, 0) + 1
    canonical = compact_json(rows).encode("utf-8")
    return {
        "packets": len(rows),
        "first": rows[0]["cmd"] if rows else None,
        "last": rows[-1]["cmd"] if rows else None,
        "commands": by_cmd,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def response_summary(message_type: str, body: Any) -> str:
    if not isinstance(body, dict):
        return f"[{message_type}] non-JSON body length={len(str(body))}"
    if message_type == "SCLogin":
        # connectionId/sessionInfo/args can be credentials.  Never print them.
        return f"[SCLogin] success={bool(body.get('loginSuccess'))} workMode={body.get('workMode')} wle={body.get('wle')}"
    if message_type == "SCStatus":
        return f"[SCStatus] status={body.get('status')} message={body.get('message')} hash={body.get('hash') or '-'}"
    if message_type == "SCPing":
        return f"[SCPing] timestamp={body.get('timestamp')}"
    if message_type == "SCAction":
        # Action decisions are the purpose of direct diagnostics and contain no
        # login credential. Keep arguments compact, but expose the exact decision.
        return (
            "[SCAction] "
            f"type={body.get('type')} subtype={body.get('subtype')} "
            f"amount={body.get('amount')} delay={body.get('delay')} "
            f"lifetime={body.get('lifetime')} message={body.get('message')} "
            f"arguments={compact_json(body.get('arguments')) if not isinstance(body.get('arguments'), str) else body.get('arguments')}"
        )
    if message_type == "SCSettingsBillingRates":
        # These are strategy/profile selectors, not credentials.  Showing the
        # values is essential when comparing otherwise identical oracle runs.
        safe = {
            key: body.get(key)
            for key in ("gameType", "workingMode", "statProfileName")
            if key in body
        }
        return f"[SCSettingsBillingRates] {compact_json(safe)}"
    keys = ",".join(sorted(str(key) for key in body))
    return f"[{message_type}] keys={keys}"


def _p_int(field: int, value: int) -> bytes:
    if field <= 0 or value < 0:
        raise ProtocolError("cleanup protobuf integers must be non-negative")
    return _varint((field << 3) | 0) + _varint(value)


def _protobuf_varints(payload: bytes) -> dict[int, list[int]]:
    """Read only the primitive fields needed to derive a clean table exit."""
    result: dict[int, list[int]] = {}
    pos = 0
    while pos < len(payload):
        tag, pos = _read_varint(payload, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _read_varint(payload, pos)
            result.setdefault(field, []).append(value)
        elif wire == 1:
            pos += 8
        elif wire == 2:
            size, pos = _read_varint(payload, pos)
            pos += size
        elif wire == 5:
            pos += 4
        else:
            raise ProtocolError(f"unsupported cleanup protobuf wire {wire}")
        if pos > len(payload):
            raise ProtocolError("truncated cleanup protobuf")
    return result


def cleanup_packets(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a best-effort Finish/StandUp/Leave tail for prefix diagnostics.

    Full-session fixtures already contain LeaveRoomRSP and return an empty tail.
    Prefix fixtures must never strand the backend account at an outstanding hint.
    """
    commands = [str(row.get("cmd") or "") for row in packets]
    if "pb.EnterRoomRSP" not in commands or "pb.LeaveRoomRSP" in commands:
        return []
    uid = next((str(row.get("uid") or "") for row in packets if str(row.get("uid") or "") not in ("", "0")), "0")
    table_id = 0
    hero_seat = 0
    for row in packets:
        command = str(row.get("cmd") or "")
        try:
            body = base64.b64decode(str(row.get("data") or ""), validate=True)
            fields = _protobuf_varints(body)
        except Exception:
            continue
        if command == "pb.RoundHintMultipleTableRSP" and fields.get(4):
            table_id = int(fields[4][0])
        elif command == "pb.EnterRoomREQ" and not table_id and fields.get(1):
            table_id = int(fields[1][0])
        elif command == "pb.SitDownRSP" and fields.get(3):
            hero_seat = int(fields[3][0])

    now = int(time.time() * 1000)
    next_seq = max((int(row.get("seq") or 0) for row in packets), default=-1) + 1
    rows: list[dict[str, Any]] = []

    def add(command: str, body: bytes, location: str = "TABLE") -> None:
        nonlocal next_seq
        rows.append({
            "direction": "IN",
            "type": command,
            "pid": uid,
            "cmd": command,
            "uid": uid,
            "data": base64.b64encode(body).decode("ascii"),
            "timestamp": now + len(rows),
            "location": location,
            "dataExtra": "",
            "seq": next_seq,
        })
        next_seq += 1

    if "pb.RoundHintMultipleTableRSP" in commands and "pb.FinishRoundHintRSP" not in commands and table_id:
        add("pb.FinishRoundHintRSP", _p_int(1, table_id))
    add("pb.StandUpREQ", b"")
    add("pb.StandUpRSP", _p_int(1, 0))
    add("pb.StandUpBRC", _p_int(1, hero_seat))
    add("pb.LeaveRoomREQ", _p_int(1, 0) + _p_int(2, 0) + _p_int(3, 0))
    # The target is an ordinary club-only context.  In particular, field 5 is
    # Leagueid, so omitting it is materially different from replaying the stale
    # league id carried by an unrelated reference capture.
    add("pb.LeaveRoomRSP", _p_int(1, 0) + _p_int(3, 0) + _p_int(4, TARGET_CLUB_ID) + _p_int(6, 0))
    add("pb.OtherLeaveRoomBRC", _p_int(1, int(uid or 0)), "OTHERS")
    return rows


def protocol_selftest() -> None:
    body = '{"timestamp":1}'
    expected = bytes.fromhex("0a06435350696e67120f7b2274696d657374616d70223a317d")
    actual = encode_envelope("CSPing", body)
    assert actual == expected, (actual.hex(), expected.hex())
    assert decode_envelope(actual) == ("CSPing", body)
    sample = {
        "direction": "ServerToClient",
        "pid": "1",
        "uid": "2",
        "cmd": "pb.HeartBeatRSP",
        "data": base64.b64encode(b"\x08\x01").decode(),
        "timestamp": 123,
        "location": "TABLE",
        "seq": 0,
    }
    packet = normalize_packet(sample)
    assert packet["direction"] == "IN"
    assert packet["type"] == packet["cmd"] == "pb.HeartBeatRSP"
    assert packet["dataExtra"] == ""
    status_wire = encode_envelope("SCStatus", {"status": "ERROR", "message": "GAME_IS_BROKEN", "hash": "abc"})
    kind, status = decode_response(status_wire)
    assert kind == "SCStatus" and status["message"] == "GAME_IS_BROKEN"
    assert "GAME_IS_BROKEN" in response_summary(kind, status)
    prefix = [
        dict(packet, cmd="pb.EnterRoomRSP", type="pb.EnterRoomRSP"),
        dict(packet, cmd="pb.SitDownRSP", type="pb.SitDownRSP", data=base64.b64encode(_p_int(3, 4)).decode()),
        dict(packet, cmd="pb.RoundHintMultipleTableRSP", type="pb.RoundHintMultipleTableRSP",
             data=base64.b64encode(_p_int(4, 12345)).decode()),
    ]
    tail = cleanup_packets(prefix)
    assert [row["cmd"] for row in tail] == [
        "pb.FinishRoundHintRSP", "pb.StandUpREQ", "pb.StandUpRSP", "pb.StandUpBRC",
        "pb.LeaveRoomREQ", "pb.LeaveRoomRSP", "pb.OtherLeaveRoomBRC",
    ]
    leave = next(row for row in tail if row["cmd"] == "pb.LeaveRoomRSP")
    leave_fields = _protobuf_varints(base64.b64decode(leave["data"], validate=True))
    assert leave_fields.get(4) == [TARGET_CLUB_ID] and 5 not in leave_fields, leave_fields
    reconnect_protocol_selftest()
    asyncio.run(resilient_stream_selftest())
    print("EYE BACKEND PROTOCOL SELFTEST PASS")
    print(f" - method: {GRPC_METHOD}")
    print(" - CSData/SCData wire: type=1, body=2")
    print(" - CSLogin/CSPacket/CSPing envelopes")
    print(" - SCStatus GAME_IS_BROKEN decoder")
    print(" - prefix diagnostics always Finish/StandUp/Leave")
    print(" - PID_CHANGED reconnect: resetSeq=false + exact 50-packet replay")


async def _connect(args: argparse.Namespace) -> int:
    try:
        import grpc
    except ImportError as exc:
        raise SystemExit("grpcio is required for connect mode") from exc

    password = os.environ.get(args.password_env) if args.password_env else None
    if password is None:
        password = getpass.getpass("PokerEYE agent password/code (not stored): ")
    if not password:
        raise SystemExit("empty credential refused")

    login = LoginParams(
        device_id=args.device_id,
        password=password,
        version=args.version,
        lang=args.lang,
        bundle_identifier=args.bundle_id,
        device=args.device,
        serial=args.serial or args.android_id,
        reset_seq=args.reset_seq,
        geo=args.geo,
        tweak_version=args.tweak_version,
    )
    initial_login_wire = login_envelope(login)
    reconnect_login_wire = login_envelope(replace(login, reset_seq=False))
    # Drop our only extra references as early as possible.  Python cannot promise
    # memory erasure, but the value is never written or logged by this program.
    password = None
    login = None

    packets: list[dict[str, Any]] = []
    if args.packets:
        # A direct experiment must never silently omit a malformed state packet.
        packets = load_packets(
            Path(args.packets),
            args.packet_start,
            args.packet_limit,
            strict=not args.allow_malformed_input,
        )
        print("[PLAN] " + compact_json(packet_manifest(packets)))

    target = f"{args.host}:{args.port}"
    metadata = (("x-android-id", args.android_id), ("x-host", target))
    options = (
        ("grpc.max_receive_message_length", 32 * 1024 * 1024),
        ("grpc.max_send_message_length", 32 * 1024 * 1024),
    )
    channel = grpc.aio.secure_channel(target, grpc.ssl_channel_credentials(), options=options)
    stream: Optional[ResilientEyeStream] = None
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=args.connect_timeout)
        method = channel.stream_stream(
            GRPC_METHOD,
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        broken_event = asyncio.Event()
        error_event = asyncio.Event()
        action_event = asyncio.Event()
        error_statuses: list[dict[str, Any]] = []

        async def on_message(message_type: str, body: Any) -> None:
            print(response_summary(message_type, body))
            if message_type == "SCStatus" and isinstance(body, dict):
                status_message = str(body.get("message") or "").upper()
                if status_message == "GAME_IS_BROKEN":
                    broken_event.set()
                # PID_CHANGED is EYE's normal pid=0 -> assigned pid handshake;
                # the accompanying gRPC trailer drives the reconnect/replay.
                if status_message != "PID_CHANGED" and str(body.get("status") or "").upper() == "ERROR":
                    error_statuses.append(dict(body))
                    error_event.set()
            elif message_type == "SCAction" and isinstance(body, dict):
                action_event.set()

        async def on_reconnect(directive: ReconnectDirective, replay_count: int) -> None:
            detail = directive.details or "backend requested reconnect"
            print(
                f"[RECONNECT] {detail} interval={directive.interval_ms}ms "
                f"replay={replay_count} (normal backend PID/session transition)"
            )

        stream = ResilientEyeStream(
            lambda: method(metadata=metadata, compression=grpc.Compression.Gzip, wait_for_ready=True),
            lambda reset_seq: initial_login_wire if reset_seq else reconnect_login_wire,
            initial_reset_seq=args.reset_seq,
            login_timeout=args.login_timeout,
            on_message=on_message,
            on_reconnect=on_reconnect,
        )
        try:
            await stream.start()
        except PermissionError:
            print("[RESULT] login was not accepted; no packets sent", file=sys.stderr)
            return 2

        print(f"[RESULT] authenticated; packets={len(packets)}")
        ping_task: Optional[asyncio.Task[Any]] = None

        async def ping_loop() -> None:
            while True:
                await asyncio.sleep(2.5)
                await stream.send_wire(ping_envelope())

        async def clean_table() -> None:
            tail = [] if args.no_auto_cleanup else cleanup_packets(packets)
            if not tail:
                return
            print(f"[CLEANUP] Finish/StandUp/Leave packets={len(tail)}")
            for packet in tail:
                packet = dict(packet)
                packet["timestamp"] = int(time.time() * 1000)
                try:
                    await stream.send_packet(packet)
                except Exception as exc:
                    print(f"[CLEANUP] failed at {packet['cmd']}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    return
                if not args.quiet_sends:
                    print(f"[CLEANUP SEND] {packet['cmd']}")
                await asyncio.sleep(0.03)
            await asyncio.sleep(max(0.0, args.cleanup_wait_seconds))
            print("[CLEANUP] table context closed")

        ping_task = asyncio.create_task(ping_loop())
        for index, packet in enumerate(packets, 1):
            if args.interval_ms and index > 1:
                await asyncio.sleep(args.interval_ms / 1000.0)
            if not args.preserve_packet_timestamps:
                packet = dict(packet)
                packet["timestamp"] = int(time.time() * 1000)
            await stream.send_packet(packet)
            if not args.quiet_sends:
                print(f"[SEND] {index}/{len(packets)} {packet['direction']} {packet['location']} {packet['cmd']}")
            watched_event = error_event if args.stop_on_error else broken_event
            if args.stop_on_broken or args.stop_on_error:
                # Give SCStatus a bounded chance to arrive after this exact prefix.
                try:
                    await asyncio.wait_for(watched_event.wait(), timeout=max(0.01, args.status_wait_ms / 1000.0))
                except asyncio.TimeoutError:
                    pass
                if watched_event.is_set():
                    message = str(error_statuses[-1].get("message") or "ERROR") if error_statuses else "GAME_IS_BROKEN"
                    print(f"[RESULT] stopped on {message} after packet {index}: {packet['cmd']}")
                    await clean_table()
                    ping_task.cancel()
                    await asyncio.gather(ping_task, return_exceptions=True)
                    return 3

        await stream.send_wire(ping_envelope())
        await asyncio.sleep(max(0.0, args.observe_seconds))
        messages = [str(row.get("message") or "ERROR") for row in error_statuses]
        print(f"[RESULT] error_statuses={len(messages)} messages={messages}")
        await clean_table()
        ping_task.cancel()
        await asyncio.gather(ping_task, return_exceptions=True)
        return 0
    finally:
        if stream is not None:
            await stream.close()
        await channel.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline/direct PokerEYE gRPC protocol probe")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="verify recovered protobuf/JSON contracts offline")

    inspect = sub.add_parser("inspect", help="normalize bridge/reference packets without network access")
    inspect.add_argument("path")
    inspect.add_argument("--start", type=int, default=0)
    inspect.add_argument("--limit", type=int)

    connect = sub.add_parser("connect", help="explicitly authenticate and optionally send a packet prefix")
    connect.add_argument("--host", required=True)
    connect.add_argument("--port", type=int, default=DEFAULT_PORT)
    connect.add_argument("--android-id", required=True, help="Android secure ID used in gRPC metadata")
    connect.add_argument("--device-id", required=True, help="PokerEYE login/user value")
    connect.add_argument("--serial", help="CSLogin serial; defaults to --android-id")
    connect.add_argument("--version", required=True, help="selected room APK md5Hash")
    connect.add_argument("--bundle-id", required=True, help="selected room profile id (not package name)")
    connect.add_argument("--device", required=True, help="exact app device fingerprint string")
    connect.add_argument("--lang", default="en")
    connect.add_argument("--geo", default="")
    connect.add_argument("--tweak-version", default=DEFAULT_TWEAK_VERSION)
    connect.add_argument("--reset-seq", action="store_true")
    connect.add_argument("--password-env", default="POKEREYE_AGENT_CODE", help="environment variable name; prompt if unset")
    connect.add_argument("--packets", help="reference/NDJSON packet file; omitted means login+ping only")
    connect.add_argument(
        "--allow-malformed-input",
        action="store_true",
        help="skip and warn about malformed archive rows (new synthetic fixtures should remain strict)",
    )
    connect.add_argument("--packet-start", type=int, default=0)
    connect.add_argument("--packet-limit", type=int)
    connect.add_argument("--interval-ms", type=int, default=0)
    connect.add_argument("--quiet-sends", action="store_true")
    connect.add_argument("--preserve-packet-timestamps", action="store_true")
    connect.add_argument("--stop-on-broken", action="store_true")
    connect.add_argument("--stop-on-error", action="store_true")
    connect.add_argument("--status-wait-ms", type=int, default=100, help="per-prefix SCStatus wait with --stop-on-broken")
    connect.add_argument("--connect-timeout", type=float, default=10.0)
    connect.add_argument("--login-timeout", type=float, default=15.0)
    connect.add_argument("--observe-seconds", type=float, default=3.0)
    connect.add_argument("--no-auto-cleanup", action="store_true", help="leave a replayed table open (unsafe for prefix diagnostics)")
    connect.add_argument("--cleanup-wait-seconds", type=float, default=0.5)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "selftest":
        protocol_selftest()
        return 0
    if args.command == "inspect":
        packets = load_packets(Path(args.path), args.start, args.limit)
        print(compact_json(packet_manifest(packets)))
        return 0
    if args.command == "connect":
        return asyncio.run(_connect(args))
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
