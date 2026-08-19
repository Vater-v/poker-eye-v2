#!/usr/bin/env python3
from pathlib import Path
import re

t = Path(r"C:\projects\pokereye\poker-eye-v2\logs\_eye_panel_index.js").read_text(
    encoding="utf-8", errors="replace"
)
for key in ["partnerAuthKey", "partId", "credentialsV2", "Ol=", "qn=", "browserProfile"]:
    print("count", key, t.count(key))

i = t.find("partnerAuthKey")
print("first partnerAuthKey", i)
print(t[max(0, i - 400) : i + 600])

print("\n===== Ol object =====")
m = re.search(r"Ol=\{[^}]{0,800}\}", t)
if m:
    print(m.group(0)[:800])
m = re.search(r"qn=\{[^}]{0,800}\}", t)
print("\n===== qn object =====")
if m:
    print(m.group(0)[:800])

print("\n===== Account limit =====")
i = t.find("Account limit reached")
print(t[max(0, i - 200) : i + 200])
