from __future__ import annotations

import asyncio
import base64
import collections
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.v6router.automation import DeviceAutomation
from core.v6router.router import (
    DeviceIngressRouter, LiveTableSession, RoutedEvent, _LiveTableSnapshot, _SessionSlot,
    TableLogic, hero_sitout_for_watchdog, in_play_enough_to_lease, swap_logic_between_hands,
)
from core.verified_v1.coin_action_wire import (
    _Byte, _Int, _Obj, _Short, _Str, decode_packet, encode_packet,
    CAPTURED_TIMEOUT_SITOUT_MAP, SITOUT_MAP_KEYS,
    build_game_sitout_packet, sitout_map_latched,
)
from core.verified_v1 import coin_ppp_bridge as ppp
from core.verified_v1.bombpot_support import (
    BombpotTracker,
    detect_double_board,
    stale_double_board_should_drop,
)
from core.verified_v1.coin_autoplay import CoinAutoplayCoordinator
from core.verified_v1.coin_bridge_live import (
    LiveCoinBridge, compute_net_profits, device_hud_payload, format_action_hint,
    hud_action, hud_clear, normalized_coin_seat_action,
)


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "coin_edge_samples.json").read_text(
        encoding="utf-8"
    )
)
ROOM = int(FIXTURES["room"])


def payload(command: str, data: dict, room: int | None = ROOM) -> dict:
    outer = {"c": command, "p": {"data": data}}
    if room is not None:
        outer["r"] = int(room)
    return {"p": outer}


class DoubleBoardPcapRegressionTests(unittest.TestCase):
    def test_room_capability_does_not_eject_ordinary_table(self):
        active, reason = detect_double_board(
            payload(
                "lobby.join_game_table",
                FIXTURES["lobby_doubleboard_capability"],
                None,
            ),
            "lobby.join_game_table",
        )
        self.assertFalse(active, reason)

    def test_ordinary_bombpot_hand_is_supported(self):
        active, reason = detect_double_board(
            payload("game.bombpot_info", FIXTURES["ordinary_bombpot_hand"]),
            "game.bombpot_info",
        )
        self.assertFalse(active, reason)

    def test_actual_doubleboard_hand_is_detected(self):
        active, reason = detect_double_board(
            payload(
                "game.bombpot_info",
                FIXTURES["active_doubleboard_bombpot"],
            ),
            "game.bombpot_info",
        )
        self.assertTrue(active)
        self.assertIn("isBombpotHand", reason)

    def test_nonempty_second_board_is_detected_without_boolean(self):
        active, reason = detect_double_board(
            payload("game.dealer_cards", FIXTURES["second_board_cards"]),
            "game.dealer_cards",
        )
        self.assertTrue(active)
        self.assertIn("dealerCardsDoubleBoard", reason)

    def test_empty_preflop_doubleboard_map_is_not_a_second_board(self):
        extra = payload(
            "game.user_turn",
            {
                "timerName": "extraTimer",
                "whoseTurn": "Weedman834",
                "userTurnOptions": {"4": [0.03], "7": None},
                "dealerCards": {"FLOP": [], "TURN": [], "RIVER": []},
                "dealerCardsDoubleBoard": {"FLOP": [], "TURN": [], "RIVER": []},
                "potAmountListDoubleBoard": [],
                "isDoubleBoard": True,
            },
        )
        active, reason = detect_double_board(extra, "game.user_turn")
        self.assertFalse(active, reason)
        self.assertTrue(stale_double_board_should_drop(extra, "game.user_turn", detected=False))

    def test_flop_extratimer_without_second_board_keeps_real_db_mark(self):
        flop = payload(
            "game.user_turn",
            {
                "timerName": "extraTimer",
                "dealerCards": {
                    "FLOP": [{"suit": "HEARTS", "value": "ACE"}, {"suit": "CLUBS", "value": "KING"}, {"suit": "SPADES", "value": "TWO"}],
                    "TURN": [],
                    "RIVER": [],
                },
                "dealerCardsDoubleBoard": {"FLOP": [], "TURN": [], "RIVER": []},
            },
        )
        self.assertFalse(detect_double_board(flop, "game.user_turn")[0])
        self.assertFalse(stale_double_board_should_drop(flop, "game.user_turn", detected=False))

    def test_lobby_second_board_preview_is_not_active_hand_evidence(self):
        active, reason = detect_double_board(
            payload(
                "lobby.join_game_table",
                {
                    "tablesToJoin": [{
                        "roomProperties": {
                            "isBombpot": True,
                            "bombpotInducesDoubleBoard": True,
                            "dealerCardsDoubleBoard": {"FLOP": [1, 2, 3]},
                        }
                    }]
                },
                None,
            ),
            "lobby.join_game_table",
        )
        self.assertFalse(active, reason)

    def test_catalog_is_double_board_flag_does_not_eject_nlh(self):
        active, reason = detect_double_board(
            payload(
                "game.game_init",
                {
                    "gameId": "115457300358",
                    "isDoubleBoard": True,
                    "doubleBoard": True,
                    "miniGameTypeId": 1,
                },
            ),
            "game.game_init",
        )
        self.assertFalse(active, reason)

    def test_splash_text_never_becomes_doubleboard_evidence(self):
        active, _ = detect_double_board(
            payload(
                "game.mega_splash",
                {"message": "DOUBLE BOARD BONUS", "isSplash": True},
            ),
            "game.mega_splash",
        )
        self.assertFalse(active)

    def test_tracker_clears_hand_scoped_state_on_new_hand(self):
        tracker = BombpotTracker()
        tracker.observe(
            payload(
                "lobby.join_game_table",
                FIXTURES["lobby_doubleboard_capability"],
                None,
            )
        )
        self.assertTrue(tracker.state.induces_double_board)
        self.assertFalse(tracker.state.is_double_board)
        tracker.observe(
            payload(
                "game.bombpot_info",
                FIXTURES["active_doubleboard_bombpot"],
            )
        )
        self.assertTrue(tracker.state.is_double_board)
        tracker.observe(
            payload(
                "game.pre_hand_start_info",
                {"gameId": "next-hand", "bbAmount": 0.02},
            )
        )
        self.assertEqual(tracker.state.hand_id, "next-hand")
        self.assertFalse(tracker.state.is_double_board)
        self.assertFalse(tracker.state.is_bombpot_hand)
        self.assertTrue(tracker.state.induces_double_board)


class VillainActionPcapRegressionTests(unittest.TestCase):
    def test_real_postflop_bet_wins_over_historical_raise(self):
        row = FIXTURES["villain_open_bet"]
        action = normalized_coin_seat_action(row)
        self.assertEqual(action, "BET")
        self.assertEqual(
            ppp.ppp_action_for_state(action, current_before=0, target=16, last_full_raise=2),
            "BET",
        )

    def test_villain_pot_button_bet_is_not_ignored(self):
        row = FIXTURES["villain_pot_bet"]
        action = normalized_coin_seat_action(row)
        self.assertEqual(action, "RAISE")
        # Coin labels the pot-size opening wager as Raise/Pot; PPP requires BET
        # on an unopened street and RAISE when a wager already exists.
        self.assertEqual(
            ppp.ppp_action_for_state(action, current_before=0, target=37, last_full_raise=2),
            "BET",
        )
        self.assertEqual(
            ppp.ppp_action_for_state(action, current_before=10, target=37, last_full_raise=10),
            "RAISE",
        )

    def test_inuse_refresh_does_not_repeat_historical_raise(self):
        self.assertEqual(
            normalized_coin_seat_action(FIXTURES["stale_raise_refresh"]), ""
        )


class TurnOptionPcapRegressionTests(unittest.TestCase):
    def test_advance_player_action_options_feed_following_hero_turn(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Hero", "user_id": 1, "_hook_room": ROOM}
        advance_event = {
            "direction": "in",
            "ws_id": "abcd0001",
            "_channel_id": "channel-1",
        }
        autoplay.observe(
            advance_event,
            payload("game.advance_player_action", FIXTURES["advance_options"]),
            b"",
            state,
        )
        turn_event = {
            "direction": "in",
            "ws_id": "abcd0001",
            "_channel_id": "channel-1",
        }
        autoplay.observe(
            turn_event,
            payload("game.user_turn", dict(FIXTURES["hero_turn_without_options"])),
            b"",
            state,
        )
        self.assertTrue(turn_event.get("_hmuriy_options_from_advance"))
        turn = autoplay.turn_by_room[ROOM]
        self.assertEqual(set(turn["userTurnOptions"]), {"3", "8", "9", "10"})
        fallback = autoplay.schedule_failsafe(state, reason="TEST")
        self.assertEqual(fallback["action"], "CHECK")

    def test_timeout_fallback_never_invents_paid_call(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Hero", "user_id": 1, "_hook_room": ROOM}
        autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"4": [0.24], "7": None},
            "_turn_id": "turn-call-or-fold",
            "_observed_monotonic": 0.0,
        }
        fallback = autoplay.schedule_failsafe(state, reason="TEST")
        self.assertEqual(fallback["action"], "FOLD")

    def test_failsafe_folds_when_coin_omits_legal_options(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Hero", "user_id": 1, "_hook_room": ROOM}
        autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {},
            "_turn_id": "turn-empty-options",
            "_observed_monotonic": 0.0,
            "_ws_id": "abcd0001",
        }
        fallback = autoplay.schedule_failsafe(state, reason="NO_OPTIONS")
        self.assertEqual(fallback["action"], "FOLD")

    def test_failsafe_does_not_steal_another_room_check(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Hero", "user_id": 1, "_hook_room": ROOM}
        autoplay.turn_by_room[99999] = {
            "whoseTurn": "Other",
            "userTurnOptions": {"3": None},
            "_turn_id": "other-check",
            "_observed_monotonic": time.monotonic(),
        }
        fallback = autoplay.schedule_failsafe(state, reason="THIS_ROOM")
        self.assertEqual(fallback["action"], "FOLD")
        self.assertEqual(fallback["room"], ROOM)

    def test_hero_turn_name_case_still_stores_options(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Weedman834", "user_id": 42, "_hook_room": ROOM, "hero_seat": 5}
        event = {"direction": "in", "ws_id": "abcd0001"}
        autoplay.observe(
            event,
            payload("game.user_turn", {
                "whoseTurn": "weedman834",
                "userTurnOptions": {"3": None, "7": None},
            }),
            b"",
            state,
        )
        self.assertIn(ROOM, autoplay.turn_by_room)
        fallback = autoplay.schedule_failsafe(state, reason="CASE")
        self.assertEqual(fallback["action"], "CHECK")

    def test_extratimer_empty_options_keeps_pending_check(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "TelImit", "user_id": 1, "_hook_room": ROOM}
        first = {"direction": "in", "ws_id": "abcd0001"}
        autoplay.observe(
            first,
            payload("game.user_turn", {
                "whoseTurn": "TelImit",
                "userTurnOptions": {"3": None, "7": None},
                "callAmount": 0,
                "totalPot": 0.03,
                "turnTime": 16,
                "timerName": "playerHandTimer",
                "extraTimerEnabled": True,
            }),
            b"",
            state,
        )
        autoplay.pending = {
            "room": ROOM,
            "turn_id": first.get("_hmuriy_turn_id"),
            "action": "CHECK",
            "delay_ms": 4509,
        }
        extra = {"direction": "in", "ws_id": "abcd0001"}
        result = autoplay.observe(
            extra,
            payload("game.user_turn", {
                "whoseTurn": "TelImit",
                "userTurnOptions": {},
                "turnTime": 16,
                "timerName": "extraTimer",
                "extraTimerEnabled": True,
                "extraTurnTime": 14,
            }),
            b"",
            state,
        )
        self.assertTrue(extra.get("_hmuriy_duplicate_turn"))
        self.assertFalse(result.cancel_reason)
        self.assertIsNotNone(autoplay.pending)
        self.assertEqual(autoplay.pending["action"], "CHECK")
        self.assertEqual(first.get("_hmuriy_turn_id"), extra.get("_hmuriy_turn_id"))
        forced = autoplay.force_pending_now()
        self.assertEqual(int(forced["delay_ms"]), 0)
        self.assertTrue(forced.get("extra_urgent"))
        self.assertLessEqual(float(forced["due"]), time.monotonic() + 0.05)

    def test_check_delay_never_eats_the_main_clock(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Hero", "user_id": 1, "_hook_room": ROOM, "hand_id": "h1"}
        now = time.monotonic()
        autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "t-check",
            "_observed_monotonic": now,
            "turnTime": 16,
            "_ws_id": "abcd0001",
        }
        autoplay.schedule_cc({"action": "check", "delay": 4509, "lifetime": 4000}, state)
        self.assertLessEqual(int(autoplay.pending["delay_ms"]), 800)
        self.assertEqual(autoplay.pending["action"], "CHECK")

    def test_failsafe_folds_when_turn_map_is_empty_but_room_is_known(self):
        autoplay = CoinAutoplayCoordinator(chip_scale=100)
        state = {"user_name": "Hero", "user_id": 1, "_hook_room": ROOM}
        autoplay.ws_by_room[ROOM] = {"ws_id": "abcd0001", "url": "", "channel_id": "ch"}
        fallback = autoplay.schedule_failsafe(state, reason="NO_TURN_YET")
        self.assertEqual(fallback["action"], "FOLD")
        self.assertEqual(fallback["room"], ROOM)


class CcTimeoutFailsafeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_cc_timeout_queues_check_or_fold(self):
        bridge = LiveCoinBridge()
        bridge.cc_timeout_seconds = 0.05
        bridge.cc_fallback_margin_seconds = 0.0
        bridge.active_hook_room = ROOM
        bridge.state["_hook_room"] = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "t-cc",
            "_observed_monotonic": time.monotonic(),
            "turnTime": 15,
            "_ws_id": "ws",
        }
        await bridge._wait_cc_and_schedule()
        pending = bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "CHECK")
        self.assertTrue(pending.get("fallback"))
        self.assertEqual(pending.get("fallback_reason"), "CC_TIMEOUT")

    async def test_wait_cc_timeout_folds_when_check_absent(self):
        bridge = LiveCoinBridge()
        bridge.cc_timeout_seconds = 0.05
        bridge.cc_fallback_margin_seconds = 0.0
        bridge.active_hook_room = ROOM
        bridge.state["_hook_room"] = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"4": [0.2], "7": None},
            "_turn_id": "t-fold",
            "_observed_monotonic": time.monotonic(),
            "turnTime": 15,
            "_ws_id": "ws",
        }
        await bridge._wait_cc_and_schedule()
        pending = bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "FOLD")
        self.assertTrue(pending.get("fallback"))


class ReconnectSilenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_turn_after_pending_cleared_reschedules(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=1, _hook_room=ROOM)
        bridge.active_hook_room = ROOM
        turn = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"7": None, "4": [0.02]},
            "turnTime": 12,
        }
        body = payload("game.user_turn", turn)
        first = {"direction": "in", "ws_id": "aabb", "url": "wss://coin"}
        bridge.autoplay.observe(first, body, b"", bridge.state)
        self.assertFalse(first.get("_hmuriy_duplicate_turn"))
        bridge.autoplay.schedule_failsafe(bridge.state, reason="FIRST")
        self.assertIsNotNone(bridge.autoplay.pending)
        bridge.autoplay.pending = None
        replay = {"direction": "in", "ws_id": "aabb", "url": "wss://coin"}
        bridge.autoplay.observe(replay, body, b"", bridge.state)
        self.assertTrue(replay.get("_hmuriy_duplicate_turn"))
        hinted = []

        async def fake_hint(event, payload, raw, data):
            hinted.append("incremental")

        bridge.current_hand = SimpleNamespace(hand_id="h-ak")
        bridge.cold_hands.add("h-ak")
        bridge.start_incremental_hint = fake_hint  # type: ignore[method-assign]
        await bridge._on_hero_user_turn(replay, body, b"", turn, ROOM)
        self.assertEqual(hinted, ["incremental"])
        self.assertIsNone(bridge.autoplay.pending)

    async def test_ensure_action_after_reconnect_wipe_sets_failsafe(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        self.assertTrue(await bridge.ensure_action_if_hero_silent(reason="RECONNECT_SILENCE"))
        self.assertEqual(bridge.autoplay.pending["action"], "CHECK")
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "RECONNECT_SILENCE")

    async def test_hero_turn_failsafe_when_eye_is_recovering(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = SimpleNamespace(
            _recovery_in_progress=True,
            _hint_watchdog=SimpleNamespace(recovery_pending=True),
            backend_status_snapshot=SimpleNamespace(
                status="RECOVERING", health="red", message="GAME_IS_BROKEN",
            ),
        )
        hinted = []

        async def boom(*_args, **_kwargs):
            hinted.append("hint")

        bridge.start_cold_hint = boom  # type: ignore[method-assign]
        bridge.start_incremental_hint = boom  # type: ignore[method-assign]
        turn = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "turnTime": 12,
        }
        await bridge._on_hero_user_turn({}, {}, b"", turn, ROOM)
        self.assertEqual(hinted, [])
        self.assertEqual(bridge.autoplay.pending["action"], "CHECK")
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "EYE_UNAVAILABLE")

    async def test_cc_wait_abort_queues_failsafe_not_manual_silence(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.abort_cc_wait("RECOVERY")
        await bridge._wait_cc_and_schedule()
        self.assertEqual(bridge.autoplay.pending["action"], "FOLD")
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "RECOVERY")

    def test_failsafe_between_hands_is_not_coin_sitout(self):
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.wait_blind_cancelled = True
        bridge.cc_miss_streak = 1
        bridge._missed_deals = 0
        bridge.current_hand = None
        bridge._apply_hero_sitout_flags(False, False)
        self.assertFalse(bridge.hero_sitting_out)
        self.assertFalse(bridge._sit_in_wanted)
        self.assertFalse(hero_sitout_for_watchdog(bridge))

    def test_watchdog_sitout_ignores_failsafe_streak_between_hands(self):
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.cc_miss_streak = 4
        bridge.current_hand = None
        bridge.hero_sitting_out = False
        self.assertFalse(hero_sitout_for_watchdog(bridge))
        bridge.hero_sitting_out = True
        self.assertTrue(hero_sitout_for_watchdog(bridge))

    def test_watchdog_source_uses_bridge_latch_only(self):
        source = (Path(__file__).parents[1] / "core" / "v6router" / "router.py").read_text(encoding="utf-8")
        self.assertIn("sitout = hero_sitout_for_watchdog(bridge)", source)
        self.assertNotIn("and not getattr(bridge, \"current_hand\", None)", source)

    def test_coin_flag_or_missed_deal_wants_sit_in(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=42)
        bridge.identity.update(user_name="Hero", user_id=42)
        bridge.hero_sitting = True
        bridge.current_hand = None
        bridge.wait_blind_cancelled = True
        bridge.seat_map = {
            2: {"seatId": 2, "userId": 42, "userName": "Hero", "userChips": 2.0, "isPlaying": False},
        }
        bridge._apply_hero_sitout_flags(True, False)
        self.assertTrue(bridge.hero_sitting_out)
        self.assertTrue(bridge._sit_in_wanted)
        bridge.hero_sitting_out = False
        bridge._sit_in_wanted = False
        bridge._note_missed_deal_if_idle()
        self.assertEqual(bridge._missed_deals, 0)
        self.assertFalse(bridge.hero_sitting_out)
        bridge._clock_missed_this_orbit = True
        bridge._note_missed_deal_if_idle()
        self.assertEqual(bridge._missed_deals, 0)
        self.assertFalse(bridge.hero_sitting_out)
        self.assertFalse(bridge._sit_in_wanted)
        bridge._apply_hero_sitout_flags(True, False, source="sitout_map")
        self.assertTrue(bridge.hero_sitting_out)
        bridge._apply_hero_sitout_flags(False, False, source="sitout_map")
        self.assertFalse(bridge.hero_sitting_out)
        self.assertFalse(bridge._sit_in_wanted)

    def test_between_hands_isplaying_false_is_not_sitout(self):
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.current_hand = None
        bridge.wait_blind_cancelled = True
        bridge._missed_deals = 3
        bridge._apply_hero_sitout_flags(False, False)
        self.assertFalse(bridge.hero_sitting_out)
        bridge._note_missed_deal_if_idle()
        self.assertFalse(bridge.hero_sitting_out)

    def test_inbound_sitout_map_latches_without_seat_flag(self):
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.current_hand = None
        payload = {"sitOutMap": dict(CAPTURED_TIMEOUT_SITOUT_MAP),
                   "msg": "Since you are timed-out, you will sit-out in the next hand"}
        self.assertTrue(sitout_map_latched(payload))
        bridge._apply_hero_sitout_flags(sitout_map_latched(payload), False, source="sitout_map")
        self.assertTrue(bridge.hero_sitting_out)
        bridge._apply_hero_sitout_flags(False, False, source="seat")
        self.assertTrue(bridge.hero_sitting_out)
        bridge._apply_hero_sitout_flags(False, False, source="sitout_map")
        self.assertFalse(bridge.hero_sitting_out)


class SitOutPacketTests(unittest.TestCase):
    def test_sit_in_uses_captured_sitout_map(self):
        raw = build_game_sitout_packet(77, sit_out_next_hand=False)
        decoded = decode_packet(raw)
        self.assertEqual(decoded["p"]["c"], "game.sitout")
        self.assertEqual(decoded["p"]["r"], 77)
        self.assertNotEqual(decoded["p"]["c"], "game.leave_Seat")
        self.assertNotEqual(decoded["p"]["c"], "game.quit_table")
        self.assertNotEqual(decoded["p"]["c"], "game.sitOut")
        data = json.loads(decoded["p"]["p"]["data"])
        self.assertEqual(set(data["sitOutMap"]), set(CAPTURED_TIMEOUT_SITOUT_MAP))
        self.assertEqual(set(data["sitOutMap"]), set(SITOUT_MAP_KEYS))
        self.assertIs(data["sitOutMap"]["sitOutNextHand"], False)
        armed = dict(CAPTURED_TIMEOUT_SITOUT_MAP)
        self.assertTrue(sitout_map_latched({"sitOutMap": armed}))
        self.assertFalse(sitout_map_latched({"sitOutMap": data["sitOutMap"]}))


class RecoveryKeepsFailsafeTests(unittest.TestCase):
    def test_keep_failsafe_pending_does_not_wipe_check_fold(self):
        from core.verified_v1.eye_direct_proxy import keep_failsafe_pending

        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
        }
        bridge.autoplay.schedule_failsafe(bridge.state, reason="CC_TIMEOUT")
        raw = bridge.autoplay.pending
        keep_failsafe_pending(bridge)
        self.assertIs(bridge.autoplay.pending, raw)
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "CC_TIMEOUT")

    def test_keep_failsafe_pending_replaces_eye_mapped_action(self):
        from core.verified_v1.eye_direct_proxy import keep_failsafe_pending

        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
        }
        with bridge.autoplay.lock:
            bridge.autoplay.pending = {
                "action": "RAISE", "fallback": False, "raw": b"x",
            }
        keep_failsafe_pending(bridge)
        self.assertEqual(bridge.autoplay.pending["action"], "CHECK")
        self.assertTrue(bridge.autoplay.pending.get("fallback"))

    def test_recover_bridge_source_does_not_hold_hint_lock(self):
        source = (
            Path(__file__).parents[1] / "core" / "verified_v1" / "eye_direct_proxy.py"
        ).read_text(encoding="utf-8")
        self.assertIn("keep_failsafe_pending(bridge)", source)
        self.assertIn("Do not hold hint_lock across recycle/login", source)
        self.assertNotIn("async with hint_lock:\n                        await perform()", source)


