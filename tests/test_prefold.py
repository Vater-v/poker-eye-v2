from __future__ import annotations

import time
import unittest

from core.verified_v1.coin_bridge_live import LiveCoinBridge
from core.verified_v1.prefold import (
    PrefoldContext,
    canonical_nlh_hand,
    default_nlh_prefold_config,
    evaluate_prefold,
    facing_from_street,
    parse_prefold_config,
    position_from_seats,
)


class PrefoldChartTests(unittest.TestCase):
    def test_canonical_hand_and_positions(self):
        self.assertEqual(
            canonical_nlh_hand(
                ({"value": "SEVEN", "suit": "HEARTS"}, {"value": "TWO", "suit": "CLUBS"})
            ),
            "72o",
        )
        self.assertEqual(position_from_seats(1, 1, [1, 2, 3, 4, 5, 6]), "BTN")
        self.assertEqual(position_from_seats(2, 1, [1, 2, 3, 4, 5, 6]), "SB")
        self.assertEqual(position_from_seats(3, 1, [1, 2, 3, 4, 5, 6]), "BB")
        self.assertEqual(position_from_seats(4, 1, [1, 2, 3, 4, 5, 6]), "UTG")
        self.assertEqual(facing_from_street({0: 1, 1: 2, 2: 3}, hero_ppp_seat=4, bb_chips=2), "RAISE")

    def test_default_chart_folds_utg_trash_without_asking_eye_for_check(self):
        config = default_nlh_prefold_config()
        decision = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="UTG",
                facing="UNOPENED",
                hole_cards=({"value": "SEVEN", "suit": "SPADES"}, {"value": "TWO", "suit": "HEARTS"}),
            ),
        )
        self.assertTrue(decision.matched)
        self.assertEqual(decision.recommended_action, "FOLD")
        self.assertTrue(decision.bypass_ai)
        self.assertFalse(decision.audit_only)

        check = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="BB",
                facing="UNOPENED",
                hole_cards=({"value": "SEVEN", "suit": "SPADES"}, {"value": "TWO", "suit": "HEARTS"}),
                can_check=True,
            ),
        )
        self.assertFalse(check.matched)
        self.assertEqual(check.reason_code, "PREFOLD_FREE_CHECK_BLOCKED")

    def test_live_file_parses_openholdem_like_cells(self):
        config = parse_prefold_config(
            "enabled=true\nmode=live\nWHEN players=6 position=UTG facing=RAISE cards=72o,83o action=FOLD id=cell-1\n"
        )
        self.assertTrue(config.enabled)
        decision = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="UTG",
                facing="RAISE",
                hole_cards=("7s", "2h"),
            ),
        )
        self.assertTrue(decision.matched)
        self.assertEqual(decision.canonical_hand, "72o")

    def test_room_isstraddle_flag_is_special_without_canonical_hand(self):
        """Coin wait_list sends isStraddle:1 as a table capability, not a live straddle."""
        config = default_nlh_prefold_config()
        decision = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="BB",
                facing="RAISE",
                hole_cards=({"value": "JACK", "suit": "DIAMONDS"}, {"value": "THREE", "suit": "HEARTS"}),
                game_family="NLH",
                can_check=False,
                straddle=bool(1),
            ),
        )
        self.assertFalse(decision.matched)
        self.assertIsNone(decision.canonical_hand)
        self.assertEqual(decision.reason_code, "PREFOLD_SPECIAL_GAME_BLOCKED")

    def test_bb_trash_vs_raise_folds_without_free_check(self):
        config = default_nlh_prefold_config()
        decision = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="BB",
                facing="RAISE",
                hole_cards=({"value": "JACK", "suit": "DIAMONDS"}, {"value": "THREE", "suit": "HEARTS"}),
                can_check=False,
            ),
        )
        self.assertTrue(decision.matched)
        self.assertEqual(decision.canonical_hand, "J3o")
        self.assertEqual(decision.recommended_action, "FOLD")
        self.assertTrue(decision.bypass_ai)

    def test_try_logs_miss_even_without_canonical_hand(self):
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH"}

        class Hand:
            hero_seat = 3
            ppp_hero_seat = 2
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 1}
            cards = ({"value": "ACE", "suit": "SPADES"}, {"value": "ACE", "suit": "HEARTS"})
            room = Room()
            table_id = 1153889
            hero_name = "Hero"
            hand_id = "115388900108"

        notes = []

        def sink(tag, message, detail=None):
            notes.append((tag, message, dict(detail or {})))

        bridge = LiveCoinBridge(diagnostic_sink=sink)
        bridge.active_hook_room = 10
        bridge.state["_hook_room"] = 10
        bridge.autoplay.turn_by_room[10] = {
            "userTurnOptions": {"7": [], "4": [0.02]},
            "_ws_id": "ws",
            "_turn_id": "t1",
            "turnTime": 15,
        }
        self.assertFalse(asyncio.run(bridge._try_nlh_prefold(Hand())))
        tags = [row[0] for row in notes]
        self.assertIn("prefold_miss", tags)
        miss = next(row for row in notes if row[0] == "prefold_miss")
        self.assertEqual(miss[2].get("reason"), "PREFOLD_NO_EXPLICIT_RULE")
        self.assertEqual(miss[2].get("hand"), "AA")

    def test_77_utg_unopened_is_never_chart_folded(self):
        config = default_nlh_prefold_config()
        for family in ("NLH", "RING", "Ring", "CASH"):
            decision = evaluate_prefold(
                config,
                PrefoldContext(
                    dealt_in_players=6,
                    position="UTG",
                    facing="UNOPENED",
                    hole_cards=({"value": "SEVEN", "suit": "SPADES"}, {"value": "SEVEN", "suit": "CLUBS"}),
                    game_family=family,
                ),
            )
            self.assertFalse(decision.matched, family)
            self.assertEqual(decision.canonical_hand, "77")
            self.assertEqual(decision.reason_code, "PREFOLD_NO_EXPLICIT_RULE")

    def test_kk_vs_rfi_is_never_chart_folded(self):
        config = default_nlh_prefold_config()
        decision = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="UTG",
                facing="RAISE",
                hole_cards=({"value": "KING", "suit": "SPADES"}, {"value": "KING", "suit": "CLUBS"}),
            ),
        )
        self.assertFalse(decision.matched)
        self.assertEqual(decision.canonical_hand, "KK")

    def test_live_schedule_path_folds_trash_without_hint(self):
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH"}

        class Hand:
            hero_seat = 4
            ppp_hero_seat = 3
            roster = [{"seatId": i} for i in range(1, 7)]
            pre = {"dealerSeatId": 1}
            cards = ({"value": "SEVEN", "suit": "SPADES"}, {"value": "TWO", "suit": "HEARTS"})
            room = Room()
            table_id = 1154179
            hero_name = "Hero"
            hand_id = "h1"

        bridge = LiveCoinBridge()
        bridge.active_hook_room = 10
        bridge.state["_hook_room"] = 10
        bridge.autoplay.turn_by_room[10] = {
            "userTurnOptions": {"7": [], "4": [0.02]},
            "_ws_id": "ws",
            "_turn_id": "t1",
            "turnTime": 15,
        }
        self.assertTrue(asyncio.run(bridge._try_nlh_prefold(Hand())))
        self.assertEqual(bridge.autoplay.pending["action"], "FOLD")
        self.assertTrue(bridge.autoplay.pending.get("prefold"))
        self.assertEqual(bridge.autoplay.pending.get("delay_ms"), 0)
        due = float(bridge.autoplay.pending.get("due") or 0)
        self.assertLessEqual(abs(due - time.monotonic()), 0.25)

    def test_live_schedule_folds_64o_co_vs_raise_not_aa(self):
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH"}

        class Trash:
            hero_seat = 6
            ppp_hero_seat = 5
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 1}
            cards = ({"value": "SIX", "suit": "SPADES"}, {"value": "FOUR", "suit": "HEARTS"})
            room = Room()
            table_id = 1154179
            hero_name = "Hero"
            hand_id = "h64"

        class Aces:
            hero_seat = 6
            ppp_hero_seat = 5
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 1}
            cards = ({"value": "ACE", "suit": "SPADES"}, {"value": "ACE", "suit": "HEARTS"})
            room = Room()
            table_id = 1154179
            hero_name = "Hero"
            hand_id = "hAA"

        bridge = LiveCoinBridge()
        bridge.active_hook_room = 10
        bridge.state["_hook_room"] = 10
        bridge.autoplay.turn_by_room[10] = {
            "userTurnOptions": {"7": [], "4": [0.02]},
            "_ws_id": "ws",
            "_turn_id": "t-co",
            "turnTime": 15,
        }
        config = default_nlh_prefold_config()
        co_raise = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="CO",
                facing="RAISE",
                hole_cards=Trash.cards,
            ),
        )
        self.assertTrue(co_raise.matched)
        self.assertEqual(co_raise.canonical_hand, "64o")
        self.assertTrue(asyncio.run(bridge._try_nlh_prefold(Trash())))
        self.assertEqual(bridge.autoplay.pending["action"], "FOLD")
        self.assertTrue(bridge.autoplay.pending.get("prefold"))
        aa = evaluate_prefold(
            config,
            PrefoldContext(
                dealt_in_players=6,
                position="CO",
                facing="RAISE",
                hole_cards=Aces.cards,
            ),
        )
        self.assertFalse(aa.matched)
        self.assertEqual(aa.canonical_hand, "AA")
        bridge.autoplay.pending = None
        self.assertFalse(asyncio.run(bridge._try_nlh_prefold(Aces())))
        self.assertIsNone(bridge.autoplay.pending)

    def test_catalog_isstraddle_does_not_block_live_prefold(self):
        """Coin wait_list isStraddle:1 is a table capability, not this-hand straddle."""
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH", "isStraddle": 1}

        class Hand:
            hero_seat = 5
            ppp_hero_seat = 4
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 3, "straddleSeatId": -1}
            cards = ({"value": "JACK", "suit": "DIAMONDS"}, {"value": "THREE", "suit": "HEARTS"})
            room = Room()
            table_id = 1153889
            hero_name = "Weedman834"
            hand_id = "115388900108"

        notes = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda tag, msg, detail=None: notes.append(tag))
        bridge.active_hook_room = 58951
        bridge.state["_hook_room"] = 58951
        bridge.state["straddleSeatId"] = 0
        bridge.autoplay.turn_by_room[58951] = {
            "userTurnOptions": {"4": [0.02], "5": [0.06, 2.08], "7": None},
            "_ws_id": "ws",
            "_turn_id": "t108",
            "turnTime": 16,
        }
        self.assertTrue(asyncio.run(bridge._try_nlh_prefold(Hand())))
        self.assertIn("prefold_ready", notes)

    def test_live_straddle_seat_blocks_prefold(self):
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH"}

        class Hand:
            hero_seat = 5
            ppp_hero_seat = 4
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 3, "straddleSeatId": 6}
            cards = ({"value": "JACK", "suit": "DIAMONDS"}, {"value": "THREE", "suit": "HEARTS"})
            room = Room()
            table_id = 1153889
            hero_name = "Weedman834"
            hand_id = "115388900108"

        notes = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda tag, msg, detail=None: notes.append((tag, dict(detail or {}))))
        bridge.active_hook_room = 58951
        bridge.state["_hook_room"] = 58951
        bridge.autoplay.turn_by_room[58951] = {
            "userTurnOptions": {"4": [0.02], "7": None},
            "_ws_id": "ws",
            "_turn_id": "t108",
            "turnTime": 16,
        }
        self.assertFalse(asyncio.run(bridge._try_nlh_prefold(Hand())))
        miss = next(row for row in notes if row[0] == "prefold_miss")
        self.assertEqual(miss[1].get("reason"), "PREFOLD_SPECIAL_GAME_BLOCKED")

    def test_bb_j3o_vs_raise_schedules_prefold(self):
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH"}

        class Hand:
            hero_seat = 5
            ppp_hero_seat = 4
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 3}
            cards = ({"value": "JACK", "suit": "DIAMONDS"}, {"value": "THREE", "suit": "HEARTS"})
            room = Room()
            table_id = 1153889
            hero_name = "Weedman834"
            hand_id = "115388900108"

        notes = []
        bridge = LiveCoinBridge(diagnostic_sink=lambda tag, msg, detail=None: notes.append(tag))
        bridge.active_hook_room = 58951
        bridge.state["_hook_room"] = 58951
        bridge.autoplay.turn_by_room[58951] = {
            "userTurnOptions": {"4": [0.02], "5": [0.06, 2.08], "7": None},
            "_ws_id": "ws",
            "_turn_id": "t108",
            "turnTime": 16,
        }
        self.assertTrue(asyncio.run(bridge._try_nlh_prefold(Hand())))
        self.assertIn("prefold_ready", notes)

    def test_leftover_flop_does_not_skip_next_preflop_chart(self):
        import asyncio

        class Room:
            props = {"miniGameTypeId": 1, "_gameTypeLabel": "NLH"}

        class Hand:
            hero_seat = 2
            ppp_hero_seat = 1
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 1}
            cards = ({"value": "ACE", "suit": "DIAMONDS"}, {"value": "JACK", "suit": "DIAMONDS"})
            room = Room()
            table_id = 1
            hero_name = "SUX0DOPO4"
            hand_id = "new-hand"

        bridge = LiveCoinBridge()
        bridge.active_hook_room = 10
        bridge.state["_hook_room"] = 10
        bridge.autoplay.turn_by_room[10] = {
            "userTurnOptions": {"4": [0.02], "7": None},
            "_ws_id": "ws",
            "_turn_id": "t-ajs",
            "turnTime": 16,
        }
        bridge.emitted_primary_stages.add("FLOP")
        bridge._street_hand_id = "old-hand"
        context = bridge._nlh_prefold_context(Hand())
        self.assertEqual(context.street, "PREFLOP")
        self.assertEqual(bridge._street_hand_id, "new-hand")
        self.assertFalse(asyncio.run(bridge._try_nlh_prefold(Hand())))
        # AJs is not chart trash; leftover FLOP must not become PREFOLD_NOT_PREFLOP.
        self.assertEqual(context.street, "PREFLOP")

    def test_ring_label_and_77_are_chart_miss_not_failsafe_fodder(self):
        class Room:
            props = {"miniGameTypeId": 1, "gameType": "Ring"}

        class Hand:
            hero_seat = 4
            ppp_hero_seat = 3
            roster = [{"seatId": i, "isPlaying": True} for i in range(1, 7)]
            pre = {"dealerSeatId": 1}
            cards = ({"value": "SEVEN", "suit": "SPADES"}, {"value": "SEVEN", "suit": "HEARTS"})
            room = Room()
            table_id = 1
            hero_name = "SUX0DOPO4"
            hand_id = "77-utg"

        bridge = LiveCoinBridge()
        bridge.active_hook_room = 10
        bridge.state["_hook_room"] = 10
        bridge.current_hand = Hand()
        bridge.autoplay.turn_by_room[10] = {
            "userTurnOptions": {"5": [0.04, 2.0], "7": None},
            "_ws_id": "ws",
            "_turn_id": "t-77",
            "turnTime": 16,
        }
        context = bridge._nlh_prefold_context(Hand())
        self.assertEqual(context.game_family, "NLH")
        self.assertEqual(context.position, "UTG")
        self.assertTrue(bridge._chart_miss_preflop(Hand()))
        bridge.awaiting_cc = True
        event = {"_hmuriy_duplicate_turn": True, "_hmuriy_turn_refresh": "extraTimer"}
        import asyncio
        asyncio.run(bridge._on_hero_user_turn(
            event, None, b"",
            {"timerName": "extraTimer", "userTurnOptions": {"5": [0.04, 2.0], "7": None}},
            10,
        ))
        self.assertTrue(bridge.awaiting_cc)
        self.assertIsNone(bridge.autoplay.pending)

    def test_extra_timer_bank_counts_in_remaining(self):
        bridge = LiveCoinBridge()
        now = time.monotonic()
        turn = {
            "_observed_monotonic": now - 15.3,
            "turnTime": 16.0,
            "extraTimerEnabled": True,
            "extraTurnTime": 14.0,
        }
        remaining = bridge._hero_turn_remaining_seconds(turn)
        self.assertGreater(remaining, 10.0)
        without_extra = max(0.0, 16.0 - 15.3)
        self.assertGreater(remaining, without_extra + 5.0)


if __name__ == "__main__":
    unittest.main()
