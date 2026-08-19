#!/usr/bin/env python3
"""List partner game accounts. Prints ids/flags only."""
from __future__ import annotations

import http.cookiejar
import json
import ssl
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


def post(path, body):
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 PokerEyeProbe")
    with OPENER.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    auth = post("/api/authenticate", {"partnerAuthKey": AGENT})
    print("auth", auth.get("status"), "partId", auth.get("partId"))
    rows = []
    for page in range(0, 20):
        data = post("/api/accounts/accountsReqN", {"page": page, "limit": 50, "partId": auth.get("partId")})
        print("page", page, data.get("status"), data.get("code"), "pageSize", data.get("pageSize"), "totalPages", data.get("totalPages"))
        accounts = data.get("accounts") or []
        if not accounts:
            break
        for item in accounts:
            acc = item.get("account") or {}
            sess = item.get("session") or {}
            rows.append({
                "deviceId": acc.get("deviceId"),
                "accId": acc.get("accId"),
                "enabled": acc.get("enabled"),
                "online": item.get("online"),
                "pid": acc.get("pid"),
                "game": sess.get("gameType"),
                "room": sess.get("lastRoom"),
            })
        total = data.get("totalPages") or 0
        if total and page + 1 >= total:
            break
        if len(accounts) < 5 and page > 0:
            break
    print("count", len(rows))
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    live = [r["deviceId"] for r in rows if r.get("enabled")]
    print("enabled", live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
