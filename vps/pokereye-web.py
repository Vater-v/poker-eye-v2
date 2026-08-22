#!/usr/bin/env python3
"""Token-protected static Nuxt console + thin JSON proxy to the trainer control plane."""
from __future__ import annotations

import http.client
import http.cookies
import io
import json
import mimetypes
import os
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote

ROOT = Path(os.environ.get("POKEREYE_ROOT", "/opt/pokereye")).resolve()
TOKEN_FILE = ROOT / "secrets" / "web.token"
CONSOLE_DIR = Path(os.environ.get("POKEREYE_CONSOLE_DIR", str(ROOT / "vps" / "web-dist"))).resolve()
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 19101
HOST = "127.0.0.1"
PORT = 19100
WEB_ID = "console-nuxt-v6"
_LAST_STATE: dict = {}
_LATEST_RUN: tuple[float, Path | None] = (0.0, None)


def token_value() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def attach_reload_fields(snapshot: dict) -> dict:
    """Disk BUILD_ID vs live trainer. Web restarts even when the trainer is held."""
    snapshot = dict(snapshot or {})
    try:
        from core.deploy_reload import annotate_snapshot, read_disk_build, request_path

        return annotate_snapshot(
            snapshot,
            disk_build=read_disk_build(ROOT),
            request_exists=request_path(ROOT).is_file(),
            live_build=str(snapshot.get("build") or ""),
        )
    except Exception:
        return snapshot


def merge_live_state(
    snapshot: dict,
    run: Path | None,
    last: dict | None,
    events: list | None = None,
) -> dict:
    """Keep the last live fleet when a timeout returns an empty 0/0 snapshot."""
    last = last if isinstance(last, dict) else {}
    last_snap = dict(last.get("snapshot") or {})
    snapshot = dict(snapshot or {})
    devices = list(snapshot.get("devices") or [])
    last_devices = list(last_snap.get("devices") or [])
    new_online = sum(1 for row in devices if isinstance(row, dict) and row.get("connected"))
    last_online = sum(1 for row in last_devices if isinstance(row, dict) and row.get("connected"))
    if (not devices and last_devices) or (not new_online and last_online and not devices):
        snapshot = dict(last_snap)
        snapshot["stale"] = True
        snapshot["snapshot_error"] = str(
            snapshot.get("snapshot_error") or last_snap.get("snapshot_error") or "empty_live_snapshot"
        )
        devices = list(snapshot.get("devices") or [])
    run_name = (
        str(snapshot.get("run") or "").strip()
        or (run.name if run is not None else "")
        or str(last.get("run") or "").strip()
        or ""
    )
    if run_name:
        snapshot["run"] = run_name
    return {
        "ok": True,
        "snapshot": snapshot,
        "events": list(events or last.get("events") or []),
        "run": run_name,
    }


def latest_run() -> Path | None:
    global _LATEST_RUN
    now = time.monotonic()
    stamp, cached = _LATEST_RUN
    if cached is not None and now - stamp < 2.0:
        return cached
    logs = ROOT / "logs"
    latest = None
    latest_mtime = -1.0
    try:
        with os.scandir(logs) as it:
            for ent in it:
                if not ent.name.startswith("run_") or not ent.is_dir(follow_symlinks=False):
                    continue
                try:
                    mtime = ent.stat().st_mtime
                except OSError:
                    continue
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest = Path(ent.path)
    except OSError:
        latest = cached
    _LATEST_RUN = (now, latest)
    return latest


def tail_jsonl(path: Path | None, limit: int = 300) -> list[dict]:
    """Last ``limit`` JSON objects. Never decode the whole run file."""
    if path is None or not path.is_file():
        return []
    limit = max(20, min(1500, int(limit)))
    # ~1 MiB from EOF is enough for a few hundred console events; a 45 MiB
    # run file must not become a full json.loads scan on each poll.
    max_bytes = 1024 * 1024
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        chunks: list[bytes] = []
        nlines = 0
        taken = 0
        while pos > 0 and nlines <= limit and taken < max_bytes:
            take = min(65536, pos)
            pos -= take
            f.seek(pos)
            chunk = f.read(take)
            chunks.append(chunk)
            nlines += chunk.count(b"\n")
            taken += take
        data = b"".join(reversed(chunks))
    if pos > 0:
        cut = data.find(b"\n")
        if cut >= 0:
            data = data[cut + 1 :]
    rows = []
    for line in data.splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(k) or "") for k in ("device_id", "device_label", "message"))
        if "selftest" in text.lower():
            continue
        rows.append(item)
    return rows[-limit:]


