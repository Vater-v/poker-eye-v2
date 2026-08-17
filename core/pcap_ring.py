"""Bounded per-table PCAP ring with a verified game-traffic BPF filter.

Policy (from docs/RETURN_PLAN.md and TRAFFIC_CORPUS.md):

- segment: 64 MiB, 4 segments per active table (ring of 4),
- cap: 256 MiB per table,
- completed capture retention: 3 completed capture sets per table,
- global quota across tables (default 2 GiB),
- BPF filter restricts capture to the verified game port (never ``-i any -s 0``).

The ring writes classic PCAP format itself (24-byte global header + records), so
the policy and rotation logic are fully testable without a capture library. The
actual packet source is a driver (e.g. tcpdump subprocess on Linux); the ring only
stores what the driver feeds it.
"""
from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PCAP_MAGIC = 0xA1B2C3D4
LINKTYPE_ETHERNET = 1
GLOBAL_HEADER = struct.pack("<IHHiIII", PCAP_MAGIC, 2, 4, 0, 0, 65535, LINKTYPE_ETHERNET)


def bpf_for_game_port(port: int, *, interface: Optional[str] = None) -> str:
    """BPF restricting capture to the verified game TCP port, both directions."""
    if not 1 <= int(port) <= 65535:
        raise ValueError("invalid game port")
    iface = f" and host {interface}" if interface else ""
    return f"tcp port {int(port)}{iface}"


@dataclass(frozen=True)
class PcapRingPolicy:
    segment_bytes: int = 64 * 1024 * 1024
    segments: int = 4
    per_table_cap: int = 256 * 1024 * 1024
    completed_retention: int = 3
    global_cap: int = 2 * 1024 * 1024 * 1024
    game_port: int = 17770
    interface: Optional[str] = None

    def __post_init__(self) -> None:
        if self.segment_bytes <= 0 or self.segments <= 0:
            raise ValueError("ring geometry must be positive")
        if self.segment_bytes * self.segments > self.per_table_cap:
            raise ValueError("ring geometry exceeds per-table cap")
        if self.completed_retention < 0:
            raise ValueError("completed retention must be non-negative")

    @property
    def bpf(self) -> str:
        return bpf_for_game_port(self.game_port, interface=self.interface)


@dataclass
class PcapRingStats:
    current_file: str
    current_size: int
    segments_written: int
    completed_sets: int
    dropped: int
    table_bytes: int


class PcapRing:
    """One ring per active table. Thread-safe append-only writer."""

    def __init__(self, directory: str | Path, policy: Optional[PcapRingPolicy] = None) -> None:
        self.policy = policy or PcapRingPolicy()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._current: Optional[Any] = None  # open file handle
        self._current_name: str = ""
        self._current_size: int = 0
        self._segment_index: int = 0
        self._generation: int = 0
        self._dropped: int = 0
        self._completed: List[Path] = []
        self._rotate()

    # -- internal -----------------------------------------------------------
    def _path(self, index: int) -> Path:
        return self.directory / f"seg_{self._generation:04d}_{index:02d}.pcap"

    def _open_segment(self) -> None:
        if self._current is not None:
            self._current.close()
        # A finished generation's files become a completed capture set.
        if self._segment_index >= self.policy.segments and self._generation > 0:
            self._retire_generation()
        self._segment_index = self._segment_index % self.policy.segments
        name = str(self._path(self._segment_index))
        fh = open(name, "wb")
        fh.write(GLOBAL_HEADER)
        self._current = fh
        self._current_name = name
        self._current_size = len(GLOBAL_HEADER)

    def _retire_generation(self) -> None:
        gen_prefix = f"seg_{self._generation:04d}"
        self._completed.append(gen_prefix)
        while len(self._completed) > self.policy.completed_retention:
            self._completed.pop(0)
        # Delete segment files from generations older than the retained sets.
        keep = set(self._completed)
        for p in sorted(self.directory.glob("seg_*.pcap")):
            parts = p.stem.split("_")  # ["seg", generation, index]
            gen = "_".join(parts[:2]) if len(parts) >= 2 else parts[0]
            if gen not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass
        self._generation += 1

    def _rotate(self) -> None:
        with self._lock:
            self._open_segment()

    def _enforce_cap(self) -> None:
        files = sorted(self.directory.glob("seg_*.pcap"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while total > self.policy.per_table_cap and len(files) > 1:
            victim = files.pop(0)
            try:
                total -= victim.stat().st_size
                victim.unlink()
            except OSError:
                pass

    # -- public -------------------------------------------------------------
    def write_packet(self, ts: float, data: bytes) -> bool:
        """Append one packet. Returns False if the packet was dropped (ring full)."""
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        record = struct.pack("<IIII", sec, usec, len(data), len(data)) + data
        with self._lock:
            if self._current_size + len(record) > self.policy.segment_bytes:
                self._segment_index += 1
                self._rotate()
                if self._current_size + len(record) > self.policy.segment_bytes:
                    self._dropped += 1
                    return False
            self._current.write(record)
            self._current_size += len(record)
            if self._current_size >= self.policy.segment_bytes:
                self._segment_index += 1
                self._rotate()
            return True

    def flush(self) -> None:
        with self._lock:
            if self._current is not None:
                self._current.flush()
                os.fsync(self._current.fileno())

    def stats(self) -> PcapRingStats:
        with self._lock:
            files = list(self.directory.glob("seg_*.pcap"))
            return PcapRingStats(
                current_file=self._current_name,
                current_size=self._current_size,
                segments_written=self._segment_index,
                completed_sets=len(self._completed),
                dropped=self._dropped,
                table_bytes=sum(p.stat().st_size for p in files),
            )

    def close(self) -> None:
        with self._lock:
            if self._current is not None:
                self._current.flush()
                os.fsync(self._current.fileno())
                self._current.close()
                self._current = None


class PcapRingManager:
    """Global quota across table rings; a full disk never stops the trainer."""

    def __init__(self, root: str | Path, policy: Optional[PcapRingPolicy] = None) -> None:
        self.policy = policy or PcapRingPolicy()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._rings: Dict[str, PcapRing] = {}

    def ring(self, table_key: str) -> PcapRing:
        with self._lock:
            existing = self._rings.get(table_key)
            if existing is not None:
                return existing
            ring = PcapRing(self.root / table_key, self.policy)
            self._rings[table_key] = ring
            return ring

    def release(self, table_key: str) -> None:
        with self._lock:
            ring = self._rings.pop(table_key, None)
        if ring is not None:
            ring.close()
            self._enforce_global_quota()

    def _enforce_global_quota(self) -> None:
        files = sorted(self.root.glob("*/seg_*.pcap"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while total > self.policy.global_cap and files:
            victim = files.pop(0)
            try:
                total -= victim.stat().st_size
                victim.unlink()
            except OSError:
                pass

    def stats(self) -> Dict[str, PcapRingStats]:
        with self._lock:
            return {k: r.stats() for k, r in self._rings.items()}

    def close_all(self) -> None:
        with self._lock:
            for ring in self._rings.values():
                ring.close()
            self._rings.clear()
