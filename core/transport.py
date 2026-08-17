"""Threaded IPv4 TCP trainer endpoint: authenticated per-table connections.

Protocol (4-byte big-endian length + JSON, see core.protocol):

  client -> trainer:
    {"type":"hello","version":2,"device_id":D,"table_id":T,"proof":P}
    {"type":"ws_message","v":3,"id":...,"kind":"ws_message","direction":...,
     "text":bool,"url":...,"ws_id":...,"payload_b64":...}
    {"type":"heartbeat","sequence":N}
  trainer -> client:
    {"type":"welcome","version":2,"device_id":D,"table_id":T,"slot":N}
    {"id":<same id>,"action":"forward|drop|replace|schedule_send",
     "payload_b64":...,"delay_ms":N,"token":...}   (response to ws_message)
    {"type":"heartbeat_ack","sequence":N}
    {"type":"error","error":"..."}

Every ws_message gets an immediate response (the hook has a ~220 ms read
timeout). The decision comes from an injected ``ws_handler(table_id, message)``
callback; the default is ``forward``. Slot reservation happens only after a
successful authenticated handshake and is released when the connection closes.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from .protocol import PROTOCOL_VERSION, recv_frame, send_frame, verify_hello


class TrainerServer:
    def __init__(self, secret: bytes, session_id: str, advertised_nonce: str, slot_pool=None,
                 host="0.0.0.0", port=0, on_connect=None, on_event=None,
                 ws_handler: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None):
        self.secret, self.session_id, self.advertised_nonce = secret, session_id, advertised_nonce
        self.slot_pool, self.host, self.port = slot_pool, host, port
        self.on_connect, self.on_event = on_connect, on_event
        self.ws_handler = ws_handler
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._active: set = set()
        self._lock = threading.Lock()
        self._threads: list = []
        self._seq = 0

    def _emit(self, event, **fields):
        if self.on_event:
            try:
                self.on_event(event, **fields)
            except Exception:
                pass

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(32)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self.port

    def _accept_loop(self):
        self._server.settimeout(.2)
        while not self._stop.is_set():
            try:
                conn, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._emit("tcp.accept", peer=str(address))
            thread = threading.Thread(target=self._handle, args=(conn, address), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _handle(self, conn, address):
        device_id = table_id = None
        try:
            conn.settimeout(10)
            try:
                device_id, table_id = verify_hello(recv_frame(conn), self.secret, self.advertised_nonce, self.session_id)
            except ValueError as exc:
                send_frame(conn, {"type": "error", "error": type(exc).__name__})
                self._emit("hello.rejected", peer=str(address), error=type(exc).__name__)
                return
            connection_key = (device_id, table_id)
            with self._lock:
                if connection_key in self._active:
                    send_frame(conn, {"type": "error", "error": "duplicate_connection"})
                    return
                # Reservation is after successful authenticated handshake only.
                slot = self.slot_pool.reserve(connection_key) if self.slot_pool else None
                if self.slot_pool and slot is None:
                    send_frame(conn, {"type": "error", "error": "no_free_slots"})
                    return
                self._active.add(connection_key)
            self._emit("hello.authenticated", device_id=device_id, table_id=table_id, peer=str(address))
            self._emit("slot.reserved", device_id=device_id, table_id=table_id, slot=slot)
            send_frame(conn, {"type": "welcome", "version": PROTOCOL_VERSION,
                              "device_id": device_id, "table_id": table_id, "slot": slot})
            if self.on_connect:
                try:
                    self.on_connect(table_id, slot, conn, address)
                except Exception:
                    pass
            conn.settimeout(5)
            while not self._stop.is_set():
                try:
                    message = recv_frame(conn)
                except socket.timeout:
                    continue
                mtype = message.get("type")
                if mtype == "heartbeat":
                    send_frame(conn, {"type": "heartbeat_ack", "sequence": message.get("sequence")})
                    self._emit("heartbeat.received", device_id=device_id, table_id=table_id,
                               sequence=message.get("sequence"))
                elif mtype == "ws_message":
                    decision = self._decide(table_id, message)
                    response = dict(decision) if isinstance(decision, dict) else {}
                    response.setdefault("id", message.get("id"))
                    response.setdefault("action", "forward")
                    send_frame(conn, response)
                    self._emit("ws_message.handled", device_id=device_id, table_id=table_id,
                               message_id=message.get("id"), action=response.get("action"))
                else:
                    self._emit("device.unknown_message", device_id=device_id, table_id=table_id,
                               message_type=mtype, severity="WARN")
        except (ConnectionError, OSError, ValueError) as exc:
            self._emit("tcp.disconnected", severity="WARN", device_id=device_id, table_id=table_id,
                       peer=str(address), error=type(exc).__name__)
        finally:
            if table_id and device_id:
                key = (device_id, table_id)
                with self._lock:
                    self._active.discard(key)
                if self.slot_pool:
                    self.slot_pool.release(key)
                self._emit("slot.released", device_id=device_id, table_id=table_id)
            try:
                conn.close()
            except OSError:
                pass

    def _decide(self, table_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """One ws_message -> one decision. Never raises: default is forward."""
        if self.ws_handler is None:
            return {"id": message.get("id"), "action": "forward"}
        try:
            return self.ws_handler(table_id, message)
        except Exception as exc:
            self._emit("ws_handler.error", table_id=table_id, error=type(exc).__name__, severity="ERROR")
            return {"id": message.get("id"), "action": "forward"}

    def stop(self):
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
