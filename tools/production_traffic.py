#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    root = Path(args.repo).resolve()
    runs = sorted((root / "logs").glob("run_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not runs:
        raise SystemExit("no run logs")
    events = runs[0] / "events.jsonl"
    if not events.is_file():
        raise SystemExit(f"missing {events}")
    rows = []
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("event") == "traffic.window" or str(row.get("event") or "").startswith("v6."):
            rows.append(row)
    for row in rows[-max(1,args.limit):]:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
