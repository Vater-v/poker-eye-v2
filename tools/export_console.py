#!/usr/bin/env python3
from __future__ import annotations
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
src = root / "web" / "console" / ".output" / "public"
dst = root / "vps" / "web-dist"
if not src.is_dir():
    raise SystemExit(f"missing generated console: {src}")
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst)
print(f"[OK] console -> {dst} ({sum(p.stat().st_size for p in dst.rglob('*'))} bytes)")
