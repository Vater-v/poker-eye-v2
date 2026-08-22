#!/usr/bin/env python3
"""Compact last-run dump: events tail, operator, errors, lite snapshot."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("POKEREYE_ROOT", "/opt/pokereye")).resolve()
LOGS = ROOT / "logs"


def latest_run() -> Path | None:
    latest = None
    latest_mtime = -1.0
    if not LOGS.is_dir():
        return None
    for ent in LOGS.iterdir():
        if not ent.name.startswith("run_") or not ent.is_dir():
            continue
        try:
            mtime = ent.stat().st_mtime
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = ent
    return latest


def copy_tail(src: Path, dest: Path, limit: int = 400) -> None:
    if not src.is_file():
        dest.write_text("", encoding="utf-8")
        return
    text = src.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    dest.write_text("\n".join(lines[-limit:]) + ("\n" if lines else ""), encoding="utf-8")


def lite_snapshot() -> dict:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:19101/snapshot", timeout=4) as response:
            snap = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    devices = []
    for row in snap.get("devices") or []:
        devices.append({
            "id": row.get("device_id"),
            "connected": row.get("connected"),
            "label": row.get("device_label") or row.get("display_name"),
            "hero": row.get("hero_name"),
            "tables": [
                {
                    "id": table.get("table_id"),
                    "sitting": table.get("hero_sitting"),
                    "state": table.get("state"),
                    "phase": table.get("phase"),
                    "last": table.get("last_action"),
                    "source": table.get("last_action_source"),
                }
                for table in (row.get("tables") or [])
            ],
        })
    return {
        "ok": snap.get("ok"),
        "build": snap.get("build"),
        "stale": snap.get("stale"),
        "snapshot_error": snap.get("snapshot_error"),
        "connected_devices": snap.get("connected_devices"),
        "seated_tables": snap.get("seated_tables"),
        "live_tables": snap.get("live_tables"),
        "devices": devices,
    }


def main() -> int:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "debug-dump").resolve()
    dest.mkdir(parents=True, exist_ok=True)
    run = latest_run()
    meta = {"run": run.name if run else None, "root": str(ROOT)}
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    if run is not None:
        copy_tail(run / "events.jsonl", dest / "events-tail.jsonl", 400)
        copy_tail(run / "operator.txt", dest / "operator.txt", 200)
        copy_tail(run / "error.log", dest / "error.log", 200)
        manifest = run / "manifest.json"
        if manifest.is_file():
            shutil.copy2(manifest, dest / "manifest.json")
    (dest / "snapshot.json").write_text(
        json.dumps(lite_snapshot(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:19101/health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        health = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    (dest / "health.json").write_text(
        json.dumps(health, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
