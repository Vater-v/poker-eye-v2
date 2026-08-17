"""Public bootstrap and per-emulator callback allocation.

The bootstrap listener is intentionally separate from table traffic. An emulator
makes an authenticated outbound connection to ``0.0.0.0:19037`` (publicly
forwarded as 37.192.228.101:19037), identifies its local LAN IPv4, and receives
the lowest free callback port in 54300..54399. The callback listener is then
bound locally and authenticated with a one-use token plus generation.

No ADB, VPS, or runtime port forwarding is performed here. The host/network
operator must expose 19037 and the callback range to the target LAN/public path.
"""
from __future__ import annotations

import hmac
import secrets
import socket
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .protocol import PROTOCOL_VERSION, recv_frame, send_frame, proof, verify_hello


@dataclass(frozen=True)
class CallbackLease:
    device_id: str
    local_ipv4: str
    callback_port: int
    token: str
    generation: int


class CallbackAllocator:
    """Lowest-free reusable callback ports, bounded to 100 emulators."""

    def __init__(self, start: int = 54300, end: int = 54399):
        if not (1 <= start <= end <= 65535):
            raise ValueError("invalid callback range")
        self.start, self.end = start, end
        self._lock = threading.Lock()
        self._leases: Dict[str, CallbackLease] = {}
        self._ports: Dict[int, str] = {}
        self._generations: Dict[str, int] = {}

    def allocate(self, device_id: str, local_ipv4: str) -> CallbackLease:
        with self._lock:
            old = self._leases.get(device_id)
            if old is not None:
                return old
            port = next((p for p in range(self.start, self.end + 1) if p not in self._ports), None)
            if port is None:
                raise RuntimeError("callback capacity exhausted")
            generation = self._generations.get(device_id, 0) + 1
            lease = CallbackLease(device_id, local_ipv4, port, secrets.token_hex(32), generation)
            self._leases[device_id] = lease
            self._ports[port] = device_id
            self._generations[device_id] = generation
            return lease

    def release(self, device_id: str, generation: Optional[int] = None) -> bool:
        with self._lock:
            lease = self._leases.get(device_id)
            if lease is None or (generation is not None and lease.generation != generation):
                return False
            self._leases.pop(device_id, None)
            self._ports.pop(lease.callback_port, None)
            return True

    def get(self, device_id: str) -> Optional[CallbackLease]:
        with self._lock:
            return self._leases.get(device_id)

    def available(self) -> int:
        with self._lock:
            return (self.end - self.start + 1) - len(self._leases)

    def leases(self) -> Dict[str, CallbackLease]:
        with self._lock:
            return dict(self._leases)


