"""Trainer-side PokerEYE channel client (length-prefixed JSON over TCP).

The v2 trainer connects to the operator's PokerEYE app (or its backend proxy)
exactly like the legacy hook channel: 4-byte big-endian length prefix + UTF-8
JSON frames. The trainer sends ``traffic``/``broadcast`` frames and consumes
``cc`` (SCAction) frames. Reconnect is bounded with generation tracking so a
late frame from a stale socket is never accepted.
"""
from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

MAX_FRAME = 20_000_000


def lp_pack(obj: dict) -> bytes:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer disconnected")
        data.extend(chunk)
    return bytes(data)


def lp_read_sock(sock: socket.socket) -> Optional[bytes]:
    try:
        header = _read_exact(sock, 4)
    except (ConnectionError, OSError):
        return None
    size = struct.unpack(">I", header)[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError(f"bad frame size {size}")
    return _read_exact(sock, size)


@dataclass
class EyeFrame:
    tag: str
    data: Any
    msg: Any = None
    package_name: Optional[str] = None
    generation: int = 0
    ts: float = field(default_factory=time.time)


class EyeChannelClient:
    """Bounded-reconnect TCP client to the PokerEYE endpoint.

    ``on_frame`` receives every frame; ``cc_queue`` receives only ``tag=="cc"``.
    A write failure invalidates the current generation; the reader thread drops
    late frames from stale sockets (never accept a late cc/control frame from a
    retired generation).
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        package_name: str = "com.lein.pppoker.android",
        on_frame: Optional[Callable[[EyeFrame], None]] = None,
        on_state: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        reconnect_base: float = 0.25,
        reconnect_max: float = 3.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host, self.port = host, int(port)
        self.package_name = package_name
        self.on_frame = on_frame
        self.on_state = on_state
        self.reconnect_base, self.reconnect_max = reconnect_base, reconnect_max
        self.connect_timeout = connect_timeout
        self.cc_queue: "queue.Queue[EyeFrame]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._generation = 0
        self._writer_lock = threading.Lock()
        self.connected = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="eye-channel")

    def start(self) -> None:
        self._thread.start()

    # -- state ------------------------------------------------------------
    def _emit_state(self, event: str, **fields: Any) -> None:
        if self.on_state:
            try:
                self.on_state(event, fields)
            except Exception:
                pass

    # -- connection loop --------------------------------------------------
    def _connect(self) -> Optional[socket.socket]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        try:
            sock.connect((self.host, self.port))
        except OSError:
            sock.close()
            return None
        sock.settimeout(None)
        return sock

    def _run(self) -> None:
        delay = self.reconnect_base
        while not self._stop.is_set():
            sock = self._connect()
            if sock is None:
                self._emit_state("eye.connect_failed", host=self.host, port=self.port)
                if self._stop.wait(delay):
                    break
                delay = min(self.reconnect_max, delay * 1.7)
                continue
            delay = self.reconnect_base
            with self._lock:
                self._sock = sock
                self._generation += 1
                generation = self._generation
            self.connected.set()
            self._emit_state("eye.connected", host=self.host, port=self.port, generation=generation)
            try:
                self._read_loop(sock, generation)
            except (ConnectionError, OSError, ValueError) as exc:
                self._emit_state("eye.disconnected", generation=generation, error=type(exc).__name__)
            finally:
                self.connected.clear()
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                if self._stop.wait(delay):
                    break
                delay = min(self.reconnect_max, delay * 1.7)

    def _read_loop(self, sock: socket.socket, generation: int) -> None:
        while not self._stop.is_set():
            raw = lp_read_sock(sock)
            if raw is None:
                return
            with self._lock:
                current = self._generation
            if generation != current:
                return  # stale socket: never accept a late frame
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            frame = EyeFrame(
                tag=str(obj.get("tag", "")),
                data=obj.get("data"),
                msg=obj.get("msg"),
                package_name=obj.get("packageName"),
                generation=generation,
            )
            if frame.tag == "cc":
                self.cc_queue.put(frame)
            if self.on_frame:
                try:
                    self.on_frame(frame)
                except Exception:
                    pass

    # -- sending ----------------------------------------------------------
    def send(self, tag: str, data: Any, *, msg: Any = "") -> bool:
        """Send one frame on the current generation. Returns False if disconnected."""
        frame = {"data": data if data is not None else "", "msg": msg or "",
                 "packageName": self.package_name, "tag": tag}
        payload = lp_pack(frame)
        with self._writer_lock:
            with self._lock:
                sock = self._sock
                if sock is None:
                    return False
            try:
                sock.sendall(payload)
                return True
            except OSError:
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                self.connected.clear()
                return False

    def send_outer(self, envelope: dict, label: str = "") -> bool:
        """Send a pre-built envelope (like the legacy eye_send_outer)."""
        frame = dict(envelope)
        frame.setdefault("packageName", self.package_name)
        frame.setdefault("tag", label or "traffic")
        payload = lp_pack(frame)
        with self._writer_lock:
            with self._lock:
                sock = self._sock
                if sock is None:
                    return False
            try:
                sock.sendall(payload)
                return True
            except OSError:
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                self.connected.clear()
                return False

    # -- lifecycle --------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.connected.clear()
