#!/usr/bin/env python3
"""Read-only PokerEYE credential/account login probe.

This intentionally does not modify config/backend_accounts.local.json and sends no
Poker/game traffic.  It opens the same recovered gRPC stream as the Trainer,
waits for SCLogin, and reports whether each full account id is accepted.  The
credential value is never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.verified_v1.eye_direct_proxy import (
    BackendLoginRejected,
    DirectBackendProxy,
    DirectBackendSlot,
)


def suffix_key(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value.rsplit("-", 1)[1]))
    except Exception:
        return (1, value)


def registry_accounts(path: Path, *, include_invalid: bool = False) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows: list[str] = []
    for item in payload.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("account_id") or "").strip()
        if not account_id:
            continue
        state = str(item.get("state") or "").upper()
        validated = bool(item.get("validated"))
        if include_invalid or (validated and state not in {"INVALID", "QUARANTINED"}):
            rows.append(account_id)
    return sorted(dict.fromkeys(rows), key=suffix_key)


async def probe_one(
    account_id: str,
    credential_path: Path,
    *,
    host: str,
    port: int,
    timeout: float,
    settle: float,
) -> dict[str, object]:
    proxy = DirectBackendProxy(
        DirectBackendSlot(
            account_id=account_id,
            credential_file=credential_path,
            host=host,
            port=port,
        ),
        connect_timeout=timeout,
        login_timeout=timeout,
    )
    reader = None
    writer = None
    result: dict[str, object] = {"account_id": account_id}
    try:
        listen_host, listen_port = await proxy.start()
        reader, writer = await asyncio.open_connection(listen_host, listen_port)
        await proxy.wait_backend_ready(timeout)
        if settle > 0:
            await asyncio.sleep(settle)
        status = proxy.backend_status_snapshot
        fuel = proxy.backend_fuel_snapshot
        result.update(
            ok=True,
            login="accepted",
            backend_status=status.status,
            backend_health=status.health,
            backend_message=status.message,
            fuel_qty=fuel.quantity,
            fuel_rate=fuel.rate_per_hand,
            fuel_available=fuel.available,
            fuel_reason=fuel.reason_code,
        )
    except BackendLoginRejected as exc:
        result.update(ok=False, login="rejected", error=str(exc))
    except asyncio.TimeoutError:
        result.update(ok=False, login="timeout", error=f"no SCLogin within {timeout:.1f}s")
    except Exception as exc:
        result.update(ok=False, login="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        with contextlib.suppress(Exception):
            await proxy.close()
        # Keep the unused reader referenced until after proxy.close so CPython does
        # not finalize the local stream in the middle of the result snapshot.
        _ = reader
    return result


async def run(args: argparse.Namespace, credential_path: Path) -> int:
    accounts = list(args.account_id or [])
    if args.prefix and args.suffix:
        accounts.extend(f"{args.prefix}-{n}" for n in args.suffix)
    if not accounts:
        accounts = registry_accounts(args.account_file, include_invalid=args.include_invalid)
    accounts = sorted(dict.fromkeys(accounts), key=suffix_key)
    if args.limit > 0:
        accounts = accounts[: args.limit]
    if not accounts:
        print("[ERROR] no account IDs to probe")
        return 2

    print(f"[PokerEye] backend={args.host}:{args.port} accounts={len(accounts)} mode=SCLogin-only")
    print("[PokerEye] credential=<hidden>; registry is NOT modified")
    accepted: list[str] = []
    for account_id in accounts:
        result = await probe_one(
            account_id,
            credential_path,
            host=args.host,
            port=args.port,
            timeout=args.timeout,
            settle=args.settle,
        )
        login = str(result.get("login"))
        if result.get("ok"):
            accepted.append(account_id)
            fuel = ""
            if result.get("fuel_available"):
                fuel = f" fuel={result.get('fuel_qty')} rate={result.get('fuel_rate')}"
            print(
                f"[PASS] {account_id} login=accepted status={result.get('backend_status') or '-'}"
                f" health={result.get('backend_health') or '-'}{fuel}"
            )
        else:
            print(f"[FAIL] {account_id} login={login} error={result.get('error') or '-'}")
        if accepted and args.stop_on_pass:
            break

    print()
    if accepted:
        print(f"[RESULT] accepted={','.join(accepted)}")
        return 0
    print("[RESULT] no tested account id accepted this credential")
    return 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only PokerEYE SCLogin credential probe")
    p.add_argument("--account-file", type=Path, default=Path("config/backend_accounts.local.json"))
    p.add_argument("--credential-file", type=Path)
    p.add_argument("--account-id", action="append", default=[])
    p.add_argument("--prefix", default="")
    p.add_argument("--suffix", type=int, action="append", default=[])
    p.add_argument("--include-invalid", action="store_true")
    p.add_argument("--limit", type=int, default=7)
    p.add_argument("--host", default="gs.eye-panel.com")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--stop-on-pass", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    if args.credential_file:
        path = args.credential_file.expanduser().resolve()
        if not path.is_file() or not path.read_text(encoding="utf-8-sig").strip():
            print(f"[ERROR] missing/empty credential file: {path}")
            return 2
        return asyncio.run(run(args, path))

    secret = getpass.getpass("Coin agent / PokerEYE credential (hidden): ").strip()
    if not secret:
        print("[ERROR] empty credential")
        return 2
    fd, name = tempfile.mkstemp(prefix="pokereye-credential-", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret + "\n")
        secret = ""
        return asyncio.run(run(args, Path(name)))
    finally:
        with contextlib.suppress(OSError):
            os.remove(name)


if __name__ == "__main__":
    raise SystemExit(main())
