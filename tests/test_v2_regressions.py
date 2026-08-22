from __future__ import annotations

import collections
import tempfile
import asyncio
import socket
import struct
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.production_runtime import (
    BUILD_ID,
    NATIVE_MAGIC,
    NATIVE_VERSION,
    NATIVE_WS_FRAME,
    NativeIngressServer,
    OperatorConsole,
    TrafficMeter,
    apk_mismatch_device_hud,
    direct_proof,
    recv_json_frame,
    send_json_frame,
    send_raw_frame,
)
from core.v6router.accounts import AccountPool, AccountPoolExhausted, AccountState
from core.v6router.router import (
    LiveTableSession,
    LiveTableSessionFactory,
    RoutedEvent,
    RouterObservation,
    RouterSeed,
)
from tests.test_async_hotfix import coin_event


class _Logger:
    def __init__(self) -> None:
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))

    def error(self, *args, **kwargs):
        self.rows.append(("error", kwargs))


class V2RegressionTests(unittest.TestCase):
    def test_account_pool_grows_monotonically_past_known_suffixes(self):
        pool = AccountPool.dynamic_registered(
            "base",
            known_suffixes=(9, 10),
            auto_expand_unbounded=True,
            max_probe_concurrency=2,
        )
        self.assertTrue(pool.auto_expand_unbounded)
        self.assertEqual(pool.acquire("a").account_id, "base-9")
        self.assertEqual(pool.acquire("b").account_id, "base-10")

        probe1 = pool.acquire("c")
        self.assertEqual(probe1.account_id, "base-11")
        self.assertEqual(pool.state_for("base-11"), AccountState.PROBING)
        pool.invalidate("c", probe1.token, retry_seconds=9999)

        probe2 = pool.acquire("d")
        self.assertEqual(probe2.account_id, "base-12")

        # A released verified configured ID always beats generated candidates.
        pool.release("a")
        self.assertEqual(pool.acquire("e").account_id, "base-9")

    def test_account_pool_reuses_gap_before_growing_high_water(self):
        pool = AccountPool.dynamic_registered(
            "base",
            known_suffixes=(9, 11),
            auto_expand_unbounded=True,
            max_probe_concurrency=2,
        )
        self.assertEqual(pool.acquire("a").account_id, "base-9")
        self.assertEqual(pool.acquire("b").account_id, "base-11")
        probe = pool.acquire("c")
        self.assertEqual(probe.account_id, "base-10")

    def test_account_registry_persists_generated_invalid_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "accounts.json"
            pool = AccountPool.dynamic_registered(
                "base",
                known_suffixes=(9,),
                auto_expand_unbounded=True,
                registry_path=registry,
            )
            pool.acquire("a")
            probe = pool.acquire("b")
            self.assertEqual(probe.account_id, "base-10")
            pool.invalidate("b", probe.token, retry_seconds=9999)

            reloaded = AccountPool.dynamic_registered(
                "base",
                known_suffixes=(9,),
                auto_expand_unbounded=True,
                registry_path=registry,
            )
            reloaded.acquire("a")
            self.assertEqual(reloaded.acquire("c").account_id, "base-11")

    def test_account_registry_preserves_validated_quarantine(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "accounts.json"
            pool = AccountPool.dynamic_registered(
                "base",
                known_suffixes=(9, 10),
                registry_path=registry,
            )
            lease = pool.acquire("a")
            pool.release(
                "a",
                lease.token,
                quarantine_seconds=900,
                reason="PokerEYE backend login rejected",
            )
            self.assertEqual(pool.state_for("base-9"), AccountState.QUARANTINED)

            reloaded = AccountPool.dynamic_registered(
                "base",
                known_suffixes=(9, 10),
                registry_path=registry,
            )
            self.assertEqual(reloaded.state_for("base-9"), AccountState.QUARANTINED)
            self.assertEqual(reloaded.acquire("b").account_id, "base-10")

    def test_operator_shows_table_from_observing_not_only_seat(self):
        logger = _Logger()
        console = OperatorConsole(logger, 1)
        device = "dev-obs"
        console.device_up(device)
        console.observation(RouterObservation(
            "table_pending", device, 1148030, status="yellow",
            reason="allocated; waiting for game_init",
            pending=True,
            detail={"provisional": True},
        ))
        messages = [str(fields.get("message") or "") for _event, fields in logger.rows]
        self.assertTrue(any("наблюдаем" in text for text in messages))

    def test_operator_counts_hands_by_game_type_per_device_session(self):
        logger = _Logger()
        console = OperatorConsole(logger, 7, accounts_dynamic=True)
        device = "dev-1"
        console.device_up(device)
        console.observation(RouterObservation("hand_completed", device, 100, game_type="NLH", hand_id="h1"))
        console.observation(RouterObservation("hand_completed", device, 100, game_type="PLO", hand_id="h2"))
        console.observation(RouterObservation("hand_completed", device, 101, game_type="PLO6", hand_id="h3"))
        self.assertEqual(
            console._session_hands_text(device),
            "NLH: 1 · PLO4: 1 · PLO5: 0 · PLO6: 1",
        )

        # A confirmed offline->online boundary is a new emulator session; a quick
        # transport reconnect never sets _device_down_announced and does not reset.
        console._device_online.discard(device)
        console._device_down_announced.add(device)
        console.device_up(device)
        self.assertEqual(
            console._session_hands_text(device),
            "NLH: 0 · PLO4: 0 · PLO5: 0 · PLO6: 0",
        )

    def test_native_send_ack_stops_action_retries(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        bridge.pending_action_ack = {
            "action": "CHECK", "retries": 0, "token": "hand:1", "seat": 0,
            "retry_at": 0.0, "hand_id": "h",
        }
        rows = []
        bridge._diagnostic = lambda tag, msg, detail=None: rows.append((tag, detail or {}))
        self.assertTrue(bridge.confirm_pending_action(source="coin-seat", action="CHECK"))
        self.assertIsNone(bridge.pending_action_ack)
        self.assertTrue(rows)
        self.assertTrue(rows[-1][1].get("hud", {}).get("clear"))

    def test_dead_red_backend_requests_close_after_120s(self):
        session = object.__new__(LiveTableSession)
        session._backend_red_since = time.monotonic() - 121
        session._snapshot = lambda: SimpleNamespace(
            backend_health="red",
            backend_message="NO_TRAFFIC_FROM_ROOM",
            phase="offline",
        )
        request = session.take_dead_table_request()
        self.assertIsNotNone(request)
        self.assertIn("120s", request["reason"])
        session._backend_red_since = 0.0
        session._snapshot = lambda: SimpleNamespace(
            backend_health="green", backend_message="", phase="table",
        )
        self.assertIsNone(session.take_dead_table_request())

    def test_low_stack_guard_requests_exit_below_79bb_only(self):
        session = object.__new__(LiveTableSession)
        session.device_id = "dev"
        session.table_id = 77
        session.account_id = "base-9"
        session._candidate_hand = "123"
        session._stack_guard = {}
        session._low_stack_exit_request = None
        session._bridge = SimpleNamespace(
            identity={"user_id": 42, "user_name": "hero"},
            state={"user_id": 42, "user_name": "hero"},
            active_money_profile=None,
        )
        observed = []
        session._sink = observed.append
        session._snapshot = lambda: SimpleNamespace(game_type="NLH")

        session._arm_stack_guard("123", {"bbAmount": 2.0})
        routed = RoutedEvent(
            event={"id": "x"}, payload=None, raw=b"", command="game.seatInfo",
            direction="in", room_id=9, table_ids=(77,), websocket_id="01020304",
            data={"seatResponseDataList": [{"userId": 42, "userName": "hero", "userChips": 157.0}]},
        )
        session._observe_stack_guard(routed)
        request = session.take_low_stack_exit_request()
        self.assertIsNotNone(request)
        self.assertAlmostEqual(request["stack_bb"], 78.5)
        self.assertEqual(observed[-1].kind, "low_stack_exit")

        session._arm_stack_guard("124", {"bbAmount": 2.0})
        session._observe_stack_guard(
            RoutedEvent(
                event={"id": "y"}, payload=None, raw=b"", command="game.seatInfo",
                direction="in", room_id=9, table_ids=(77,), websocket_id="01020304",
                data={"seatResponseDataList": [{"userId": 42, "userChips": 158.0}]},
            )
        )
        self.assertIsNone(session.take_low_stack_exit_request())

    def test_low_stack_guard_ignores_zero_chips_until_real_stack(self):
        session = object.__new__(LiveTableSession)
        session.device_id = "dev"
        session.table_id = 77
        session.account_id = "base-9"
        session._candidate_hand = "123"
        session._stack_guard = {}
        session._low_stack_exit_request = None
        session._stack_guard_saw_positive = False
        session._bridge = SimpleNamespace(
            identity={"user_id": 42, "user_name": "hero"},
            state={"user_id": 42, "user_name": "hero"},
            active_money_profile=None,
        )
        session._sink = lambda _row: None
        session._snapshot = lambda: SimpleNamespace(game_type="NLH")
        session._arm_stack_guard("123", {"bbAmount": 0.02})
        session._observe_stack_guard(
            RoutedEvent(
                event={"id": "zero"}, payload=None, raw=b"", command="game.seatInfo",
                direction="in", room_id=9, table_ids=(77,), websocket_id="01020304",
                data={"seatResponseDataList": [{"userId": 42, "userName": "hero", "userChips": 0}]},
            )
        )
        self.assertIsNone(session.take_low_stack_exit_request())

    def test_low_stack_guard_tolerates_malformed_seat_user_id(self):
        session = object.__new__(LiveTableSession)
        session.device_id = "dev"
        session.table_id = 77
        session.account_id = "base-9"
        session._candidate_hand = "125"
        session._stack_guard = {}
        session._low_stack_exit_request = None
        session._bridge = SimpleNamespace(
            identity={"user_id": 42, "user_name": "hero"},
            state={"user_id": 42, "user_name": "hero"},
            active_money_profile=None,
        )
        session._sink = lambda _row: None
        session._snapshot = lambda: SimpleNamespace(game_type="NLH")
        session._arm_stack_guard("125", {"bbAmount": 2.0})
        session._observe_stack_guard(
            RoutedEvent(
                event={"id": "bad-id"}, payload=None, raw=b"", command="game.seatInfo",
                direction="in", room_id=9, table_ids=(77,), websocket_id="01020304",
                data={
                    "seatResponseDataList": [
                        {"userId": "not-an-int", "userName": "other", "userChips": 200},
                        {"userId": 42, "userName": "hero", "userChips": 157},
                    ]
                },
            )
        )
        self.assertIsNotNone(session.take_low_stack_exit_request())

    def test_native_uses_single_vps_ipv4_route_without_lan_or_adb_bypass(self):
        repo = Path(__file__).parents[1]
        bridge = (repo / "android" / "HmuriyBridge.java").read_text(encoding="utf-8")
        source = (repo / "android" / "native" / "HmuriyNative.cpp").read_text(encoding="utf-8")
        build = (repo / "android" / "build_v2_native.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('private static final String TRAINER_HOST = "5.42.124.216";', bridge)
        self.assertIn('private static final String TRAINER_FALLBACK = "84.32.231.194";', bridge)
        self.assertNotIn('compact.equals("menu")', bridge)
        self.assertIn("nativeUiDump", bridge)
        self.assertIn("kHeartbeatNs = 1ull * 1000ull * 1000ull * 1000ull", source)
        self.assertIn("kMsgUiDump", source)
        self.assertNotIn("LAN_HOSTS", bridge)
        self.assertNotIn("nonVpnNetworkHandle", bridge)
        self.assertNotIn("android_setsocknetwork", source)
        self.assertNotIn("try_lan_upgrade", source)
        self.assertNotIn("10.0.2.2", build)
        self.assertNotIn("10.0.3.2", build)
        self.assertIn("try_trainer_uplink(g_trainer_host, kConnectMs, kHandshakeMs)", source)
        self.assertIn("failover via bridge", source)
        self.assertIn("kHandshakeMs = 3000", source)
        self.assertIn("socket(AF_INET", source)


    def test_operator_uses_hero_nick_and_human_table_number(self):
        logger = _Logger()
        console = OperatorConsole(logger, 7, accounts_dynamic=True)
        device = "dev-nick"
        console.device_up(device)
        console.observation(
            RouterObservation(
                "table_close", device, 991, reason="left",
                detail={"hero_name": "HeroNick", "crashed": False},
            )
        )
        rows = [fields.get("message", "") for event, fields in logger.rows if event == "operator.table_close"]
        self.assertTrue(rows)
        self.assertIn("Стол 1: вышли со стола", rows[-1])

    def test_operator_reuses_human_table_numbers_after_close(self):
        logger = _Logger()
        console = OperatorConsole(logger, 7, accounts_dynamic=True)
        device = "dev-recycle"
        console.device_up(device)
        console.observation(RouterObservation("hand_completed", device, 10, game_type="NLH", hand_id="a"))
        console.observation(RouterObservation("table_close", device, 10, reason="left", detail={"crashed": False}))
        console.observation(RouterObservation("hand_completed", device, 11, game_type="NLH", hand_id="b"))
        self.assertEqual(console._table(device, 11), 1)

    def test_ingress_meter_never_decodes_coin_payload_inline(self):
        emitted = []
        meter = TrafficMeter(lambda **row: emitted.append(row), window_seconds=0.25)
        event = {
            "_raw": b"\x80\x00\x01\x12",
            "direction": "out",
            "ws_id": "01020304",
        }
        with patch(
            "core.production_runtime._decode_event",
            side_effect=AssertionError("ingress diagnostics must not decode"),
        ):
            meter.observe("dev", event)
            # Force the next observation across the meter window without sleeping.
            meter._rows["dev"]["last_print"] -= 1.0
            meter.observe("dev", event)

        self.assertEqual(len(emitted), 1)
        row = emitted[0]
        self.assertEqual(row["frames"], 2)
        self.assertEqual(row["outgoing"], 2)
        self.assertEqual(row["incoming"], 0)
        self.assertEqual(row["last_first"], 0x80)


    def test_native_ingress_reader_does_not_wait_for_slow_router(self):
        emitted = []
        meter = TrafficMeter(lambda **row: emitted.append(row), window_seconds=0.25)
        secret = b"secret"

        class SlowRouter:
            def __init__(self):
                self.started = threading.Event()

            def transport_up(self, _device_id):
                return None

            def transport_down(self, _device_id):
                return None

            def handle(self, _device_id, event):
                self.started.set()
                time.sleep(1.0)
                return {
                    "id": event.get("id"),
                    "ws_id": event.get("ws_id"),
                    "_ws_u32": event.get("_ws_u32"),
                    "action": "forward",
                }

            def action_result(self, _device_id, _message):
                return True

        router = SlowRouter()
        server = NativeIngressServer(
            secret,
            router,
            meter,
            capture=None,
            operator=None,
            host="127.0.0.1",
            port=0,
        )
        port = server.start()

        def ws_frame(seq: int, payload: bytes) -> bytes:
            return (
                NATIVE_MAGIC
                + bytes([NATIVE_WS_FRAME, NATIVE_VERSION])
                + struct.pack("!H", 0)
                + struct.pack("!Q", seq)
                + struct.pack("!I", 0x01020304)
                + bytes([1, 0, 0, 0])
                + struct.pack("!I", len(payload))
                + payload
            )

        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        try:
            device = "dev-test"
            transport = f"{device}-native-test"
            send_json_frame(
                sock,
                {
                    "type": "direct_hello",
                    "version": 2,
                    "device_id": device,
                    "table_id": transport,
                    "proof": direct_proof(secret, device, transport),
                    "native_mux": 1,
                },
            )
            welcome = recv_json_frame(sock)
            self.assertEqual(welcome["type"], "welcome")
            self.assertEqual(welcome["build_id"], BUILD_ID)
            send_raw_frame(sock, ws_frame(1, b"first"))
            self.assertTrue(router.started.wait(0.5))

            # The router worker is still sleeping.  The socket reader must continue
            # consuming HMN1 and reach TrafficMeter on the second frame.
            time.sleep(0.30)
            send_raw_frame(sock, ws_frame(2, b"second"))
            deadline = time.monotonic() + 0.5
            while not emitted and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(emitted, "second frame was blocked behind router.handle")
            self.assertGreaterEqual(emitted[-1]["frames"], 2)
        finally:
            sock.close()
            server.stop()

    def test_windows_listener_requests_exclusive_port_ownership(self):
        source = (Path(__file__).parents[1] / "core" / "production_runtime.py").read_text(encoding="utf-8")
        self.assertIn("SO_EXCLUSIVEADDRUSE", source)
        self.assertIn("close the old PokerEye process", source)

    def test_apk_label_skew_does_not_paint_leave_hud(self):
        self.assertIsNone(apk_mismatch_device_hud("V7.4.48-HMN1-VPS", BUILD_ID))
        self.assertIsNone(apk_mismatch_device_hud(BUILD_ID, BUILD_ID))
        self.assertIsNone(apk_mismatch_device_hud("legacy-unversioned", BUILD_ID))
        source = (Path(__file__).parents[1] / "core" / "production_runtime.py").read_text(encoding="utf-8")
        self.assertIn("mismatch_hud = apk_mismatch_device_hud", source)
        self.assertNotIn("авто-CC нестабилен", source)
        self.assertNotIn('action": "LEAVE"', source)

class V2UnboundedFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbounded_production_probe_ignores_finite_per_table_budget(self):
        from core.verified_v1.eye_direct_proxy import BackendLoginRejected

        pool = AccountPool.dynamic_registered(
            "base", known_suffixes=(9,), auto_expand_unbounded=True, max_probe_concurrency=1
        )
        held = pool.acquire("already-in-use")
        observations = []

        class FakeProxy:
            def __init__(self, slot, logger=None):
                self.slot = slot

            async def start(self):
                suffix = int(str(self.slot.account_id).rsplit("-", 1)[1])
                if suffix <= 12:
                    raise BackendLoginRejected(f"reject {self.slot.account_id}")
                return "127.0.0.1", 12345

            async def wait_backend_ready(self, _timeout):
                return None

            async def close(self):
                return None

            def bind_bridge(self, *_args, **_kwargs):
                return None

        class FakeBridge:
            def __init__(self, *_args, **_kwargs):
                self.state = {}
                self.autoplay = SimpleNamespace(pending=None)
                self.pending_action_ack = None
                self.lifecycle_phase = "lobby"
                self.active_money_profile = None
                self.current_hand = None
                self.context_hand = None
                self.eye_w = None

            def seed_router_context(self, *_args, **_kwargs):
                return None

            async def ensure_eye(self):
                return None

        factory = LiveTableSessionFactory(
            accounts=pool,
            credential_file=Path("unused"),
            backend_host="unused",
            backend_port=443,
            observation_sink=observations.append,
            connect_timeout=0.2,
            probe_attempts_per_table=2,
            probe_backoff_seconds=0.05,
        )
        seed = RouterSeed(events=tuple(), requested_table_id=77, requested_config_id=0)
        with patch("core.verified_v1.eye_direct_proxy.DirectBackendProxy", FakeProxy), patch(
            "core.verified_v1.coin_bridge_live.LiveCoinBridge", FakeBridge
        ):
            session = await factory.create("dev", 77, seed)

        self.assertEqual(session.account_id, "base-13")
        quarantined = [row for row in observations if row.kind == "account_quarantined"]
        self.assertEqual(len(quarantined), 3)
        self.assertTrue(all(row.detail.get("probe_unbounded") for row in quarantined))
        self.assertTrue(all(row.detail.get("probe_attempt_limit") is None for row in quarantined))
        self.assertEqual(pool.state_for("base-10"), AccountState.QUARANTINED)

        session._closed = True
        session._monitor.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await session._monitor
        pool.release("already-in-use", held.token)
        pool.release("device/dev/table/77")

    async def test_create_waits_for_released_slot_then_auto_logins(self):
        pool = AccountPool(
            ["base-9"],
            dynamic_base="base",
            auto_expand_unbounded=False,
        )
        held = pool.acquire("already-in-use")
        observations = []

        class FakeProxy:
            def __init__(self, slot, logger=None):
                self.slot = slot

            async def start(self):
                return "127.0.0.1", 12345

            async def wait_backend_ready(self, _timeout):
                return None

            async def close(self):
                return None

            def bind_bridge(self, *_args, **_kwargs):
                return None

        class FakeBridge:
            def __init__(self, *_args, **_kwargs):
                self.state = {}
                self.autoplay = SimpleNamespace(pending=None)
                self.pending_action_ack = None
                self.lifecycle_phase = "lobby"
                self.active_money_profile = None
                self.current_hand = None
                self.context_hand = None
                self.eye_w = None

            def seed_router_context(self, *_args, **_kwargs):
                return None

            async def ensure_eye(self):
                return None

        factory = LiveTableSessionFactory(
            accounts=pool,
            credential_file=Path("unused"),
            backend_host="unused",
            backend_port=443,
            observation_sink=observations.append,
            connect_timeout=0.2,
            probe_attempts_per_table=2,
            probe_backoff_seconds=0.05,
        )
        seed = RouterSeed(events=tuple(), requested_table_id=77, requested_config_id=0)

        async def releaser():
            await asyncio.sleep(0.2)
            pool.release("already-in-use", held.token)

        release_task = asyncio.create_task(releaser())
        with patch("core.verified_v1.eye_direct_proxy.DirectBackendProxy", FakeProxy), patch(
            "core.verified_v1.coin_bridge_live.LiveCoinBridge", FakeBridge
        ):
            session = await factory.create("dev", 77, seed)
        await release_task

        self.assertEqual(session.account_id, "base-9")
        self.assertTrue(any(row.kind == "account_waiting" for row in observations))
        session._closed = True
        session._monitor.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await session._monitor
        pool.release("device/dev/table/77")

    async def test_create_provisions_new_panel_account_when_pool_is_full(self):
        from core.verified_v1.eye_panel_admin import CreatedAccount

        pool = AccountPool(["base-4"], dynamic_base="base", auto_expand_unbounded=False)
        held = pool.acquire("already-in-use")
        observations = []
        calls = []

        def provisioner():
            calls.append(1)
            return CreatedAccount(account_id="base-20", acc_id=99, enabled=True)

        class FakeProxy:
            def __init__(self, slot, logger=None):
                self.slot = slot

            async def start(self):
                return "127.0.0.1", 12345

            async def wait_backend_ready(self, _timeout):
                return None

            async def close(self):
                return None

            def bind_bridge(self, *_args, **_kwargs):
                return None

        class FakeBridge:
            def __init__(self, *_args, **_kwargs):
                self.state = {}
                self.autoplay = SimpleNamespace(pending=None)
                self.pending_action_ack = None
                self.lifecycle_phase = "lobby"
                self.active_money_profile = None
                self.current_hand = None
                self.context_hand = None
                self.eye_w = None

            def seed_router_context(self, *_args, **_kwargs):
                return None

            async def ensure_eye(self):
                return None

        factory = LiveTableSessionFactory(
            accounts=pool,
            credential_file=Path("unused"),
            backend_host="unused",
            backend_port=443,
            observation_sink=observations.append,
            connect_timeout=0.2,
            probe_attempts_per_table=2,
            probe_backoff_seconds=0.05,
            account_provisioner=provisioner,
        )
        seed = RouterSeed(events=tuple(), requested_table_id=77, requested_config_id=0)
        with patch("core.verified_v1.eye_direct_proxy.DirectBackendProxy", FakeProxy), patch(
            "core.verified_v1.coin_bridge_live.LiveCoinBridge", FakeBridge
        ):
            session = await factory.create("dev", 77, seed)

        self.assertEqual(session.account_id, "base-20")
        self.assertEqual(calls, [1])
        self.assertTrue(any(row.kind == "account_registered" for row in observations))
        session._closed = True
        session._monitor.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await session._monitor
        pool.release("already-in-use", held.token)
        pool.release("device/dev/table/77")


class SCLoginDiagnosticTests(unittest.TestCase):
    def test_sc_login_rejection_keeps_slot_diagnostics(self):
        from core.verified_v1.eye_direct_proxy import _sc_login_rejection_detail

        detail = _sc_login_rejection_detail({
            "loginSuccess": False,
            "wle": {"left": 0, "limit": 7},
            "workMode": "AUTO",
            "args": {"password": "super-secret", "code": 12},
        })
        self.assertIn("wle=", detail)
        self.assertIn("workMode=", detail)
        self.assertIn("<redacted>", detail)
        self.assertNotIn("super-secret", detail)

    def test_hook_game_mode_is_always_auto(self):
        from core.verified_v1.eye_direct_proxy import hook_game_mode, hook_game_mode_frame, _work_mode

        self.assertEqual(_work_mode({"workMode": 0}), "man")
        self.assertEqual(hook_game_mode("man"), "auto")
        self.assertEqual(hook_game_mode_frame({"workMode": 0})["data"], "auto")
        self.assertEqual(hook_game_mode_frame({"workMode": "MAN"})["tag"], "game_mode")
        self.assertEqual(hook_game_mode_frame({"workingMode": "vip"})["data"], "auto")

    def test_hook_game_type_is_always_pppoker(self):
        import json
        from core.verified_v1.eye_backend_probe import decode_envelope, settings_envelope
        from core.verified_v1.eye_direct_proxy import hook_game_type, hook_working_mode

        self.assertEqual(hook_game_type(""), "PPPoker")
        self.assertEqual(hook_game_type(None), "PPPoker")
        self.assertEqual(hook_working_mode("MANUAL"), "AUTO")
        message_type, body = decode_envelope(settings_envelope())
        self.assertEqual(message_type, "CSSettings")
        self.assertEqual(json.loads(body), {"game_type": "PPPoker", "working_mode": "AUTO"})

    def test_local_pool_file_has_20_21_not_19(self):
        import json
        path = Path(__file__).resolve().parents[1] / "config" / "backend_accounts.local.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = [str(row.get("account_id") or "") for row in (data.get("accounts") or [])]
        self.assertNotIn("709393393-19", ids)
        self.assertNotIn("709393393-17", ids)
        self.assertIn("709393393-20", ids)
        self.assertIn("709393393-21", ids)
        blocked = data.get("blocked_accounts") or []
        self.assertIn("709393393-19", blocked)
        self.assertIn("709393393-17", blocked)

    def test_pool_acquires_20_21_and_skips_blocked_19_hole(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "accounts.json"
            pool = AccountPool(
                ["709393393-18", "709393393-20", "709393393-21"],
                dynamic_base="709393393",
                registry_path=registry,
                profile="PPPoker",
                auto_expand_unbounded=True,
                blocked_accounts=["709393393-19", "709393393-17"],
            )
            self.assertEqual(pool.acquire("a").account_id, "709393393-18")
            self.assertEqual(pool.acquire("b").account_id, "709393393-20")
            self.assertEqual(pool.acquire("c").account_id, "709393393-21")
            fourth = pool.acquire("d")
            self.assertNotEqual(fourth.account_id, "709393393-19")
            self.assertNotEqual(fourth.account_id, "709393393-17")
            self.assertIsNone(pool.state_for("709393393-19"))
            self.assertIsNone(pool.state_for("709393393-17"))

    def test_autogrow_off_uses_explicit_pool_and_does_not_fill_holes(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "accounts.json"
            pool = AccountPool(
                [
                    "709393393-4",
                    "709393393-13",
                    "709393393-14",
                    "709393393-18",
                    "709393393-20",
                    "709393393-21",
                ],
                dynamic_base="709393393",
                registry_path=registry,
                profile="PPPoker",
                auto_expand_unbounded=False,
                blocked_accounts=["709393393-19", "709393393-17"],
            )
            for owner in "abcdef":
                lease = pool.acquire(owner)
                self.assertNotEqual(lease.account_id, "709393393-19")
                self.assertNotEqual(lease.account_id, "709393393-17")
                self.assertNotEqual(lease.account_id, "709393393-5")
            with self.assertRaises(AccountPoolExhausted):
                pool.acquire("g")
            self.assertIsNone(pool.state_for("709393393-5"))
            self.assertIsNone(pool.state_for("709393393-19"))
            self.assertIsNone(pool.state_for("709393393-17"))

    def test_live_file_blocked_17_is_never_acquired_on_hole_fill(self):
        import json
        path = Path(__file__).resolve().parents[1] / "config" / "backend_accounts.local.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        blocked = [str(item) for item in (data.get("blocked_accounts") or [])]
        self.assertIn("709393393-17", blocked)
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "accounts.json"
            pool = AccountPool(
                ["709393393-13", "709393393-18"],
                dynamic_base=str(data.get("base") or "709393393"),
                registry_path=registry,
                profile="PPPoker",
                auto_expand_unbounded=True,
                blocked_accounts=blocked,
                max_probe_concurrency=20,
            )
            seen = set()
            for i in range(8):
                seen.add(pool.acquire(f"owner-{i}").account_id)
            self.assertNotIn("709393393-17", seen)
            self.assertNotIn("709393393-19", seen)
            self.assertIsNone(pool.state_for("709393393-17"))
            self.assertIsNone(pool.state_for("709393393-19"))


class MidHandStandupTests(unittest.IsolatedAsyncioTestCase):
    def test_standup_during_hand_does_not_abort_cc_or_tell_eye(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.state["hand_id"] = "112"
        bridge.current_hand = object()
        self.assertTrue(bridge._hand_in_progress())
        self.assertFalse(bridge._lifecycle_aborts_cc("game.leave_Seat"))
        self.assertFalse(bridge._should_announce_standup_to_eye())
        self.assertTrue(bridge._lifecycle_aborts_cc("game.quit_table"))

        bridge.current_hand = None
        bridge.state["hand_id"] = ""
        self.assertFalse(bridge._hand_in_progress())
        self.assertTrue(bridge._lifecycle_aborts_cc("game.leave_Seat"))
        self.assertTrue(bridge._should_announce_standup_to_eye())

    async def test_handle_event_leave_seat_mid_hand_keeps_pending_cc(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.state["hand_id"] = "112"
        bridge.current_hand = object()
        bridge.pending_action_ack = {
            "action": "CHECK",
            "token": "hand:112",
            "retries": 0,
            "hand_id": "112",
            "retry_at": time.monotonic() + 2,
        }
        decision, _ = await bridge.handle_event(
            coin_event("game.leave_Seat", 59059, mid="ls-mid", direction="out")
        )
        self.assertIsNotNone(bridge.pending_action_ack)
        self.assertEqual(bridge.pending_action_ack["token"], "hand:112")
        self.assertNotEqual(decision.get("cancel_schedule"), "hand:112")
        task = bridge.protocol_task
        if task is not None:
            task.cancel()

    async def test_handle_event_quit_table_cancels_pending_cc(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.state["hand_id"] = "112"
        bridge.current_hand = object()
        bridge.pending_action_ack = {
            "action": "CHECK",
            "token": "hand:112",
            "retries": 0,
            "hand_id": "112",
            "retry_at": time.monotonic() + 2,
        }
        decision, _ = await bridge.handle_event(
            coin_event("game.quit_table", 59059, mid="qt", direction="out")
        )
        self.assertIsNone(bridge.pending_action_ack)
        self.assertEqual(decision.get("cancel_schedule"), "hand:112")
        task = bridge.protocol_task
        if task is not None:
            task.cancel()

    def test_first_sit_packet_claims_the_table_room(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        self.assertFalse(bridge._is_active_room(None))
        self.assertTrue(bridge._is_active_room(57953))
        self.assertEqual(bridge.active_hook_room, 57953)
        self.assertTrue(bridge._is_active_room(57953))
        self.assertFalse(bridge._is_active_room(1))

    def test_hero_sit_defers_zero_uid_and_zero_chips(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        bridge.hero_sitting = False
        bridge.hero_departing = False
        bridge.context_active = True
        bridge.context_hand = SimpleNamespace(hero_id=0, hero_seat=0, hero_name="", table_id=1)
        bridge.state.update(user_id=0, user_name="")
        bridge.identity.update(user_id=0, user_name="")
        sent = []

        async def _ready(*_a, **_k):
            return None

        async def _send(*_a, **_k):
            sent.append(True)
            return b""

        bridge.ensure_observer_context = _ready  # type: ignore[method-assign]
        bridge._build_context_hand = lambda: bridge.context_hand  # type: ignore[method-assign]
        bridge._activate_money_profile = lambda *_a, **_k: None  # type: ignore[method-assign]
        bridge.eye_send_cmd = _send  # type: ignore[method-assign]

        asyncio.run(bridge._send_hero_sit({"seatId": 3, "userId": 0, "userChips": 0}))
        self.assertFalse(bridge.hero_sitting)
        self.assertEqual(bridge.state.get("user_id"), 0)
        self.assertEqual(sent, [])

        builder = SimpleNamespace(_sitdown_brc=lambda *_a, **_k: b"")
        with patch("core.verified_v1.coin_bridge_live.core.PPPBuilder", return_value=builder):
            asyncio.run(bridge._send_hero_sit({
                "seatId": 3, "userId": 42, "buyinAmount": 2.0, "userName": "Hero",
            }))
        self.assertTrue(bridge.hero_sitting)
        self.assertEqual(bridge.state.get("user_id"), 42)
        self.assertTrue(sent)

    def test_immediate_sit_ack_without_uid_is_still_hero(self):
        from core.verified_v1.coin_bridge_live import LiveCoinBridge

        bridge = LiveCoinBridge()
        bridge.state.update(user_name="HeroNick", user_id=42)
        bridge.identity.update(user_name="HeroNick", user_id=42)
        self.assertTrue(bridge._is_hero_row({"userName": "HeroNick", "userId": 0, "seatId": 3}))
        self.assertTrue(bridge._is_hero_row({"userId": 42, "seatId": 3}))
        bridge.active_hook_room = 100
        self.assertTrue(bridge._is_hero_row({"seatId": 2, "buyinAmount": 2}, room=100))
        self.assertFalse(bridge._is_hero_row({"seatId": 2, "playerCards": ["Ah", "Kd"]}, room=100))
        self.assertFalse(bridge._is_hero_row({"userName": "Villain", "userId": 9, "seatId": 1}))
        self.assertTrue(bridge._is_hero_turn({"whoseTurn": "HeroNick"}))

    def test_operator_fleet_snapshot_counts_hands_by_type(self):
        logger = _Logger()
        console = OperatorConsole(logger, 7, accounts_dynamic=True)
        device = "dev-1"
        console.device_up(device)
        console.observation(RouterObservation(
            "bridge_diag", device, 100, detail={"tag": "identity_name", "name": "HeroNick"},
        ))
        console.observation(RouterObservation("hand_completed", device, 100, game_type="NLH", hand_id="h1"))
        console.observation(RouterObservation("hand_completed", device, 100, game_type="PLO4", hand_id="h2"))
        row = console.fleet_snapshot()[device]
        self.assertEqual(row["nick"], "HeroNick")
        self.assertEqual(row["hands_by_type"]["NLH"], 1)
        self.assertEqual(row["hands_by_type"]["PLO4"], 1)
        self.assertIn("NLH: 1", row["session_hands"])


if __name__ == "__main__":
    unittest.main()
