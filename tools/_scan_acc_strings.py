#!/usr/bin/env python3
from pathlib import Path
import re
from collections import Counter

t = Path(r"C:\projects\pokereye\poker-eye-v2\logs\_eye_panel_index.js").read_text(
    encoding="utf-8", errors="replace"
)

print("=== suffix contexts ===")
for m in re.finditer("suffix", t, re.I):
    print(t[max(0, m.start() - 90) : m.end() + 90].replace("\n", " "))
    print("---")

print("=== quoted *ccount* strings ===")
strs = re.findall(r'"([^"\\]{0,50}[Aa]ccount[^"\\]{0,50})"', t)
for s, c in Counter(strs).most_common(50):
    print(c, s)

print("=== Qt().api.X ===")
for m in re.finditer(r"Qt\(\)[^\n]{0,40}api\.([A-Za-z0-9]+)", t):
    print(m.group(1), "at", m.start())

print("=== /api/accounts/ as queryKey ===")
for m in re.finditer(r"/api/accounts/[A-Za-z0-9]+", t):
    print(m.group(0), t.count(m.group(0)))
