"""UDP broadcast advertisement and optional trainer slot bookkeeping."""
import json
import secrets
import socket
import threading
from .protocol import PROTOCOL_VERSION

DEFAULT_INTERVAL = 1.25


class SlotPool:
    """Compatibility bookkeeping; advertisements never reserve client slots."""
    def __init__(self, slots=1):
        if slots < 1:
            raise ValueError("slots must be positive")
        self._total = slots
        self._lock = threading.Lock()
        self._claims = {}

    def reserve(self, table_id):
        with self._lock:
            if table_id in self._claims:
                return self._claims[table_id]
            used = set(self._claims.values())
            free = [n for n in range(1, self._total + 1) if n not in used]
            if not free: return None
            self._claims[table_id] = free[0]
            return free[0]

    def release(self, table_id):
        with self._lock: return self._claims.pop(table_id, None)

    def claimed(self):
        with self._lock: return dict(self._claims)

    def metadata(self):
        with self._lock:
            claims = {}
            for key, value in self._claims.items():
                label = f"{key[0]}:{key[1]}" if isinstance(key, tuple) else str(key)
                claims[label] = value
            return {"slots": self._total, "available": self._total - len(self._claims), "claims": claims}


class Broadcaster:
    def __init__(self, host, tcp_port, secret, slots=1, interval=DEFAULT_INTERVAL,
                 broadcast_port=37020, session_id="session", slot_pool=None):
        self.host, self.tcp_port, self.secret = host, tcp_port, secret
        self.interval, self.broadcast_port, self.session_id = interval, broadcast_port, session_id
        # Share the transport allocator so advertisements reflect reservations.
        self.slot_pool = slot_pool or SlotPool(slots)
        self.advertised_nonce = None
        self._stop = threading.Event()

    def advertisement(self):
        return {"type": "trainer", "version": PROTOCOL_VERSION, "session_id": self.session_id,
                "host": self.host, "tcp_port": self.tcp_port, "nonce": self.advertised_nonce,
                "metadata": self.slot_pool.metadata()}

    def run(self):
        if self.advertised_nonce is None: self.advertised_nonce = secrets.token_hex(16)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            payload = lambda: json.dumps(self.advertisement(), separators=(",", ":")).encode()
            while not self._stop.is_set():
                sock.sendto(payload(), ("255.255.255.255", self.broadcast_port))
                self._stop.wait(self.interval)
        finally: sock.close()

    def stop(self): self._stop.set()
