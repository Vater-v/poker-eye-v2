"""Generation-safe device/table identities and reusable display table numbers."""
from __future__ import annotations
from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class TableLease:
    device_id: str
    table_id: str
    generation: int
    number: int


class SessionRegistry:
    """Allocates recyclable human labels while retaining immutable generation IDs."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation: dict[tuple[str, str], int] = {}
        self._active: dict[tuple[str, str], TableLease] = {}
        self._free: dict[str, list[int]] = {}
        self._next: dict[str, int] = {}

    def open_table(self, device_id: str, table_id: str) -> TableLease:
        key = (device_id, table_id)
        with self._lock:
            existing = self._active.get(key)
            if existing:
                return existing
            generation = self._generation.get(key, 0) + 1
            self._generation[key] = generation
            free = self._free.setdefault(device_id, [])
            number = free.pop(0) if free else self._next.get(device_id, 1)
            if not free:
                self._next[device_id] = max(self._next.get(device_id, 1), number + 1)
            lease = TableLease(device_id, table_id, generation, number)
            self._active[key] = lease
            return lease

    def close_table(self, lease: TableLease) -> bool:
        key = (lease.device_id, lease.table_id)
        with self._lock:
            if self._active.get(key) != lease:
                return False
            del self._active[key]
            free = self._free.setdefault(lease.device_id, [])
            free.append(lease.number)
            free.sort()
            return True

    def active(self, device_id: str, table_id: str) -> TableLease | None:
        with self._lock:
            return self._active.get((device_id, table_id))
