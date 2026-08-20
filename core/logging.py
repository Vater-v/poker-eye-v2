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
        self.technical_path = self.directory / "technical.log"
        self.error_path = self.directory / "error.log"
        # Create the canonical files immediately.  Monitoring and tests must not
        # interpret a quiet run as a missing/broken logging pipeline.
        for path in (self.operator_path, self.technical_path, self.error_path):
            path.touch(exist_ok=True)
        self.manifest_path = self.directory / "manifest.json"
        self._operator_lock = threading.Lock()
        self._technical_lock = threading.Lock()
        self._error_lock = threading.Lock()
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
             operator: bool = False, flush: bool = False, **fields: Any) -> dict[str, Any]:
        record = {"schema_version": 1, "ts": datetime.now(timezone.utc).isoformat(),
                  "event": event, "severity": severity, "run_id": self.run_id,
                  **{k: v for k, v in fields.items() if v is not None}}
        if message:
            record["message"] = message
        if operator:
            record["operator"] = True
        text = message or event
        if self._closed:
            raise RuntimeError("session logger is closed")
        flush_io = flush or severity in {"WARN", "ERROR"}
        self.run_events.write(record, flush=flush_io)
        # Every event has a compact plain-text technical twin, but the operator
        # file is reserved strictly for the same human Russian lines shown in console.
        with self._technical_lock:
            with self.technical_path.open("a", encoding="utf-8", newline="\n") as fh:
                field_text = " ".join(
                    f"{k}={json.dumps(v, ensure_ascii=False, default=str)}"
                    for k, v in fields.items() if v is not None
                )
                fh.write(f"{record['ts']} [{severity}] {event}{(' ' + field_text) if field_text else ''}\n")
                if flush_io:
                    fh.flush()
                    os.fsync(fh.fileno())
        # A supplied human-readable message is operator-facing by definition.
        if operator or message is not None:
            with self._operator_lock:
                with self.operator_path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"{record['ts']} {text}\n")
                    if flush_io:
                        fh.flush()
                        os.fsync(fh.fileno())
        return record

    def error(self, event: str, *, message: str = "", **fields: Any) -> dict[str, Any]:
        """Append one self-contained diagnostic incident beside the session logs.

        ``error.log`` is intentionally line-delimited JSON.  It contains enough
        state to diagnose a missed/failed action without scraping console output;
        raw Coin bytes remain in the always-on PCAP instead of being duplicated here.
        """
        record = {
            "schema_version": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            "run_id": self.run_id,
            "message": str(message or ""),
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with self._error_lock:
            with self.error_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        # Keep the canonical structured event stream in sync with the incident file.
        self.run_events.write({
            "schema_version": 1,
            "ts": record["ts"],
            "event": f"error.{event}",
            "severity": "ERROR",
            "run_id": self.run_id,
            **{k: v for k, v in fields.items() if v is not None},
        }, flush=True)
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