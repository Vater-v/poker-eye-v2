#!/usr/bin/env python3
"""Local, non-wagering HMN1 capacity/reconnect test.

This validates framing, authentication, logical device/channel accounting and
reconnect routing on localhost.  It deliberately does not connect to CoinPoker,
PokerEYE, a proxy, or the public Internet.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any


def find_repo(explicit: str | None) -> Path:
    root = Path(os.path.abspath(explicit)).expanduser() if explicit else Path(os.path.abspath(os.curdir))
    if not (root / "core" / "production_runtime.py").is_file():
        raise SystemExit(f"[ERROR] PokerEye repo not found: {root}")
    return root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo")
    parser.add_argument("--devices", type=int, default=30)
    parser.add_argument("--channels", type=int, default=5)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--output")
    args = parser.parse_args()

    if not 1 <= args.devices <= 100:
        raise SystemExit("[ERROR] --devices must be 1..100")
    if not 1 <= args.channels <= 16:
        raise SystemExit("[ERROR] --channels must be 1..16")
    if not 1 <= args.frames <= 1000:
        raise SystemExit("[ERROR] --frames must be 1..1000")

    repo = find_repo(args.repo)
    sys.path.insert(0, str(repo))
    from core.production_runtime import (  # type: ignore
        NATIVE_MAGIC,
        NATIVE_VERSION,
        NATIVE_WS_FRAME,
        NativeIngressServer,
        TrafficMeter,
        direct_proof,
        recv_json_frame,
        send_json_frame,
        send_raw_frame,
    )

    secret = b"offline-audit-only"

    class Router:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.frames = 0
            self.up: set[str] = set()
            self.down = 0
            self.results = 0

        def transport_up(self, device_id: str) -> None:
            with self.lock:
                self.up.add(device_id)

        def transport_down(self, _device_id: str) -> None:
            with self.lock:
                self.down += 1

        def handle(self, _device_id: str, event: dict[str, Any]):
            with self.lock:
                self.frames += 1
            return {
                "id": event.get("id"),
                "ws_id": event.get("ws_id"),
                "_ws_u32": event.get("_ws_u32"),
                "action": "forward",
            }

        def action_result(self, _device_id: str, _message: dict[str, Any]) -> bool:
            with self.lock:
                self.results += 1
            return True

    router = Router()
    meter = TrafficMeter(window_seconds=9999)
    server = NativeIngressServer(
        secret,
        router,
        meter,
        capture=None,
        operator=None,
        host="127.0.0.1",
        port=0,
    )
    port = server.start()

    clients: list[tuple[int, int, socket.socket]] = []
    clients_lock = threading.Lock()
    errors: list[tuple[int, int, str, str]] = []
    errors_lock = threading.Lock()
    welcome_count = 0
    welcome_lock = threading.Lock()

    def ws_frame(sequence: int, payload: bytes = b"\x80offline-audit") -> bytes:
        return (
            NATIVE_MAGIC
            + bytes([NATIVE_WS_FRAME, NATIVE_VERSION])
            + struct.pack("!H", 0)
            + struct.pack("!Q", sequence)
            + struct.pack("!I", 0x01020304)
            + bytes([1, 0, 0, 0])
            + struct.pack("!I", len(payload))
            + payload
        )

    def connect_one(device_index: int, channel_index: int, frames: int) -> None:
        nonlocal welcome_count
        device = f"audit-device-{device_index:02d}"
        transport = f"{device}-native-channel-{channel_index}"
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
            send_json_frame(
                sock,
                {
                    "type": "direct_hello",
                    "version": 2,
                    "device_id": device,
                    "table_id": transport,
                    "proof": direct_proof(secret, device, transport),
                    "native_mux": 1,
                    "build_id": "offline-audit",
                },
            )
            welcome = recv_json_frame(sock)
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"unexpected welcome: {welcome}")
            with welcome_lock:
                welcome_count += 1
            for number in range(frames):
                send_raw_frame(sock, ws_frame((channel_index + 1) * 1000 + number))
            with clients_lock:
                clients.append((device_index, channel_index, sock))
            sock = None
        except Exception as exc:  # diagnostic output, not business logic
            with errors_lock:
                errors.append((device_index, channel_index, type(exc).__name__, str(exc)))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def run_parallel(targets: list[tuple[int, int]], frames: int) -> None:
        threads = [
            threading.Thread(target=connect_one, args=(d, c, frames), daemon=True)
            for d, c in targets
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if alive:
            with errors_lock:
                errors.append((-1, -1, "ThreadTimeout", f"{len(alive)} connector threads alive"))

    def wait_frames(expected: int, timeout: float = 12.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with router.lock:
                observed = router.frames
            if observed >= expected:
                return observed
            time.sleep(0.02)
        with router.lock:
            return router.frames

    started = time.monotonic()
    targets = [(d, c) for d in range(args.devices) for c in range(args.channels)]
    run_parallel(targets, args.frames)
    connect_elapsed = time.monotonic() - started

    expected_phase1 = args.devices * args.channels * args.frames
    observed_phase1 = wait_frames(expected_phase1)

    reconnect_targets: list[tuple[int, int]] = []
    survivors: list[tuple[int, int, socket.socket]] = []
    with clients_lock:
        current_clients = list(clients)
        clients.clear()
    for device_index, channel_index, sock in current_clients:
        if channel_index == 0:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            reconnect_targets.append((device_index, channel_index))
        else:
            survivors.append((device_index, channel_index, sock))
    with clients_lock:
        clients.extend(survivors)

    time.sleep(0.5)
    run_parallel(reconnect_targets, 1)
    expected_phase2 = expected_phase1 + args.devices
    observed_phase2 = wait_frames(expected_phase2)
    active_devices = server.count()
    active_channels = server.channel_count()

    with clients_lock:
        closing = list(clients)
        clients.clear()
    for _device_index, _channel_index, sock in closing:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    server.stop()

    result = {
        "scope": "localhost synthetic HMN1; no game or public-network proof",
        "devices": args.devices,
        "channels_per_device": args.channels,
        "connections": args.devices * args.channels,
        "welcome_count": welcome_count,
        "connect_elapsed_s": round(connect_elapsed, 3),
        "expected_phase1_frames": expected_phase1,
        "observed_phase1_frames": observed_phase1,
        "expected_after_reconnect": expected_phase2,
        "observed_after_reconnect": observed_phase2,
        "active_devices_before_stop": active_devices,
        "active_channels_before_stop": active_channels,
        "errors": errors,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")

    ok = (
        not errors
        and observed_phase1 >= expected_phase1
        and observed_phase2 >= expected_phase2
        and active_devices == args.devices
        and active_channels == args.devices * args.channels
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
