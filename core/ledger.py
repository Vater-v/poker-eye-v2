"""Append-only, crash-safe local accounting ledger (JSONL).

Each hand outcome is finalized exactly once with an idempotency key. The ledger is
the source of truth for "did this action actually complete"; it never re-opens a
finalized hand for a stale ACK.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


class LedgerStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_OPERATOR = "needs_operator"


@dataclass(frozen=True)
class LedgerEntry:
    key: str  # idempotency key: device:table:generation:hand_id:action
    status: LedgerStatus
    action: Optional[str]
    amount: Optional[float]
    ts: float
    extra: Dict[str, Any]


class Ledger:
    """One JSONL file per run; appends are flushed and idempotent per key."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._keys.add(json.loads(line)["key"])
            except (json.JSONDecodeError, KeyError):
                continue

    def finalize(
        self,
        key: str,
        status: LedgerStatus,
        *,
        action: Optional[str] = None,
        amount: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Write exactly once per key. Returns False if the key is already finalized."""
        with self._lock:
            if key in self._keys:
                return False
            entry = LedgerEntry(
                key=key,
                status=status,
                action=action,
                amount=amount,
                ts=time.time(),
                extra=dict(extra or {}),
            )
            record = {
                "key": entry.key,
                "status": entry.status.value,
                "action": entry.action,
                "amount": entry.amount,
                "ts": entry.ts,
                "extra": entry.extra,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._keys.add(key)
            return True

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def count(self) -> int:
        with self._lock:
            return len(self._keys)
