#!/usr/bin/env python3
from __future__ import annotations
import re
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}
out = Path(__file__).resolve().parents[1] / "logs" / "_eye_panel_index.js"
url = "https://mobile.eye-panel.com/assets/index-D5hvlv8D.js"
req = urllib.request.Request(url, headers=UA)
raw = urllib.request.urlopen(req, timeout=30).read()
out.write_bytes(raw)
text = raw.decode("utf-8", "replace")
print("bytes", len(raw))

paths = sorted(set(re.findall(r'["\'](/[a-zA-Z0-9_./{}?-]{2,120})["\']', text))
)
print("=== interesting paths ===")
keys = ("api", "account", "user", "login", "regist", "agent", "club", "device", "cred", "create", "add", "suffix", "ppp", "coin", "auth", "slot", "poker")
for x in paths:
    if any(k in x.lower() for k in keys):
        print(x)

print("=== hosts ===")
for m in sorted(set(re.findall(r"https?://[a-zA-Z0-9._:-]+", text))):
    print(m)

print("=== keywords nearby ===")
for pat in ("createAccount", "addAccount", "register", "CSLogin", "agentCode", "deviceId", "suffix", "709393393"):
    if pat.lower() in text.lower():
        print("HIT", pat)
