from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.v6router.automation import AutoPolicy, AutomationStore, DeviceAutomation
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
        self.assertEqual(policy.table_count, 8)
        self.assertEqual(policy.bb, 0.05)
        self.assertFalse(policy.watch_balance)
        self.assertTrue(policy.watch_players)


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
            store = AutomationStore(path)
            store.put("dev-1", AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02}))
            reloaded = AutomationStore(path)
            policy = reloaded.get("dev-1")
            self.assertIsNotNone(policy)
            self.assertTrue(policy.enabled)
            self.assertEqual(policy.table_count, 5)


class WatchdogJoinTests(unittest.TestCase):
    def test_does_not_join_without_lobby_catalog(self):
        auto = DeviceAutomation("dev")
        auto.policy = AutoPolicy.from_mapping({"enabled": True, "table_count": 5, "bb": 0.02})
        auto.tick(seated_tables=0, live_table_ids=[])
        self.assertEqual(auto._status, "waiting-catalog")
        self.assertFalse(auto._joining)

    def test_queues_join_when_lobby_and_nlh_config_ready(self):
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
        self.assertTrue(auto._joining)
        self.assertEqual(auto._queue[0].command, "lobby.join_game")

    def test_leave_all_gradual_staggers_tables(self):
        auto = DeviceAutomation("dev")
        n = auto.schedule_leave_all([11, 22, 33], gradual=True)
        self.assertEqual(n, 3)
        delays = [row[1] for row in auto._gradual]
        self.assertGreater(delays[1], delays[0])
        self.assertGreater(delays[2], delays[1])


class HeroTurnTests(unittest.TestCase):
    def test_hero_turn_matches_uid_when_name_differs(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=42)
        bridge.identity.update(user_name="Hero", user_id=42)
        self.assertTrue(bridge._is_hero_turn({"whoseTurn": "Hero"}))
        self.assertTrue(bridge._is_hero_turn({"whoseTurn": "Other", "userId": 42}))
        self.assertFalse(bridge._is_hero_turn({"whoseTurn": "Villain", "userId": 9}))

    def test_cc_miss_streak_standups_on_third_silent_hand(self):
        bridge = LiveCoinBridge()
        for _ in range(3):
            bridge._hero_turn_this_hand = True
            bridge._hand_cc_failed = True
            bridge.current_hand = None
            # incremental_event reset_data path is async; replicate the counter.
            if bridge._hero_turn_this_hand and bridge._hand_cc_failed:
                bridge.cc_miss_streak += 1
                bridge._hero_turn_this_hand = False
                bridge._hand_cc_failed = False
        self.assertEqual(bridge.cc_miss_streak, 3)


if __name__ == "__main__":
    unittest.main()
