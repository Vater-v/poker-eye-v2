#!/usr/bin/env python3
from pathlib import Path
import re

t = Path(r"C:\projects\pokereye\poker-eye-v2\logs\_eye_panel_index.js").read_text(encoding="utf-8", errors="replace")

# Find function names / object keys around loginAccount
for key in [
    "loginAccountReq",
    "loginAccount",
    "accountsReqN",
    "deleteAccountsReq",
    "LoginAccount",
    "accountLogin",
]:
    idxs = [m.start() for m in re.finditer(re.escape(key), t)]
    print(f"\n##### {key} hits={len(idxs)}")
    for i in idxs[:8]:
        chunk = t[max(0, i - 250) : i + 500]
        print("---", i)
        print(chunk.replace("\n", " ")[:700])
        print()

# look for likely payload fields near loginAccountReq call sites
print("\n##### nearby identifiers after loginAccountReq(")
for m in re.finditer(r"loginAccountReq\(", t):
    print(t[m.start() : m.start() + 400])
    print()

print("\n##### schema-like keys containing Account")
for m in re.finditer(r"[A-Za-z]{0,20}Account[A-Za-z]{0,30}", t):
    s = m.group(0)
    if s in {"loginAccountReq", "loggedAccountDataReq", "deleteAccountsReq", "accountsReqN", "accountsFromSessionsReqV2"}:
        continue
print("done scan")
