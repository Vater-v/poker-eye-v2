"""Versioned authenticated length-prefixed JSON protocol; metadata only in normal logs."""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import socket
import struct

MAX_FRAME = 64 * 1024
PROTOCOL_VERSION = 2


def nonce() -> str: return secrets.token_hex(16)
def payload_sha256(payload: bytes) -> str: return hashlib.sha256(payload).hexdigest()

def frame(message: dict) -> bytes:
    body = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_FRAME: raise ValueError("message too large")
    return struct.pack("!I", len(body)) + body

def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk: raise ConnectionError("peer disconnected")
        data.extend(chunk)
    return bytes(data)

def recv_frame(sock: socket.socket) -> dict:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if not 0 < size <= MAX_FRAME: raise ValueError("invalid frame size")
    raw = _read_exact(sock, size)
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ValueError("invalid JSON frame") from exc
    if not isinstance(value, dict): raise ValueError("frame must contain an object")
    return value

def send_frame(sock: socket.socket, message: dict) -> None: sock.sendall(frame(message))
def proof(secret: bytes, advertised_nonce: str, table_id: str, session_id: str) -> str:
    return hmac.new(secret, "|".join((advertised_nonce, table_id, session_id, str(PROTOCOL_VERSION))).encode(), hashlib.sha256).hexdigest()
def verify_hello(message: dict, secret: bytes, advertised_nonce: str, session_id: str) -> tuple[str, str]:
    if message.get("type") != "hello" or message.get("version") != PROTOCOL_VERSION: raise ValueError("expected compatible hello")
    table_id, device_id, supplied = message.get("table_id"), message.get("device_id"), message.get("proof")
    if not all(isinstance(v, str) and v for v in (table_id, device_id, supplied)): raise ValueError("malformed hello")
    if not hmac.compare_digest(supplied, proof(secret, advertised_nonce, table_id, session_id)): raise ValueError("authentication failed")
    return device_id, table_id
