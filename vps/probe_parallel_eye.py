#!/usr/bin/env python3
"""Read-only concurrent PokerEYE SCLogin probe for multitable acceptance.

Selects currently AVAILABLE validated accounts from the trainer control snapshot,
opens N independent backend streams concurrently, and reports SCLogin/fuel.  It
never edits the account registry and sends no poker hand traffic.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import json
import sys
import time
from pathlib import Path

ROOT = Path('/opt/pokereye')
sys.path.insert(0, str(ROOT))

from core.verified_v1.eye_direct_proxy import (
    BackendLoginRejected,
    DirectBackendProxy,
    DirectBackendSlot,
    backend_android_id,
)


def live_accounts(limit: int) -> list[str]:
    try:
        c = http.client.HTTPConnection('127.0.0.1', 19101, timeout=2)
        c.request('GET', '/snapshot')
        r = c.getresponse(); payload = json.loads(r.read().decode())
        c.close()
        rows = [
            str(x.get('account_id') or '') for x in payload.get('accounts', [])
            if x.get('validated') and str(x.get('state') or '') == 'AVAILABLE'
        ]
    except Exception:
        data = json.loads((ROOT/'config'/'backend_accounts.local.json').read_text(encoding='utf-8-sig'))
        rows = [
            str(x.get('account_id') or '') for x in data.get('accounts', [])
            if x.get('validated') and str(x.get('state') or '').upper() == 'AVAILABLE'
        ]
    def key(v: str):
        try: return int(v.rsplit('-',1)[1])
        except Exception: return 10**9
    return sorted(dict.fromkeys(x for x in rows if x), key=key)[:max(1, limit)]


async def probe_one(account_id: str, timeout: float) -> dict:
    started = time.monotonic()
    proxy = DirectBackendProxy(
        DirectBackendSlot(
            account_id=account_id,
            credential_file=ROOT/'secrets'/'eye.agent',
            host='gs.eye-panel.com',
            port=443,
            android_id=backend_android_id(account_id),
        ),
        connect_timeout=timeout,
        login_timeout=timeout,
    )
    writer = None
    try:
        host, port = await proxy.start()
        _reader, writer = await asyncio.open_connection(host, port)
        await proxy.wait_backend_ready(timeout)
        fuel = proxy.backend_fuel_snapshot
        status = proxy.backend_status_snapshot
        return {
            'account_id': account_id,
            'ok': True,
            'elapsed': time.monotonic()-started,
            'status': status.status,
            'health': status.health,
            'fuel': fuel.quantity,
            'android_id': backend_android_id(account_id),
        }
    except BackendLoginRejected as exc:
        return {'account_id': account_id, 'ok': False, 'elapsed': time.monotonic()-started, 'error': 'LOGIN_REJECTED: '+str(exc), 'android_id': backend_android_id(account_id)}
    except Exception as exc:
        return {'account_id': account_id, 'ok': False, 'elapsed': time.monotonic()-started, 'error': f'{type(exc).__name__}: {exc}', 'android_id': backend_android_id(account_id)}
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        with contextlib.suppress(Exception):
            await proxy.close()


async def main_async(args) -> int:
    accounts = list(args.account or []) or live_accounts(args.count)
    if len(accounts) < args.count:
        print(f'[WARN] only {len(accounts)} currently AVAILABLE validated accounts found')
    if not accounts:
        print('[FAIL] no AVAILABLE validated accounts')
        return 2
    print(f'[PokerEye] parallel SCLogin probe accounts={len(accounts)} timeout={args.timeout:g}s')
    print('[PokerEye] registry is read-only; unique per-account Android serials enabled')
    results = await asyncio.gather(*(probe_one(a, args.timeout) for a in accounts))
    print()
    ok = 0
    for row in results:
        if row['ok']:
            ok += 1
            fuel = '—' if row.get('fuel') is None else f"{float(row['fuel']):.2f}"
            print(f"[PASS] {row['account_id']} {row['elapsed']:.2f}s status={row.get('status') or '-'} health={row.get('health') or '-'} fuel={fuel} serial={row['android_id']}")
        else:
            print(f"[FAIL] {row['account_id']} {row['elapsed']:.2f}s {row.get('error')} serial={row['android_id']}")
    print()
    print(f'[RESULT] parallel accepted={ok}/{len(results)}')
    return 0 if ok == len(results) else 1


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('--count', type=int, default=4)
    p.add_argument('--timeout', type=float, default=10.0)
    p.add_argument('--account', action='append', default=[])
    return asyncio.run(main_async(p.parse_args()))

if __name__ == '__main__':
    raise SystemExit(main())