class LiveSnapshotNotEmptyTests(unittest.TestCase):
    def test_timeout_keeps_native_sessions_instead_of_zero_zero(self):
        from core.production_runtime import RouterService

        svc = RouterService.__new__(RouterService)
        svc._routers = {"dev-sony": object()}
        svc._connected = {"dev-sony"}
        svc._device_labels = {"dev-sony": "Sony"}
        svc._heartbeat_at = {"dev-sony": time.monotonic()}
        svc._last_seen = {}
        svc._last_snapshot = {
            "ok": True,
            "devices": [{"device_id": "dev-sony", "connected": True, "tables": [{"table_id": 1}]}],
            "connected_devices": 1,
        }
        snap = svc._snapshot_on_timeout(TimeoutError())
        self.assertTrue(snap.get("stale"))
        self.assertEqual(len(snap.get("devices") or []), 1)
        self.assertNotEqual(snap.get("connected_devices"), 0)

    def test_skeleton_lists_connected_device_without_last_snapshot(self):
        from core.production_runtime import RouterService

        svc = RouterService.__new__(RouterService)
        svc._routers = {"dev-sony": object()}
        svc._connected = {"dev-sony"}
        svc._device_labels = {"dev-sony": "Sony"}
        svc._heartbeat_at = {"dev-sony": time.monotonic()}
        svc._last_seen = {}
        svc._last_snapshot = None
        snap = svc._snapshot_on_timeout(TimeoutError("control"))
        ids = [row["device_id"] for row in snap.get("devices") or []]
        self.assertIn("dev-sony", ids)
        self.assertGreaterEqual(int(snap.get("connected_devices") or 0), 1)

    def test_empty_snapshot_does_not_wipe_run_or_connected_device(self):
        from core.production_runtime import RouterService

        svc = RouterService.__new__(RouterService)
        svc.run_name = "run_live"
        svc._routers = {"dev-1": object()}
        svc._connected = {"dev-1"}
        svc._device_labels = {"dev-1": "Sony"}
        svc._heartbeat_at = {"dev-1": time.monotonic()}
        svc._last_seen = {}
        svc._last_snapshot = {
            "ok": True,
            "devices": [{"device_id": "dev-1", "connected": True, "tables": [{"table_id": 11}]}],
            "connected_devices": 1,
            "run": "run_live",
        }
        kept = svc._remember_snapshot({"ok": True, "devices": [], "run": ""})
        self.assertEqual(len(kept.get("devices") or []), 1)
        self.assertEqual(kept.get("run"), "run_live")
        self.assertTrue(kept.get("stale"))
        self.assertNotEqual(int(kept.get("connected_devices") or 0), 0)
        timeout = svc._snapshot_on_timeout(TimeoutError("control"))
        timeout = svc._remember_snapshot(timeout)
        self.assertEqual(timeout.get("run"), "run_live")
        self.assertEqual(len(timeout.get("devices") or []), 1)

    def test_web_merge_keeps_run_and_devices_on_empty_proxy(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "vps" / "pokereye-web.py"
        spec = importlib.util.spec_from_file_location("pokereye_web_state", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        last = {
            "snapshot": {
                "devices": [{"device_id": "dev-1", "connected": True, "tables": [{}]}],
                "connected_devices": 1,
            },
            "run": "run_abc",
            "events": [],
        }
        merged = mod.merge_live_state({"ok": True, "devices": []}, None, last, [])
        self.assertEqual(merged.get("run"), "run_abc")
        self.assertTrue((merged.get("snapshot") or {}).get("devices"))
        self.assertTrue((merged.get("snapshot") or {}).get("stale"))

    def test_persist_error_is_recorded(self):
        from core.production_runtime import RouterService

        svc = RouterService.__new__(RouterService)
        technical = []
        errors = []
        svc.technical_sink = lambda event, **fields: technical.append((event, fields))
        svc.error_sink = lambda event, **fields: errors.append((event, fields))
        svc.persist_error("control.snapshot", TimeoutError("slow"), where="test")
        self.assertTrue(technical)
        self.assertEqual(technical[0][0], "control.snapshot")
        self.assertEqual(technical[0][1].get("error_type"), "TimeoutError")
        self.assertTrue(errors)

    def test_handle_and_snapshot_persist_errors_in_source(self):
        from core import production_runtime as runtime

        source = Path(runtime.__file__).read_text(encoding="utf-8")
        self.assertIn('self.persist_error("router.handle", exc, device_id=device_id)', source)
        self.assertIn('self.persist_error("control.snapshot", exc, compact=compact)', source)
        self.assertIn('owner.router_service.persist_error("control.http.snapshot", exc)', source)
        self.assertIn("return self._remember_snapshot(self._snapshot_on_timeout(exc))", source)
        self.assertIn("self._remember_snapshot", source)


class FailsafeAckAndReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_extratimer_sends_pending_eye_immediately(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="TelImit", user_id=1, _hook_room=ROOM, hand_id="h1")
        bridge.active_hook_room = ROOM
        now = time.monotonic()
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "TelImit",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "t-eye",
            "_observed_monotonic": now,
            "turnTime": 16,
            "_ws_id": "ws",
        }
        bridge.autoplay.pending = {
            "room": ROOM,
            "turn_id": "t-eye",
            "action": "CHECK",
            "delay_ms": 4509,
            "due": now + 4.509,
            "_arbiter_ready_at": now + 4.509,
            "hand_id": "h1",
        }
        event = {"_hmuriy_duplicate_turn": True, "_hmuriy_turn_refresh": "extraTimer"}
        data = {
            "whoseTurn": "TelImit",
            "timerName": "extraTimer",
            "extraTimerEnabled": True,
            "userTurnOptions": {},
        }
        await bridge._on_hero_user_turn(event, {}, b"", data, ROOM)
        pending = bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "CHECK")
        self.assertEqual(int(pending["delay_ms"]), 0)
        self.assertTrue(pending.get("extra_urgent"))
        self.assertFalse(pending.get("fallback"))
        self.assertLessEqual(float(pending["due"]), time.monotonic() + 0.05)
        self.assertFalse(bridge._clock_missed_this_orbit)

    async def test_extratimer_without_eye_queues_check_now(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=1, _hook_room=ROOM, hand_id="h2")
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "t-none",
            "_observed_monotonic": time.monotonic(),
            "turnTime": 16,
            "_ws_id": "ws",
        }
        event = {"_hmuriy_duplicate_turn": True, "_hmuriy_turn_refresh": "extraTimer"}
        data = {
            "whoseTurn": "Hero",
            "timerName": "extraTimer",
            "extraTimerEnabled": True,
            "userTurnOptions": {"3": None, "7": None},
        }
        started = time.monotonic()
        await bridge._on_hero_user_turn(event, {}, b"", data, ROOM)
        self.assertLess(time.monotonic() - started, 0.5)
        pending = bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "CHECK")
        self.assertTrue(pending.get("fallback"))
        self.assertEqual(pending.get("fallback_reason"), "EXTRA_TIMER")
        self.assertEqual(int(pending.get("delay_ms") or 0), 0)
        self.assertFalse(bridge._clock_missed_this_orbit)

    async def test_extra_flushes_queued_failsafe_instead_of_second_reason(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=1, _hook_room=ROOM, hand_id="")
        bridge.active_hook_room = ROOM
        now = time.monotonic()
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "t-mid",
            "_observed_monotonic": now,
            "turnTime": 16,
            "_ws_id": "ws",
        }
        bridge.autoplay.pending = {
            "due": now + 2.0,
            "action": "CHECK",
            "delay_ms": 0,
            "fallback": True,
            "fallback_reason": "MID_HAND_NO_SEED",
            "turn_id": "t-mid",
            "room": ROOM,
            "ws_id": "ws",
        }
        event = {"_hmuriy_duplicate_turn": True, "_hmuriy_turn_refresh": "extraTimer"}
        data = {
            "whoseTurn": "Hero",
            "timerName": "extraTimer",
            "extraTimerEnabled": True,
            "userTurnOptions": {"3": None, "7": None},
        }
        await bridge._on_hero_user_turn(event, {}, b"", data, ROOM)
        pending = bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending.get("fallback_reason"), "MID_HAND_NO_SEED")
        self.assertNotEqual(pending.get("fallback_reason"), "EXTRA_TIMER")
        self.assertTrue(pending.get("extra_urgent"))
        self.assertLessEqual(float(pending["due"]), time.monotonic() + 0.05)

    async def test_extratimer_after_send_does_not_mark_clock_miss(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", user_id=1, _hook_room=ROOM, hand_id="h3")
        bridge.active_hook_room = ROOM
        bridge.pending_action_ack = {"action": "CHECK", "retries": 0}
        event = {"_hmuriy_duplicate_turn": True, "_hmuriy_turn_refresh": "extraTimer"}
        data = {"whoseTurn": "Hero", "timerName": "extraTimer", "extraTimerEnabled": True}
        await bridge._on_hero_user_turn(event, {}, b"", data, ROOM)
        self.assertFalse(bridge._clock_missed_this_orbit)

    def test_cc_timeout_default_is_eight_seconds(self):
        bridge = LiveCoinBridge()
        self.assertLessEqual(bridge.cc_timeout_seconds, 8.0)
        self.assertGreaterEqual(bridge.cc_timeout_seconds, 2.0)
        self.assertLessEqual(bridge._cc_wait_timeout(), 8.0)
        self.assertEqual(bridge.cc_timeout_seconds, 8.0)

    def _broken_eye(self, *, recovering: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            _recovery_in_progress=recovering,
            _closed=False,
            _hint_watchdog=SimpleNamespace(recovery_pending=recovering),
            backend_status_snapshot=SimpleNamespace(
                status="RECOVERING" if recovering else "ERROR",
                health="red",
                message="GAME_IS_BROKEN",
            ),
        )

    async def test_game_is_broken_replays_same_hand_not_failsafe(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM, hand_id="h-broken")
        bridge.active_hook_room = ROOM
        bridge.current_hand = SimpleNamespace(hand_id="h-broken")
        bridge.cold_hands.add("h-broken")
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = self._broken_eye(recovering=False)
        finished: list[str] = []
        hinted: list[str] = []

        async def close_hint(why: str = "") -> bool:
            finished.append(why)
            return True

        async def cold(*_args, **_kwargs) -> None:
            hinted.append("resend")

        bridge._close_open_hint = close_hint  # type: ignore[method-assign]
        bridge.start_cold_hint = cold  # type: ignore[method-assign]
        started = time.monotonic()
        turn = {"whoseTurn": "Hero", "userTurnOptions": {"3": None, "7": None}, "turnTime": 12}
        await bridge._on_hero_user_turn({}, {}, b"", turn, ROOM)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(finished, ["GAME_IS_BROKEN"])
        self.assertEqual(hinted, ["resend"])
        self.assertIsNone(bridge.autoplay.pending)
        self.assertNotIn("h-broken", bridge.cold_hands)

    async def test_game_is_broken_failsafe_only_after_replay_spent(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM, hand_id="h-broken")
        bridge.active_hook_room = ROOM
        bridge.current_hand = SimpleNamespace(hand_id="h-broken")
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = self._broken_eye(recovering=False)
        hinted: list[str] = []

        async def close_hint(why: str = "") -> bool:
            return True

        async def cold(*_args, **_kwargs) -> None:
            hinted.append("resend")

        bridge._close_open_hint = close_hint  # type: ignore[method-assign]
        bridge.start_cold_hint = cold  # type: ignore[method-assign]
        turn = {"whoseTurn": "Hero", "userTurnOptions": {"3": None, "7": None}, "turnTime": 12}
        await bridge._on_hero_user_turn({}, {}, b"", turn, ROOM)
        self.assertEqual(hinted, ["resend"])
        self.assertIsNone(bridge.autoplay.pending)
        await bridge._on_hero_user_turn({}, {}, b"", turn, ROOM)
        self.assertEqual(hinted, ["resend"])
        self.assertEqual(bridge.autoplay.pending["action"], "CHECK")
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "EYE_UNAVAILABLE")

    async def test_hero_turn_red_eye_still_sends_hint(self):
        """Red/ERROR is not a dead socket. Ask Eye; failsafe only if it does not answer."""
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = SimpleNamespace(
            _recovery_in_progress=False,
            _closed=False,
            _hint_watchdog=SimpleNamespace(recovery_pending=False),
            backend_status_snapshot=SimpleNamespace(
                status="ERROR", health="red", message="NO_TRAFFIC_FROM_ROOM",
            ),
        )
        hinted: list[str] = []

        async def cold(*_args, **_kwargs) -> None:
            hinted.append("hint")

        bridge.start_cold_hint = cold  # type: ignore[method-assign]
        turn = {"whoseTurn": "Hero", "userTurnOptions": {"3": None, "7": None}, "turnTime": 12}
        await bridge._on_hero_user_turn({}, {}, b"", turn, ROOM)
        self.assertEqual(hinted, ["hint"])
        self.assertIsNone(bridge.autoplay.pending)

    async def test_cc_wait_red_without_broken_waits_for_answer(self):
        bridge = LiveCoinBridge()
        bridge.cc_timeout_seconds = 0.35
        bridge.cc_fallback_margin_seconds = 0.0
        bridge.state.update(user_name="Hero", _hook_room=ROOM, hand_id="h-live")
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
            "turnTime": 12,
        }
        bridge.eye_backend = SimpleNamespace(
            _recovery_in_progress=False,
            _closed=False,
            _hint_watchdog=SimpleNamespace(recovery_pending=False),
            backend_status_snapshot=SimpleNamespace(
                status="ERROR", health="red", message="NO_TRAFFIC_FROM_ROOM",
            ),
        )
        started = time.monotonic()
        await bridge._wait_cc_and_schedule()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertEqual(bridge.autoplay.pending["action"], "CHECK")
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "CC_TIMEOUT")

    async def test_cc_wait_game_is_broken_replays_not_multisecond_hang(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM, hand_id="h-broken")
        bridge.active_hook_room = ROOM
        bridge.current_hand = SimpleNamespace(hand_id="h-broken")
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = self._broken_eye(recovering=False)
        hinted: list[str] = []

        async def close_hint(why: str = "") -> bool:
            return True

        async def cold(*_args, **_kwargs) -> None:
            hinted.append("resend")

        bridge._close_open_hint = close_hint  # type: ignore[method-assign]
        bridge._start_cold_hint_locked = cold  # type: ignore[method-assign]
        started = time.monotonic()
        await bridge._wait_cc_and_schedule()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(hinted, ["resend"])
        self.assertIsNone(bridge.autoplay.pending)

    async def test_cc_wait_game_is_broken_resend_under_held_hint_lock(self):
        """Outer wait holds hint_lock; leftover cc_queue.get() must not steal the resend CC.

        Uses the real ``_start_cold_hint_locked`` → nested ``_wait_cc_and_schedule``.
        """
        hand_id = 112595900001
        table_id = ppp.table_id_from_hand(hand_id)
        ev = ColdReplayRoomWindowTests._event
        model = ppp.CoinCaptureModel([
            ev(0, None, {"userId": 1, "userName": "Hero", "sessionId": "s"}, room=None),
            ev(1, "game.wait_list_data", {"tableId": table_id, "configId": 7}),
            ev(
                2, "game.pre_hand_start_info",
                {
                    "gameId": hand_id, "dealerSeatId": 1,
                    "sbSeatId": 1, "bbSeatId": 2,
                    "sbAmount": 0.01, "bbAmount": 0.02,
                    "anteAmount": 0, "initTimeStamp": 1_700_000_000_000,
                },
                hand_id=hand_id, table_id=table_id,
            ),
            ev(3, "game.seatInfo", {"seatResponseDataList": [
                {"userId": 1, "userName": "Hero", "seatId": 1,
                 "userChips": 2.00, "isPlaying": True},
                {"userId": 2, "userName": "Villain", "seatId": 2,
                 "userChips": 2.00, "isPlaying": True},
            ]}),
            ev(
                4, "game.player_info",
                {"playerCards": [
                    {"value": "ACE", "suit": "SPADES"},
                    {"value": "KING", "suit": "HEARTS"},
                ]},
                hand_id=hand_id, table_id=table_id,
            ),
            ev(
                5, "game.user_turn",
                {"whoseTurn": "Hero", "callAmount": 0.0,
                 "turnTime": 15, "userTurnOptions": {"3": None, "7": None}},
            ),
        ])
        model.hero_id = 1
        model.hero_name = "Hero"
        cands = [row for row in model.candidate_hands() if ppp.table_id_from_hand(row[0]) == table_id]
        self.assertTrue(cands)

        bridge = LiveCoinBridge()
        bridge.cc_timeout_seconds = 0.4
        bridge.cc_fallback_margin_seconds = 0.0
        bridge.mid_hand_recovery_grace = 0.0
        bridge.frame_delay = 0.0
        bridge._prefold_config_loaded = True
        bridge._prefold_config = None
        bridge.identity.update(user_id=1, user_name="Hero")
        bridge.state.update(
            user_name="Hero", user_id=1, _hook_room=ROOM, hand_id="h-broken",
            table_id=table_id, _pending_finish_hint=11,
        )
        bridge.active_hook_room = ROOM
        bridge.room_to_table[ROOM] = table_id
        bridge.table_to_room[table_id] = ROOM
        bridge.current_hand = SimpleNamespace(hand_id="h-broken")
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = self._broken_eye(recovering=False)
        bridge.finish_frame_by_table[11] = {
            "msg": json.dumps({"cmd": "pb.FinishRoundHintRSP"}),
        }
        sent: list[str] = []

        async def send_cmd(cmd, body=b""):
            sent.append(str(cmd))
            return True

        async def send_outer(frame, cmd):
            sent.append(str(cmd))

        async def must_not_reenter(*_args, **_kwargs):
            raise AssertionError("start_cold_hint re-entered under hint_lock")

        bridge.eye_send_cmd = send_cmd  # type: ignore[method-assign]
        bridge.eye_send_outer = send_outer  # type: ignore[method-assign]
        bridge.eye_w = SimpleNamespace(is_closing=lambda: False)
        bridge.start_cold_hint = must_not_reenter  # type: ignore[method-assign]
        self.assertIs(
            bridge._start_cold_hint_locked.__func__,
            LiveCoinBridge._start_cold_hint_locked,
        )
        bridge._cold_candidate_snapshot = (  # type: ignore[method-assign]
            lambda: (model, table_id, cands)
        )
        bridge._restore_active_money_profile = lambda: SimpleNamespace(  # type: ignore[method-assign]
            valid_blinds=True, chip_scale=1,
        )
        bridge._quantize_pending_cc_amount = lambda cc, profile: ""  # type: ignore[method-assign]

        async def run():
            async with bridge.hint_lock:
                wait = asyncio.create_task(bridge._wait_cc_and_schedule())
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    if bridge._broken_replay_in_progress and bridge.awaiting_cc:
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("nested _wait_cc_and_schedule never started")
                await asyncio.sleep(0.02)
                await bridge.cc_queue.put({
                    "data": {"type": "CHECK", "delay": 0, "lifetime": 4000, "amount": 0.0},
                })
                await wait

        started = time.monotonic()
        await asyncio.wait_for(run(), 3.0)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn("pb.FinishRoundHintRSP", sent)
        pending = bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "CHECK")
        self.assertFalse(pending.get("fallback"))
        self.assertNotEqual(pending.get("fallback_reason"), "CC_TIMEOUT")

    def _held_ack(self, *, retries: int = 0) -> LiveCoinBridge:
        bridge = LiveCoinBridge()
        now = time.monotonic()
        bridge.state.update(hand_id="h1", user_name="Hero")
        bridge.pending_action_ack = {
            "action": "FOLD",
            "raw": b"fold",
            "retries": retries,
            "retry_at": now - 1.0,
            "hand_id": "h1",
            "ws_id": "ws",
            "url": "wss://coin",
            "token": "tok",
            "_exhausted_reported": False,
        }
        self.assertTrue(bridge.defer_pending_exhaustion(source="extraTimer"))
        return bridge

    def _dummy(self) -> tuple[dict, dict]:
        event = {"direction": "out", "v": 6, "ws_id": "ws", "url": "wss://coin", "id": "d1"}
        payload = {"p": {"c": "lobby.dummy", "r": 1, "p": {"data": {}}}}
        return event, payload

    async def test_extra_timer_hold_blocks_dummy_retry(self):
        bridge = self._held_ack(retries=0)
        event, payload = self._dummy()
        inj = await bridge._maybe_inject_async(event, payload, b"")
        self.assertIsNone(inj.inject_raw)
        ack = bridge.pending_action_ack
        self.assertEqual(int(ack.get("retries") or 0), 0)
        self.assertTrue(ack.get("_refresh_hold"))
        self.assertFalse(bridge.report_action_exhausted_if_due(now=time.monotonic() + 10))

    def test_extra_timer_hold_blocks_action_offers_retry(self):
        bridge = self._held_ack(retries=0)
        session = LiveTableSession.__new__(LiveTableSession)
        session.play_enabled = True
        session.table_id = 11
        session._bridge = bridge
        session._arbiter_cancellations = collections.deque()
        offers = session.action_offers({"v": 6, "ws_id": "ws", "url": "wss://coin"})
        self.assertEqual(offers, ())
        self.assertEqual(int(bridge.pending_action_ack.get("retries") or 0), 0)
        claim = session.action_claim({"v": 6, "ws_id": "ws", "url": "wss://coin"})
        self.assertIsNone(claim)

    async def test_duplicate_turn_holds_before_dummy_inject(self):
        bridge = LiveCoinBridge()
        now = time.monotonic()
        bridge.state.update(hand_id="h1", user_name="Hero")
        bridge.pending_action_ack = {
            "action": "FOLD",
            "raw": b"fold",
            "retries": 0,
            "retry_at": now - 1.0,
            "due": now - 1.0,
            "hand_id": "h1",
            "ws_id": "ws",
            "url": "wss://coin",
            "token": "tok",
        }
        event = {"_hmuriy_duplicate_turn": True, "direction": "in"}
        self.assertIsNone(bridge._note_pending_ack_on_turn(
            event, "game.user_turn", "in", {"whoseTurn": "Hero"},
        ))
        self.assertTrue(bridge.pending_action_ack.get("_refresh_hold"))
        dummy_event, dummy_payload = self._dummy()
        inj = await bridge._maybe_inject_async(dummy_event, dummy_payload, b"")
        self.assertIsNone(inj.inject_raw)
        self.assertEqual(int(bridge.pending_action_ack.get("retries") or 0), 0)
        self.assertTrue(bridge.pending_action_ack.get("_refresh_hold"))
        self.assertFalse(bridge.report_action_exhausted_if_due(now=now + 10))
        self.assertTrue(bridge.confirm_pending_action(source="coin-seat", action="FOLD"))
        self.assertIsNone(bridge.pending_action_ack)

    def test_new_hero_turn_releases_hold(self):
        bridge = self._held_ack()
        self.assertTrue(bridge.pending_action_ack.get("_refresh_hold"))
        bridge._note_pending_ack_on_turn(
            {"direction": "in"}, "game.user_turn", "in", {"whoseTurn": "Hero"},
        )
        self.assertIsNone(bridge.pending_action_ack)
        self.assertFalse(bridge.report_action_exhausted_if_due())

    async def test_unacked_dummy_still_retries_without_extra_timer(self):
        bridge = LiveCoinBridge()
        now = time.monotonic()
        bridge.state.update(hand_id="h1")
        bridge.pending_action_ack = {
            "action": "FOLD",
            "raw": b"fold",
            "retries": 0,
            "retry_at": now - 1.0,
            "hand_id": "h1",
            "ws_id": "ws",
            "url": "wss://coin",
            "token": "tok",
        }
        event, payload = self._dummy()
        inj = await bridge._maybe_inject_async(event, payload, b"")
        self.assertIsNotNone(inj.inject_raw)
        self.assertEqual(int(bridge.pending_action_ack.get("retries") or 0), 1)

    def test_coin_ack_clears_exhausted(self):
        bridge = LiveCoinBridge()
        now = time.monotonic()
        bridge.pending_action_ack = {
            "action": "FOLD",
            "retries": 2,
            "retry_at": now - 1.0,
        }
        self.assertTrue(bridge.confirm_pending_action(source="coin-seat", action="FOLD"))
        self.assertFalse(bridge.report_action_exhausted_if_due(now=now + 10))

    def test_recovery_failsafe_does_not_exhaust_leftover_ack(self):
        from core.verified_v1.eye_direct_proxy import keep_failsafe_pending

        bridge = LiveCoinBridge()
        now = time.monotonic()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"3": None, "7": None},
        }
        bridge.pending_action_ack = {
            "action": "RAISE",
            "retries": 2,
            "retry_at": now - 1.0,
        }
        keep_failsafe_pending(bridge)
        self.assertTrue(bridge.autoplay.pending.get("fallback"))
        self.assertIsNone(bridge.pending_action_ack)
        self.assertFalse(bridge.report_action_exhausted_if_due(now=now + 10))

    async def test_cc_wait_failsafe_when_eye_has_nothing_to_answer(self):
        bridge = LiveCoinBridge()
        bridge.state.update(user_name="Hero", _hook_room=ROOM)
        bridge.active_hook_room = ROOM
        bridge.autoplay.turn_by_room[ROOM] = {
            "whoseTurn": "Hero",
            "userTurnOptions": {"7": None},
            "_turn_id": "live",
            "_ws_id": "ws",
        }
        bridge.eye_backend = SimpleNamespace(
            _recovery_in_progress=True,
            _hint_watchdog=SimpleNamespace(recovery_pending=True),
            backend_status_snapshot=SimpleNamespace(
                status="RECOVERING", health="red", message="GAME_IS_BROKEN",
            ),
        )
        started = time.monotonic()
        await bridge._wait_cc_and_schedule()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(bridge.autoplay.pending["action"], "FOLD")
        self.assertEqual(bridge.autoplay.pending.get("fallback_reason"), "EYE_UNAVAILABLE")

    def test_swap_logic_between_hands_changes_cc_wait(self):
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.current_hand = None
        bridge.state["hand_id"] = ""
        before = bridge._cc_wait_timeout()
        self.assertEqual(before, 8.0)
        logic = TableLogic(revision="r2", cc_timeout_seconds=2.5)
        self.assertTrue(swap_logic_between_hands(bridge, logic))
        self.assertTrue(bridge.hero_sitting)
        self.assertEqual(bridge._cc_wait_timeout(), 2.5)
        self.assertEqual(bridge.cc_timeout_seconds, 2.5)
        self.assertEqual(bridge.table_logic.revision, "r2")
        self.assertFalse(bridge.hero_departing)
        bridge.current_hand = SimpleNamespace(hand_id="h1")
        self.assertFalse(swap_logic_between_hands(
            bridge, TableLogic(revision="r3", cc_timeout_seconds=3.0),
        ))
        self.assertEqual(bridge._cc_wait_timeout(), 2.5)
        self.assertTrue(bridge.hero_sitting)
        self.assertFalse(bridge.hero_departing)

    def test_pending_logic_applies_between_hands(self):
        session = LiveTableSession.__new__(LiveTableSession)
        bridge = LiveCoinBridge()
        bridge.hero_sitting = True
        bridge.current_hand = SimpleNamespace(hand_id="h1")
        bridge.state["hand_id"] = "h1"
        session._bridge = bridge
        session._pending_logic = None
        session._logic = None
        self.assertFalse(session.swap_logic_between_hands(
            TableLogic(revision="r2", cc_timeout_seconds=2.5),
        ))
        self.assertEqual(bridge._cc_wait_timeout(), 8.0)
        self.assertTrue(bridge.hero_sitting)
        self.assertIsNotNone(session._pending_logic)
        bridge.current_hand = None
        bridge.state["hand_id"] = ""
        self.assertTrue(session._apply_pending_table_logic())
        self.assertEqual(bridge._cc_wait_timeout(), 2.5)
        self.assertTrue(bridge.hero_sitting)
        self.assertIsNone(session._pending_logic)

    def test_failsafe_streak_standup_still_arms(self):
        bridge = LiveCoinBridge()
        for _ in range(4):
            bridge._hero_turn_this_hand = True
            bridge._hand_cc_failed = True
            bridge._account_hero_hand_cc()
        self.assertTrue(bridge.cc_failsafe_standup)
        self.assertTrue(bridge.hero_departing)


