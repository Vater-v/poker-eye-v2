#!/usr/bin/env python3
from __future__ import annotations
import re, sys
from pathlib import Path

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
p=root/'core'/'production_runtime.py'
s=p.read_text(encoding='utf-8')
marker='# POKEREYE_SAFE_ACCOUNT_BOOTSTRAP_V2'
if marker not in s:
    pattern=re.compile(
        r'''        accounts = \[\n'''
        r'''            str\(row\.get\("account_id"\) or ""\)\.strip\(\)\n'''
        r'''            for row in \(data\.get\("accounts"\) or \[\]\)\n'''
        r'''            if isinstance\(row, dict\)\n'''
        r'''            and row\.get\("account_id"\)\n'''
        r'''            and bool\(row\.get\("validated"\)\)\n'''
        r'''            and str\(row\.get\("state"\) or ""\)\.upper\(\) not in \{"INVALID", "QUARANTINED"\}\n'''
        r'''        \]\n'''
        r'''        if not accounts:\n'''
        r'''            raise RuntimeError\("no validated PokerEYE accounts"\)\n'''
        r'''        credential = Path\(credential_file\)\n'''
        r'''        if not credential\.is_file\(\) or not credential\.read_text\(encoding="utf-8-sig"\)\.strip\(\):\n'''
        r'''            raise RuntimeError\("PokerEYE credential missing/empty"\)\n\n'''
        r'''        account_base = str\(data\.get\("base"\) or ""\)\.strip\(\)\n'''
        r'''        if not account_base:\n'''
        r'''            first_account = accounts\[0\]\n'''
        r'''            account_base = first_account\.rsplit\("-", 1\)\[0\] if "-" in first_account else ""\n'''
        r'''        pool = AccountPool\(\n'''
        r'''            accounts,\n'''
        r'''            dynamic_base=account_base or None,\n'''
        r'''            registry_path=account_path if account_base else None,\n'''
        r'''            profile=str\(data\.get\("profile"\) or "PPPoker"\),\n'''
        r'''            auto_expand_unbounded=bool\(account_base\),\n'''
        r'''        \)\n'''
        r'''        self\.account_count = len\(accounts\)\n''', re.MULTILINE)
    replacement='''        # POKEREYE_SAFE_ACCOUNT_BOOTSTRAP_V2\n        account_rows = [\n            row for row in (data.get("accounts") or [])\n            if isinstance(row, dict) and str(row.get("account_id") or "").strip()\n        ]\n        account_ids = [str(row.get("account_id") or "").strip() for row in account_rows]\n        validated_accounts = [\n            str(row.get("account_id") or "").strip()\n            for row in account_rows\n            if bool(row.get("validated"))\n            and str(row.get("state") or "").upper() not in {"INVALID", "QUARANTINED"}\n        ]\n        if not account_ids:\n            raise RuntimeError("PokerEYE account registry is empty")\n\n        credential = Path(credential_file)\n        if not credential.is_file() or not credential.read_text(encoding="utf-8-sig").strip():\n            raise RuntimeError("PokerEYE credential missing/empty")\n\n        account_base = str(data.get("base") or "").strip()\n        if not account_base:\n            first_account = account_ids[0]\n            account_base = first_account.rsplit("-", 1)[0] if "-" in first_account else ""\n        autogrow = bool(account_base) and os.getenv("POKEREYE_ACCOUNT_AUTOGROW", "0").strip().lower() in {"1", "true", "yes", "on"}\n        pool = AccountPool(\n            account_ids,\n            dynamic_base=account_base or None,\n            registry_path=account_path if account_base else None,\n            profile=str(data.get("profile") or "PPPoker"),\n            auto_expand_unbounded=autogrow,\n        )\n        self.account_count = len(validated_accounts)\n'''
    patched,n=pattern.subn(replacement,s,count=1)
    if n!=1:
        raise SystemExit('[ERROR] production_runtime account bootstrap block not found; refusing blind patch')
    p.write_text(patched,encoding='utf-8')
    print('[OK] patched',p)
else:
    print('[OK] runtime patch already present')
