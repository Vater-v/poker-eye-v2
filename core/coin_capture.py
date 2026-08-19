"""Always-on raw Coin hook capture for production diagnostics.

The Android native hook already gives Trainer the exact SmartFox WebSocket payload.
This module records that payload BEFORE router/business handling, so captures remain
useful even when table routing or PokerEYE startup fails.

Classic PCAP is used with LINKTYPE_USER0 (147). Each record contains a compact
16-byte Hmuriy pseudo-header followed by the untouched Coin payload:

    0..3   b"HMR1"
    4..7   ws_id (big-endian uint32)
    8      direction: 0=in, 1=out
    9..11  reserved
    12..15 raw payload length (big-endian uint32)
    16..   original Coin WebSocket payload

Writing is done on a dedicated bounded background queue. Capture backpressure can
drop diagnostic records but never blocks native ingress or CoinPoker.
"""
from __future__ import annotations

import os
import queue
import re
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

PCAP_MAGIC = 0xA1B2C3D4
LINKTYPE_USER0 = 147
PCAP_SNAPLEN = 20_000_016
GLOBAL_HEADER = struct.pack(
    "<IHHiIII", PCAP_MAGIC, 2, 4, 0, 0, PCAP_SNAPLEN, LINKTYPE_USER0
)
HMR_HEADER = struct.Struct("!4sIB3xI")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str, fallback: str = "device") -> str:
    text = _SAFE.sub("_", str(value)).strip("._")
    return text[:96] or fallback


class _DeviceCapture:
    def __init__(
        self,
        root: Path,
        device_id: str,
        *,
        segment_bytes: int,
        segments: int,
        on_open: Optional[Callable[[str, Path], None]],
    ) -> None:
        self.device_id = str(device_id)
        self.directory = root / _safe(self.device_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.segment_bytes = int(segment_bytes)
        self.segments = int(segments)
        self.on_open = on_open
        self.index = 0
        self.size = 0
        self.fh = None
        self.last_flush = 0.0
        self._open()

    def _path(self) -> Path:
        return self.directory / f"coin_{self.index:02d}.pcap"

    def _open(self) -> None:
        if self.fh is not None:
            self.fh.flush()
            self.fh.close()
        path = self._path()
        self.fh = path.open("wb")
        self.fh.write(GLOBAL_HEADER)
        self.size = len(GLOBAL_HEADER)
        self.last_flush = time.monotonic()
        if self.on_open is not None:
            self.on_open(self.device_id, path)

    def write(self, ts: float, ws_u32: int, direction: int, raw: bytes) -> None:
        pseudo = HMR_HEADER.pack(
            b"HMR1",
            int(ws_u32) & 0xFFFFFFFF,
            1 if int(direction) else 0,
            len(raw),
        )
        packet = pseudo + raw
        sec = int(ts)
        usec = max(0, min(999_999, int((ts - sec) * 1_000_000)))
        record = struct.pack("<IIII", sec, usec, len(packet), len(packet)) + packet

        if self.size + len(record) > self.segment_bytes:
            self.index = (self.index + 1) % self.segments
            self._open()

        self.fh.write(record)
        self.size += len(record)

        now = time.monotonic()
        if now - self.last_flush >= 1.0:
            self.fh.flush()
            self.last_flush = now

    def close(self) -> None:
        if self.fh is not None:
            self.fh.flush()
            self.fh.close()
            self.fh = None


class RawCoinCaptureManager:
    """Bounded async raw capture; never blocks production ingress."""

    def __init__(
        self,
        root: str | Path,
        *,
        queue_size: int = 8192,
        segment_bytes: int = 64 * 1024 * 1024,
        segments: int = 4,
        on_open: Optional[Callable[[str, Path], None]] = None,
        on_drop: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.segment_bytes = int(segment_bytes)
        self.segments = int(segments)
        self.on_open = on_open
        self.on_drop = on_drop
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(128, int(queue_size)))
        self._writers: dict[str, _DeviceCapture] = {}
        self._stop = threading.Event()
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._run, name="coin-pcap-writer", daemon=True
        )
        self._thread.start()

    @property
    def dropped(self) -> int:
        return int(self._dropped)

    def observe(self, device_id: str, event: dict[str, Any]) -> None:
        raw = event.get("_raw")
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            return
        raw_bytes = raw if isinstance(raw, bytes) else bytes(raw)
        if not raw_bytes:
            return
        try:
            ws_u32 = int(event.get("_ws_u32") or 0)
            direction = 1 if str(event.get("direction") or "").lower() == "out" else 0
            self._queue.put_nowait(
                (time.time(), str(device_id), ws_u32, direction, raw_bytes)
            )
        except queue.Full:
            self._dropped += 1
            if self.on_drop is not None and self._dropped in {1, 10, 100, 1000}:
                self.on_drop(self._dropped)

    def _writer(self, device_id: str) -> _DeviceCapture:
        writer = self._writers.get(device_id)
        if writer is None:
            writer = _DeviceCapture(
                self.root,
                device_id,
                segment_bytes=self.segment_bytes,
                segments=self.segments,
                on_open=self.on_open,
            )
            self._writers[device_id] = writer
        return writer

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                ts, device_id, ws_u32, direction, raw = item
                self._writer(device_id).write(ts, ws_u32, direction, raw)
            except Exception:
                # Diagnostics must never take down Trainer.
                self._dropped += 1
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._thread.join(timeout=max(0.1, float(timeout)))
        for writer in list(self._writers.values()):
            try:
                writer.close()
            except Exception:
                pass
        self._writers.clear()
