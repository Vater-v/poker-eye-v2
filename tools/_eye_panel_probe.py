#!/usr/bin/env python3
"""Probe eye-panel APIs. Never prints secrets, credentials, or cookies."""
from __future__ import annotations

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = (ROOT / "secrets" / "eye.agent").read_text(encoding="utf-8-sig").strip()
BASE = "https://mobile.eye-panel.com"
CTX = ssl.create_default_context()
COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=CTX),
    urllib.request.HTTPCookieProcessor(COOKIES),
)


def redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = str(k).lower()
            if any(p in lk for p in ("pass", "token", "auth", "secret", "cred", "cookie", "key")):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value[:40]]
    if isinstance(value, str) and ":" in value and len(value) > 12:
        return value.split(":", 1)[0] + ":<redacted>"
    return value


def call(path, *, method="POST", body=None, android=None, cookie=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("User-Agent", "Mozilla/5.0 PokerEyeProbe")
    req.add_header("Accept", "application/json,text/plain,*/*")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if android:
        req.add_header("x-android-id", android)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with OPENER.open(req, timeout=20) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
            headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        parsed = {"_text": raw.decode("utf-8", "replace")[:300]}
    set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
    return {
        "path": path,
        "status": status,
        "data": redact(parsed),
        "has_set_cookie": bool(set_cookie),
        "header_names": sorted(headers.keys()),
        "cookie_names": sorted({c.name for c in COOKIES}),
    }


def main() -> int:
    if not AGENT:
        print("missing agent")
        return 2
    print("agent_len", len(AGENT))
    print(json.dumps(call("/api/isLoggedIn", method="GET"), ensure_ascii=False))
    print(json.dumps(call("/api/accounts/loggedAccountDataReq", body={}), ensure_ascii=False))
    print(json.dumps(call("/api/accounts/loggedPartnerDataReq", body={}), ensure_ascii=False))
    for body in ({}, {"page": 1, "limit": 50}):
        print(json.dumps(call("/api/accounts/accountsReqN", body=body), ensure_ascii=False))
    # Authenticate as partner? unknown shape — try a few without dumping.
    print("AUTH partnerAuthKey")
    row = call("/api/authenticate", body={"partnerAuthKey": AGENT})
    print(json.dumps({"status": row["status"], "data": row["data"], "cookies": row.get("cookie_names"), "headers": row.get("header_names")}, ensure_ascii=False))
    print(json.dumps(call("/api/isLoggedIn", method="GET"), ensure_ascii=False))
    print(json.dumps(call("/api/accounts/loggedPartnerDataReq", body={}), ensure_ascii=False))
    print(json.dumps(call("/api/accounts/loggedAccountDataReq", body={}), ensure_ascii=False))
    for body in ({}, {"page": 1, "limit": 50}, {"partId": 13238}):
        print("LIST", json.dumps(call("/api/accounts/accountsReqN", body=body), ensure_ascii=False)[:1200])
    print("cookies_after", len(list(COOKIES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
