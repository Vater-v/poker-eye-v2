from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from core.v6router.router import (
    DeviceIngressRouter, LiveTableSession, _LiveTableSnapshot, _SessionSlot,
)
from core.verified_v1 import coin_ppp_bridge as ppp
from core.verified_v1.bombpot_support import BombpotTracker, detect_double_board
from core.verified_v1.coin_autoplay import CoinAutoplayCoordinator
from core.verified_v1.coin_bridge_live import (
    LiveCoinBridge, compute_net_profits, normalized_coin_seat_action,
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
