#!/usr/bin/env python3
"""Offline diagnostic of v2 runs, HMR1 captures, and leftover trees. No secrets."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
HMR = struct.Struct("!4sIB3xI")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: Path):
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summarize_runs():
    runs = sorted((ROOT / "logs").glob("run_*"), key=lambda p: p.stat().st_mtime)
    print("=== V2 RUNS ===")
    for run in runs:
        events = load_jsonl(run / "events.jsonl")
        counts = collections.Counter(str(r.get("event") or "") for r in events)
        op = (run / "operator.txt").read_text(encoding="utf-8", errors="replace") if (run / "operator.txt").is_file() else ""
        hands = sum(1 for line in op.splitlines() if "раздача завершена" in line)
        ready = sum(1 for line in op.splitlines() if "PokerEYE готов" in line)
        wait = sum(1 for line in op.splitlines() if "слотов" in line or "недоступен" in line)
        fail = sum(1 for line in op.splitlines() if "не поднялся" in line or "ошибка" in line.lower())
        pcaps = list((run / "captures").rglob("*.pcap")) if (run / "captures").is_dir() else []
        pcap_bytes = sum(p.stat().st_size for p in pcaps)
        print(
            f"{run.name} events={len(events):5d} hands_op={hands} eye_ready={ready} "
            f"slot_issues={wait} failish={fail} pcaps={len(pcaps)} pcap_bytes={pcap_bytes} "
            f"top={counts.most_common(6)}"
        )
        interesting = [
            k for k in counts
            if any(x in k for x in (
                "account_", "table_start", "error", "quarantine", "invalid",
                "waiting", "fail", "cc", "action", "eye"
            ))
        ]
        if interesting:
            print("   " + str({k: counts[k] for k in sorted(interesting)}))


def read_hmr1(path: Path, limit_payloads: int = 40):
    data = path.read_bytes()
    if len(data) < 24:
        return {"path": str(path), "error": "too small"}
    magic, major, minor, _zone, _sig, snaplen, linktype = struct.unpack("<IHHiIII", data[:24])
    out = {
        "path": str(path.relative_to(ROOT) if str(path).startswith(str(ROOT)) else path),
        "bytes": len(data),
        "magic": hex(magic),
        "linktype": linktype,
        "snaplen": snaplen,
        "records": 0,
        "hmr1": 0,
        "in": 0,
        "out": 0,
        "ws": collections.Counter(),
        "first_bytes": collections.Counter(),
        "sfs_80": 0,
        "jsonish": 0,
        "payload_bytes": 0,
        "commands": collections.Counter(),
    }
    offset = 24
    sys.path.insert(0, str(ROOT))
    from core.verified_v1.coin_bridge_live import cmd_room_data, decode_hook_payload

    while offset + 16 <= len(data):
        _sec, _usec, cap, _orig = struct.unpack("<IIII", data[offset:offset + 16])
        offset += 16
        if cap < 0 or offset + cap > len(data):
            break
        rec = data[offset:offset + cap]
        offset += cap
        out["records"] += 1
        if len(rec) < 16 or rec[:4] != b"HMR1":
            continue
        magic_b, ws, direction, plen = HMR.unpack(rec[:16])
        payload = rec[16:16 + plen]
        out["hmr1"] += 1
        out["in" if direction == 0 else "out"] += 1
        out["ws"][f"{ws:08x}"] += 1
        out["payload_bytes"] += len(payload)
        if payload:
            out["first_bytes"][f"0x{payload[0]:02x}"] += 1
            if payload[0] in (0x80, 0x88, 0x60, 0x70):
                out["sfs_80"] += 1
            if payload[:1] in (b"{", b"[") or payload[:1].isalpha():
                out["jsonish"] += 1
        event = {
            "_raw": payload,
            "direction": "in" if direction == 0 else "out",
            "ws_id": f"{ws:08x}",
            "text": False,
        }
        try:
            _payload, _raw = decode_hook_payload(event)
            command, _room, _data = cmd_room_data(_payload)
            if command:
                out["commands"][command] += 1
        except Exception:
            pass
    out["ws"] = dict(out["ws"].most_common(8))
    out["first_bytes"] = dict(out["first_bytes"].most_common(8))
    out["commands"] = dict(out["commands"].most_common(20))
    return out


def leftover_diff():
    pairs = [
        ("core/verified_v1/coin_bridge_live.py", "ready_v6/coin_bridge_live.py"),
        ("core/verified_v1/coin_action_wire.py", "ready_v6/coin_action_wire.py"),
        ("core/verified_v1/coin_autoplay.py", "ready_v6/coin_autoplay.py"),
        ("core/verified_v1/coin_ppp_bridge.py", "ready_v6/coin_ppp_bridge.py"),
        ("core/verified_v1/eye_direct_proxy.py", "ready_v6/eye_direct_proxy.py"),
        ("core/verified_v1/eye_backend_probe.py", "ready_v6/eye_backend_probe.py"),
        ("core/verified_v1/bombpot_support.py", "ready_v6/bombpot_support.py"),
        ("core/v6router/router.py", "ready_v6/supervisor/runtime/router.py"),
        ("core/v6router/accounts.py", "ready_v6/supervisor/runtime/accounts.py"),
        ("vps/pokereye-web.py", "ready_v6/supervisor/web_admin.py"),
    ]
    print("=== FILE DIFF v2 vs leftover ===")
    for left, right in pairs:
        a = ROOT / left if not left.startswith("C:") else Path(left)
        if not a.is_file():
            a = ROOT / left
        b = REPO / right
        if not a.is_file() or not b.is_file():
            print(f"MISSING {left if a.is_file() else a} vs {b}")
            continue
        sa, sb = sha256(a), sha256(b)
        same = sa == sb
        print(f"{'SAME' if same else 'DIFF'} {left} ({a.stat().st_size}) vs {right} ({b.stat().st_size})")


def pcap_magic_scan():
    print("=== ROOT PCAP MAGIC ===")
    files = list(REPO.glob("*.pcap")) + list(REPO.glob("*.pcapng"))
    for path in sorted(files, key=lambda p: p.stat().st_size, reverse=True):
        data = path.read_bytes()[:24]
        if len(data) < 24:
            print(path.name, "too small")
            continue
        if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"M<\xb2\xa1", b"\xa1\xb2<M"):
            endian = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1") else ">"
            linktype = struct.unpack(endian + "IHHIIII", data)[-1]
            kind = "pcap"
        elif data[:4] == b"\n\r\r\n":
            kind, linktype = "pcapng", -1
        else:
            kind, linktype = f"unknown:{data[:4]!r}", -1
        print(f"{path.name:48} {path.stat().st_size:10d} {kind} linktype={linktype}")


def main():
    leftover_diff()
    print()
    summarize_runs()
    print()
    pcap_magic_scan()
    print()
    print("=== HMR1 V2 CAPTURES ===")
    for path in sorted((ROOT / "logs").rglob("coin_00.pcap")):
        info = read_hmr1(path)
        print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
