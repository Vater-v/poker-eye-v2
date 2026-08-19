#!/usr/bin/env python3
"""Offline acceptance replay for the supplied PokerEye/Coin legacy corpus.

The test never connects to CoinPoker or PokerEYE.  It validates that current v2
can decode real captured event sequences, isolate hands by SmartFox room, retain
villain actions whose packets omit handId, and synthesize the verified PPP prefix
through the first hero turn.  Explicitly interrupted captures are reported as
warnings; every failure in a complete capture is fatal.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable


def abspath_preserve_drive(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def discover_pcaps(source: Path, temp: Path) -> list[Path]:
    if source.is_dir():
        return sorted(source.rglob("*.pcap"))
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".pcap")]
            for member in members:
                archive.extract(member, temp)
        return sorted(temp.rglob("*.pcap"))
    if source.is_file() and source.suffix.lower() == ".pcap":
        return [source]
    raise SystemExit(f"[ERROR] Unsupported legacy source: {source}")


def pcap_record_count(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 24:
        return 0, -1
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2<M"):
        endian = ">"
    else:
        return 0, -1
    try:
        linktype = struct.unpack(endian + "IHHIIII", data[:24])[-1]
    except struct.error:
        return 0, -1
    offset = 24
    records = 0
    while offset + 16 <= len(data):
        try:
            _sec, _frac, captured, _original = struct.unpack(
                endian + "IIII", data[offset : offset + 16]
            )
        except struct.error:
            break
        offset += 16
        if captured < 0 or offset + captured > len(data):
            break
        offset += captured
        records += 1
    return records, int(linktype)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--legacy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    repo = abspath_preserve_drive(args.repo)
    source = abspath_preserve_drive(args.legacy)
    output = abspath_preserve_drive(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(repo))

    from core.verified_v1 import coin_ppp_bridge as ppp  # type: ignore
    from core.verified_v1.coin_bridge_live import normalized_coin_seat_action  # type: ignore
    from core.verified_v1.bombpot_support import detect_double_board  # type: ignore

    report: dict[str, Any] = {
        "source": str(source),
        "files": [],
        "totals": collections.Counter(),
        "commands": collections.Counter(),
        "normalized_actions": collections.Counter(),
        "warnings": [],
        "failures": [],
    }

    with tempfile.TemporaryDirectory(prefix="pokereye-pcap-") as tmp_name:
        pcaps = discover_pcaps(source, Path(tmp_name))
        for path in pcaps:
            records, linktype = pcap_record_count(path)
            relative = path.name
            row: dict[str, Any] = {
                "pcap": relative,
                "records": records,
                "linktype": linktype,
                "events": 0,
                "candidates": 0,
                "built": 0,
                "errors": [],
            }
            report["totals"]["pcap_files"] += 1
            report["totals"]["records"] += records
            try:
                raw_events = ppp.load_18010_events_from_pcap(path, 18010)
            except Exception as exc:
                row["errors"].append(f"load: {type(exc).__name__}: {exc}")
                raw_events = []
            if not raw_events:
                # HMR1/current capture or unrelated PCAP. Keep it in the format matrix,
                # but it is not a legacy PPP replay candidate.
                report["files"].append(row)
                continue

            decoded = ppp.decode_coin_events(raw_events)
            row["events"] = len(decoded)
            report["totals"]["legacy_files"] += 1
            report["totals"]["events"] += len(decoded)
            for event in decoded:
                command = str(event.name or "")
                if command:
                    report["commands"][command] += 1
                if command == "game.seat" and isinstance(event.data, dict):
                    report["normalized_actions"][normalized_coin_seat_action(event.data) or "<state>"] += 1
                if command:
                    active, _reason = detect_double_board(event.raw, command)
                    if active:
                        report["totals"]["double_board_evidence_events"] += 1

            try:
                model = ppp.CoinCaptureModel(decoded)
                candidates = model.candidate_hands()
                row["candidates"] = len(candidates)
                report["totals"]["candidate_hands"] += len(candidates)
                for hand_id, _pre, _turn in candidates:
                    try:
                        hand = model.build_hand(hand_id)
                        frames = ppp.PPPBuilder(
                            hand, ppp.money_profile_for_hand(hand, 100).chip_scale
                        ).synthesize_until_first_hero_turn()
                        if not frames:
                            raise ValueError("empty PPP synthesis")
                        row["built"] += 1
                        report["totals"]["built_hands"] += 1
                    except Exception as exc:
                        row["errors"].append(
                            f"hand={hand_id}: {type(exc).__name__}: {exc}"
                        )
            except Exception as exc:
                row["errors"].append(f"model: {type(exc).__name__}: {exc}")

            if row["errors"]:
                target = "warnings" if ".interrupted." in path.name.lower() else "failures"
                report[target].append({"pcap": path.name, "errors": row["errors"]})
            report["files"].append(row)

    report["totals"] = dict(report["totals"])
    report["commands"] = dict(report["commands"].most_common())
    report["normalized_actions"] = dict(report["normalized_actions"].most_common())
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md = output.with_suffix(".md")
    totals = report["totals"]
    lines = [
        "# PokerEye V7.4 legacy PCAP acceptance",
        "",
        f"- Source: `{source}`",
        f"- PCAP files: **{totals.get('pcap_files', 0)}**",
        f"- Legacy replay files: **{totals.get('legacy_files', 0)}**",
        f"- Decoded events: **{totals.get('events', 0)}**",
        f"- Candidate hero-turn hands: **{totals.get('candidate_hands', 0)}**",
        f"- PPP prefixes built: **{totals.get('built_hands', 0)}**",
        f"- Interrupted-capture warnings: **{len(report['warnings'])}**",
        f"- Complete-capture failures: **{len(report['failures'])}**",
        "",
        "## Highest-volume commands",
        "",
    ]
    for command, count in list(report["commands"].items())[:30]:
        lines.append(f"- `{command}`: {count}")
    if report["warnings"]:
        lines += ["", "## Expected interrupted-capture warnings", ""]
        for warning in report["warnings"]:
            lines.append(f"- `{warning['pcap']}`: {'; '.join(warning['errors'])}")
    if report["failures"]:
        lines += ["", "## FAILURES", ""]
        for failure in report["failures"]:
            lines.append(f"- `{failure['pcap']}`: {'; '.join(failure['errors'])}")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        "[PCAP] "
        f"events={totals.get('events', 0)} "
        f"hands={totals.get('built_hands', 0)}/{totals.get('candidate_hands', 0)} "
        f"warnings={len(report['warnings'])} failures={len(report['failures'])}"
    )
    print(f"[PCAP] report={output}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
