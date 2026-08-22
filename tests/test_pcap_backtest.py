"""Replay live HMR1 Coin pcaps through shipped decoder + bridge/router."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

from core.coin_capture import RawCoinCaptureManager, iter_hmr1_pcap
from core.production_runtime import OperatorConsole, RouterService
from core.verified_v1.coin_action_wire import _Byte, _Int, _Obj, _Short, _Str, encode_packet
from core.verified_v1.coin_bridge_live import LiveCoinBridge, cmd_room_data, decode_hook_payload
from core.v6router.history_ledger import FakeSheetsTransport, HistoryLedger
from core.v6router.router import DeviceIngressRouter, LiveTableSession, RouterObservation


FIXTURE = Path(__file__).parent / "fixtures" / "sit_vanish_v7459_coin_00.pcap"
SCRATCH = Path(os.environ.get(
    "POKEREYE_SCRATCH",
    r"C:\Temp\grok-goal-a516937c1f3e\implementer",
))
PCAP_LIST = Path(os.environ.get(
    "POKEREYE_PCAP_LIST",
    str(SCRATCH / "pcap-list.txt"),
))
PCAP_DIR = Path(os.environ.get(
    "POKEREYE_PCAP_DIR",
    str(SCRATCH / "pcaps" / "pcaps"),
))
ACTION_TAGS = {"fallback_ready", "action_ready", "prefold_ready"}


def _coin_raw(command: str, data: dict, *, room: int | None = 1) -> bytes:
    inner = {"c": _Str(command), "p": _Obj({"data": _Str(json.dumps(data, separators=(",", ":")))})}
    if room is not None:
        inner["r"] = _Int(int(room))
    return encode_packet({"c": _Byte(1), "a": _Short(13), "p": _Obj(inner)})


class _Log:
    def emit(self, *args, **kwargs):
        return {}


class _DummyProxy:
    async def close(self, *args, **kwargs):
        return None

    def bind_bridge(self, *args, **kwargs):
        return None


def _arm_backtest_eye(bridge: LiveCoinBridge) -> None:
    """Local replay must not open Eye TCP. Failsafe still runs on the shipped path."""

    async def _ok(*_a, **_k):
        bridge.login_sent = True
        bridge.eye_ready.set()
        return True

    bridge.ensure_eye = _ok  # type: ignore[method-assign]
    bridge.eye_send_outer = _ok  # type: ignore[method-assign]
    bridge.eye_send_cmd = _ok  # type: ignore[method-assign]
    bridge._eye_send_outer_generation = _ok  # type: ignore[method-assign]
    bridge._eye_send_cmd_generation = _ok  # type: ignore[method-assign]
    # Observer/login stay on the shipped path; only the Eye TCP socket is stubbed.
    bridge.frame_delay = 0
    bridge.eye_connect_timeout = 0.05
    bridge.cc_timeout_seconds = 0.05
    bridge.cc_fallback_margin_seconds = 0.0
    bridge.mid_hand_recovery_grace = 0.0
    bridge.mid_hand_recovery_attempts = 1


class _LocalSessionFactory:
    def __init__(self, sink, tags: list[str] | None = None):
        self.sink = sink
        self.tags = tags if tags is not None else []
        self.sessions: dict[int, LiveTableSession] = {}

    async def create(self, device_id, table_id, seed):
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: self.tags.append(str(t)))
        _arm_backtest_eye(bridge)
        lease = SimpleNamespace(
            account_id="test-acct",
            owner=f"device/{device_id}/table/{table_id}",
            token="tok",
        )
        session = LiveTableSession(
            device_id=str(device_id),
            table_id=int(table_id),
            lease=lease,
            bridge=bridge,
            proxy=_DummyProxy(),
            accounts=SimpleNamespace(release=lambda *a, **k: None),
            observation_sink=self.sink,
            crash_quarantine_seconds=1.0,
        )
        self.sessions[int(table_id)] = session
        return session


async def _drain_bridge(bridge: LiveCoinBridge, timeout: float = 60.0) -> None:
    abort = getattr(bridge, "abort_cc_wait", None)
    queue = getattr(bridge, "protocol_queue", None)
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        waiting = bool(getattr(bridge, "awaiting_cc", False))
        if callable(abort) and waiting:
            abort("BACKTEST_DRAIN")
        empty = queue is None or queue.empty()
        if empty and not waiting:
            await asyncio.sleep(0.03)
            waiting = bool(getattr(bridge, "awaiting_cc", False))
            empty = queue is None or queue.empty()
            if empty and not waiting:
                break
        if empty:
            await asyncio.sleep(0.02)
            continue
        with __import__("contextlib").suppress(asyncio.TimeoutError):
            await asyncio.wait_for(queue.join(), 0.3)
    worker = getattr(bridge, "protocol_task", None)
    if worker is not None and not worker.done():
        worker.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(worker, 0.5)
    await asyncio.sleep(0)


def _device_id_from_path(path: Path) -> str:
    name = path.name
    if "_dev-" in name:
        return "dev-" + name.split("_dev-", 1)[1].split("_coin")[0]
    return "backtest"


async def async_replay_bridge_pcap(
    path: Path,
    *,
    hero: str | None = None,
    progress=None,
    use_router: bool = False,
) -> dict:
    """Drive shipped LiveCoinBridge.handle_event for every HMR1 record."""
    events = list(iter_hmr1_pcap(path))
    tags: list[str] = []
    problems: list[str] = []
    hero_turns = 0
    total = len(events)
    step = max(1, total // 20)
    bridges: list[LiveCoinBridge] = []

    table_count = 1
    if use_router:
        factory = _LocalSessionFactory(lambda _obs: None, tags)
        router = DeviceIngressRouter(
            _device_id_from_path(path),
            factory,
            observation_sink=lambda _obs: None,
        )

        async def _feed(event: dict) -> None:
            await router.handle_event(event)

        def _hero_match(data: dict) -> bool:
            for session in factory.sessions.values():
                if session._bridge._is_hero_turn(data):
                    return True
            return False

        def _hero_name() -> str:
            for session in factory.sessions.values():
                name = str(
                    session._bridge.identity.get("user_name")
                    or session._bridge.state.get("user_name")
                    or ""
                )
                if name:
                    return name
            return ""

        async def _drain_all() -> None:
            nonlocal table_count
            table_count = len(factory.sessions)
            for session in factory.sessions.values():
                await _drain_bridge(session._bridge, timeout=120.0)
    else:
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        if hero:
            bridge.state["user_name"] = hero
            bridge.identity["user_name"] = hero
        _arm_backtest_eye(bridge)
        bridges.append(bridge)

        async def _feed(event: dict) -> None:
            await bridge.handle_event(event)

        def _hero_match(data: dict) -> bool:
            return bridge._is_hero_turn(data)

        def _hero_name() -> str:
            return str(bridge.identity.get("user_name") or bridge.state.get("user_name") or "")

        async def _drain_all() -> None:
            await _drain_bridge(bridge, timeout=120.0)

    for index, event in enumerate(events, 1):
        payload, _raw = decode_hook_payload(event)
        cmd, _room, data = cmd_room_data(payload)
        try:
            await _feed(event)
        except Exception as exc:
            problems.append(f"{type(exc).__name__}: {exc}")
            continue
        if (
            str(event.get("direction") or "") == "in"
            and cmd == "game.user_turn"
            and isinstance(data, dict)
            and _hero_match(data)
            and not event.get("_hmuriy_duplicate_turn")
        ):
            hero_turns += 1
        if progress is not None and (index == total or index % step == 0):
            progress(
                index,
                total,
                sum(1 for tag in tags if tag == "hero_turn"),
                sum(1 for tag in tags if tag in ACTION_TAGS),
            )
    await _drain_all()
    queued = sum(1 for tag in tags if tag in ACTION_TAGS)
    unique_hero_turns = sum(1 for tag in tags if tag == "hero_turn")
    if unique_hero_turns and queued == 0:
        problems.append(
            f"{unique_hero_turns} worker hero turns, 0 CHECK/FOLD/CC "
            f"(packets={hero_turns}) tags={tags[-16:]}"
        )
    elif unique_hero_turns > queued:
        problems.append(
            f"{unique_hero_turns} worker hero turns but only {queued} "
            f"CHECK/FOLD/CC (packets={hero_turns})"
        )
    return {
        "path": str(path),
        "events": total,
        "hero_turns": hero_turns,
        "unique_hero_turns": unique_hero_turns,
        "queued": queued,
        "problems": problems,
        "tags": tags[-32:],
        "hero": _hero_name(),
        "tables": table_count,
    }


def replay_bridge_pcap(
    path: Path, *, hero: str | None = None, progress=None, use_router: bool = False,
) -> dict:
    return asyncio.run(async_replay_bridge_pcap(
        path, hero=hero, progress=progress, use_router=use_router,
    ))


async def async_replay_router_pcap(
    path: Path, *, device_id: str = "dev-a8dc8554e1cf4bddb25db98b6970679a",
) -> dict:
    """Incident path: DeviceIngressRouter + OperatorConsole + ledger."""
    tags: list[str] = []
    seen: list[RouterObservation] = []
    console = OperatorConsole(_Log(), 1)
    ledger = HistoryLedger(FakeSheetsTransport())
    ledger.set_device_enabled(device_id, True)
    console.history_ledger = ledger

    def sink(obs: RouterObservation) -> None:
        seen.append(obs)
        console.observation(obs)

    factory = _LocalSessionFactory(sink, tags)
    router = DeviceIngressRouter(device_id, factory, observation_sink=sink)
    for event in iter_hmr1_pcap(path):
        await router.handle_event(event)
    for session in factory.sessions.values():
        await _drain_bridge(session._bridge)
    crash = [
        obs for obs in seen
        if "FrozenInstanceError" in str(obs.reason or "")
        or "table session crash" in str(obs.reason or "")
    ]
    snap = await router.control_snapshot()
    return {
        "crash": [obs.reason for obs in crash],
        "tables": list(snap.get("tables") or []),
        "sessions": list(factory.sessions),
        "tags": tags,
        "kinds": [obs.kind for obs in seen],
        "queued": sum(1 for tag in tags if tag in ACTION_TAGS),
    }


def replay_router_pcap(path: Path, *, device_id: str = "dev-a8dc8554e1cf4bddb25db98b6970679a") -> dict:
    return asyncio.run(async_replay_router_pcap(path, device_id=device_id))


def format_report(report: dict) -> str:
    problems = report.get("problems") or []
    status = "OK" if not problems else "PROBLEMS"
    lines = [
        f"FILE {report.get('path')}",
        f"status {status}",
        f"events {report.get('events')} hero_turns {report.get('hero_turns')} "
        f"unique {report.get('unique_hero_turns')} queued {report.get('queued')} "
        f"hero {report.get('hero') or '-'}",
    ]
    for problem in problems:
        lines.append(f"  - {problem}")
    return "\n".join(lines)


def listed_pcap_files() -> list[Path]:
    return sorted(PCAP_DIR.glob("*.pcap"))


def run_listed_backtest() -> list[dict]:
    return [replay_bridge_pcap(path) for path in listed_pcap_files()]


def write_backtest_report(path: Path, reports: list[dict]) -> str:
    problem_count = sum(1 for row in reports if row.get("problems"))
    chunks = [
        f"files {len(reports)}",
        f"files_with_problems {problem_count}",
        "",
    ]
    for report in reports:
        chunks.append(format_report(report))
        chunks.append("")
    text = "\n".join(chunks).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


class Hmr1DecoderTests(unittest.TestCase):
    def test_roundtrip_writer_to_iter(self):
        raw = _coin_raw("game.user_turn", {"whoseTurn": "Hero", "userTurnOptions": {"7": None}})
        tmp = tempfile.mkdtemp()
        mgr = RawCoinCaptureManager(tmp, segment_bytes=1_000_000, segments=2)
        mgr.observe("dev-test", {"_raw": raw, "_ws_u32": 99, "direction": "in"})
        mgr.close(timeout=2.0)
        path = next(Path(tmp).rglob("coin_*.pcap"))
        rows = list(iter_hmr1_pcap(path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_raw"], raw)
        self.assertEqual(rows[0]["direction"], "in")
        payload, _ = decode_hook_payload(rows[0])
        cmd, _room, data = cmd_room_data(payload)
        self.assertEqual(cmd, "game.user_turn")
        self.assertEqual(data.get("whoseTurn"), "Hero")


class SitVanishIncidentTests(unittest.IsolatedAsyncioTestCase):
    def test_live_hello_survives_empty_snapshot_and_web_merge(self):
        import importlib.util

        svc = RouterService.__new__(RouterService)
        svc.run_name = "run_35f26c0e13b1"
        svc._routers = {"dev-sony": object()}
        svc._connected = {"dev-sony"}
        svc._device_labels = {"dev-sony": "Sony"}
        svc._heartbeat_at = {"dev-sony": time.monotonic()}
        svc._last_seen = {}
        svc._last_snapshot = {
            "ok": True,
            "build": "V7.4.61-HMN1-VPS",
            "devices": [{
                "device_id": "dev-sony",
                "connected": True,
                "tables": [{"table_id": 1, "hero_sitting": True}],
            }],
            "connected_devices": 1,
            "run": "run_35f26c0e13b1",
        }
        empty = {"ok": True, "devices": [], "run": ""}
        kept = svc._remember_snapshot(empty)
        self.assertGreaterEqual(len(kept.get("devices") or []), 1)
        self.assertEqual(kept.get("run"), "run_35f26c0e13b1")
        self.assertNotEqual(int(kept.get("connected_devices") or 0), 0)
        path = Path(__file__).resolve().parents[1] / "vps" / "pokereye-web.py"
        spec = importlib.util.spec_from_file_location("pokereye_web_state", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        merged = mod.merge_live_state(
            {"ok": True, "devices": []},
            None,
            {"snapshot": kept, "run": kept.get("run"), "events": []},
            [],
        )
        self.assertEqual(merged.get("run"), "run_35f26c0e13b1")
        self.assertTrue((merged.get("snapshot") or {}).get("devices"))
        self.assertNotIn(merged.get("run"), ("", None))

    def test_hand_started_with_uchet_does_not_raise(self):
        from datetime import datetime, timezone
        ledger = HistoryLedger(
            FakeSheetsTransport(),
            now_factory=lambda: datetime.now(timezone.utc),
        )
        ledger.set_device_enabled("dev-a8dc8554e1cf4bddb25db98b6970679a", True)
        console = OperatorConsole(_Log(), 1)
        console.history_ledger = ledger
        console._device_nick["dev-a8dc8554e1cf4bddb25db98b6970679a"] = "Weedman834"
        obs = RouterObservation(
            kind="hand_started",
            device_id="dev-a8dc8554e1cf4bddb25db98b6970679a",
            table_id=1163125,
            game_type="NLH",
            coin_bb=0.02,
            hand_id="116312500141",
            detail={"nickname": "Weedman834", "stack": 1.98},
        )
        console.observation(obs)
        console.observation(obs)

    async def test_raising_ledger_sink_does_not_close_table(self):
        def boom(obs: RouterObservation) -> None:
            if obs.kind == "hand_started":
                raise FrozenInstanceError("cannot assign to field 'detail'")

        factory = _LocalSessionFactory(boom)
        session = await factory.create("dev-a8dc8554e1cf4bddb25db98b6970679a", 1163125, {})
        session._candidate_hand = "116312500141"
        session._bridge.state["user_name"] = "Weedman834"
        session._bridge.identity["user_name"] = "Weedman834"
        turn = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.user_turn",
                {
                    "whoseTurn": "Weedman834",
                    "userTurnOptions": {"3": None, "7": None},
                    "turnTime": 12,
                },
                room=60521,
            ),
            "id": "t",
            "ws_id": "table",
            "v": 6,
        }
        decision, _finish = await session.handle_event(turn)
        self.assertNotIn("FrozenInstanceError", str(decision))
        self.assertFalse(session._closed)

    async def test_incomplete_seed_failsafe_without_eye(self):
        """Eye has nothing to answer; CHECK/FOLD must still queue."""
        tags: list[str] = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        bridge.state["user_name"] = "Weedman834"
        bridge.identity["user_name"] = "Weedman834"
        bridge.cc_timeout_seconds = 0.05
        bridge.cc_fallback_margin_seconds = 0.0
        bridge.mid_hand_recovery_grace = 0.0
        bridge.eye_connect_timeout = 0.15
        bridge.frame_delay = 0
        turn = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.user_turn",
                {
                    "whoseTurn": "Weedman834",
                    "userTurnOptions": {"3": None, "7": None},
                    "turnTime": 12,
                },
                room=60521,
            ),
            "id": "t",
            "ws_id": "table",
            "v": 6,
        }
        await bridge.handle_event(turn)
        await _drain_bridge(bridge, timeout=2.0)
        pending = dict(bridge.autoplay.pending or {})
        self.assertTrue(pending or "fallback_ready" in tags, tags)
        if pending:
            self.assertIn(str(pending.get("action") or "").upper(), {"CHECK", "FOLD"})

    async def test_incident_pcap_does_not_crash_close_table(self):
        self.assertTrue(FIXTURE.is_file(), f"missing incident pcap {FIXTURE}")
        report = await async_replay_router_pcap(FIXTURE)
        self.assertFalse(report["crash"], report["crash"])
        self.assertTrue(report["tables"] or report["sessions"], report)

    async def test_incident_first_hero_turn_queues_action(self):
        self.assertTrue(FIXTURE.is_file())
        report = await async_replay_bridge_pcap(FIXTURE, hero="Weedman834", use_router=False)
        self.assertGreaterEqual(report["hero_turns"], 1, report)
        self.assertGreaterEqual(report["queued"], 1, report["problems"])
        self.assertFalse(report["problems"], report["problems"])

    async def test_cold_hands_second_turn_still_queues_failsafe(self):
        tags: list[str] = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        bridge.state["user_name"] = "Weedman834"
        bridge.identity["user_name"] = "Weedman834"
        _arm_backtest_eye(bridge)
        bridge.cold_hands.add(99)
        bridge.current_hand = None

        async def _seed(*_a, **_k):
            return None, 1, [(99, None)], "ready", 0

        bridge._wait_for_cold_seed = _seed  # type: ignore[method-assign]
        turn = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.user_turn",
                {
                    "whoseTurn": "Weedman834",
                    "userTurnOptions": {"7": None},
                    "turnTime": 12,
                    "initTimeStamp": "9",
                },
                room=60521,
            ),
            "id": "t",
            "ws_id": "table",
            "v": 6,
        }
        await bridge.handle_event(turn)
        await _drain_bridge(bridge, timeout=2.0)
        self.assertIn("fallback_ready", tags, tags)

    async def test_new_unique_turn_replaces_stale_failsafe(self):
        tags: list[str] = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        bridge.state["user_name"] = "Weedman834"
        bridge.identity["user_name"] = "Weedman834"
        _arm_backtest_eye(bridge)
        first = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.user_turn",
                {
                    "whoseTurn": "Weedman834",
                    "userTurnOptions": {"3": None, "7": None},
                    "turnTime": 12,
                    "callAmount": 0.02,
                    "totalPot": 0.03,
                    "initTimeStamp": "1",
                },
                room=60521,
            ),
            "id": "t1",
            "ws_id": "table",
            "v": 6,
        }
        second = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.user_turn",
                {
                    "whoseTurn": "Weedman834",
                    "userTurnOptions": {"3": None, "7": None},
                    "turnTime": 12,
                    "callAmount": 0.08,
                    "totalPot": 0.15,
                    "initTimeStamp": "2",
                },
                room=60521,
            ),
            "id": "t2",
            "ws_id": "table",
            "v": 6,
        }
        await bridge.handle_event(first)
        await _drain_bridge(bridge, timeout=2.0)
        await bridge.handle_event(second)
        await _drain_bridge(bridge, timeout=2.0)
        self.assertGreaterEqual(tags.count("fallback_ready"), 2, tags)

    async def test_inflight_second_unique_turn_still_queued(self):
        """Hook observe of turn 2 must not steal turn 1's failsafe identity."""
        tags: list[str] = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        bridge.state["user_name"] = "Weedman834"
        bridge.identity["user_name"] = "Weedman834"
        _arm_backtest_eye(bridge)

        def _turn(stamp: str, call: float, pot: float, eid: str) -> dict:
            return {
                "direction": "in",
                "_raw": _coin_raw(
                    "game.user_turn",
                    {
                        "whoseTurn": "Weedman834",
                        "userTurnOptions": {"3": None, "7": None},
                        "turnTime": 12,
                        "callAmount": call,
                        "totalPot": pot,
                        "initTimeStamp": stamp,
                    },
                    room=60521,
                ),
                "id": eid,
                "ws_id": "table",
                "v": 6,
            }

        await bridge.handle_event(_turn("1", 0.02, 0.03, "t1"))
        await bridge.handle_event(_turn("2", 0.08, 0.15, "t2"))
        await _drain_bridge(bridge, timeout=4.0)
        self.assertGreaterEqual(tags.count("hero_turn"), 2, tags)
        queued = sum(1 for tag in tags if tag in ACTION_TAGS)
        self.assertGreaterEqual(queued, 2, tags)
        self.assertGreaterEqual(queued, tags.count("hero_turn"), tags)

    async def test_dealer_cards_does_not_treat_next_check_as_extratimer(self):
        tags: list[str] = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        bridge.state["user_name"] = "Weedman834"
        bridge.identity["user_name"] = "Weedman834"
        _arm_backtest_eye(bridge)
        option = {
            "whoseTurn": "Weedman834",
            "userTurnOptions": {"3": None, "5": [0.02], "7": None},
            "turnTime": 12,
            "callAmount": 0,
            "totalPot": 0.03,
        }
        first = {
            "direction": "in",
            "_raw": _coin_raw("game.user_turn", option, room=60521),
            "id": "bb-option",
            "ws_id": "table",
            "v": 6,
        }
        flop = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.dealer_cards",
                {"cards": [1, 2, 3], "roundName": "FLOP"},
                room=60521,
            ),
            "id": "flop",
            "ws_id": "table",
            "v": 6,
        }
        second = {
            "direction": "in",
            "_raw": _coin_raw("game.user_turn", option, room=60521),
            "id": "flop-first",
            "ws_id": "table",
            "v": 6,
        }
        await bridge.handle_event(first)
        await _drain_bridge(bridge, timeout=2.0)
        await bridge.handle_event(flop)
        await bridge.handle_event(second)
        await _drain_bridge(bridge, timeout=2.0)
        self.assertGreaterEqual(tags.count("hero_turn"), 2, tags)
        self.assertGreaterEqual(sum(1 for tag in tags if tag in ACTION_TAGS), 2, tags)

    async def test_lobby_negative_room_does_not_drop_table_turns(self):
        tags: list[str] = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda t, m, d=None: tags.append(str(t)))
        bridge.state["user_name"] = "Weedman834"
        bridge.identity["user_name"] = "Weedman834"
        _arm_backtest_eye(bridge)
        join = {
            "direction": "out",
            "_raw": _coin_raw("lobby.join_game", {"buyInAmount": 2}, room=-1),
            "id": "j",
            "ws_id": "lobby",
            "v": 6,
        }
        turn = {
            "direction": "in",
            "_raw": _coin_raw(
                "game.user_turn",
                {
                    "whoseTurn": "Weedman834",
                    "userTurnOptions": {"3": None, "7": None},
                    "turnTime": 12,
                },
                room=60521,
            ),
            "id": "t",
            "ws_id": "table",
            "v": 6,
        }
        await bridge.handle_event(join)
        self.assertNotEqual(getattr(bridge, "active_hook_room", None), -1)
        await bridge.handle_event(turn)
        await _drain_bridge(bridge, timeout=2.0)
        self.assertTrue(
            bridge.autoplay.pending or "fallback_ready" in tags or "hero_turn" in tags,
            tags,
        )


class NewestPcapBacktestTests(unittest.TestCase):
    def test_listed_pcaps_exist_for_backtest(self):
        if not PCAP_DIR.is_dir():
            self.skipTest(f"pcap dir missing {PCAP_DIR}")
        files = listed_pcap_files()
        self.assertGreaterEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
