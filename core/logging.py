"""Structured per-run/per-device/per-session/per-table logging.

Compact operator view (``operator.txt``) + append-only JSONL evidence
(``events.jsonl`` at each scope). Technical IDs and human display numbers
are separate: ``table_01`` is a recyclable operator label; every event also
carries stable ``table_id`` and ``table_generation``.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str, fallback: str = "unknown") -> str:
    text = _SAFE.sub("_", str(value)).strip("._")
    return text[:96] or fallback


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict, *, flush: bool = False) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                if flush:
                    fh.flush()
                    os.fsync(fh.fileno())


class TableLogger:
    dump_traffic = False

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = _JsonlWriter(directory / "events.jsonl")

    def emit(self, event: str, *, severity: str = "INFO", flush: bool = False, **fields: Any) -> dict:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "severity": severity, **{k: v for k, v in fields.items() if v is not None}}
        self.events.write(record, flush=flush)
        return record

    def close(self) -> None: pass


class DeviceLogger:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = _JsonlWriter(directory / "events.jsonl")
        self._tables: dict[str, TableLogger] = {}
        self._lock = threading.Lock()

    def table(self, label: str) -> TableLogger:
        label = _safe(label, "table")
        with self._lock:
            existing = self._tables.get(label)
            if existing is not None:
                return existing
            tl = TableLogger(self.directory / "tables" / label)
            self._tables[label] = tl
            return tl

    def emit(self, event: str, *, severity: str = "INFO", flush: bool = False, **fields: Any) -> dict:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "severity": severity, **{k: v for k, v in fields.items() if v is not None}}
        self.events.write(record, flush=flush)
        return record

    def close(self) -> None:
        for tl in self._tables.values():
            tl.close()

    def __iter__(self):
        return iter(self._tables.items())


class SessionLogger:
    def __init__(self, root: str | Path = "logs", *, run_id: str | None = None,
                 emulator_name: str | None = None, hero_ref: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.emulator_name = emulator_name
        self.hero_ref = hero_ref
        self.directory = Path(root) / f"run_{_safe(self.run_id)}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_events = _JsonlWriter(self.directory / "events.jsonl")
        self.operator_path = self.directory / "operator.txt"
        self.manifest_path = self.directory / "manifest.json"
        self._operator_lock = threading.Lock()
        self._closed = False
        self._devices: dict[str, DeviceLogger] = {}
        self._device_lock = threading.Lock()
        self._write_manifest()

    def _write_manifest(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "emulator_name": self.emulator_name,
            "hero_ref": self.hero_ref,
        }
        self.manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---- device loggers --------------------------------------------------
    def device(self, name: str) -> DeviceLogger:
        name = _safe(name, "device")
        with self._device_lock:
            existing = self._devices.get(name)
            if existing is not None:
                return existing
            dl = DeviceLogger(self.directory / "devices" / name)
            self._devices[name] = dl
            return dl

    # ---- emit (run-scoped) -----------------------------------------------
    def emit(self, event: str, *, severity: str = "INFO", message: str | None = None,
             flush: bool = False, **fields: Any) -> dict[str, Any]:
        record = {"schema_version": 1, "ts": datetime.now(timezone.utc).isoformat(),
                  "event": event, "severity": severity, "run_id": self.run_id,
                  **{k: v for k, v in fields.items() if v is not None}}
        text = message or event
        if self._closed:
            raise RuntimeError("session logger is closed")
        flush_io = flush or severity in {"WARN", "ERROR"}
        self.run_events.write(record, flush=flush_io)
        with self._operator_lock:
            with self.operator_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{record['ts']} [{severity}] {text}\n")
                if flush_io:
                    fh.flush()
                    os.fsync(fh.fileno())
        return record

    # ---- close -----------------------------------------------------------
    def close(self, *, reason: str = "shutdown") -> None:
        if self._closed:
            return
        self.emit("trainer.stopped", message=f"Trainer остановлен: {reason}", flush=True, reason=reason)
        for dl in self._devices.values():
            dl.close()
        self._closed = True

    def __enter__(self) -> "SessionLogger":
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()