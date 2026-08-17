"""Minimal PPP protobuf frame helpers for the PokerEYE channel.

Ported from the verified legacy helpers (``coin_ppp_bridge.py``): varint field
encoding and the hint frames the trainer must synthesize for the EYE backend.
Stdlib-only. No payloads go to normal logs.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative varint")
    out = bytearray()
    n = value
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def p_int(field_no: int, value: int, *, emit_zero: bool = True) -> bytes:
    if value == 0 and not emit_zero:
        return b""
    return _varint((field_no << 3) | 0) + _varint(int(value))


def p_bytes(field_no: int, value: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(value)) + value


def p_str(field_no: int, value: str) -> bytes:
    return p_bytes(field_no, value.encode("utf-8"))


def p_msg(field_no: int, value: bytes) -> bytes:
    return p_bytes(field_no, value)


def money(value: Any, scale: int = 100) -> int:
    if value is None:
        return 0
    return int(round(float(value) * scale))


# --- EYE envelope ---------------------------------------------------------
def eye_envelope(cmd: str, body: bytes, *, package: str = "com.lein.pppoker.android",
                 uid: str = "0", location: str = "TABLE", tag: str = "traffic") -> dict:
    """One frame of the hook channel: 4-byte BE length + this JSON object."""
    msg = {
        "timestamp": int(time.time() * 1000),
        "pid": uid,
        "uid": uid,
        "cmd": cmd,
        "direction": "ServerToClient",
        "data": base64.b64encode(body).decode(),
        "location": location,
        "seq": 0,
    }
    return {"data": "", "msg": json.dumps(msg, separators=(",", ":")),
            "packageName": package, "tag": tag}


# --- hint frames ----------------------------------------------------------
def action_notify_brc(seat: int, call: int, min_raise: int, max_raise: int, can: int) -> bytes:
    """pb.ActionNotifyBRC: emitted before every actor (seat, call, min, max, can)."""
    return (p_int(1, seat) + p_int(2, call) + p_int(3, min_raise)
            + p_int(4, max_raise) + p_int(5, can))


def round_hint_multiple_table(
    *,
    room_type: int = 5,
    small_blind: float = 0.02,
    ante: float = 0.0,
    table_id: int = 0,
    club_id: int = 3663333,
    turn_time: int = 15,
    room_name: str = "Table",
    club_name: Optional[str] = None,
    max_size: int = 6,
    room_mode: int = 3,
    game_mode: int = 5,
    ppsrid: Optional[int] = None,
    scale: int = 100,
) -> bytes:
    """pb.RoundHintMultipleTableRSP body the trainer sends to the EYE backend."""
    parts = [
        p_int(1, room_type),
        p_int(2, money(small_blind, scale)),
        p_int(3, money(ante, scale)),
        p_int(4, table_id),
        p_int(5, club_id),
        p_int(7, turn_time),
        p_str(8, room_name),
    ]
    if club_name:
        parts.append(p_str(9, club_name))
    parts += [
        p_int(10, max_size),
        p_int(11, room_mode),
        p_int(12, room_type),
        p_int(13, game_mode),
    ]
    if ppsrid is not None:
        parts.append(p_int(14, ppsrid))
    parts.append(p_int(15, 0))
    return b"".join(parts)


def finish_round_hint(table_id: int) -> bytes:
    """pb.FinishRoundHintRSP: idempotent hint finish (f1 = table id)."""
    return p_int(1, table_id)
