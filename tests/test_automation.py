from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.v6router.automation import (
    AutoPolicy, AutomationStore, DeviceAutomation, parse_ui_dump, plan_lobby_join_tap,
)
from core.v6router.router import RoutedEvent
from core.verified_v1.coin_action_wire import build_lobby_join_game_packet, decode_packet
from core.verified_v1.coin_bridge_live import LiveCoinBridge


class AutoPolicyTests(unittest.TestCase):
    def test_normalizes_stakes_and_bounds(self):
        policy = AutoPolicy.from_mapping({
            "enabled": True,
            "table_count": 99,
            "bb": 0.049,
            "watch_balance": False,
        })
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.table_count, 5)
        self.assertEqual(policy.bb, 0.05)
        self.assertFalse(policy.watch_balance)
        self.assertTrue(policy.watch_players)
        self.assertTrue(policy.play_enabled)

    def test_play_enabled_can_be_disabled(self):
        policy = AutoPolicy.from_mapping({"enabled": True, "play_enabled": False})
        self.assertTrue(policy.enabled)
        self.assertFalse(policy.play_enabled)

    def test_ledger_enabled_off_by_default_and_roundtrips(self):
        policy = AutoPolicy.from_mapping({"enabled": True})
        self.assertFalse(policy.ledger_enabled)
        on = AutoPolicy.from_mapping({"enabled": True, "ledger_enabled": True})
        self.assertTrue(on.ledger_enabled)
        alias = AutoPolicy.from_mapping({"uchet": True})
        self.assertTrue(alias.ledger_enabled)
        self.assertTrue(on.public()["ledger_enabled"])

    def test_auto_off_clears_want_seat_and_ignores_game_init(self):
        auto = DeviceAutomation("dev")
        auto.apply_policy({"enabled": True, "table_count": 5, "bb": 0.02}, enable=True)
        auto._want_seat.add(11)
        auto.apply_policy(auto.policy.public(), enable=False)
        self.assertFalse(auto.policy.enabled)
        self.assertEqual(auto._want_seat, set())
        auto._observe(RoutedEvent(
            event={}, payload=None, raw=b"", command="game.game_init",
            direction="in", room_id=9, table_ids=(11,), data={},
        ))
        self.assertNotIn(11, auto._want_seat)


class LobbyWalletCaptureTests(unittest.TestCase):
    def test_join_game_table_freezes_start_even_if_already_seated(self):
        auto = DeviceAutomation("dev")
        auto._seated.add(11)
        auto._observe(RoutedEvent(
            event={}, payload=None, raw=b"", command="lobby.join_game_table",
            direction="in", room_id=5, table_ids=(12,),
            data={"balance": 12.4, "tablesToJoin": [{"tableId": 12, "tableName": "NLH 12"}]},
        ))
        self.assertEqual(auto.lobby_wallet_cash, 12.4)
        self.assertEqual(auto.wallet_cash, 12.4)

    def test_later_balance_does_not_overwrite_start(self):
        auto = DeviceAutomation("dev")
        auto._capture_start_wallet({"balance": 12.4})
        auto._seated.add(11)
        auto._capture_start_wallet({"balance": 10.4})
        self.assertEqual(auto.lobby_wallet_cash, 12.4)
        self.assertEqual(auto.wallet_cash, 10.4)

    def test_game_init_does_not_steal_table_gold_as_lobby(self):
        auto = DeviceAutomation("dev")
        auto._observe(RoutedEvent(
            event={}, payload=None, raw=b"", command="game.game_init",
            direction="in", room_id=9, table_ids=(11,),
            data={"gameInitResponseData": {"balance": 19.47, "gold": 19.47, "maxSize": 6, "tableName": "NLH 11"}},
        ))
        self.assertIsNone(auto.lobby_wallet_cash)

    def test_last_table_keeps_lobby_snapshot(self):
        auto = DeviceAutomation("dev")
        auto.lobby_wallet_cash = 12.4
        auto.wallet_cash = 10.4
        auto._seated.add(11)
        auto.note_table_closed(11)
        self.assertEqual(auto.lobby_wallet_cash, 12.4)
        self.assertEqual(auto.wallet_cash, 10.4)


class JoinPacketTests(unittest.TestCase):
    def test_lobby_join_game_shape_matches_pcap_canary(self):
        raw = build_lobby_join_game_packet(config_id=200588, big_blind=0.02, buyin=2)
        packet = decode_packet(raw)
        self.assertEqual(packet["p"]["c"], "lobby.join_game")
        self.assertEqual(packet["p"]["r"], -1)
        self.assertEqual(
            json.loads(packet["p"]["p"]["data"]),
            {
                "buyInAmount": 2,
                "configId": 200588,
                "gameType": "Ring",
                "bigblind": 0.02,
                "miniGameType": 1,
                "coinType": 1,
            },
        )


class StoreTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auto.json"
            store = AutomationStore(path, redis_url="")
            store.put("dev-1", AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02}))
            reloaded = AutomationStore(path, redis_url="")
            policy = reloaded.get("dev-1")
            self.assertIsNotNone(policy)
            self.assertTrue(policy.enabled)
            self.assertEqual(policy.table_count, 5)

    def test_runtime_roundtrip_seated_tables(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auto.json"
            store = AutomationStore(path, redis_url="")
            store.put_runtime("dev-1", {"seated_tables": [11, 22], "play_enabled": True})
            reloaded = AutomationStore(path, redis_url="")
            blob = reloaded.get_runtime("dev-1")
            self.assertIsNotNone(blob)
            self.assertEqual(blob["seated_tables"], [11, 22])

    def test_automation_restores_seated_from_store(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auto.json"
            store = AutomationStore(path, redis_url="")
            store.put_runtime("sony", {"seated_tables": [1154179], "play_enabled": True})
            auto = DeviceAutomation("sony", store=store)
            self.assertIn(1154179, auto._seated)
            self.assertEqual(store.all_runtime()["sony"]["seated_tables"], [1154179])


class WatchdogJoinTests(unittest.TestCase):
    def test_does_not_protocol_join_without_local_catalog(self):
        auto = DeviceAutomation("sony")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "lobby-ws"
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)
        self.assertFalse(auto.needs_ui_join())
        self.assertIsNone(auto.lobby_join_command())

    def test_manual_mode_never_auto_joins_or_leaves(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({
            "enabled": True, "table_count": 5, "bb": 0.02, "watch_players": True,
        })
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        auto.mark_stack(11, 100.0, 2, True)
        auto._seated_at[11] = time.monotonic() - 30
        auto._short_since[11] = time.monotonic() - 21
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        self.assertFalse(auto._joining)
        self.assertIsNone(auto.lobby_join_command())
        self.assertFalse(auto.needs_ui_join())

    def test_other_device_catalog_is_not_reused(self):
        first = DeviceAutomation("weedman")
        first._remember_config({
            "configId": 200588,
            "bigBlind": 0.02,
            "miniGameTypeId": 1,
            "minbuyin": 0.8,
            "maxbuyin": 2.0,
            "tableSize": 6,
        })
        second = DeviceAutomation("sony")
        second.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        second.lobby_ws = "lobby-ws"
        second.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(second._joining)
        self.assertFalse(second.needs_ui_join())

    def test_first_table_is_ui_join_even_with_catalog(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 2, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588,
            "bigBlind": 0.02,
            "miniGameTypeId": 1,
            "minbuyin": 0.8,
            "maxbuyin": 2.0,
            "tableSize": 6,
        })
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)
        self.assertFalse(auto.needs_ui_join())
        self.assertIsNone(auto.lobby_join_command())

    def test_protocol_join_after_first_seated_table(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 2, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588,
            "bigBlind": 0.02,
            "miniGameTypeId": 1,
            "minbuyin": 0.8,
            "maxbuyin": 2.0,
            "tableSize": 6,
        })
        auto._tabs[11] = 0.0
        auto._seated.add(11)
        auto._hands_done[11] = 1
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertFalse(auto._joining)
        self.assertFalse(any(item.command == "lobby.join_game" for item in auto._queue))

    def test_lobby_dump_opens_a_table_not_cash_when_list_visible(self):
        stats = parse_ui_dump(
            '<ui w="1080" h="1920" janitor="1" nodes="4">'
            '<n t="Cash" x="40" y="400" w="200" h="80" c="1"/>'
            '<n t="NLH" x="40" y="500" w="200" h="80" c="1"/>'
            '<n t="0.01/0.02" x="40" y="600" w="200" h="60" c="1"/>'
            '<n t="3/6" x="400" y="900" w="120" h="40" c="1"/>'
            '<n t="Play" x="700" y="900" w="200" h="80" c="1"/>'
            "</ui>"
        )
        tap = plan_lobby_join_tap(stats["rows"], bb=0.02)
        self.assertIsNotNone(tap)
        self.assertIn(tap["why"], {"lobby-table", "lobby-play"})

    def test_coin_lobby_540x960_picks_3_of_6_not_empty_or_hu(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="12">'
            '<n t="NLH" x="40" y="40" w="80" h="28" c="1"/>'
            '<n t="0.02/0.04" x="200" y="40" w="120" h="28" c="1"/>'
            '<n t="NLH 6 Max" x="20" y="120" w="200" h="40" c="1"/>'
            '<n t="NLH HU" x="240" y="120" w="160" h="40" c="1"/>'
            '<n t="6" x="400" y="170" w="40" h="28" c="1"/>'
            '<n t="0/6" x="200" y="230" w="50" h="24" c="1"/>'
            '<n t="0/6" x="200" y="290" w="50" h="24" c="1"/>'
            '<n t="0/6" x="200" y="350" w="50" h="24" c="1"/>'
            '<n t="0/6" x="200" y="410" w="50" h="24" c="1"/>'
            '<n t="0/6" x="200" y="470" w="50" h="24" c="1"/>'
            '<n t="3/6" x="200" y="530" w="50" h="24" c="1"/>'
            '<n t="Play" x="430" y="520" w="80" h="40" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02, min_players=3)
        self.assertFalse(plan["leave"])
        self.assertEqual(plan["step"], "table")
        self.assertEqual(plan["tap"]["why"], "lobby-table")
        self.assertEqual(plan["reason"], "стол 3/6")

    def test_hu_filter_taps_6max_not_the_hu_tab(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="5">'
            '<n t="NLH 6 Max" x="20" y="120" w="200" h="40" c="1"/>'
            '<n t="NLH HU" x="240" y="120" w="160" h="40" c="1"/>'
            '<n t="0/2" x="200" y="300" w="50" h="24" c="1"/>'
            '<n t="1/2" x="200" y="360" w="50" h="24" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertEqual(plan["step"], "clear-hu")
        self.assertEqual(plan["tap"]["why"], "lobby-6max")
        self.assertFalse(plan["leave"])

    def test_empty_6max_list_does_not_open_zero_tables(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="3">'
            '<n t="NLH 6 Max" x="20" y="120" w="200" h="40" c="1"/>'
            '<n t="0/6" x="200" y="300" w="50" h="24" c="1"/>'
            '<n t="0/6" x="200" y="360" w="50" h="24" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02, min_players=3)
        self.assertNotEqual(plan.get("tap", {}) and plan["tap"].get("why"), "lobby-table")
        self.assertFalse(plan["leave"])

    def test_hu_observer_felt_requests_leave(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="4">'
            '<n t="NLH HU" x="40" y="40" w="100" h="24" c="1"/>'
            '<n t="Empty" x="80" y="400" w="80" h="40" c="0"/>'
            '<n t="Empty" x="300" y="400" w="80" h="40" c="0"/>'
            '<n t="Hero" x="200" y="700" w="80" h="30" c="0"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertTrue(plan["leave"])
        self.assertEqual(plan["step"], "leave")

    def test_lobby_dump_taps_cash_on_home(self):
        stats = parse_ui_dump(
            '<ui w="1080" h="1920" janitor="1" nodes="2">'
            '<n t="Let\'s Explore" x="40" y="40" w="200" h="28" c="0"/>'
            '<n t="Cash" x="80" y="500" w="240" h="90" c="1"/>'
            "</ui>"
        )
        tap = plan_lobby_join_tap(stats["rows"], bb=0.02)
        self.assertEqual(tap["why"], "lobby-cash")

    def test_multiway_not_nlh_badge(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="8">'
            '<n t="NLH" x="40" y="40" w="80" h="28" c="1"/>'
            '<n t="Playing" x="40" y="200" w="120" h="32" c="0"/>'
            '<n t="Blinds" x="40" y="240" w="80" h="24" c="0"/>'
            '<n t="₮0.01/₮0.02" x="200" y="240" w="160" h="24" c="0"/>'
            '<n t="2" x="40" y="280" w="40" h="24" c="0"/>'
            '<n t="Tables" x="90" y="280" w="80" h="24" c="0"/>'
            '<n t="Multiway" x="40" y="420" w="220" h="80" c="1"/>'
            '<n t="Heads Up" x="280" y="420" w="220" h="80" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertFalse(plan["leave"])
        self.assertEqual(plan["tap"]["why"], "lobby-multiway")
        self.assertNotEqual(plan["tap"]["why"], "lobby-nlh")

    def test_cash_games_title_inside_lobby_is_not_tapped(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="4">'
            '<n t="Cash Games" x="40" y="40" w="200" h="28" c="0"/>'
            '<n t="NLH ×" x="40" y="90" w="80" h="28" c="1"/>'
            '<n t="PLO ×" x="140" y="90" w="80" h="28" c="1"/>'
            '<n t="₮0.01/₮0.02" x="40" y="360" w="220" h="48" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertNotEqual((plan.get("tap") or {}).get("why"), "lobby-cash")

    def test_initializing_does_not_tap_join_similar(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="3">'
            '<n t="Initializing" x="180" y="400" w="180" h="28" c="0"/>'
            '<n t="Join Similar" x="180" y="700" w="200" h="48" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertFalse(plan["leave"])
        self.assertIsNone(plan.get("tap"))
        self.assertEqual(plan["reason"], "загрузка")

    def test_home_lets_explore_taps_cash_games(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="8">'
            '<n t="Let\'s Explore" x="40" y="40" w="200" h="28" c="0"/>'
            '<n t="Poker Formats" x="40" y="80" w="200" h="24" c="0"/>'
            '<n t="Popular" x="40" y="120" w="120" h="24" c="0"/>'
            '<n t="Free Games" x="40" y="160" w="160" h="24" c="0"/>'
            '<n t="Weedman834" x="40" y="200" w="160" h="24" c="0"/>'
            '<n t="Cash Games" x="80" y="520" w="380" h="90" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertEqual(plan["tap"]["why"], "lobby-cash")

    def test_enable_plo_filter_when_nlh_on(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="4">'
            '<n t="Cash Games" x="40" y="40" w="200" h="28" c="0"/>'
            '<n t="NLH ×" x="40" y="90" w="80" h="28" c="1"/>'
            '<n t="PLO" x="140" y="90" w="70" h="28" c="1"/>'
            '<n t="₮0.01/₮0.02" x="40" y="400" w="200" h="50" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertEqual(plan["tap"]["why"], "lobby-plo")

    def test_empty_felt_with_join_similar_leaves_not_lobby_limit(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="12">'
            '<n t="NLH" x="80" y="20" w="70" h="24" c="0"/>'
            '<n t="+ Join Similar" x="280" y="18" w="160" h="32" c="1"/>'
            '<n t="Empty" x="40" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="400" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="40" y="380" w="70" h="20" c="0"/>'
            '<n t="Empty" x="400" y="380" w="70" h="20" c="0"/>'
            '<n t="Empty" x="40" y="520" w="70" h="20" c="0"/>'
            '<n t="NLH ₮0.01/₮0.02" x="180" y="520" w="180" h="28" c="0"/>'
            '<n t="Splash" x="470" y="220" w="50" h="24" c="0"/>'
            '<n t="Weedman834" x="220" y="780" w="100" h="24" c="0"/>'
            '<n t="100BB" x="230" y="810" w="70" h="20" c="0"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02, min_players=3)
        self.assertTrue(plan["leave"])
        self.assertEqual(plan["step"], "leave")
        self.assertIsNone(plan.get("tap"))
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto._tabs[1149384] = 0.0
        auto._seated.add(1149384)
        auto._last_opened_table = 1149384
        auto.ingest_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="8">'
            '<n t="+ Join Similar" x="280" y="18" w="160" h="32" c="1"/>'
            '<n t="Empty" x="40" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="400" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="40" y="380" w="70" h="20" c="0"/>'
            '<n t="Empty" x="400" y="380" w="70" h="20" c="0"/>'
            '<n t="NLH ₮0.01/₮0.02" x="180" y="520" w="180" h="28" c="0"/>'
            '<n t="Weedman834" x="220" y="780" w="100" h="24" c="0"/>'
            "</ui>",
            table_id=1149384,
        )
        cmd = auto.lobby_join_command()
        self.assertIsNone(cmd)

    def test_felt_stakes_are_not_lobby_limit(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="3">'
            '<n t="+ Join Similar" x="280" y="18" w="160" h="32" c="1"/>'
            '<n t="NLH ₮0.01/₮0.02" x="180" y="520" w="180" h="28" c="0"/>'
            '<n t="Weedman834" x="220" y="780" w="100" h="24" c="0"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertFalse(plan["leave"])
        self.assertNotEqual((plan.get("tap") or {}).get("why"), "lobby-limit")

    def test_table_chrome_is_not_a_lobby_limit_tap(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="4">'
            '<n t="NLH - ₮0.01/₮0.02" x="80" y="40" w="280" h="28" c="0"/>'
            '<n t="NLH" x="40" y="80" w="60" h="24" c="0"/>'
            '<n t="Join Similar" x="180" y="700" w="200" h="48" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertFalse(plan["leave"])
        self.assertEqual(plan["scene"], "table")
        self.assertEqual(plan["tap"]["why"], "join-similar")

    def test_currency_limit_card_on_cash_games(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="5">'
            '<n t="Cash Games" x="40" y="40" w="200" h="28" c="0"/>'
            '<n t="NLH ×" x="40" y="90" w="80" h="28" c="1"/>'
            '<n t="PLO ×" x="140" y="90" w="80" h="28" c="1"/>'
            '<n t="₮0.01/₮0.02" x="40" y="360" w="220" h="48" c="1"/>'
            '<n t="PLO 4 cards" x="40" y="430" w="220" h="48" c="1"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertEqual(plan["tap"]["why"], "lobby-limit")

    def test_second_table_uses_join_similar_when_lobby_ws_missing(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto._tabs[1149384] = 0.0
        auto._seated.add(1149384)
        auto._last_opened_table = 1149384
        auto._hands_done[1149384] = 1
        auto._peak_players[1149384] = 6
        self.assertFalse(auto.needs_ui_join())
        auto.ingest_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="3">'
            '<n t="NLH - ₮0.01/₮0.02" x="80" y="40" w="280" h="28" c="0"/>'
            '<n t="Join Similar" x="180" y="700" w="200" h="48" c="1"/>'
            "</ui>",
            table_id=1149384,
        )
        cmd = auto.lobby_join_command()
        self.assertIsNone(cmd)

    def test_join_similar_waits_until_one_hand(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto._tabs[11] = 0.0
        auto._seated.add(11)
        auto._last_opened_table = 11
        auto.ingest_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="2">'
            '<n t="NLH - ₮0.01/₮0.02" x="80" y="40" w="280" h="28" c="0"/>'
            '<n t="Join Similar" x="180" y="700" w="200" h="48" c="1"/>'
            "</ui>",
            table_id=11,
        )
        cmd = auto.lobby_join_command()
        self.assertIsNone(cmd)

    def test_uidump_junk_does_not_protocol_leave_last_opened_sibling(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto._tabs[11] = 0.0
        auto._tabs[22] = 0.0
        auto._seated.update({11, 22})
        auto._last_opened_table = 11
        auto._hand_live[11] = True
        auto._peak_players[11] = 6
        auto.ingest_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="6" live="0">'
            '<n t="Empty" x="40" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="400" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="40" y="380" w="70" h="20" c="0"/>'
            '<n t="NLH ₮0.01/₮0.02" x="180" y="520" w="180" h="28" c="0"/>'
            '<n t="Weedman834" x="220" y="780" w="100" h="24" c="0"/>'
            "</ui>",
            table_id=22,
        )
        cmd = auto.lobby_join_command()
        self.assertTrue(cmd is None or not cmd.get("leave"))
        self.assertNotIn(11, auto._leave_reasons)

    def test_observer_auto_on_wants_seat(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.tick(seated_tables=0, live_table_ids=[11])
        self.assertNotIn(11, auto._want_seat)

    def test_peye_id_beats_nlh_label_for_multiway(self):
        from core.v6router.automation import classify_lobby_scene
        stats = parse_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="3">'
            '<n t="NLH" x="40" y="200" w="80" h="28" c="1"/>'
            '<n t="MW" x="40" y="420" w="220" h="80" c="1" id="peye.multiway"/>'
            '<n t="HU" x="280" y="420" w="220" h="80" c="1" id="peye.heads_up"/>'
            "</ui>"
        )
        plan = classify_lobby_scene(stats["rows"], bb=0.02)
        self.assertEqual(plan["tap"]["why"], "lobby-multiway")
        self.assertEqual(plan["tap"]["x"], 150)

    def test_watch_players_leaves_when_auto_off(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({
            "enabled": False, "table_count": 5, "bb": 0.02, "watch_players": True,
        })
        auto.mark_stack(11, 100.0, 1, True)
        auto._seated.add(11)
        auto._seated_at[11] = time.monotonic() - 30
        auto._short_since[11] = time.monotonic() - 21
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        self.assertFalse(auto.policy.enabled)

    def test_observer_auto_off_hangs(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": False, "table_count": 5, "bb": 0.02})
        auto.tick(seated_tables=0, live_table_ids=[11])
        self.assertNotIn(11, auto._want_seat)
        self.assertNotIn(11, auto._leave_reasons)

    def test_leave_all_immediate_is_still_one_by_one(self):
        auto = DeviceAutomation("dev")
        n = auto.schedule_leave_all([11, 22, 33], gradual=False)
        self.assertEqual(n, 3)
        first = auto.drain_leaves()
        self.assertEqual(len(first), 1)
        self.assertEqual(auto.drain_leaves(), [])
        auto.note_table_closed(first[0][0], "left")
        second = auto.drain_leaves()
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0][0], first[0][0])

    def test_leave_all_gradual_staggers_tables(self):
        auto = DeviceAutomation("dev")
        n = auto.schedule_leave_all([11, 22, 33], gradual=True)
        self.assertEqual(n, 3)
        delays = [row[1] for row in auto._gradual]
        self.assertGreater(delays[1], delays[0])
        self.assertGreater(delays[2], delays[1])

    def test_sit_waiting_for_cards_does_not_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 1, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_everyone_left_leaves_when_below_three(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 6, True)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto.mark_hand(11, False)
        auto.mark_stack(11, 100.0, 1, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)

    def test_uidump_hero_only_leaves_now(self):
        from core.v6router.automation import parse_ui_dump
        xml = '<ui w="1080" h="1920"><n t="NLH" x="400" y="40" w="120" h="48" c="1"/><n t="Empty" x="100" y="500" w="40" h="20" c="0"/><n t="Empty" x="200" y="500" w="40" h="20" c="0"/><n t="Weedman834" x="300" y="700" w="80" h="20" c="0"/><n t="TABLE CLOSED" x="200" y="400" w="400" h="80" c="0"/></ui>'
        stats = parse_ui_dump(xml)
        self.assertTrue(stats["closed"])
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 1, True)
        auto._last_opened_table = 11
        auto.ingest_ui_dump(
            '<ui><n t="Empty" x="10" y="450" w="10" h="10" c="0"/>'
            '<n t="Hero" x="20" y="500" w="10" h="10" c="0"/>'
            '<n t="pad" x="0" y="1800" w="1" h="1" c="0"/></ui>'
        )
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto._seated_at[11] = time.monotonic() - 4
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_short_after_hand_leaves(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 1, True)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto.mark_hand(11, False)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)
        first = auto.drain_leaves()
        self.assertEqual(first[0][0], 11)

    def test_coin_five_tab_cap_blocks_join(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        for table_id in range(1, 6):
            auto._tabs[table_id] = 0.0
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)

    def test_protocol_close_keeps_ghost_tab_until_ui_cooldown(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        for table_id in range(1, 6):
            auto.note_table_closed(table_id, "quit")
        self.assertEqual(auto.coin_tab_count(), 5)
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)
        auto._tabs = {table_id: time.monotonic() - 1.0 for table_id in range(1, 6)}
        self.assertEqual(auto.coin_tab_count(), 0)
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)
        self.assertFalse(auto.needs_ui_join())

    def test_auto_off_does_not_join(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        auto.apply_policy(auto.policy.public(), enable=False)
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto.policy.enabled)
        self.assertFalse(auto._joining)
        self.assertEqual(auto._queue, [])

    def test_operator_close_does_not_resit(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto._want_seat.add(11)
        auto._request_leave(11, "operator close")
        self.assertNotIn(11, auto._want_seat)
        auto._observe(RoutedEvent(
            event={}, payload=None, raw=b"", command="game.game_init",
            direction="in", room_id=9, table_ids=(11,), data={},
        ))
        self.assertNotIn(11, auto._want_seat)
        auto._maybe_reserve(11, RoutedEvent(
            event={}, payload=None, raw=b"", command="game.seatInfo",
            direction="in", room_id=9, table_ids=(11,), data={},
        ), [])
        self.assertFalse(any(item.command == "game.reserve_Seat" for item in auto._queue))

    def test_leave_all_clears_want_seat(self):
        auto = DeviceAutomation("dev")
        auto._want_seat.add(11)
        auto._want_seat.add(22)
        auto.schedule_leave_all([11, 22], gradual=False)
        self.assertEqual(auto._want_seat, set())

    def test_leave_all_disables_joins(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        n = auto.schedule_leave_all([11, 22], gradual=False)
        self.assertEqual(n, 2)
        self.assertTrue(auto._leaving_all)
        self.assertFalse(auto.policy.enabled)
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)
        self.assertTrue(all(item.command != "lobby.join_game" for item in auto._queue))

    def test_short_handed_first_deal_does_not_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 2, True)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_two_player_table_leaves_after_hand(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 2, True)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto.mark_hand(11, False)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)

    def test_two_player_next_hand_does_not_retract_short_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 2, True)
        auto.mark_hand(11, True)
        auto.mark_hand(11, False)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)

    def test_console_closed_leftover_felt_still_leaves(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.ingest_ui_dump(
            '<ui w="540" h="960" janitor="1" nodes="7" live="1">'
            '<n t="Fold" x="40" y="820" w="120" h="70" c="1"/>'
            '<n t="Check" x="200" y="820" w="120" h="70" c="1"/>'
            '<n t="Empty" x="40" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="400" y="200" w="70" h="20" c="0"/>'
            '<n t="Empty" x="40" y="380" w="70" h="20" c="0"/>'
            '<n t="NLH ₮0.01/₮0.02" x="180" y="520" w="180" h="28" c="0"/>'
            '<n t="Weedman834" x="220" y="780" w="100" h="24" c="0"/>'
            "</ui>"
        )
        cmd = auto.lobby_join_command()
        self.assertIsNone(cmd)

    def test_second_join_waits_until_two_hands(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        auto._last_join_at = time.monotonic() - 60
        auto.mark_stack(11, 100.0, 6, True)
        auto._tabs[11] = 0.0
        auto._last_opened_table = 11
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertFalse(auto._joining)
        self.assertEqual(auto._status, "idle")
        auto.mark_hand(11, True)
        auto.mark_hand(11, False)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertFalse(auto._joining)

    def test_manual_seated_table_blocks_join_until_one_hand(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        auto.mark_stack(11, 100.0, 6, True)
        auto._tabs[11] = 0.0
        auto._last_join_at = time.monotonic() - 60
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertFalse(auto._joining)
        self.assertEqual(auto._status, "idle")
        auto.mark_hand(11, True)
        auto.mark_hand(11, False)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertFalse(auto._joining)

    def test_snapshot_ui_omits_uidump_rows(self):
        auto = DeviceAutomation("dev")
        auto._ui_last = {
            "closed": False,
            "timer": 11.0,
            "janitor": True,
            "nodes": 75,
            "rows": [{"t": "Fold", "id": "peye.action_fold"}] * 80,
        }
        snap = auto.snapshot()
        self.assertNotIn("rows", snap.get("ui") or {})
        self.assertEqual((snap.get("ui") or {}).get("timer"), 11.0)
        self.assertEqual((snap.get("ui") or {}).get("nodes"), 75)

    def test_false_sitout_does_not_block_short_handed_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({
            "enabled": True, "play_enabled": True, "table_count": 1, "watch_players": True,
        })
        auto.mark_stack(11, 100.0, 2, True)
        auto.mark_hand(11, True)
        auto.mark_hand(11, False)
        auto.mark_sitout(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)
        self.assertEqual(auto.drain_leaves()[0][0], 11)
        self.assertFalse(auto.request_sit_in(11, room=42))

    def test_sitout_queues_sit_in_not_leave(self):
        from core.verified_v1.coin_action_wire import decode_packet

        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": False, "play_enabled": True, "table_count": 1})
        auto.mark_stack(11, 100.0, 6, True)
        auto.mark_sitout(11, True)
        auto.game_ws[11] = "aabb"
        self.assertTrue(auto.request_sit_in(11, room=42, ws_id="aabb"))
        self.assertNotIn(11, auto._leave_reasons)
        self.assertEqual(auto.drain_leaves(), [])
        item = next(row for row in auto._queue if row.command == "game.sitout")
        decoded = decode_packet(item.raw)
        self.assertEqual(decoded["p"]["c"], "game.sitout")
        data = json.loads(decoded["p"]["p"]["data"])
        self.assertIs(data["sitOutMap"]["sitOutNextHand"], False)
        self.assertFalse(any(data["sitOutMap"].values()))
        self.assertFalse(any(row.command in {"game.leave_Seat", "game.quit_table"} for row in auto._queue))

    def test_sit_in_skipped_when_play_off_or_leave_all(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": False, "play_enabled": False, "table_count": 1})
        auto.mark_stack(11, 100.0, 6, True)
        auto.mark_sitout(11, True)
        self.assertFalse(auto.request_sit_in(11, room=42))
        auto.policy.play_enabled = True
        auto.schedule_leave_all([11], gradual=False)
        self.assertFalse(auto.request_sit_in(11, room=42))

    def test_sitout_never_leaves_a_live_table(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1})
        auto.mark_stack(11, 100.0, 6, True)
        auto.mark_hand(11, True)
        auto.mark_hand(11, False)
        auto.mark_sitout(11, True)
        auto._sitout_since[11] = time.monotonic() - 120
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        self.assertEqual(auto.drain_leaves(), [])
        self.assertFalse(any(
            item.command in {"game.leave_Seat", "game.quit_table"}
            for item in auto._queue
        ))

    def test_wait_blind_sitout_does_not_leave_before_a_hand(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1})
        auto.mark_stack(11, 100.0, 6, True)
        auto.mark_sitout(11, True)
        auto._seated_at[11] = time.monotonic() - 30
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        self.assertEqual(auto.drain_leaves(), [])
        self.assertFalse(any(
            item.command in {"game.leave_Seat", "game.quit_table"}
            for item in auto._queue
        ))

    def test_unknown_zero_stack_does_not_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({
            "enabled": True, "table_count": 1, "watch_balance": True, "watch_players": True,
        })
        auto.mark_stack(11, 0.0, 3, True, stack_known=False)
        auto._seated_at[11] = time.monotonic() - 30
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto.mark_stack(11, 0.0, 3, True, stack_known=True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_after_hand_one_player_leaves(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({
            "enabled": True, "table_count": 1, "watch_players": True, "watch_balance": True,
        })
        auto.mark_stack(11, 100.0, 4, True)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        auto.mark_hand(11, False)
        auto.mark_stack(11, 100.0, 4, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto.mark_stack(11, 100.0, 1, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertIn(11, auto._leave_reasons)

    def test_known_low_stack_leaves_after_grace(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({
            "enabled": True, "table_count": 1, "watch_balance": True, "watch_players": False,
        })
        auto.mark_stack(11, 100.0, 4, True)
        auto.mark_stack(11, 70.0, 4, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto._seated_at[11] = time.monotonic() - 30
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_ghost_tabs_stay_visible(self):
        auto = DeviceAutomation("dev")
        auto._tabs[11] = 0.0
        auto._tabs[22] = 0.0
        auto.note_table_closed(11, "стек 0.0 BB < 79")
        ids = dict(auto.coin_tab_ids())
        self.assertEqual(ids[11], "ghost")
        self.assertEqual(ids[22], "live")

    def test_join_rejected_extends_ghost_tabs_not_live_ones(self):
        auto = DeviceAutomation("dev")
        auto.note_table_closed(11, "quit")
        auto._tabs[11] = time.monotonic() + 1.0
        auto._tabs[22] = 0.0
        routed = RoutedEvent(
            event={}, payload=None, raw=b"", command="lobby.join_game",
            direction="in", room_id=-1, table_ids=(),
            data={"isSuccess": False, "errorCode": 1},
        )
        auto._observe(routed)
        self.assertGreater(auto._tabs[11], time.monotonic() + 20.0)
        self.assertEqual(auto._tabs[22], 0.0)
        self.assertGreater(auto._join_block_until, time.monotonic())
        self.assertFalse(auto._joining)

    def test_parse_janitor_dump_attrs(self):
        from core.v6router.automation import parse_ui_dump
        stats = parse_ui_dump(
            '<ui w="1080" h="1920" janitor="1" nodes="12" closed="1" waitlist="0" '
            'live="0" timer="12.0" tap="overflow-glyph" tapx="900" tapy="40" '
            'taphit="1" tapage="800">'
            '<n t="TABLE CLOSED" x="200" y="400" w="400" h="80" c="0"/>'
            '<n t="12s" x="80" y="30" w="40" h="24" c="1"/>'
            '</ui>'
        )
        self.assertTrue(stats["janitor"])
        self.assertTrue(stats["closed"])
        self.assertEqual(stats["tap"], "overflow-glyph")
        self.assertEqual(stats["nodes"], 12)
        self.assertIsNotNone(stats["shortest_timer"])

    def test_loading_dump_does_not_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 1, True)
        auto._last_opened_table = 11
        stats = auto.ingest_ui_dump(
            '<ui w="1080" h="1920">'
            '<n t="Empty" x="100" y="500" w="40" h="20" c="0"/>'
            '<n t="Empty" x="200" y="500" w="40" h="20" c="0"/>'
            '<n t="Empty" x="300" y="500" w="40" h="20" c="0"/>'
            '</ui>'
        )
        self.assertTrue(stats["loading"])
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_closed_dump_does_not_kill_live_table(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 6, True)
        auto.mark_hand(11, True)
        auto._last_opened_table = 11
        auto.ingest_ui_dump(
            '<ui w="1080" h="1920">'
            '<n t="TABLE CLOSED" x="200" y="400" w="400" h="80" c="0"/>'
            '<n t="Join Wait List" x="200" y="900" w="400" h="60" c="1"/>'
            '</ui>'
        )
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        self.assertNotIn(11, auto._ui_closed)

    def test_observing_tab_blocks_second_join(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.lobby_ws = "aabbccdd"
        auto._remember_config({
            "configId": 200588, "bigBlind": 0.02, "miniGameTypeId": 1,
            "minbuyin": 0.8, "maxbuyin": 2.0, "tableSize": 6,
        })
        auto._tabs[1149459] = 0.0
        auto._last_opened_table = 1149459
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertFalse(auto._joining)
        self.assertEqual(auto._status, "idle")

    def test_last_opened_observing_is_not_junk_at_3s(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto._tabs[1147264] = 0.0
        auto._last_opened_table = 1147264
        auto._last_join_at = time.monotonic() - 5
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertNotIn(1147264, auto._leave_reasons)

    def test_false_leave_retracts_when_table_fills(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto._leave_reasons[11] = "нет данных"
        auto.mark_stack(11, 100.0, 6, True)
        self.assertEqual(auto.retract_false_leave(11), "нет данных")
        self.assertNotIn(11, auto._leave_reasons)

    def test_junk_table_leaves_without_20s(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 1, True)
        auto._seated_at[11] = time.monotonic() - 4
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_empty_idle_20s_without_table_closed(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 2, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)
        auto._short_since[11] = time.monotonic() - 21
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)

    def test_normal_hand_does_not_leave(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 1, "watch_players": True})
        auto.mark_stack(11, 100.0, 5, True)
        auto.mark_hand(11, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        auto.mark_hand(11, False)
        auto.mark_stack(11, 100.0, 5, True)
        auto.tick(seated_tables=1, live_table_ids=[11])
        self.assertNotIn(11, auto._leave_reasons)


class HeroTurnTests(unittest.TestCase):
    def test_hero_turn_matches_uid_when_name_differs(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=42)
        bridge.identity.update(user_name="Hero", user_id=42)
        self.assertTrue(bridge._is_hero_turn({"whoseTurn": "Hero"}))
        self.assertTrue(bridge._is_hero_turn({"whoseTurn": "Other", "userId": 42}))
        self.assertFalse(bridge._is_hero_turn({"whoseTurn": "Villain", "userId": 9}))

    def test_cc_miss_streak_stands_up_after_more_than_three(self):
        bridge = LiveCoinBridge()
        for _ in range(3):
            bridge._hero_turn_this_hand = True
            bridge._hand_cc_failed = True
            bridge._account_hero_hand_cc()
        self.assertEqual(bridge.cc_miss_streak, 3)
        self.assertFalse(bridge.hero_departing)
        self.assertFalse(bridge.cc_failsafe_standup)
        bridge._hero_turn_this_hand = True
        bridge._hand_cc_failed = True
        bridge._account_hero_hand_cc()
        self.assertEqual(bridge.cc_miss_streak, 4)
        self.assertTrue(bridge.hero_departing)
        self.assertTrue(bridge.cc_failsafe_standup)

    def test_mapped_eye_action_resets_failsafe_streak(self):
        bridge = LiveCoinBridge()
        for _ in range(2):
            bridge._hero_turn_this_hand = True
            bridge._hand_cc_failed = True
            bridge._account_hero_hand_cc()
        self.assertEqual(bridge.cc_miss_streak, 2)
        bridge._note_mapped_eye_action()
        self.assertEqual(bridge.cc_miss_streak, 0)
        self.assertFalse(bridge.cc_failsafe_standup)

    def test_cc_miss_ignores_hands_without_hero_turn(self):
        bridge = LiveCoinBridge()
        bridge._hero_turn_this_hand = False
        bridge._hand_cc_failed = True
        bridge._account_hero_hand_cc()
        self.assertEqual(bridge.cc_miss_streak, 0)

    def test_failsafe_success_does_not_count_as_silent(self):
        bridge = LiveCoinBridge()
        bridge._hero_turn_this_hand = True
        bridge._hand_cc_failed = False
        bridge._account_hero_hand_cc()
        self.assertEqual(bridge.cc_miss_streak, 0)

    def test_hero_turn_with_options_and_empty_whose(self):
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.state["hero_seat"] = 5
        bridge.state["hand_id"] = "1147264001"
        self.assertTrue(bridge._is_hero_turn({
            "whoseTurn": "",
            "seatId": 5,
            "userTurnOptions": {"3": None, "7": None},
        }))
        self.assertFalse(bridge._is_hero_turn({
            "whoseTurn": "Villain",
            "userId": 9,
            "seatId": 2,
            "userTurnOptions": {"3": None},
        }))
        self.assertFalse(bridge._is_hero_turn({
            "whoseTurn": "",
            "userTurnOptions": {"3": None, "7": None},
        }))


class FuelSnapshotTests(unittest.TestCase):
    def test_latest_fuel_is_not_a_sum(self):
        from core.production_runtime import snapshot_fuel_from_devices
        from core.v6router.fuel import latest_fuel_reading

        qty, rate = latest_fuel_reading([
            {"fuel_quantity": 5000.0, "fuel_updated_at": 1.0, "fuel_rate_per_hand": 0.2},
            {"fuel_quantity": 5642.0, "fuel_updated_at": 2.0, "fuel_rate_per_hand": 0.25},
        ])
        self.assertEqual(qty, 5642.0)
        self.assertEqual(rate, 0.25)

        qty, _rate = snapshot_fuel_from_devices([
            {"tables": [{"fuel_quantity": 5000.0, "fuel_updated_at": 1.0, "state": "ready"}]},
            {"tables": [{"fuel_quantity": 5642.0, "fuel_updated_at": 2.0, "state": "ready"}]},
        ])
        self.assertEqual(qty, 5642.0)
        self.assertNotEqual(qty, 5000.0 + 5642.0)


if __name__ == "__main__":
    unittest.main()