def control(method: str, path: str, body: dict | None = None, timeout: float = 3.0) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(CONTROL_HOST, CONTROL_PORT, timeout=timeout)
    raw = None
    headers = {}
    if body is not None:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=raw, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        try:
            value = json.loads(payload.decode("utf-8"))
        except Exception:
            value = {"ok": False, "error": payload.decode("utf-8", errors="replace")[:500]}
        return int(response.status), value if isinstance(value, dict) else {"value": value}
    except Exception as exc:
        return 599, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _safe_console_file(rel: str) -> Path | None:
    if not CONSOLE_DIR.is_dir():
        return None
    clean = unquote(rel).lstrip("/")
    if clean.startswith("pokereye/"):
        clean = clean[len("pokereye/"):]
    if not clean or clean.endswith("/"):
        clean = "index.html"
    target = (CONSOLE_DIR / clean).resolve()
    try:
        target.relative_to(CONSOLE_DIR)
    except ValueError:
        return None
    if target.is_file():
        return target
    index = CONSOLE_DIR / "index.html"
    return index if index.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "PokerEyeWeb/4"

    def log_message(self, _fmt, *_args):
        return

    def _auth(self) -> tuple[bool, bool]:
        expected = token_value()
        if not expected:
            return False, False
        parsed = urlsplit(self.path)
        supplied = (parse_qs(parsed.query).get("token") or [""])[0]
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        row = cookie.get("pokereye_token")
        from_cookie = row.value if row else ""
        return supplied == expected or from_cookie == expected, supplied == expected

    def _send(self, status: int, body, ctype="application/json; charset=utf-8", cookie=False, cache="no-store, max-age=0"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if cookie:
            self.send_header(
                "Set-Cookie",
                "pokereye_token=" + token_value() + "; Path=/pokereye/; Secure; HttpOnly; SameSite=Strict",
            )
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        try:
            size = min(65536, max(0, int(self.headers.get("Content-Length") or 0)))
            raw = self.rfile.read(size) if size else b"{}"
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        ok, via_query = self._auth()
        if not ok:
            self._send(401, "token required", "text/plain; charset=utf-8")
            return
        path = urlsplit(self.path).path
        if path in ("/health", "/pokereye/health"):
            status, ctl = control("GET", "/health")
            body = {"ok": status == 200, "web": WEB_ID, "control": ctl}
            live_build = ""
            if isinstance(ctl, dict):
                live_build = str(ctl.get("build") or "")
            body.update(attach_reload_fields({"build": live_build}))
            self._send(
                200 if status == 200 else 503,
                body,
                cookie=via_query,
            )
            return
        if path in ("/api/state", "/pokereye/api/state"):
            try:
                status, snapshot = control("GET", "/snapshot", timeout=8.0)
            except Exception as exc:
                status, snapshot = 599, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if status != 200 or not isinstance(snapshot, dict):
                snapshot = dict(_LAST_STATE.get("snapshot") or {})
                if not snapshot:
                    snapshot = {
                        "ok": False,
                        "build": "",
                        "devices": [],
                        "stale": True,
                        "snapshot_error": "trainer control unavailable",
                    }
                snapshot["stale"] = True
            else:
                snapshot["devices"] = [
                    d for d in snapshot.get("devices", [])
                    if "selftest" not in str(d.get("device_id", "")).lower()
                ]
            run = latest_run()
            try:
                events = tail_jsonl((run / "events.jsonl") if run else None, 400)
            except Exception:
                events = list(_LAST_STATE.get("events") or [])
            payload = merge_live_state(snapshot, run, _LAST_STATE, events)
            if not payload.get("run") and run is not None:
                payload["run"] = run.name
            if isinstance(payload.get("snapshot"), dict):
                payload["snapshot"] = attach_reload_fields(payload["snapshot"])
            merged_devices = list((payload.get("snapshot") or {}).get("devices") or [])
            last_devices = list((_LAST_STATE.get("snapshot") or {}).get("devices") or [])
            if merged_devices or not last_devices:
                _LAST_STATE.update(payload)
            self._send(200, payload, cookie=via_query)
            return
        if path in ("/api/logs", "/pokereye/api/logs"):
            run = latest_run()
            src = (run / "events.jsonl") if run else None
            if src is None or not src.is_file():
                self._send(404, {"ok": False, "error": "no events"}, cookie=via_query)
                return
            data = src.read_bytes()
            name = (run.name if run else "run") + "-events.jsonl"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            if via_query:
                self.send_header(
                    "Set-Cookie",
                    "pokereye_token=" + token_value() + "; Path=/pokereye/; Secure; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/api/issues.zip", "/pokereye/api/issues.zip"):
            run = latest_run()
            if run is None:
                self._send(404, {"ok": False, "error": "no run"}, cookie=via_query)
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in ("error.log", "operator.txt", "events.jsonl", "technical.log", "manifest.json"):
                    src = run / name
                    if src.is_file():
                        archive.write(src, arcname=f"{run.name}/{name}")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{(run.name if run else "run")}-issues.zip"')
            self.send_header("Cache-Control", "no-store")
            if via_query:
                self.send_header(
                    "Set-Cookie",
                    "pokereye_token=" + token_value() + "; Path=/pokereye/; Secure; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.write(data)
            return
        asset = _safe_console_file(path)
        if asset is None:
            self._send(404, {"ok": False, "error": "console build missing"}, cookie=via_query)
            return
        ctype = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        cache = "no-store" if asset.name == "index.html" else "public, max-age=300"
        self._send(200, asset.read_bytes(), ctype, cookie=via_query, cache=cache)

    def do_POST(self):
        ok, via_query = self._auth()
        if not ok:
            self._send(401, {"ok": False, "error": "token required"})
            return
        path = urlsplit(self.path).path
        mapping = {
            "/api/control/table/close": "/table/close",
            "/pokereye/api/control/table/close": "/table/close",
            "/api/control/table/restart": "/table/restart",
            "/pokereye/api/control/table/restart": "/table/restart",
            "/api/control/device/reset": "/device/reset",
            "/pokereye/api/control/device/reset": "/device/reset",
            "/api/control/device/auto": "/device/auto",
            "/pokereye/api/control/device/auto": "/device/auto",
            "/api/control/device/leave-all": "/device/leave-all",
            "/pokereye/api/control/device/leave-all": "/device/leave-all",
        }
        target = mapping.get(path)
        if target is None:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        status, value = control("POST", target, self._body(), timeout=9.0)
        self._send(status, value, cookie=via_query)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
