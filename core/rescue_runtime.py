"""Minimal v2 direct transport + verified v1 business runtime."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import socket
import struct
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .logging import SessionLogger
from .pcap_ring import PcapRingManager, PcapRingPolicy
from .verified_v1.coin_bridge_live import LiveCoinBridge
from .verified_v1.coin_action_wire import decode_packet as _decode_v1_packet
from .verified_v1.eye_direct_proxy import DirectBackendProxy, DirectBackendSlot

PROTOCOL_VERSION = 2
TRANSPORT_MAX_FRAME = 16 * 1024 * 1024
DEFAULT_PORT = 19037
DEFAULT_PUBLIC_HOST = "37.192.228.101"


def _short(value: Any, limit: int = 180) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _read_exact(sock: socket.socket, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise ConnectionError("peer disconnected")
        out.extend(chunk)
    return bytes(out)


def recv_frame(sock: socket.socket) -> Dict[str, Any]:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if not 0 < size <= TRANSPORT_MAX_FRAME:
        raise ValueError(f"invalid transport frame size {size}")
    value = json.loads(_read_exact(sock, size).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("transport frame must be JSON object")
    return value


def send_frame(sock: socket.socket, value: Dict[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > TRANSPORT_MAX_FRAME:
        raise ValueError("transport reply too large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def direct_proof(secret: bytes, device_id: str, table_id: str) -> str:
    material = f"{device_id}|{table_id}|trainer|{PROTOCOL_VERSION}".encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _event_meta(event: Dict[str, Any]) -> tuple[str, Optional[int]]:
    if bool(event.get("text", False)):
        return "", None
    encoded = str(event.get("payload_b64") or "")
    if not encoded:
        return "", None
    try:
        raw = base64.b64decode(encoded, validate=True)
        packet = _decode_v1_packet(raw)
        p = packet.get("p")
        if not isinstance(p, dict):
            return "", None
        cmd = str(p.get("c") or "")
        room_raw = p.get("r")
        room = None
        try:
            room = int(room_raw) if room_raw is not None else None
        except (TypeError, ValueError):
            room = None
        return cmd, room
    except Exception:
        return "", None


def _event_command(event: Dict[str, Any]) -> str:
    return _event_meta(event)[0]


class DirectTableServer:
    def __init__(
        self,
        secret: bytes,
        *,
        host: str,
        port: int,
        on_message: Callable[[str, str, Dict[str, Any]], Dict[str, Any]],
        on_connect: Optional[Callable[[str, str, Tuple[str, int]], None]] = None,
        on_disconnect: Optional[Callable[[str, str, Tuple[str, int], str], None]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.secret = secret
        self.host = host
        self.port = int(port)
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_event = on_event
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._active: Dict[Tuple[str, str], socket.socket] = {}
        self._all: set[socket.socket] = set()

    def _emit(self, event: str, **fields: Any) -> None:
        if self.on_event:
            try:
                self.on_event(event, fields)
            except Exception:
                pass

    def counts(self) -> tuple[int, int]:
        with self._lock:
            keys = tuple(self._active)
        return len({device for device, _ in keys}), len(keys)

    def start(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        server.bind((self.host, self.port))
        server.listen(256)
        server.settimeout(0.25)
        self._server = server
        self.port = int(server.getsockname()[1])
        threading.Thread(target=self._accept_loop, daemon=True, name="v2-direct-accept").start()
        return self.port

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            with self._lock:
                self._all.add(conn)
            self._emit("tcp.accept", peer=str(addr))
            threading.Thread(
                target=self._handle,
                args=(conn, addr),
                daemon=True,
                name=f"v2-table-{addr[0]}-{addr[1]}",
            ).start()

    def _handle(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        device_id = ""
        table_id = ""
        key: Optional[Tuple[str, str]] = None
        reason = "closed"
        try:
            conn.settimeout(5.0)
            hello = recv_frame(conn)
            device_id = str(hello.get("device_id") or "")
            table_id = str(hello.get("table_id") or "")
            self._emit(
                "hello.received",
                peer=str(addr),
                hello_type=hello.get("type"),
                version=hello.get("version"),
                device_id=device_id,
                table_id=table_id,
            )
            if hello.get("type") != "direct_hello" or hello.get("version") != PROTOCOL_VERSION:
                send_frame(conn, {"type": "error", "error": "invalid_direct_hello"})
                raise ValueError("invalid_direct_hello")
            if not device_id or not table_id or device_id.lower() == "unknown":
                send_frame(conn, {"type": "error", "error": "invalid_identity"})
                raise ValueError("invalid_identity")
            if not hmac.compare_digest(
                str(hello.get("proof") or ""),
                direct_proof(self.secret, device_id, table_id),
            ):
                send_frame(conn, {"type": "error", "error": "bad_proof"})
                raise ValueError("bad_proof")

            key = (device_id, table_id)
            with self._lock:
                old = self._active.get(key)
                self._active[key] = conn
            if old is not None and old is not conn:
                try:
                    old.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    old.close()
                except OSError:
                    pass

            devices, tables = self.counts()
            send_frame(
                conn,
                {
                    "type": "welcome",
                    "version": PROTOCOL_VERSION,
                    "device_id": device_id,
                    "table_id": table_id,
                    "devices": devices,
                    "tables": tables,
                },
            )
            self._emit("hello.authenticated", device_id=device_id, table_id=table_id, peer=str(addr))
            if self.on_connect:
                self.on_connect(device_id, table_id, addr)
            conn.settimeout(15.0)

            while not self._stop.is_set():
                try:
                    msg = recv_frame(conn)
                except socket.timeout:
                    continue
                kind = str(msg.get("type") or msg.get("kind") or "")
                if kind == "heartbeat":
                    send_frame(conn, {"type": "heartbeat_ack", "sequence": msg.get("sequence")})
                    continue
                if kind == "ws_message":
                    try:
                        decision = self.on_message(device_id, table_id, msg)
                    except Exception as exc:
                        self._emit(
                            "handler.error",
                            device_id=device_id,
                            table_id=table_id,
                            error=type(exc).__name__,
                        )
                        decision = {"id": msg.get("id"), "action": "forward"}
                    if not isinstance(decision, dict):
                        decision = {}
                    decision.setdefault("id", msg.get("id"))
                    decision.setdefault("ws_id", msg.get("ws_id"))
                    decision.setdefault("action", "forward")
                    send_frame(conn, decision)
                    continue
                send_frame(conn, {"id": msg.get("id"), "action": "forward"})
        except (ConnectionError, EOFError, OSError, ValueError, json.JSONDecodeError) as exc:
            reason = type(exc).__name__
            self._emit(
                "tcp.disconnected",
                device_id=device_id or None,
                table_id=table_id or None,
                peer=str(addr),
                error=reason,
            )
        finally:
            if key is not None:
                with self._lock:
                    if self._active.get(key) is conn:
                        self._active.pop(key, None)
            with self._lock:
                self._all.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            if device_id and table_id and self.on_disconnect:
                self.on_disconnect(device_id, table_id, addr, reason)

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            conns = tuple(self._all)
            self._all.clear()
            self._active.clear()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


class AccountPool:
    def __init__(self, accounts: list[str]) -> None:
        clean = []
        for account in accounts:
            account = str(account or "").strip()
            if account and account not in clean:
                clean.append(account)
        if not clean:
            raise ValueError("no usable PokerEYE accounts")
        self._accounts = clean
        self._lock = threading.Lock()
        self._by_owner: Dict[str, str] = {}
        self._owner: Dict[str, str] = {}
        self._bad: set[str] = set()

    @classmethod
    def from_file(cls, path: Path) -> "AccountPool":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"{path}: accounts[] missing")
        accounts = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            account = str(row.get("account_id") or "").strip()
            state = str(row.get("state") or "").strip().upper()
            if account and bool(row.get("validated")) and state not in {"INVALID", "QUARANTINED"}:
                accounts.append(account)
        if not accounts:
            raise ValueError(f"{path}: no validated usable accounts")
        return cls(accounts)

    def acquire(self, owner: str) -> Optional[str]:
        with self._lock:
            old = self._by_owner.get(owner)
            if old:
                return old
            for account in self._accounts:
                if account in self._bad or account in self._owner:
                    continue
                self._by_owner[owner] = account
                self._owner[account] = owner
                return account
            return None

    def mark_bad(self, owner: str, account: str) -> None:
        with self._lock:
            self._bad.add(account)
            if self._by_owner.get(owner) == account:
                self._by_owner.pop(owner, None)
            if self._owner.get(account) == owner:
                self._owner.pop(account, None)

    def release(self, owner: str) -> None:
        with self._lock:
            account = self._by_owner.pop(owner, None)
            if account and self._owner.get(account) == owner:
                self._owner.pop(account, None)

    def available(self) -> int:
        with self._lock:
            return max(0, len(self._accounts) - len(self._owner) - len(self._bad))


@dataclass
class Worker:
    device_id: str
    table_id: str
    owner: str
    ready: bool = False
    failed: bool = False
    failure: str = ""
    connected: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    bridge: Optional[LiveCoinBridge] = None
    proxy: Optional[DirectBackendProxy] = None
    account_id: Optional[str] = None
    async_lock: Optional[asyncio.Lock] = None
    backlog: list[Dict[str, Any]] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)


class VerifiedBusinessHub:
    def __init__(
        self,
        repo_root: Path,
        *,
        chip_scale: int = 100,
        account_file: Optional[str] = None,
        credential_file: Optional[str] = None,
        backend_host: str = "gs.eye-panel.com",
        backend_port: int = 443,
        on_status: Optional[Callable[[str, str, str, str], None]] = None,
    ) -> None:
        if int(chip_scale) != 100:
            raise ValueError("chip-scale must be 100")
        if importlib.util.find_spec("grpc") is None:
            raise RuntimeError("grpcio is required: python -m pip install grpcio")

        self.repo_root = repo_root.resolve()
        self.chip_scale = int(chip_scale)
        self.backend_host = backend_host
        self.backend_port = int(backend_port)
        self.on_status = on_status

        account_candidates = [
            Path(account_file).expanduser() if account_file else None,
            self.repo_root / "config" / "backend_accounts.local.json",
            self.repo_root.parent / "runtime_state" / "backend_accounts.json",
            self.repo_root.parent / "ready_v6" / "runtime_state" / "backend_accounts.json",
        ]
        credential_candidates = [
            Path(credential_file).expanduser() if credential_file else None,
            self.repo_root / "secrets" / "eye.agent",
            self.repo_root.parent / ".eye",
            self.repo_root.parent / "ready_v6" / ".eye",
        ]
        self.account_file = next((p for p in account_candidates if p and p.is_file()), None)
        self.credential_file = next((p for p in credential_candidates if p and p.is_file()), None)
        if self.account_file is None:
            raise FileNotFoundError("PokerEYE account registry not found")
        if self.credential_file is None:
            raise FileNotFoundError("PokerEYE credential not found")
        if not self.credential_file.read_text(encoding="utf-8-sig").strip():
            raise RuntimeError("PokerEYE credential file is empty")

        self.accounts = AccountPool.from_file(self.account_file)
        self._workers: Dict[Tuple[str, str], Worker] = {}
        self._lock = threading.RLock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="verified-v1-business")
        self.thread.start()
        self._reaper_future = asyncio.run_coroutine_threadsafe(self._reaper_loop(), self.loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def description(self) -> str:
        return (
            f"verified-v1 direct {self.backend_host}:{self.backend_port} "
            f"accounts_free={self.accounts.available()}"
        )

    def _status(self, worker: Worker, state: str, detail: str = "") -> None:
        if self.on_status:
            try:
                self.on_status(worker.device_id, worker.table_id, state, detail)
            except Exception:
                pass

    def _get_or_create(self, device_id: str, table_id: str) -> Worker:
        key = (device_id, table_id)
        with self._lock:
            worker = self._workers.get(key)
            if worker:
                return worker
            worker = Worker(device_id, table_id, f"{device_id}:{table_id}")
            self._workers[key] = worker
        asyncio.run_coroutine_threadsafe(self._start_worker(worker), self.loop)
        return worker


    async def _start_worker(self, worker: Worker) -> None:
        worker.async_lock = asyncio.Lock()
        self._status(worker, "STARTING", "PokerEYE login")
        last_error: Optional[BaseException] = None
        try:
            for _ in range(8):
                account = self.accounts.acquire(worker.owner)
                if not account:
                    raise RuntimeError("no free validated PokerEYE account slot")
                worker.account_id = account
                proxy = DirectBackendProxy(
                    DirectBackendSlot(
                        account_id=account,
                        credential_file=self.credential_file,
                        host=self.backend_host,
                        port=self.backend_port,
                    ),
                    logger=lambda tag, msg, w=worker: self._status(
                        w, f"EYE:{tag}", _short(msg, 220)
                    ),
                )
                try:
                    eye_host, eye_port = await proxy.start()
                    bridge = LiveCoinBridge(
                        eye_host,
                        eye_port,
                        self.chip_scale,
                        0.01,
                        False,
                        diagnostic_sink=lambda tag, msg, w=worker: self._status(
                            w, f"BIZ:{tag}", _short(msg, 220)
                        ),
                    )
                    ensure_task = asyncio.create_task(bridge.ensure_eye())
                    worker.tasks.append(ensure_task)
                    await proxy.wait_backend_ready(timeout=18.0)
                    await asyncio.wait_for(bridge.eye_ready.wait(), timeout=5.0)
                    proxy.bind_bridge(bridge)
                    worker.proxy = proxy
                    worker.bridge = bridge
                    worker.tasks.append(asyncio.create_task(bridge.heartbeat_loop()))
                    break
                except BaseException as exc:
                    last_error = exc
                    try:
                        await proxy.close()
                    except Exception:
                        pass
                    self.accounts.mark_bad(worker.owner, account)
                    worker.account_id = None
                    self._status(worker, "EYE_RETRY", f"{type(exc).__name__}: {_short(exc)}")

            if worker.bridge is None:
                raise RuntimeError(f"backend accounts exhausted: {last_error}")

            while True:
                with self._lock:
                    if not worker.backlog:
                        worker.ready = True
                        break
                    event = worker.backlog.pop(0)
                # Rebuild table/hero/room context, but never replay an old turn or
                # dummy/action frame into a newly-ready EYE channel. The first bot
                # action must come from a fresh live hero turn after READY.
                if _event_command(event) in {"game.user_turn", "lobby.dummy", "game.user_action"}:
                    continue
                async with worker.async_lock:
                    _decision, finish = await worker.bridge.handle_event(event)
                    if finish:
                        asyncio.create_task(worker.bridge.finish_hint(finish))
            suffix = worker.account_id.rsplit("-", 1)[-1] if worker.account_id else "?"
            self._status(worker, "READY", f"account=*{suffix}")
        except BaseException as exc:
            worker.failed = True
            worker.failure = f"{type(exc).__name__}: {_short(exc, 300)}"
            self._status(worker, "FAILED", worker.failure)

    async def _process(self, worker: Worker, event: Dict[str, Any]) -> Dict[str, Any]:
        assert worker.bridge is not None and worker.async_lock is not None
        async with worker.async_lock:
            decision, finish = await worker.bridge.handle_event(event)
            if finish:
                # For an immediate async action, preserve the verified v1 ordering:
                # FinishRoundHint must be on the EYE channel before the action
                # command is released to Android.
                await worker.bridge.finish_hint(finish)
            return decision if isinstance(decision, dict) else {
                "id": event.get("id"),
                "action": "forward",
            }

    def handle(self, device_id: str, table_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        worker = self._get_or_create(device_id, table_id)
        with self._lock:
            worker.last_seen = time.monotonic()
            if worker.failed:
                return {"id": event.get("id"), "action": "forward"}
            if not worker.ready:
                if len(worker.backlog) >= 6000:
                    worker.backlog.pop(0)
                worker.backlog.append(dict(event))
                return {"id": event.get("id"), "action": "forward"}
        try:
            future = asyncio.run_coroutine_threadsafe(self._process(worker, dict(event)), self.loop)
            return future.result(timeout=1.75)
        except Exception as exc:
            self._status(worker, "SLOW/ERROR", f"{type(exc).__name__}: {_short(exc)}")
            return {"id": event.get("id"), "action": "forward"}

    async def _close_worker(self, key: Tuple[str, str], worker: Worker) -> None:
        for task in tuple(worker.tasks):
            if not task.done():
                task.cancel()
        if worker.tasks:
            await asyncio.gather(*worker.tasks, return_exceptions=True)
        if worker.proxy:
            try:
                await worker.proxy.close()
            except Exception:
                pass
        self.accounts.release(worker.owner)
        with self._lock:
            if self._workers.get(key) is worker:
                self._workers.pop(key, None)
        self._status(worker, "RELEASED", "idle table context closed")

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(15.0)
                now = time.monotonic()
                with self._lock:
                    stale = [
                        (key, worker)
                        for key, worker in self._workers.items()
                        if now - worker.last_seen > 300.0
                    ]
                for key, worker in stale:
                    await self._close_worker(key, worker)
        except asyncio.CancelledError:
            return

    async def _shutdown(self) -> None:
        with self._lock:
            rows = list(self._workers.items())
        for key, worker in rows:
            await self._close_worker(key, worker)

    def close(self) -> None:
        if not self.thread.is_alive():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            future.result(timeout=8.0)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3.0)


TABLE_ACTIVATION_COMMANDS = {
    "game.take_Seat",
    "game.take_seat",
    "game.reserve_Seat",
    "game.reserve_seat",
    "game.game_init",
    "game.pre_hand_start_info",
    "game.seat",
    "game.user_turn",
}


class RescueTrainer:
    def __init__(
        self,
        *,
        secret: str,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        public_host: str = DEFAULT_PUBLIC_HOST,
        log_dir: str = "logs",
        game_port: int = 17770,
        chip_scale: int = 100,
        account_file: Optional[str] = None,
        credential_file: Optional[str] = None,
        backend_host: str = "gs.eye-panel.com",
        backend_port: int = 443,
    ) -> None:
        if int(chip_scale) != 100:
            raise ValueError("chip-scale must be 100")
        self.secret = secret.encode("utf-8")
        self.host = host
        self.port = int(port)
        self.public_host = public_host
        self.logger = SessionLogger(log_dir)
        self._stop = threading.Event()
        self.pcap = PcapRingManager(
            Path(log_dir) / f"run_{self.logger.run_id}" / "pcap",
            PcapRingPolicy(game_port=game_port),
        )
        self.business = VerifiedBusinessHub(
            Path.cwd(),
            chip_scale=chip_scale,
            account_file=account_file,
            credential_file=credential_file,
            backend_host=backend_host,
            backend_port=backend_port,
            on_status=self._business_status,
        )
        # Transport channel != logical poker table. Keep a cheap bounded prelude
        # per channel and allocate PokerEYE only once real table/game traffic
        # identifies a SmartFox room.
        self._route_lock = threading.RLock()
        self._device_prelude: Dict[str, deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._room_prelude: Dict[Tuple[str, int], deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=2500)
        )
        self._ws_room: Dict[Tuple[str, str], int] = {}
        self._logical_seen: set[Tuple[str, str]] = set()
        self.server = DirectTableServer(
            self.secret,
            host=host,
            port=port,
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_event=self._transport_event,
        )

    def _transport_event(self, event: str, fields: Dict[str, Any]) -> None:
        severity = "WARN" if event in {"tcp.disconnected", "handler.error"} else "INFO"
        self.logger.emit(f"transport.{event}", severity=severity, **fields)
        if event == "tcp.accept":
            print(f"[>] TCP {fields.get('peer')}", flush=True)
        elif event == "hello.received":
            print(
                f"[>] HELLO type={fields.get('hello_type')} "
                f"device={fields.get('device_id') or '?'} "
                f"table={fields.get('table_id') or '?'}",
                flush=True,
            )
        elif event == "handler.error":
            print(
                f"[!] HANDLER device={fields.get('device_id')} "
                f"table={fields.get('table_id')} error={fields.get('error')}",
                flush=True,
            )

    def _business_status(self, device: str, table: str, state: str, detail: str) -> None:
        severity = "WARN" if state in {"FAILED", "EYE_RETRY", "SLOW/ERROR"} else "INFO"
        self.logger.emit(
            "business.state",
            severity=severity,
            device_id=device,
            table_id=table,
            state=state,
            detail=detail,
        )
        if state in {"STARTING", "READY", "FAILED", "EYE_RETRY", "RELEASED"}:
            sign = "+" if state == "READY" else ("!" if state in {"FAILED", "EYE_RETRY"} else "~")
            print(
                f"[{sign}] BIZ {device}/{table} {state}"
                + (f" {detail}" if detail else ""),
                flush=True,
            )

    def _on_connect(self, device_id: str, transport_id: str, addr: Tuple[str, int]) -> None:
        devices, transports = self.server.counts()
        print(
            f"[+] DEVICE ONLINE device={device_id} transport={transport_id} "
            f"peer={addr[0]}:{addr[1]} devices={devices} transports={transports}",
            flush=True,
        )

    def _on_disconnect(
        self, device_id: str, transport_id: str, addr: Tuple[str, int], reason: str
    ) -> None:
        devices, transports = self.server.counts()
        print(
            f"[~] DEVICE OFFLINE device={device_id} transport={transport_id} "
            f"reason={reason} devices={devices} transports={transports}",
            flush=True,
        )
        # Logical room workers are intentionally retained. Android reconnects the
        # one device transport without forcing PokerEYE login/table rebuild.

    def _logical_table_for(
        self, device_id: str, message: Dict[str, Any]
    ) -> tuple[Optional[str], str, Optional[int], bool, str]:
        cmd, room = _event_meta(message)
        ws_id = str(message.get("ws_id") or "")
        key = (device_id, ws_id)
        activate = cmd in TABLE_ACTIVATION_COMMANDS and room is not None and room > 0

        with self._route_lock:
            # Keep this map lazy so lightweight diagnostic instances created
            # without the heavyweight constructor still route safely.
            ws_room = getattr(self, "_ws_room", None)
            if ws_room is None:
                ws_room = {}
                self._ws_room = ws_room
            if room is not None and room > 0 and cmd.startswith("game.") and ws_id:
                ws_room[key] = room
            known_room = (
                room if room is not None and room > 0
                else ws_room.get(key) if ws_id
                else None
            )

        logical = f"room:{known_room}" if known_room is not None else None
        return logical, cmd, known_room, activate, ws_id

    def _on_message(
        self, device_id: str, transport_id: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        logical, cmd, room, activate, ws_id = self._logical_table_for(
            device_id, message
        )

        payload_b64 = str(message.get("payload_b64") or "")
        if payload_b64 and not bool(message.get("text", False)):
            try:
                raw = base64.b64decode(payload_b64, validate=True)
                if raw:
                    ring_key = logical or f"{device_id}-unrouted"
                    self.pcap.ring(ring_key).write_packet(time.time(), raw)
            except Exception:
                pass

        with self._route_lock:
            logical_key = (device_id, logical) if logical is not None else None
            already_active = logical_key in self._logical_seen if logical_key else False

            # Before a real room is activated, retain shared device context and
            # room-specific context separately. This is important because Coin can
            # move one room across several RealWebSocket objects.
            if not already_active:
                if room is not None and room > 0:
                    self._room_prelude[(device_id, room)].append(dict(message))
                else:
                    self._device_prelude[device_id].append(dict(message))

            if activate and logical is not None and not already_active and room is not None:
                self._logical_seen.add((device_id, logical))
                replay = list(self._device_prelude.get(device_id, ()))
                replay.extend(self._room_prelude.pop((device_id, room), ()))
            else:
                replay = None

        if logical is None:
            return {
                "id": message.get("id"),
                "ws_id": ws_id,
                "action": "forward",
            }

        if replay is not None:
            print(
                f"[+] TABLE DISCOVERED device={device_id} {logical} "
                f"via={cmd} ws={ws_id or '?'}",
                flush=True,
            )
            decision = {
                "id": message.get("id"),
                "ws_id": ws_id,
                "action": "forward",
            }
            for event in replay:
                current = self.business.handle(device_id, logical, event)
                if event.get("id") == message.get("id"):
                    decision = current
            decision.setdefault("ws_id", ws_id)
            return decision

        if already_active or (device_id, logical) in self._logical_seen:
            decision = self.business.handle(device_id, logical, message)
            decision.setdefault("ws_id", ws_id)
            if str(decision.get("action") or "") in {"schedule_send", "replace"}:
                print(
                    f"[!] ACTION device={device_id} {logical} "
                    f"ws={ws_id or '?'} action={decision.get('action')} "
                    f"delay={int(decision.get('delay_ms') or 0)}ms",
                    flush=True,
                )
            return decision

        return {
            "id": message.get("id"),
            "ws_id": ws_id,
            "action": "forward",
        }


    def start(self) -> None:
        port = self.server.start()
        self.logger.emit(
            "trainer.ready",
            flush=True,
            host=self.host,
            port=port,
            public_host=self.public_host,
            business=self.business.description(),
            chip_scale=100,
        )
        print("[+] Trainer v2 RESCUE ready", flush=True)
        print(f"    listen     {self.host}:{port}", flush=True)
        print(f"    public     {self.public_host}:{port}", flush=True)
        print(
            f"    protocol   direct_hello v{PROTOCOL_VERSION} "
            f"max={TRANSPORT_MAX_FRAME // (1024*1024)}MiB",
            flush=True,
        )
        print(f"    transport  one persistent TCP per physical device", flush=True)
        print(f"    business   {self.business.description()} (lazy per real room)", flush=True)
        print("    chip-scale 100", flush=True)
        print(f"    run        {self.logger.run_id} dir={self.logger.directory}", flush=True)

    def run_forever(self) -> None:
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.server.stop()
        self.business.close()
        self.pcap.close_all()
        self.logger.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="poker-eye-v2 rescue trainer")
    p.add_argument("--secret", required=True)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=19037)
    p.add_argument("--public-host", default=DEFAULT_PUBLIC_HOST)
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--game-port", type=int, default=17770)
    p.add_argument("--chip-scale", type=int, default=100)
    p.add_argument("--account-file", default=None)
    p.add_argument("--credential-file", default=None)
    p.add_argument("--backend-host", default="gs.eye-panel.com")
    p.add_argument("--backend-port", type=int, default=443)
    return p


def run_from_args(args: argparse.Namespace) -> None:
    trainer = RescueTrainer(
        secret=args.secret,
        host=args.host,
        port=args.port,
        public_host=args.public_host,
        log_dir=args.log_dir,
        game_port=args.game_port,
        chip_scale=args.chip_scale,
        account_file=args.account_file,
        credential_file=args.credential_file,
        backend_host=args.backend_host,
        backend_port=args.backend_port,
    )
    trainer.start()
    trainer.run_forever()