_EXTRA_TAGS = {
    "extraTimer", "hero_turn_resumed", "turn_refresh", "turn_refresh_hold",
}
_FAILSAFE_TAGS = {
    "GAME_IS_BROKEN", "RECOVERY", "recovery", "fallback_ready",
    "cc_timeout", "EYE_UNAVAILABLE", "hero_turn_eye_unavailable",
}


def replay_captured_exhausted(incident: dict) -> bool:
    """Replay one captured nearby sequence through shipped dummy/ACK helpers.

    Tags are applied in list order. extraTimer is not assumed. A sequence of
    only ``action_retry`` still exhausts. Returns True if exhausted is reported.
    """
    sequence = [str(tag) for tag in (incident.get("nearby") or [])]
    bridge = LiveCoinBridge()
    now = time.monotonic()
    action = str(incident.get("action") or "FOLD")
    bridge.state.update(hand_id="h1", user_name="Hero", _hook_room=ROOM)
    bridge.pending_action_ack = {
        "action": action,
        "raw": b"\x00",
        "retries": 0,
        "retry_at": now - 1.0,
        "due": now - 1.0,
        "hand_id": "h1",
        "ws_id": "ws",
        "url": "wss://coin",
        "token": "tok",
        "_exhausted_reported": False,
    }
    dummy_event = {"direction": "out", "v": 6, "ws_id": "ws", "url": "wss://coin", "id": "d"}
    dummy_payload = {"p": {"c": "lobby.dummy", "r": 1, "p": {"data": {}}}}

    async def apply_tag(tag: str) -> None:
        if tag in _EXTRA_TAGS:
            bridge._note_pending_ack_on_turn(
                {"_hmuriy_duplicate_turn": True, "direction": "in"},
                "game.user_turn",
                "in",
                {"whoseTurn": "Hero"},
            )
            return
        if tag == "action_retry":
            inj = await bridge._maybe_inject_async(dummy_event, dummy_payload, b"")
            ack = bridge.pending_action_ack
            if inj.inject_raw and ack and not bridge.ack_refresh_blocks_retry(ack):
                ack["retry_at"] = time.monotonic() - 0.01
            return
        if tag == "action_confirmed":
            if bridge.pending_action_ack:
                bridge.confirm_pending_action(source="coin-seat", action=action)
            return
        if tag in _FAILSAFE_TAGS:
            from core.verified_v1.eye_direct_proxy import keep_failsafe_pending
            bridge.autoplay.turn_by_room[ROOM] = {
                "userTurnOptions": {"7": None, "3": None},
            }
            keep_failsafe_pending(bridge)
            return
        if tag == "action_cancelled":
            bridge.pending_action_ack = None

    async def step() -> bool:
        for tag in sequence:
            await apply_tag(tag)
        late = bool(bridge.report_action_exhausted_if_due(now=time.monotonic() + 1))
        ack = bridge.pending_action_ack
        already = bool(ack and ack.get("_exhausted_reported"))
        return late or already

    return bool(asyncio.run(step()))


