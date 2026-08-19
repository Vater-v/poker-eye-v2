#!/usr/bin/env python3
"""Create one attachable support ZIP from the latest production run.

No secret/config/account-registry files are included. The archive contains only:
- raw Coin PCAP ring segments (DLT_USER0/HMR1),
- run manifest/operator/events logs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import zipfile
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--run", default=None, help="run directory name, e.g. run_abcd1234")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    logs = repo / "logs"
    if args.run:
        run = logs / args.run
    else:
        runs = sorted(
            [p for p in logs.glob("run_*") if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not runs:
            raise SystemExit("no production run directories under logs/")
        run = runs[0]

    if not run.is_dir():
        raise SystemExit(f"run directory not found: {run}")

    capture_files = sorted((run / "captures").glob("**/*.pcap"))
    if not capture_files:
        raise SystemExit(
            f"no raw Coin PCAP yet under {run / 'captures'}; "
            "keep Trainer running until [capture] appears"
        )

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = logs / f"support_{run.name}_{stamp}.zip"

    include = []
    for name in ("manifest.json", "operator.txt", "events.jsonl"):
        p = run / name
        if p.is_file():
            include.append(p)
    include.extend(capture_files)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in include:
            z.write(p, p.relative_to(run.parent))

    size_mib = out.stat().st_size / (1024 * 1024)
    print(f"[+] support archive: {out}")
    print(f"[+] raw Coin PCAP files: {len(capture_files)}")
    print(f"[+] size: {size_mib:.2f} MiB")
    print("[+] secrets/config/account registry are NOT included")


if __name__ == "__main__":
    main()
