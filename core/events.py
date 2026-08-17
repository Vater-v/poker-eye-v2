"""Normalized Coin/EYE event model.

Every observed frame becomes a normalized event carrying only metadata (direction,
type, length, sequence/correlation IDs, SHA-256 of the raw body) plus an allowlist
of decoded fields. Raw payloads are never part of a normal event.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


class FrameFamily(str, Enum):
    COIN = "coin"
    EYE = "eye"
    TRAINER = "trainer"


# Allowlisted decoded fields for each frame family. Anything outside this list is
# reduced to its metadata and never logged in normal mode.
COIN_ALLOWLIST = {"cmd", "room_id", "user_action", "bet_amount", "whose_turn", "turn_id"}
EYE_ALLOWLIST = {"cmd", "tag", "seat", "call", "min", "max", "can", "action", "type", "amount", "subtype"}


def _pick(data: Dict[str, Any], allowlist: set[str]) -> Dict[str, Any]:
    return {k: data[k] for k in allowlist if k in data}


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class FrameEvent:
    family: FrameFamily
    direction: Direction
    frame_type: str
    length: int
    body_hash: str
    seq: Optional[int] = None
    correlation_id: Optional[str] = None
    ts: float = field(default_factory=time.time)
    decoded: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "family": self.family.value,
            "direction": self.direction.value,
            "type": self.frame_type,
            "length": self.length,
            "sha256": self.body_hash,
            "seq": self.seq,
            "correlation_id": self.correlation_id,
            "decoded": self.decoded,
            "source": self.source,
        }


def coin_frame(
    direction: Direction,
    raw: bytes,
    *,
    decoded: Optional[Dict[str, Any]] = None,
    seq: Optional[int] = None,
    correlation_id: Optional[str] = None,
    source: Optional[str] = None,
) -> FrameEvent:
    dec = _pick(decoded or {}, COIN_ALLOWLIST)
    return FrameEvent(
        family=FrameFamily.COIN,
        direction=direction,
        frame_type=str(dec.get("cmd") or "coin.frame"),
        length=len(raw),
        body_hash=body_sha256(raw),
        seq=seq,
        correlation_id=correlation_id,
        decoded=dec,
        source=source,
    )


def eye_frame(
    direction: Direction,
    raw: bytes,
    *,
    decoded: Optional[Dict[str, Any]] = None,
    seq: Optional[int] = None,
    correlation_id: Optional[str] = None,
    source: Optional[str] = None,
) -> FrameEvent:
    dec = _pick(decoded or {}, EYE_ALLOWLIST)
    return FrameEvent(
        family=FrameFamily.EYE,
        direction=direction,
        frame_type=str(dec.get("cmd") or dec.get("tag") or "eye.frame"),
        length=len(raw),
        body_hash=body_sha256(raw),
        seq=seq,
        correlation_id=correlation_id,
        decoded=dec,
        source=source,
    )


# --- EYE hook-channel frame helpers (4-byte BE length + JSON) -------------
def parse_eye_channel_frame(raw: bytes) -> Dict[str, Any]:
    """Decode one EYE channel frame into its JSON dict (tag/data/msg/...)."""
    return json.loads(raw.decode("utf-8"))


def parse_cc_data(data: Any) -> Dict[str, Any]:
    """Extract the SCAction dict from a ``tag:"cc"`` frame's data field."""
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            value = json.loads(data)
        except json.JSONDecodeError:
            return {}
        if isinstance(value, dict):
            return value
    return {}