class ExhaustedBacktestTests(unittest.TestCase):
    def test_retry_only_sequence_still_exhausts(self):
        self.assertTrue(replay_captured_exhausted({
            "action": "FOLD",
            "nearby": ["action_retry", "action_retry", "action_retry"],
        }))

    def test_extra_timer_before_retries_does_not_exhaust(self):
        self.assertFalse(replay_captured_exhausted({
            "action": "FOLD",
            "nearby": ["hero_turn_resumed", "action_retry", "action_retry", "action_retry"],
        }))

    def test_captured_46_new_path(self):
        path = Path(__file__).parent / "fixtures" / "exhausted_46.jsonl"
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        self.assertEqual(len(rows), 46)
        after = sum(1 for row in rows if replay_captured_exhausted(row))
        self.assertLess(after, 46)


class MidHandRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_seed_is_retried_before_failsafe(self):
        bridge = LiveCoinBridge()
        bridge.active_hook_room = ROOM
        bridge.room_to_table[ROOM] = 1125959
        bridge.autoplay.turn_by_room[ROOM] = {"_turn_id": "turn-1"}
        bridge.mid_hand_recovery_grace = 0.03
        bridge.mid_hand_recovery_attempts = 3

        class Model:
            pass

        model = Model()
        calls = 0

        def snapshot():
            nonlocal calls
            calls += 1
            candidates = [("1125959:hand-1", object())] if calls >= 3 else []
            return model, 1125959, candidates

        bridge._cold_candidate_snapshot = snapshot  # type: ignore[method-assign]
        got_model, table_id, candidates, state, attempts = await bridge._wait_for_cold_seed()
        self.assertIs(got_model, model)
        self.assertEqual(table_id, 1125959)
        self.assertTrue(candidates)
        self.assertEqual(state, "recovered")
        self.assertGreaterEqual(attempts, 2)

    async def test_turn_change_cancels_stale_mid_hand_recovery(self):
        bridge = LiveCoinBridge()
        bridge.active_hook_room = ROOM
        bridge.room_to_table[ROOM] = 1125959
        bridge.autoplay.turn_by_room[ROOM] = {"_turn_id": "turn-1"}
        bridge.mid_hand_recovery_grace = 0.03
        bridge.mid_hand_recovery_attempts = 3
        bridge._cold_candidate_snapshot = lambda: (object(), 1125959, [])  # type: ignore[method-assign]

        async def change_turn():
            await asyncio.sleep(0.005)
            bridge.autoplay.turn_by_room[ROOM]["_turn_id"] = "turn-2"

        task = asyncio.create_task(change_turn())
        _model, _table_id, candidates, state, _attempts = await bridge._wait_for_cold_seed()
        await task
        self.assertFalse(candidates)
        self.assertEqual(state, "turn-changed")

    async def test_transient_empty_hand_does_not_emit_completion(self):
        session = object.__new__(LiveTableSession)
        session._closed = False
        session.device_id = "device-test"
        session.table_id = 1125959
        session.account_id = "account-test"
        session._last_hand = "live-hand"
        session._started = {"live-hand"}
        session._completed = set()
        observations = []
        snapshot = _LiveTableSnapshot(
            hand="",
            pending=False,
            phase="playing",
            coin_bb=0.02,
            game_type="NLH",
            transport_up=True,
            backend_health="green",
            backend_status="NORMAL",
            backend_message="",
            backend_hash="",
            backend_sequence=1,
            fuel_quantity=5000.0,
            fuel_rate_per_hand=1.0,
            fuel_reason_code="FUEL_OK",
            fuel_sequence=1,
            fuel_low_threshold=1500.0,
            fuel_observed=True,
        )
        session._snapshot = lambda: snapshot  # type: ignore[method-assign]
        session._sink = observations.append
        monitor = asyncio.create_task(LiveTableSession._monitor_state(session))
        await asyncio.sleep(0.28)
        session._closed = True
        await asyncio.wait_for(monitor, 1.0)
        self.assertNotIn("live-hand", session._completed)
        self.assertFalse(any(getattr(row, "kind", "") == "hand_completed" for row in observations))

    def test_unstarted_table_deal_is_not_a_history_hand(self):
        session = object.__new__(LiveTableSession)
        session._started = set()
        session._completed = set()
        session._last_hand = "orbit-only"
        session._emit_hand_completed("orbit-only", "next hand")
        self.assertNotIn("orbit-only", session._completed)


class AccountingRegressionTests(unittest.TestCase):
    def test_net_profit_keeps_folded_losers_and_gross_winner_payout(self):
        profits = compute_net_profits(
            hand_participants={1, 2, 3},
            hand_contrib={0: 100, 1: 300, 2: 300},
            raw_winner_net={},
            payout_by_seat={2: 700},
            forced_adjustment_by_seat={},
        )
        self.assertEqual(profits, {0: -100, 1: -300, 2: 400})
        self.assertEqual(sum(profits.values()), 0)

    def test_raw_winner_fallback_removes_forced_adjustment_once(self):
        profits = compute_net_profits(
            hand_participants={1, 2},
            hand_contrib={0: 50, 1: 100},
            raw_winner_net={0: 151},
            payout_by_seat={},
            forced_adjustment_by_seat={0: 1},
        )
        self.assertEqual(profits[0], 150)
        self.assertEqual(profits[1], -100)


class BombpotAccountingRegressionTests(unittest.TestCase):
    @staticmethod
    def hand(*, actual_bombpot: bool):
        class Room:
            room_name = "test"
            props = {
                "isBombpot": True,
                "bombpotInducesDoubleBoard": True,
                "_isBombpotHand": actual_bombpot,
                "_bombpotAnte": 0.04,
                "smallBlind": 0.01,
                "bigBlind": 0.02,
            }

        class Hand:
            table_id = 1125959
            room = Room()
            pre = {
                "sbSeatId": 1,
                "bbSeatId": 2,
                "sbAmount": 0.01,
                "bbAmount": 0.02,
                "anteAmount": 0,
            }
            roster = [
                {"seatId": 1, "userChips": 2.00, "isPlaying": True},
                {"seatId": 2, "userChips": 2.00, "isPlaying": True},
            ]
            events_before_turn = []

        return Hand()

    def test_room_bombpot_capability_does_not_charge_ante_on_normal_hand(self):
        bridge = LiveCoinBridge(scale=100)
        bridge.active_seats = {1, 2}
        bridge._bootstrap_hand_accounting(self.hand(actual_bombpot=False))
        self.assertEqual(dict(bridge.hand_contrib), {0: 1, 1: 2})

    def test_actual_bombpot_charges_each_active_seat_once(self):
        bridge = LiveCoinBridge(scale=100)
        bridge.active_seats = {1, 2}
        bridge._bootstrap_hand_accounting(self.hand(actual_bombpot=True))
        self.assertEqual(dict(bridge.hand_contrib), {0: 4, 1: 4})
        self.assertEqual(dict(bridge.remaining_stack), {0: 196, 1: 196})


class BridgeDiagnosticRegressionTests(unittest.TestCase):
    def test_state_errors_are_visible_and_repeat_throttled(self):
        rows = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda tag, msg, detail=None: rows.append((tag, msg, detail)))
        for _ in range(7):
            bridge._record_bridge_error(
                "state_error", "game.seat", ValueError("synthetic bad action"), room=ROOM
            )
        self.assertEqual(bridge.state_error_count, 7)
        self.assertEqual([row[2]["count"] for row in rows], [1, 2, 5])
        self.assertTrue(all(row[0] == "state_error" for row in rows))


class ActionHintFormatTests(unittest.TestCase):
    def test_fold_and_raise_layout(self):
        self.assertEqual(format_action_hint("FOLD", 0, 3726), "FOLD 0.0 3726")
        self.assertEqual(format_action_hint("RAISE", 9.37, 6024), "RAISE 9.37 6024")
        self.assertEqual(format_action_hint("FOLD ACK", None, 1), "FOLD 0.0 1")
        self.assertEqual(format_action_hint("FOLD", 0, 0, source="prefold"), "PREFOLD")
        self.assertEqual(format_action_hint("FOLD", 0, 0, source="failsafe"), "FALLBACK")
        payload = hud_action("CHECK", 0.0, 800)
        self.assertEqual(payload["text"], "CHECK 0.0 800")
        self.assertFalse(payload["sticky"])
        self.assertFalse(payload["leave"])
        self.assertEqual(hud_action("FOLD", 0.0, 0, source="prefold")["text"], "PREFOLD")
        self.assertEqual(hud_action("FOLD", 0.0, 0, source="failsafe")["text"], "FALLBACK")
        clear = hud_clear()
        self.assertTrue(clear.get("clear"))
        frame = device_hud_payload(clear, fallback_text="action confirmed via coin-seat")
        self.assertEqual(frame.get("clear"), True)
        self.assertEqual(frame.get("text"), "")
        self.assertNotIn("confirmed", str(frame.get("text") or ""))


class CoinTabSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_hides_sessionless_coin_leftovers(self):
        auto = DeviceAutomation("device-test")
        auto._tabs[1147817] = 0.0
        auto._tabs[1149077] = time.monotonic() + 40.0
        router = DeviceIngressRouter("device-test", object(), automation=auto)
        snap = await router.control_snapshot()
        ids = {int(row["table_id"]): row["state"] for row in snap["tables"]}
        self.assertNotIn(1147817, ids)
        self.assertNotIn(1149077, ids)
        self.assertEqual(auto.coin_tab_count(), 2)


def coin_hook_event(command: str, data: dict, *, room: int = 59059, mid: str = "e", direction: str = "in") -> dict:
    """Real Coin SFS hook frame — same encoding handle_event decodes."""
    inner = {"c": _Str(command), "p": _Obj({"data": _Str(json.dumps(data, separators=(",", ":")))})}
    if room is not None:
        inner["r"] = _Int(int(room))
    raw = encode_packet({"c": _Byte(1), "a": _Short(13), "p": _Obj(inner)})
    return {
        "type": "ws_message",
        "kind": "ws_message",
        "v": 4,
        "async": True,
        "id": mid,
        "direction": direction,
        "text": False,
        "url": "wss://coin",
        "ws_id": "ws",
        "payload_b64": base64.b64encode(raw).decode(),
    }


class _InstantEye:
    account_id = "709393393-20"

    async def close(self, crashed=False, reason=""):
        return None


class _InstantFactory:
    async def create(self, device_id, table_id, seed):
        return _InstantEye()


