#!/usr/bin/env python3
"""Extract a sanitized eye-panel API contract from a Chrome/Edge HAR export.

Values likely to contain credentials/tokens are redacted while field names,
HTTP methods, endpoint paths, statuses and non-sensitive JSON structure remain.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE = re.compile(
    r"pass(word)?|secret|token|auth(orization)?|cookie|session|agent.?code|access.?key|api.?key|credential|(^|_)code($|_)",
    re.I,
)
INTERESTING = re.compile(r"account|user|agent|create|add|register|credential|fuel|login|profile|device", re.I)


def redact_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    return f"<redacted:{len(text)}>"


def scrub(value: object, key: str = "") -> object:
    if key and SENSITIVE.search(key):
        return redact_scalar(value)
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 400:
        return value[:120] + f"…<{len(value)} chars>"
    return value


def safe_url(raw: str) -> str:
    parts = urlsplit(raw)
    pairs = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        pairs.append((k, redact_scalar(v) if SENSITIVE.search(k) else v))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def parse_json_text(text: str) -> object | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def header_map(rows: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows or []:
        if isinstance(row, dict):
            name = str(row.get("name") or "")
            value = str(row.get("value") or "")
            if name:
                out[name] = redact_scalar(value) if SENSITIVE.search(name) else value[:300]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Sanitize eye-panel HAR and show candidate account/admin API calls")
    ap.add_argument("har", type=Path)
    ap.add_argument("--all-eye", action="store_true", help="show all eye-panel.com calls, not just account-like endpoints")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()

    payload = json.loads(args.har.read_text(encoding="utf-8-sig"))
    entries = (((payload.get("log") or {}).get("entries")) or [])
    rows: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        url = str(req.get("url") or "")
        host = urlsplit(url).hostname or ""
        if not host.endswith("eye-panel.com"):
            continue
        method = str(req.get("method") or "GET").upper()
        post = req.get("postData") or {}
        post_text = str(post.get("text") or "")
        if not args.all_eye and not (
            INTERESTING.search(url) or INTERESTING.search(post_text) or method in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            continue
        req_json = parse_json_text(post_text) if post_text else None
        content = resp.get("content") or {}
        response_text = str(content.get("text") or "")
        resp_json = parse_json_text(response_text) if response_text and len(response_text) <= 1_000_000 else None
        rows.append(
            {
                "method": method,
                "url": safe_url(url),
                "status": int(resp.get("status") or 0),
                "request_headers": header_map(req.get("headers")),
                "request_json": scrub(req_json) if req_json is not None else None,
                "request_body_length": len(post_text),
                "response_mime": str(content.get("mimeType") or ""),
                "response_json": scrub(resp_json) if resp_json is not None else None,
                "response_body_length": len(response_text),
            }
        )

    output = json.dumps({"entries": rows}, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"wrote {len(rows)} sanitized entries -> {args.output}")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