class BootstrapServer:
    """Authenticated public bootstrap plus per-emulator callback listeners."""

    def __init__(self, secret: bytes, *, host: str = "0.0.0.0", bootstrap_port: int = 19037,
                 callback_start: int = 54300, callback_end: int = 54399,
                 advertised_host: str = "37.192.228.101", on_event=None,
                 callback_handler=None):
        self.secret = secret
        self.host = host
        self.bootstrap_port = bootstrap_port
        self.advertised_host = advertised_host
        self.allocator = CallbackAllocator(callback_start, callback_end)
        self.on_event = on_event
        self.callback_handler = callback_handler
        self._server: Optional[socket.socket] = None
        self._callback_servers: Dict[int, socket.socket] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _emit(self, event: str, **fields: Any) -> None:
        if self.on_event:
            try:
                self.on_event(event, **fields)
            except Exception:
                pass

    def start(self) -> int:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.bootstrap_port))
        self._server.listen(128)
        self.bootstrap_port = self._server.getsockname()[1]
        threading.Thread(target=self._accept_bootstrap, daemon=True, name="bootstrap-accept").start()
        self._emit("bootstrap.listening", port=self.bootstrap_port, callback_start=self.allocator.start,
                   callback_end=self.allocator.end)
        return self.bootstrap_port

    def _accept_bootstrap(self) -> None:
        assert self._server is not None
        self._server.settimeout(.2)
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_bootstrap, args=(conn, addr), daemon=True).start()

    def _handle_bootstrap(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        device_id = None
        try:
            conn.settimeout(10)
            msg = recv_frame(conn)
            if msg.get("type") != "bootstrap_hello" or msg.get("version") != PROTOCOL_VERSION:
                raise ValueError("invalid bootstrap hello")
            device_id = msg.get("device_id")
            local_ipv4 = msg.get("local_ipv4")
            supplied = msg.get("proof")
            if not all(isinstance(x, str) and x for x in (device_id, local_ipv4, supplied)):
                raise ValueError("malformed bootstrap hello")
            expected = proof(self.secret, str(msg.get("nonce", "")), device_id,
                             str(msg.get("session_id", "bootstrap")))
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("bootstrap authentication failed")
            lease = self.allocator.allocate(device_id, local_ipv4)
            self._ensure_callback(lease)
            send_frame(conn, {
                "type": "bootstrap_ok", "version": PROTOCOL_VERSION,
                "device_id": device_id, "callback_host": self.advertised_host,
                "callback_port": lease.callback_port, "callback_token": lease.token,
                "generation": lease.generation,
            })
            self._emit("bootstrap.registered", device_id=device_id, local_ipv4=local_ipv4,
                       callback_port=lease.callback_port, generation=lease.generation,
                       peer=str(addr))
        except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
            try:
                send_frame(conn, {"type": "error", "error": type(exc).__name__})
            except OSError:
                pass
            self._emit("bootstrap.rejected", device_id=device_id, error=type(exc).__name__, peer=str(addr))
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _ensure_callback(self, lease: CallbackLease) -> None:
        with self._lock:
            if lease.callback_port in self._callback_servers:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, lease.callback_port))
            sock.listen(16)
            self._callback_servers[lease.callback_port] = sock
        threading.Thread(target=self._accept_callback, args=(sock, lease), daemon=True,
                         name=f"callback-{lease.callback_port}").start()

    def _accept_callback(self, server: socket.socket, lease: CallbackLease) -> None:
        server.settimeout(.2)
        while not self._stop.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_callback, args=(conn, addr, lease), daemon=True).start()

    def _handle_callback(self, conn: socket.socket, addr: Tuple[str, int], lease: CallbackLease) -> None:
        authenticated = False
        try:
            conn.settimeout(10)
            hello = recv_frame(conn)
            if hello.get("type") != "callback_hello" or hello.get("version") != PROTOCOL_VERSION:
                raise ValueError("invalid callback hello")
            if hello.get("device_id") != lease.device_id or hello.get("generation") != lease.generation:
                raise ValueError("callback identity mismatch")
            if not hmac.compare_digest(str(hello.get("token", "")), lease.token):
                raise ValueError("callback token mismatch")
            authenticated = True
            send_frame(conn, {"type": "callback_welcome", "version": PROTOCOL_VERSION,
                              "device_id": lease.device_id, "generation": lease.generation})
            self._emit("callback.connected", device_id=lease.device_id,
                       callback_port=lease.callback_port, generation=lease.generation, peer=str(addr))
            while not self._stop.is_set():
                msg = recv_frame(conn)
                if msg.get("type") == "heartbeat":
                    send_frame(conn, {"type": "heartbeat_ack", "sequence": msg.get("sequence")})
                elif msg.get("type") == "ws_message":
                    decision = {"type": "forward", "id": msg.get("id")}
                    if self.callback_handler is not None:
                        try:
                            custom = self.callback_handler(lease, msg)
                            if isinstance(custom, dict):
                                decision.update(custom)
                        except Exception as exc:
                            self._emit("callback.handler_error", device_id=lease.device_id,
                                       error=type(exc).__name__)
                    send_frame(conn, decision)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            if authenticated:
                self._emit("callback.disconnected", device_id=lease.device_id,
                           callback_port=lease.callback_port, generation=lease.generation)
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            for sock in self._callback_servers.values():
                try:
                    sock.close()
                except OSError:
                    pass
            self._callback_servers.clear()