class InPlayAdmitWithoutInitTests(unittest.IsolatedAsyncioTestCase):
    def _event(self, command: str, *, table_id: int = 1155848, data: dict | None = None) -> RoutedEvent:
        return RoutedEvent(
            event={},
            payload=None,
            raw=b"",
            command=command,
            direction="in",
            room_id=59059,
            table_ids=(table_id,),
            websocket_id="ws",
            data=dict(data or {}),
        )

    async def _await_slot(self, router: DeviceIngressRouter, table_id: int):
        slot = router._sessions.get(table_id)
        self.assertIsNotNone(slot, f"no Eye slot for table {table_id}")
        self.assertIsNotNone(slot.start_task)
        await slot.start_task
        return slot

    def test_in_play_predicate_covers_turn_cards_seats_not_potinfo(self):
        self.assertTrue(in_play_enough_to_lease(self._event("game.user_turn")))
        self.assertTrue(in_play_enough_to_lease(self._event(
            "game.hole_cards", data={"playerCards": [{"suit": "HEARTS", "value": "ACE"}]}
        )))
        self.assertTrue(in_play_enough_to_lease(self._event(
            "game.seatInfo",
            data={"seatResponseDataList": [
                {"seatId": 1, "userName": "Weedman834", "userChips": 2.1},
            ]},
        )))
        self.assertTrue(in_play_enough_to_lease(self._event(
            "game.take_Seat", data={"seatId": 3, "buyinAmount": 2.0}
        )))
        self.assertFalse(in_play_enough_to_lease(self._event("game.potInfo", data={"pot": 1})))

    def test_hole_cards_bind_owner_without_game_init(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", object())
        routed = self._event("game.hole_cards", data={"playerCards": [{"suit": "SPADES", "value": "ACE"}]})
        self.assertEqual(router._owner_locked(routed), 1155848)

    async def test_handle_event_user_turn_without_init_leases_then_close_drops(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", _InstantFactory())
        event = coin_hook_event(
            "game.user_turn",
            {"whoseTurn": "Weedman834", "tableId": 42},
            mid="turn-42",
        )
        decision, _ = await router.handle_event(event)
        self.assertTrue(decision.get("router_pending"))
        self.assertNotEqual(decision.get("router_waiting_for_game_init"), True)
        slot = await self._await_slot(router, 42)
        self.assertEqual(slot.session.account_id, "709393393-20")
        await router.close_table(42, reason="operator close ghost")
        self.assertNotIn(42, router._sessions)

    async def test_handle_event_real_seatinfo_after_join_leases_without_init(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", _InstantFactory())
        table_id = 1155848
        join, _ = await router.handle_event(coin_hook_event(
            "lobby.join_game_table",
            {"tablesToJoin": [{"tableId": table_id, "tableName": f"NLH {table_id}"}]},
            mid="join",
        ))
        self.assertNotIn(table_id, router._sessions)
        self.assertTrue(join.get("router_waiting_for_game_init") or join.get("action") == "forward")
        pot, _ = await router.handle_event(coin_hook_event(
            "game.potInfo",
            {"pot": 1},
            mid="pot",
        ))
        self.assertNotIn(table_id, router._sessions)
        self.assertTrue(pot.get("router_waiting_for_game_init"))
        seat, _ = await router.handle_event(coin_hook_event(
            "game.seatInfo",
            {"seatResponseDataList": [
                {"seatId": 1, "userName": "Weedman834", "userChips": 2.1},
            ]},
            mid="seats",
        ))
        self.assertTrue(seat.get("router_pending"))
        self.assertNotEqual(seat.get("router_waiting_for_game_init"), True)
        slot = await self._await_slot(router, table_id)
        self.assertEqual(slot.session.account_id, "709393393-20")
        await router.close_table(table_id, reason="operator close ghost")
        self.assertNotIn(table_id, router._sessions)

    async def test_handle_event_take_seat_after_join_leases_without_init(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", _InstantFactory())
        table_id = 42
        await router.handle_event(coin_hook_event(
            "lobby.join_game_table",
            {"tablesToJoin": [{"tableId": table_id, "tableName": f"NLH {table_id}"}]},
            mid="join-take",
        ))
        take, _ = await router.handle_event(coin_hook_event(
            "game.take_Seat",
            {"seatId": 3, "buyinAmount": 2.0},
            mid="take",
        ))
        self.assertTrue(take.get("router_pending"))
        slot = await self._await_slot(router, table_id)
        self.assertEqual(slot.session.account_id, "709393393-20")
        await router.close_table(table_id, reason="leave")
        self.assertNotIn(table_id, router._sessions)


class EmptyStartingSlotSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """Weedman admin «Стол 5» empty card: slot exists, Eye create never returned."""

    async def test_sessionless_timeout_row_omits_hand_fields(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", object())
        earlier = (864838, 1154882, 1153990, 1154573)
        for order, table_id in enumerate(earlier, 1):
            router._sessions[table_id] = _SessionSlot(
                table_id=table_id,
                created_order=order,
                buffer=collections.deque(),
            )
        failing = _SessionSlot(
            table_id=1154453,
            created_order=5,
            buffer=collections.deque(),
        )
        failing.startup_error = f"{TimeoutError.__name__}: {TimeoutError()}"
        failing.startup_attempts = 2
        failing.buffer.append({
            "command": "game.hole_cards",
            "data": {"playerCards": [{"suit": "HEARTS", "value": "ACE"}]},
        })
        router._sessions[1154453] = failing

        snap = await router.control_snapshot()
        row = next(item for item in snap["tables"] if int(item["table_id"]) == 1154453)

        self.assertEqual(int(row["table_no"]), 5)
        self.assertEqual(row["state"], "starting")
        self.assertEqual(row["startup_error"], "TimeoutError: ")
        self.assertEqual(row["account_id"], "")
        self.assertNotIn("phase", row)
        self.assertNotIn("hand_id", row)
        self.assertNotIn("hole_cards", row)
        self.assertNotIn("backend_status", row)
        self.assertNotIn("backend_health", row)
        self.assertNotIn("backend_message", row)
        self.assertFalse(row.get("hero_sitting"))
        self.assertFalse(row.get("pending_action"))

    async def test_restart_without_seed_drops_hung_slot(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", object())
        slot = _SessionSlot(
            table_id=1152003,
            created_order=1,
            buffer=collections.deque(),
        )
        slot.startup_error = "TimeoutError: "
        router._sessions[1152003] = slot
        self.assertTrue(await router.restart_table_backend(1152003))
        self.assertNotIn(1152003, router._sessions)

    def test_sessionless_slot_is_not_live_session(self):
        router = DeviceIngressRouter("dev-a8dc8554e1cf4bddb25db98b6970679a", object())
        slot = _SessionSlot(
            table_id=1152003,
            created_order=1,
            buffer=collections.deque(),
        )
        router._sessions[1152003] = slot
        self.assertEqual(router.active_table_ids, (1152003,))
        self.assertFalse(router.table_has_live_session(1152003))

    async def test_stale_reaper_drops_sessionless_startup_slot(self):
        router = DeviceIngressRouter(
            "dev-a8dc8554e1cf4bddb25db98b6970679a",
            object(),
            startup_stale_seconds=0.01,
        )
        slot = _SessionSlot(
            table_id=1154453,
            created_order=1,
            buffer=collections.deque(),
        )
        slot.created_at = time.monotonic() - 1.0
        slot.startup_error = "TimeoutError: "
        router._sessions[1154453] = slot
        reaped = await router.reap_stale_startups()
        self.assertEqual(reaped, (1154453,))
        self.assertNotIn(1154453, router._sessions)


class OpenHintGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_open_hint_clears_pending(self):
        sent: list[str] = []
        bridge = LiveCoinBridge()
        bridge.state["_pending_finish_hint"] = 42

        async def fake_cmd(cmd, body, location="TABLE", envelope_uid=None):
            sent.append(str(cmd))

        async def fake_outer(frame, label=""):
            sent.append(str(label or frame))

        bridge.eye_send_cmd = fake_cmd  # type: ignore[method-assign]
        bridge.eye_send_outer = fake_outer  # type: ignore[method-assign]
        self.assertTrue(await bridge._close_open_hint("dealer_cards"))
        self.assertIsNone(bridge.state["_pending_finish_hint"])
        self.assertTrue(any("FinishRoundHint" in item for item in sent))

    async def test_close_open_hint_noop_when_none(self):
        bridge = LiveCoinBridge()
        bridge.state["_pending_finish_hint"] = None
        self.assertFalse(await bridge._close_open_hint("dealer_cards"))


class FinishAfterCcTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_after_expired_hint_deadline_still_sends(self):
        """CC wait outlives the 2.5s connect window. Finish must not inherit it."""
        sent: list[str] = []
        bridge = LiveCoinBridge()
        bridge.state["_pending_finish_hint"] = 42
        bridge._eye_send_deadline = time.monotonic() - 8.0

        async def fake_ensure(*, deadline=None):
            if deadline is not None and time.monotonic() >= float(deadline):
                raise TimeoutError("eye connect deadline")

        async def fake_generation(frame, label="", generation=0):
            sent.append(str(label or frame))

        bridge.ensure_eye = fake_ensure  # type: ignore[method-assign]
        bridge._eye_send_outer_generation = fake_generation  # type: ignore[method-assign]
        self.assertTrue(await bridge.finish_hint(42))
        self.assertTrue(any("FinishRoundHint" in item for item in sent))
        self.assertIsNone(bridge.state["_pending_finish_hint"])

    async def test_failed_finish_keeps_pending_so_retry_can_close_eye(self):
        bridge = LiveCoinBridge()
        bridge.state["_pending_finish_hint"] = 42

        async def boom(*_args, **_kwargs):
            raise TimeoutError("eye connect deadline")

        bridge.eye_send_outer = boom  # type: ignore[method-assign]
        bridge.eye_send_cmd = boom  # type: ignore[method-assign]
        with self.assertRaises(TimeoutError):
            await bridge.finish_hint(42)
        self.assertEqual(bridge.state["_pending_finish_hint"], 42)


class ForcedExitDummyGuardTests(unittest.TestCase):
    def test_leave_does_not_steal_other_table_dummy(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 11
        room = 100
        incoming = RoutedEvent(
            event={"id": "leave"}, payload=None, raw=b"", command="game.leave_Seat",
            direction="out", room_id=room, table_ids=(table_id,), websocket_id="game-ws",
            data={},
        )
        router._ensure_unsupported_exit_locked(
            table_id, incoming, reset=True, exit_kind="policy",
        )
        router._ws_to_tables["game-ws"] = {table_id}
        other = RoutedEvent(
            event={"id": "dummy"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=-1, table_ids=(), websocket_id="other-ws",
            data={},
        )
        skipped = router._forced_exit_decision_locked(other, table_id)
        self.assertEqual(skipped.get("action"), "forward")
        own = RoutedEvent(
            event={"id": "dummy2"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=room, table_ids=(table_id,), websocket_id="game-ws",
            data={},
        )
        taken = router._forced_exit_decision_locked(own, table_id)
        self.assertEqual(taken.get("action"), "schedule_send")

    def test_policy_and_leave_all_standup_never_queue_quit_table(self):
        router = DeviceIngressRouter("device-test", object())
        room = 100
        incoming = RoutedEvent(
            event={"id": "pol"}, payload=None, raw=b"", command="game.leave_Seat",
            direction="out", room_id=room, table_ids=(11,), websocket_id="game-ws",
            data={},
        )
        router._ensure_unsupported_exit_locked(
            11, incoming, reset=True, exit_kind="policy",
        )
        stages = [row.get("stage") for row in router._unsupported_exit[11]["queue"]]
        self.assertNotIn("LEAVE", stages)
        self.assertNotIn("QUIT", stages)
        self.assertIn("STANDUP", stages)
        for row in router._unsupported_exit[11]["queue"]:
            raw = row.get("raw")
            if not raw:
                continue
            packet = decode_packet(bytes(raw))
            self.assertEqual(packet["p"]["c"], "game.leave_Seat")
            self.assertNotEqual(packet["p"]["c"], "game.quit_table")
        router._unsupported_exit.pop(11, None)
        router._ensure_unsupported_exit_locked(
            11, incoming, reset=True, exit_kind="leave_all",
        )
        stages = [row.get("stage") for row in router._unsupported_exit[11]["queue"]]
        self.assertEqual(stages, ["STANDUP"])
        standup = router._unsupported_exit[11]["queue"][0]
        packet = decode_packet(bytes(standup["raw"]))
        self.assertEqual(packet["p"]["c"], "game.leave_Seat")


class SharedWsDummyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_ws_dummy_prefers_sibling_cc_over_forced_exit(self):
        class LiveWithCc(LiveTableSession):
            def __init__(self, table_id: int, ws_id: str):
                self.table_id = table_id
                self.play_enabled = True
                self._closed = False
                self._lock = asyncio.Lock()
                self._bridge = LiveCoinBridge()
                self._arbiter_cancellations = collections.deque()
                self._arbiter_dispatch_context = {}
                self._bridge.autoplay.pending = {
                    "due": time.monotonic() - 1,
                    "raw": b"cc",
                    "room": 100,
                    "ws_id": ws_id,
                    "hand_id": "h1",
                    "turn_id": "t1",
                    "fallback": True,
                    "extra_urgent": True,
                    "action": "FOLD",
                }
                self._bridge.state["hand_id"] = "h1"
                self._bridge.autoplay.turn_by_room[100] = {"_turn_id": "t1"}

            async def handle_event(self, event):
                return {"id": event.get("id", ""), "action": "replace", "cc": True}, None

            def prepare_action_dispatch(self, plan):
                return True

            def finalize_action_dispatch(self, plan, decision):
                return True

        ws = "aabbccdd"
        router = DeviceIngressRouter("device-test", object())
        live = LiveWithCc(11, ws)
        router._sessions[11] = _SessionSlot(
            table_id=11, created_order=1, buffer=collections.deque(),
        )
        router._sessions[11].session = live
        router._ws_to_tables[ws] = {11, 22}
        router._room_to_table[100] = 11
        evidence = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=200, table_ids=(22,), websocket_id=ws,
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ensure_unsupported_exit_locked(
            22, evidence, reset=True, exit_kind="unsupported",
        )
        dummy = coin_hook_event("lobby.dummy", {}, room=-1, mid="dummy-shared")
        dummy["ws_id"] = ws
        dummy["direction"] = "out"
        dummy["v"] = 6
        decision, _ = await router.handle_event(dummy)
        op = decision.get("_operator_action") or {}
        self.assertEqual(decision.get("action"), "schedule_send")
        self.assertEqual(int(op.get("table_id") or 0), 22)
        self.assertIn(str(op.get("action") or ""), {"CHECK", "FOLD", "CHECKFOLD"})

    async def test_delayed_sibling_does_not_block_db_checkfold(self):
        class LiveDelayed(LiveTableSession):
            def __init__(self, table_id: int, ws_id: str):
                self.table_id = table_id
                self.play_enabled = True
                self._closed = False
                self._lock = asyncio.Lock()
                self._bridge = LiveCoinBridge()
                self._arbiter_cancellations = collections.deque()
                self._arbiter_dispatch_context = {}
                self._bridge.autoplay.pending = {
                    "due": time.monotonic() + 4.5,
                    "raw": b"cc",
                    "room": 100,
                    "ws_id": ws_id,
                    "hand_id": "h1",
                    "turn_id": "t1",
                    "action": "CHECK",
                    "_arbiter_ready_at": time.monotonic() + 4.5,
                }
                self._bridge.state["hand_id"] = "h1"
                self._bridge.autoplay.turn_by_room[100] = {"_turn_id": "t1"}

            async def handle_event(self, event):
                return {"id": event.get("id", ""), "action": "replace"}, None

            def prepare_action_dispatch(self, plan):
                return True

            def finalize_action_dispatch(self, plan, decision):
                return True

        ws = "aabbccdd"
        router = DeviceIngressRouter("device-test", object())
        live = LiveDelayed(11, ws)
        router._sessions[11] = _SessionSlot(
            table_id=11, created_order=1, buffer=collections.deque(),
        )
        router._sessions[11].session = live
        router._ws_to_tables[ws] = {11, 22}
        router._room_to_table[200] = 22
        evidence = RoutedEvent(
            event={"id": "db", "v": 6}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=200, table_ids=(22,), websocket_id=ws,
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ensure_unsupported_exit_locked(
            22, evidence, reset=True, exit_kind="unsupported",
        )
        dummy = coin_hook_event("lobby.dummy", {}, room=-1, mid="dummy-db")
        dummy["ws_id"] = "lobby-ws"
        dummy["direction"] = "out"
        dummy["v"] = 6
        decision, _ = await router.handle_event(dummy)
        op = decision.get("_operator_action") or {}
        self.assertEqual(decision.get("action"), "schedule_send")
        self.assertEqual(int(op.get("table_id") or 0), 22)
        self.assertIn(str(op.get("action") or ""), {"CHECK", "FOLD", "CHECKFOLD"})


class PlayAndCloseGuardTests(unittest.TestCase):
    def test_play_off_yields_no_action_offers(self):
        session = LiveTableSession.__new__(LiveTableSession)
        session.play_enabled = False
        self.assertEqual(session.action_offers({}), ())

    def test_policy_leave_refuses_foreign_room(self):
        router = DeviceIngressRouter("device-test", object())
        router._room_to_table[100] = 11
        ok = router._queue_policy_leave_locked(22, room=100, ws_id="aabb", kind="policy", reason="x")
        self.assertFalse(ok)
        self.assertNotIn(22, router._unsupported_exit)

    def test_policy_leave_skips_recent_closed_id(self):
        router = DeviceIngressRouter("device-test", object())
        router._room_to_table[100] = 11
        router._recent_closed[11] = time.monotonic() + 60
        ok = router._queue_policy_leave_locked(11, room=100, ws_id="aabb", kind="policy", reason="x")
        self.assertFalse(ok)

    def test_sitting_table_is_not_dead_reaped(self):
        session = LiveTableSession.__new__(LiveTableSession)
        session._bridge = SimpleNamespace(hero_sitting=True)
        session._backend_red_since = 1.0
        self.assertIsNone(session.take_dead_table_request())
        self.assertEqual(session._backend_red_since, 0.0)

    def test_live_dump_is_not_ui_leave_confirmed(self):
        self.assertFalse(DeviceIngressRouter.ui_leave_confirmed(
            {"closed": False, "waitlist": False, "tap": "focus-shortest-timer"}
        ))
        self.assertFalse(DeviceIngressRouter.ui_leave_confirmed(
            {"closed": True, "tap": "quit_table"}
        ))
        self.assertFalse(DeviceIngressRouter.ui_leave_confirmed(
            {"tap": "leave"}
        ))
        self.assertTrue(DeviceIngressRouter.ui_leave_confirmed(
            {"closed": True, "tap": "confirm-exit-table"}
        ))

    def test_observer_table_is_not_dead_reaped(self):
        session = LiveTableSession.__new__(LiveTableSession)
        session._bridge = SimpleNamespace(hero_sitting=False, context_active=True)
        session._backend_red_since = time.monotonic() - 121
        session._snapshot = lambda: SimpleNamespace(
            backend_health="red", backend_message="NO_TRAFFIC_FROM_ROOM", phase="table",
        )
        self.assertIsNone(session.take_dead_table_request())
        self.assertEqual(session._backend_red_since, 0.0)

    def test_live_phase_is_not_dead_reaped(self):
        session = LiveTableSession.__new__(LiveTableSession)
        session._bridge = SimpleNamespace(hero_sitting=False, context_active=False)
        session._backend_red_since = time.monotonic() - 121
        session._snapshot = lambda: SimpleNamespace(
            backend_health="red", backend_message="NO_TRAFFIC_FROM_ROOM", phase="observe",
        )
        self.assertIsNone(session.take_dead_table_request())


class DoubleBoardExitTests(unittest.TestCase):
    def test_double_board_does_not_mark_sibling_table_on_shared_ws(self):
        router = DeviceIngressRouter("device-test", object())
        live = 11
        db = 22
        router._sessions[live] = _SessionSlot(table_id=live, created_order=1, buffer=__import__("collections").deque())
        router._sessions[db] = _SessionSlot(table_id=db, created_order=2, buffer=__import__("collections").deque())
        router._room_to_table[100] = live
        router._ws_to_tables["aabbccdd"] = {live, db}
        evidence = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=200, table_ids=(db,), websocket_id="aabbccdd",
            data={"dealerCardsDoubleBoard": [1]},
        )
        definite = router._definite_table_locked(evidence)
        self.assertEqual(definite, db)
        guessed = router._owner_locked(evidence)
        self.assertEqual(guessed, 0)

    def test_checkfold_does_not_copy_other_table_options(self):
        router = DeviceIngressRouter("device-test", object())
        name, raw = router._checkfold_raw_locked(11, 200)
        self.assertEqual(name, "FOLD")
        self.assertTrue(raw)

    def test_forced_exit_waits_for_coin_ack_before_standup(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        incoming = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ensure_unsupported_exit_locked(
            table_id, incoming, reset=True, exit_kind="unsupported",
        )
        dummy = RoutedEvent(
            event={"id": "dummy"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=-1, table_ids=(), websocket_id="aabbccdd",
            data={},
        )
        first = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(first.get("action"), "schedule_send")
        self.assertIn(str(first.get("_operator_action", {}).get("action")), {"CHECK", "FOLD"})
        waiting = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(waiting.get("action"), "forward")
        ack = RoutedEvent(
            event={"id": "ua"}, payload=None, raw=b"", command="game.user_action",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"isSuccess": True},
        )
        self.assertTrue(router._forced_coin_ack_locked(ack, table_id))
        second = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(second.get("action"), "schedule_send")
        self.assertEqual(str(second.get("_operator_action", {}).get("action")), "STANDUP")

    def test_double_board_preempts_policy_standup(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        policy = RoutedEvent(
            event={"id": "leave"}, payload=None, raw=b"", command="game.leave_Seat",
            direction="out", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={},
        )
        router._ensure_unsupported_exit_locked(
            table_id, policy, reset=True, exit_kind="policy",
        )
        self.assertEqual(router._unsupported_exit[table_id]["kind"], "policy")
        evidence = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ensure_unsupported_exit_locked(
            table_id, evidence, reset=True, exit_kind="unsupported",
        )
        self.assertEqual(router._unsupported_exit[table_id]["kind"], "unsupported")
        dummy = RoutedEvent(
            event={"id": "dummy"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=-1, table_ids=(), websocket_id="aabbccdd",
            data={},
        )
        first = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(first.get("action"), "schedule_send")
        self.assertIn(str(first.get("_operator_action", {}).get("action")), {"CHECK", "FOLD"})

    def test_policy_standup_does_not_freeze_other_tables(self):
        router = DeviceIngressRouter("device-test", object())
        router._unsupported_exit[11] = {
            "kind": "policy",
            "queue": __import__("collections").deque(),
            "inflight": {"tok": {"stage": "STANDUP"}},
            "awaiting_coin": True,
            "wait_ui": False,
        }
        self.assertFalse(router._exit_blocks_other_play_locked())
        router._unsupported_exit[22] = {
            "kind": "unsupported",
            "queue": __import__("collections").deque(),
            "inflight": {},
            "wait_ui": False,
        }
        self.assertFalse(router._exit_blocks_other_play_locked())

    def test_forced_exit_stops_at_standup_without_quit_table(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        incoming = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ws_to_tables["aabbccdd"] = {table_id}
        router._ensure_unsupported_exit_locked(
            table_id, incoming, reset=True, exit_kind="unsupported",
        )
        stages = [row.get("stage") for row in router._unsupported_exit[table_id]["queue"]]
        self.assertIn("CHECKFOLD", stages)
        self.assertIn("STANDUP", stages)
        self.assertNotIn("LEAVE", stages)
        dummy = RoutedEvent(
            event={"id": "dummy"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=-1, table_ids=(), websocket_id="aabbccdd",
            data={},
        )
        first = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(first.get("action"), "schedule_send")
        ack = RoutedEvent(
            event={"id": "ua"}, payload=None, raw=b"", command="game.user_action",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"isSuccess": True},
        )
        self.assertTrue(router._forced_coin_ack_locked(ack, table_id))
        second = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(second.get("action"), "schedule_send")
        self.assertEqual(str(second.get("_operator_action", {}).get("action")), "STANDUP")
        stand = RoutedEvent(
            event={"id": "ls"}, payload=None, raw=b"", command="game.leave_Seat",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"isSuccess": True},
        )
        self.assertTrue(router._forced_coin_ack_locked(stand, table_id))
        third = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(third.get("action"), "forward")
        self.assertFalse(router._unsupported_exit[table_id].get("wait_ui"))
        self.assertTrue(router._unsupported_exit[table_id].get("done"))

    def test_late_owner_does_not_guess_while_join_in_flight(self):
        router = DeviceIngressRouter("device-test", object())
        auto = DeviceAutomation("device-test")
        auto._joining = True
        router.automation = auto
        router._provisional[1149459] = {"updated": time.monotonic(), "reason": "allocated"}
        routed = RoutedEvent(
            event={"id": "seat"}, payload=None, raw=b"", command="game.seatInfo",
            direction="in", room_id=53789, table_ids=(), websocket_id="0d00c38f",
            data={},
        )
        table_id, reason = router._late_owner_locked(routed)
        self.assertEqual(table_id, 0)
        self.assertEqual(reason, "")

    def test_native_v6_dummy_carries_db_checkfold_from_any_ws(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        incoming = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="game-ws",
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ws_to_tables["game-ws"] = {table_id}
        router._ensure_unsupported_exit_locked(
            table_id, incoming, reset=True, exit_kind="unsupported",
        )
        dummy = RoutedEvent(
            event={"id": "dummy", "v": 6, "ws_id": "lobby-ws"}, payload=None, raw=b"",
            command="lobby.dummy", direction="out", room_id=-1, table_ids=(),
            websocket_id="lobby-ws", data={},
        )
        decision = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(decision.get("action"), "schedule_send")
        self.assertIn(str(decision.get("_operator_action", {}).get("action")), {"CHECK", "FOLD"})
        self.assertTrue(bytes(base64.b64decode(decision.get("payload_b64") or b"")))

    def test_db_keeps_pending_fold_extra_urgent(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        session = LiveTableSession.__new__(LiveTableSession)
        session._bridge = LiveCoinBridge()
        session._bridge.autoplay.pending = {
            "action": "FOLD", "due": time.monotonic() + 4.0, "delay_ms": 4000, "raw": b"x",
        }
        router._sessions[table_id] = _SessionSlot(
            table_id=table_id, created_order=1, buffer=collections.deque(),
        )
        router._sessions[table_id].session = session
        incoming = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ensure_unsupported_exit_locked(
            table_id, incoming, reset=True, exit_kind="unsupported",
        )
        pending = session._bridge.autoplay.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "FOLD")
        self.assertTrue(pending.get("extra_urgent"))
        self.assertTrue(pending.get("fallback"))
        self.assertEqual(int(pending["delay_ms"]), 0)

    def test_release_clears_db_fallback_pending(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        session = LiveTableSession.__new__(LiveTableSession)
        session._bridge = LiveCoinBridge()
        session._bridge.autoplay.pending = {
            "action": "FOLD",
            "fallback": True,
            "extra_urgent": True,
            "delay_ms": 0,
            "raw": b"x",
        }
        router._sessions[table_id] = _SessionSlot(
            table_id=table_id, created_order=1, buffer=collections.deque(),
        )
        router._sessions[table_id].session = session
        router._unsupported_tables[table_id] = "stale"
        router._unsupported_room_reasons[room] = "stale"
        router._unsupported_exit[table_id] = {
            "kind": "unsupported",
            "room": room,
            "queue": collections.deque(),
            "inflight": {},
        }
        router._sink = lambda obs: None
        self.assertTrue(router._release_stale_double_board_locked(table_id))
        self.assertIsNone(session._bridge.autoplay.pending)

    def test_new_hand_without_second_board_drops_warning_db(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        router._unsupported_tables[table_id] = "stale"
        router._unsupported_room_reasons[room] = "stale"
        router._unsupported_exit[table_id] = {
            "kind": "unsupported",
            "room": room,
            "queue": collections.deque(),
            "inflight": {},
        }
        router._room_to_table[room] = table_id
        router._sessions[table_id] = _SessionSlot(
            table_id=table_id, created_order=1, buffer=collections.deque(),
        )
        dropped = []
        router._sink = lambda obs: dropped.append(obs)
        router._release_stale_double_board_locked(table_id)
        self.assertNotIn(table_id, router._unsupported_tables)
        self.assertNotIn(room, router._unsupported_room_reasons)
        self.assertNotIn(table_id, router._unsupported_exit)
        self.assertTrue(dropped)
        self.assertTrue((dropped[-1].detail or {}).get("hud", {}).get("clear"))

    def test_extratimer_on_db_table_arms_fold(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        session = LiveTableSession.__new__(LiveTableSession)
        session._bridge = LiveCoinBridge()
        session._bridge.active_hook_room = room
        session._bridge.autoplay.turn_by_room[room] = {
            "userTurnOptions": {"4": [0.03], "7": None},
            "_turn_id": "t-extra",
            "_ws_id": "aabbccdd",
        }
        router._sessions[table_id] = _SessionSlot(
            table_id=table_id, created_order=1, buffer=collections.deque(),
        )
        router._sessions[table_id].session = session
        router._unsupported_tables[table_id] = "DOUBLE BOARD"
        router._unsupported_exit[table_id] = {
            "kind": "unsupported",
            "room": room,
            "ws_id": "aabbccdd",
            "queue": collections.deque([{"stage": "CHECKFOLD", "attempt": 1}]),
            "inflight": {"tok": {"stage": "CHECKFOLD", "attempt": 1}},
            "awaiting_coin": True,
            "next_at": time.monotonic() + 9,
        }
        router._arm_db_checkfold_locked(table_id)
        router._unstick_db_checkfold_locked(table_id)
        pending = session._bridge.autoplay.pending
        self.assertEqual(pending["action"], "FOLD")
        self.assertTrue(pending.get("extra_urgent"))
        self.assertEqual(int(pending["delay_ms"]), 0)
        self.assertFalse(router._unsupported_exit[table_id].get("inflight"))

    def test_checkfold_injects_on_dummy_not_incoming(self):
        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        incoming = RoutedEvent(
            event={"id": "db"}, payload=None, raw=b"", command="game.dealer_cards",
            direction="in", room_id=room, table_ids=(table_id,), websocket_id="aabbccdd",
            data={"dealerCardsDoubleBoard": [1]},
        )
        router._ensure_unsupported_exit_locked(
            table_id, incoming, reset=True, exit_kind="unsupported",
        )
        decision = router._forced_exit_decision_locked(incoming, table_id)
        self.assertEqual(decision.get("action"), "forward")
        dummy = RoutedEvent(
            event={"id": "dummy"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=-1, table_ids=(), websocket_id="aabbccdd",
            data={},
        )
        decision = router._forced_exit_decision_locked(dummy, table_id)
        self.assertEqual(decision.get("action"), "schedule_send")
        self.assertIn(str(decision.get("_operator_action", {}).get("action")), {"CHECK", "FOLD"})

    def test_recent_db_table_standups_without_checkfold(self):
        from core.v6router import db_recent
        import tempfile
        path = Path(tempfile.mkdtemp()) / "db_recent.json"
        db_recent.configure(path)
        self.addCleanup(lambda: db_recent.configure())
        store = db_recent.store()
        store.remember(1149001, device_id="device-test")
        self.assertTrue(store.blocked(1149001))
        router = DeviceIngressRouter("device-test", object())
        incoming = RoutedEvent(
            event={"id": "init"}, payload=None, raw=b"", command="game.game_init",
            direction="in", room_id=ROOM, table_ids=(1149001,), websocket_id="aabbccdd",
            data={},
        )
        router._ensure_unsupported_exit_locked(
            1149001, incoming, reset=True, exit_kind="unsupported",
            detail={"recent": True},
        )
        stages = [row.get("stage") for row in router._unsupported_exit[1149001]["queue"]]
        self.assertEqual(stages, ["STANDUP"])
        dummy = RoutedEvent(
            event={"id": "dummy"}, payload=None, raw=b"", command="lobby.dummy",
            direction="out", room_id=-1, table_ids=(), websocket_id="aabbccdd",
            data={},
        )
        first = router._forced_exit_decision_locked(dummy, 1149001)
        self.assertEqual(first.get("action"), "schedule_send")
        self.assertEqual(str(first.get("_operator_action", {}).get("action")), "STANDUP")

    def test_snapshot_marks_db_warning(self):
        router = DeviceIngressRouter("device-test", object())
        router._sessions[11] = _SessionSlot(table_id=11, created_order=1, buffer=__import__("collections").deque())
        router._unsupported_tables[11] = "DOUBLE BOARD"

        async def inner():
            return await router.control_snapshot()

        snap = asyncio.run(inner())
        self.assertEqual(snap.get("warning"), "DB")
        self.assertEqual(snap["tables"][0].get("warning"), "DB")
        self.assertEqual(snap["tables"][0].get("warning_text"), "Warning: DB")


class UnsupportedRoomLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_table_cannot_poison_reused_room_id(self):
        class Session:
            async def close(self, *, crashed=False, reason="closed"):
                return None

        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        router._sessions[table_id] = _SessionSlot(
            table_id=table_id,
            created_order=1,
            buffer=__import__("collections").deque(),
            session=Session(),
        )
        router._room_to_table[room] = table_id
        router._unsupported_tables[table_id] = "real second board"
        router._unsupported_room_reasons[room] = "real second board"
        router._closing_rooms[room] = table_id
        await router.close_table(table_id, reason="test")
        self.assertNotIn(room, router._unsupported_room_reasons)
        self.assertNotIn(room, router._closing_rooms)
        self.assertIn(table_id, router._unsupported_tables)

    async def test_extratimer_empty_preflop_drops_inherited_warning_db(self):
        from core.v6router.router import _forward

        class _PassthroughSession:
            async def handle_event(self, event):
                return _forward(event), None

            def arbitration_action_ids(self):
                return set()

            def take_low_stack_exit_request(self):
                return None

        router = DeviceIngressRouter("device-test", object())
        table_id = 1125959
        room = ROOM
        router._room_to_table[room] = table_id
        router._unsupported_tables[table_id] = "stale previous hand"
        router._unsupported_room_reasons[room] = "stale previous hand"
        router._unsupported_exit[table_id] = {
            "kind": "unsupported",
            "room": room,
            "queue": collections.deque([{"stage": "CHECKFOLD", "attempt": 1}]),
            "inflight": {},
        }
        router._sessions[table_id] = _SessionSlot(
            table_id=table_id,
            created_order=1,
            buffer=collections.deque(),
            session=_PassthroughSession(),
        )
        dropped = []
        router._sink = lambda obs: dropped.append(obs)
        decision, _ = await router.handle_event(coin_hook_event(
            "game.user_turn",
            {
                "timerName": "extraTimer",
                "whoseTurn": "Weedman834",
                "tableId": table_id,
                "dealerCards": {"FLOP": [], "TURN": [], "RIVER": []},
                "dealerCardsDoubleBoard": {"FLOP": [], "TURN": [], "RIVER": []},
                "isDoubleBoard": True,
                "userTurnOptions": {"4": [0.03], "7": None},
            },
            room=room,
            mid="extra-preflop",
        ))
        self.assertNotIn(table_id, router._unsupported_tables)
        self.assertNotIn(room, router._unsupported_room_reasons)
        self.assertNotIn(table_id, router._unsupported_exit)
        self.assertEqual(decision.get("action"), "forward")
        self.assertTrue(any(
            "no second board" in str(getattr(obs, "reason", "") or "")
            for obs in dropped
        ))


class ColdReplayRoomWindowTests(unittest.TestCase):
    @staticmethod
    def _event(idx, name, data, *, hand_id=None, table_id=None, room=ROOM):
        return ppp.CoinEvent(
            idx=idx, raw={}, body=b"", name=name, data=data,
            hand_id=hand_id, table_id=table_id, hook_room=room,
        )

    def test_actions_without_repeated_hand_id_stay_in_room_scoped_hand(self):
        hand_id = 112595900001
        table_id = ppp.table_id_from_hand(hand_id)
        events = [
            self._event(
                0, None,
                {"userId": 1, "userName": "Hero", "sessionId": "s"},
                room=None,
            ),
            self._event(
                1, "game.wait_list_data",
                {"tableId": table_id, "configId": 7},
            ),
            self._event(
                2, "game.pre_hand_start_info",
                {
                    "gameId": hand_id, "dealerSeatId": 1,
                    "sbSeatId": 1, "bbSeatId": 2,
                    "sbAmount": 0.01, "bbAmount": 0.02,
                    "anteAmount": 0, "initTimeStamp": 1_700_000_000_000,
                },
                hand_id=hand_id, table_id=table_id,
            ),
            self._event(
                3, "game.seatInfo",
                {"seatResponseDataList": [
                    {"userId": 1, "userName": "Hero", "seatId": 1,
                     "userChips": 2.00, "isPlaying": True},
                    {"userId": 2, "userName": "Villain", "seatId": 2,
                     "userChips": 2.00, "isPlaying": True},
                ]},
            ),
            self._event(
                4, "game.player_info",
                {"playerCards": [
                    {"rank": "ACE", "suit": "SPADES"},
                    {"rank": "KING", "suit": "HEARTS"},
                ]},
                hand_id=hand_id, table_id=table_id,
            ),
            # Real Coin action packets commonly omit gameHandId.  The old replay
            # discarded this villain bet and then reported a call/turn mismatch.
            self._event(
                5, "game.seat",
                {"userId": 2, "userName": "Villain", "seatId": 2,
                 "caption": "Raise", "newCaption": "Bet",
                 "lastAction": "Raise", "betAmout": 0.06,
                 "userChips": 1.94},
            ),
            self._event(
                6, "game.user_turn",
                {"whoseTurn": "Hero", "callAmount": 0.05,
                 "turnTime": 15, "userTurnOptions": {"4": [0.05], "7": None}},
            ),
        ]
        model = ppp.CoinCaptureModel(events)
        hand = model.build_hand(hand_id)
        villain_rows = [
            e for e in hand.events_before_turn
            if e.name == "game.seat" and e.data.get("userName") == "Villain"
        ]
        self.assertEqual(len(villain_rows), 1)
        self.assertEqual(normalized_coin_seat_action(villain_rows[0].data), "BET")

    def test_interrupted_hand_cannot_borrow_next_hand_hero_turn(self):
        first = 112595900001
        second = 112595900002
        table_id = ppp.table_id_from_hand(first)
        events = [
            self._event(
                0, "game.pre_hand_start_info", {"gameId": first},
                hand_id=first, table_id=table_id,
            ),
            self._event(
                1, "game.pre_hand_start_info", {"gameId": second},
                hand_id=second, table_id=table_id,
            ),
            self._event(
                2, "game.user_turn", {"whoseTurn": "Hero"},
                room=ROOM,
            ),
        ]
        model = object.__new__(ppp.CoinCaptureModel)
        model.events = events
        model.hero_name = "Hero"
        model.hook_rooms_by_table = {table_id: {ROOM}}
        candidates = ppp.CoinCaptureModel.candidate_hands(model)
        self.assertNotIn(first, [row[0] for row in candidates])
        self.assertIn(second, [row[0] for row in candidates])


if __name__ == "__main__":
    unittest.main()
