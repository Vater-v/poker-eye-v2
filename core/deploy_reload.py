"""Seated-aware trainer reload. Not an in-process hot-swap of live tables.

Copying /opt/pokereye/core does not change already-imported Python. Console
↻ PokerEYE only recycles one Eye lease. ``systemctl restart pokereye`` kills
:19037 and sitting tables. This module decides when a restart is safe.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


CONTROL_SNAPSHOT_URL = "http://127.0.0.1:19101/snapshot"


def root_dir(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    env = str(os.environ.get("POKEREYE_ROOT") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


def request_path(root: Optional[Path] = None) -> Path:
    return root_dir(root) / "data" / "reload_requested"


def build_id_path(root: Optional[Path] = None) -> Path:
    return root_dir(root) / "BUILD_ID"


def read_disk_build(root: Optional[Path] = None) -> str:
    path = build_id_path(root)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_reload_request(build: str, root: Optional[Path] = None) -> Path:
    path = request_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(build or "").strip() + "\n", encoding="utf-8")
    return path


def clear_reload_request(root: Optional[Path] = None) -> None:
    path = request_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _int_field(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def annotate_snapshot(
    snapshot: dict[str, Any],
    *,
    disk_build: str,
    request_exists: bool,
    live_build: str = "",
) -> dict[str, Any]:
    snap = dict(snapshot or {})
    live = str(live_build or snap.get("build") or "").strip()
    disk = str(disk_build or "").strip()
    pending = bool(request_exists) or bool(disk and live and disk != live)
    snap["staged_build"] = disk
    snap["reload_pending"] = pending
    if live and "build" not in snap:
        snap["build"] = live
    return snap


def install_trainer_action(
    snapshot: Optional[dict[str, Any]],
    *,
    force: bool,
    trainer_active: bool,
) -> str:
    """``restart`` now, or ``hold`` so sitting tables keep :19037."""

    if force or not trainer_active:
        return "restart"
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return "hold"
    if snapshot.get("stale"):
        return "hold"
    if "seated_tables" not in snapshot and "live_tables" not in snapshot:
        return "hold"
    seated = _int_field(snapshot.get("seated_tables")) or 0
    live = _int_field(snapshot.get("live_tables")) or 0
    if seated > 0 or live > 0:
        return "hold"
    return "restart"


def should_apply_deferred_reload(
    *,
    seated_tables: int,
    live_tables: int,
    reload_pending: bool,
) -> bool:
    return bool(reload_pending) and int(seated_tables or 0) == 0 and int(live_tables or 0) == 0


def watcher_action(
    snapshot: Optional[dict[str, Any]],
    *,
    disk_build: str,
    request_exists: bool,
) -> str:
    """Root sidecar: ``wait``, ``restart`` trainer, or ``stop`` the watcher."""

    if not request_exists:
        return "stop"
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return "wait"
    if snapshot.get("stale"):
        return "wait"
    live = str(snapshot.get("build") or "").strip()
    disk = str(disk_build or "").strip()
    if disk and live and live == disk:
        return "stop"
    if "seated_tables" not in snapshot and "live_tables" not in snapshot:
        return "wait"
    seated = _int_field(snapshot.get("seated_tables")) or 0
    live_tables = _int_field(snapshot.get("live_tables")) or 0
    if seated == 0 and live_tables == 0:
        return "restart"
    return "wait"


def probe_snapshot(url: str = CONTROL_SNAPSHOT_URL, timeout: float = 4.0) -> Optional[dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _cli(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "probe"
    root = Path(argv[2]) if len(argv) > 2 else None
    if cmd == "probe":
        snap = probe_snapshot()
        print(json.dumps(snap if snap is not None else {"ok": False}, ensure_ascii=False))
        return 0 if snap and snap.get("ok") else 1
    if cmd == "decide":
        force = str(os.environ.get("POKEREYE_FORCE_RESTART") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        active = str(os.environ.get("POKEREYE_TRAINER_ACTIVE") or "").strip() in {"1", "true"}
        snap = probe_snapshot() if active else None
        action = install_trainer_action(snap, force=force, trainer_active=active)
        print(json.dumps({
            "action": action,
            "seated_tables": None if not snap else snap.get("seated_tables"),
            "live_tables": None if not snap else snap.get("live_tables"),
            "live_build": None if not snap else snap.get("build"),
            "disk_build": read_disk_build(root),
            "reload_pending": request_path(root).is_file() if root else False,
        }, ensure_ascii=False))
        return 0
    if cmd == "watch":
        snap = probe_snapshot()
        print(watcher_action(
            snap,
            disk_build=read_disk_build(root),
            request_exists=request_path(root).is_file(),
        ))
        return 0
    raise SystemExit(f"unknown deploy_reload command: {cmd}")


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv))
