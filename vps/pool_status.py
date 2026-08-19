#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def suffix(account_id: str):
    try:
        return int(account_id.rsplit('-', 1)[1])
    except Exception:
        return 10**9


def main() -> int:
    ap = argparse.ArgumentParser(description='PokerEye persisted backend account-pool status')
    ap.add_argument('--account-file', type=Path, default=Path('config/backend_accounts.local.json'))
    args = ap.parse_args()
    data=json.loads(args.account_file.read_text(encoding='utf-8-sig'))
    rows=[r for r in (data.get('accounts') or []) if isinstance(r,dict) and r.get('account_id')]
    rows.sort(key=lambda r:(suffix(str(r['account_id'])),str(r['account_id'])))
    now=time.time()
    counts={}
    for r in rows:
        state=str(r.get('state') or '?').upper(); counts[state]=counts.get(state,0)+1
    validated=[r for r in rows if r.get('validated')]
    available=[r for r in validated if str(r.get('state') or '').upper()=='AVAILABLE']
    max_suffix=max((suffix(str(r['account_id'])) for r in rows), default=0)
    if max_suffix >= 10**9: max_suffix=0
    print(f"base={data.get('base') or '-'} profile={data.get('profile') or '-'} max_suffix={data.get('max_suffix') or 150}")
    print(f"records={len(rows)} validated={len(validated)} available={len(available)} next_suffix={max_suffix+1}")
    print('states=' + ', '.join(f'{k}:{v}' for k,v in sorted(counts.items())))
    print()
    print(f"{'ID':<28} {'STATE':<12} {'VALID':<5} {'ATT':>4} {'RETRY':>7}  ERROR")
    for r in rows:
        retry=max(0.0,float(r.get('retry_after') or 0)-now)
        print(f"{str(r['account_id']):<28} {str(r.get('state') or '-'):<12} {('yes' if r.get('validated') else 'no'):<5} {int(r.get('attempts') or 0):>4} {retry:>6.0f}s  {str(r.get('last_error') or '')[:80]}")
    return 0

if __name__=='__main__':
    raise SystemExit(main())
