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
    direct_proof,
    recv_json_frame,
    send_json_frame,
    send_raw_frame,
)
from core.v6router.accounts import AccountPool, AccountState
from core.v6router.router import (
    LiveTableSession,
    LiveTableSessionFactory,
    RoutedEvent,
    RouterObservation,
    RouterSeed,
)


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
        self.assertNotIn("LAN_HOSTS", bridge)
        self.assertNotIn("nonVpnNetworkHandle", bridge)
        self.assertNotIn("android_setsocknetwork", source)
        self.assertNotIn("try_lan_upgrade", source)
        self.assertNotIn("10.0.2.2", build)
        self.assertNotIn("10.0.3.2", build)
        self.assertIn("handshake_confirmed(fd, g_trainer_host, 5000)", source)
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


class MidHandStandupTests(unittest.TestCase):
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
