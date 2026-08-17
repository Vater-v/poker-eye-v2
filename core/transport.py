"""Threaded IPv4 TCP trainer endpoint: authenticated per-table connections."""
from __future__ import annotations
import socket
import threading
from .protocol import PROTOCOL_VERSION, recv_frame, send_frame, verify_hello


class TrainerServer:
    def __init__(self, secret: bytes, session_id: str, advertised_nonce: str, slot_pool=None,
                 host="0.0.0.0", port=0, on_connect=None, on_event=None):
        self.secret, self.session_id, self.advertised_nonce = secret, session_id, advertised_nonce
        self.slot_pool, self.host, self.port = slot_pool, host, port
        self.on_connect, self.on_event = on_connect, on_event
        self._server = None; self._stop = threading.Event(); self._active = set(); self._lock = threading.Lock(); self._threads = []

    def _emit(self, event, **fields):
        if self.on_event: self.on_event(event, **fields)

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port)); self._server.listen(); self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start(); return self.port

    def _accept_loop(self):
        self._server.settimeout(.2)
        while not self._stop.is_set():
            try: conn, address = self._server.accept()
            except socket.timeout: continue
            except OSError: break
            self._emit("tcp.accept", peer=str(address))
            thread = threading.Thread(target=self._handle, args=(conn, address), daemon=True); self._threads.append(thread); thread.start()

    def _handle(self, conn, address):
        table_id = device_id = None
        try:
            conn.settimeout(10)
            device_id, table_id = verify_hello(recv_frame(conn), self.secret, self.advertised_nonce, self.session_id)
            connection_key = (device_id, table_id)
            with self._lock:
                if connection_key in self._active:
                    send_frame(conn, {"type":"error", "error":"duplicate_connection"}); return
                # Reservation is after successful authenticated handshake only.
                slot = self.slot_pool.reserve(connection_key) if self.slot_pool else None
                if self.slot_pool and slot is None:
                    send_frame(conn, {"type":"error", "error":"no_free_slots"}); return
                self._active.add(connection_key)
            self._emit("hello.authenticated", device_id=device_id, table_id=table_id, peer=str(address))
            self._emit("slot.reserved", device_id=device_id, table_id=table_id, slot=slot)
            send_frame(conn, {"type":"welcome", "version": PROTOCOL_VERSION, "device_id":device_id, "table_id":table_id, "slot":slot})
            if self.on_connect: self.on_connect(table_id, slot, conn, address)
            else:
                conn.settimeout(1)
                while not self._stop.is_set():
                    try: message = recv_frame(conn)
                    except socket.timeout: continue
                    if message.get("type") == "heartbeat":
                        send_frame(conn, {"type":"heartbeat_ack", "sequence":message.get("sequence")})
                        self._emit("heartbeat.received", device_id=device_id, table_id=table_id, sequence=message.get("sequence"))
        except (ConnectionError, OSError, ValueError) as exc:
            self._emit("tcp.disconnected", severity="WARN", device_id=device_id, table_id=table_id, peer=str(address), error=type(exc).__name__)
        finally:
            if table_id and device_id:
                key = (device_id, table_id)
                with self._lock: self._active.discard(key)
                if self.slot_pool: self.slot_pool.release(key)
                self._emit("slot.released", device_id=device_id, table_id=table_id)
            try: conn.close()
            except OSError: pass

    def stop(self):
        self._stop.set()
        if self._server:
            try: self._server.close()
            except OSError: pass
