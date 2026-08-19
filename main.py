"""PokerEye v2 production entry point.

The launcher is intentionally self-contained for the deployed Windows tree:
- relative runtime paths are resolved from this file, not the caller's CWD;
- --secret is optional when POKEREYE_V2_SECRET or secrets/trainer.secret exists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from core.production_runtime import build_parser, run_from_args


ROOT = Path(__file__).resolve().parent
SECRET_FILE = ROOT / "secrets" / "trainer.secret"


def _has_option(argv: list[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in argv)


def _default_secret() -> str:
    value = os.environ.get("POKEREYE_V2_SECRET", "").strip()
    if value:
        return value
    try:
        return SECRET_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def main() -> None:
    # Keep every existing relative default (logs/, config/, secrets/) anchored
    # to the deployed PokerEye directory even when launched from elsewhere.
    os.chdir(ROOT)

    argv = sys.argv[1:]
    if not _has_option(argv, "--secret"):
        secret = _default_secret()
        if secret:
            argv = ["--secret", secret, *argv]

    run_from_args(build_parser().parse_args(argv))


if __name__ == "__main__":
    main()
