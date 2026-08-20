"""24h DOUBLE BOARD sit-block. Wall-clock JSON, survives trainer restart."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

RECENT_SECONDS = 24 * 3600


def default_path() -> Path:
    raw = os.getenv("POKEREYE_DB_RECENT", "").strip()
    if raw:
        return Path(raw)
    data = Path("/opt/pokereye/data/db_recent.json")
    if data.parent.is_dir():
        return data
    return Path(__file__).resolve().parents[2] / "config" / "db_recent.json"


class DbRecentStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        tables = raw.get("tables") if isinstance(raw, dict) else None
        if not isinstance(tables, dict):
            return
        now = time.time()
        for key, value in tables.items():
            if not isinstance(value, dict):
                continue
            try:
                until = float(value.get("until") or 0)
            except (TypeError, ValueError):
                continue
            if until > now:
                self._rows[str(key)] = dict(value)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "tables": dict(self._rows)}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _purge_locked(self) -> None:
        now = time.time()
        dead = [key for key, row in self._rows.items() if float(row.get("until") or 0) <= now]
        for key in dead:
            self._rows.pop(key, None)

    def blocked(self, table_id: int) -> bool:
        key = str(int(table_id))
        with self._lock:
            self._purge_locked()
            return key in self._rows

    def remember(
        self,
        table_id: int,
        *,
        device_id: str = "",
        reason: str = "DOUBLE BOARD",
    ) -> None:
        key = str(int(table_id))
        row = {
            "until": time.time() + RECENT_SECONDS,
            "reason": str(reason or "DOUBLE BOARD"),
            "device_id": str(device_id or ""),
            "table_id": int(table_id),
        }
        with self._lock:
            self._rows[key] = row
            self._save()

    def until(self, table_id: int) -> float:
        with self._lock:
            return float((self._rows.get(str(int(table_id))) or {}).get("until") or 0)


_STORE: Optional[DbRecentStore] = None
_STORE_LOCK = threading.Lock()


def configure(path: Optional[Path] = None) -> DbRecentStore:
    global _STORE
    with _STORE_LOCK:
        _STORE = DbRecentStore(path)
        return _STORE


def store() -> DbRecentStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DbRecentStore()
        return _STORE
