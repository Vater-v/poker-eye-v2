"""Regression and invariant tests for the v2 core coin wire modules."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.coin_wire import (
    ACTION_CHECK, ACTION_CALL, ACTION_FOLD, ACTION_RAISE,
    ResolvedAction, build_game_user_action_packet, decode_packet,
    encode_packet, frame_metadata, resolve_eye_cc_action,
    _Byte, _Short, _Int, _Str, _Obj,
)


class CoinWireEncodeTests(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        root = {"c": _Byte(1), "a": _Short(13), "p": _Obj({"c": _Str("game.user_action"), "r": _Int(100), "p": _Obj({"data": _Str('{"userAction":4,"betAmount":0}')})})}
        pkt = encode_packet(root)
        self.assertEqual(pkt[0], 0x80)
        decoded = decode_packet(pkt)
        self.assertEqual(decoded, {"c": 1, "a": 13, "p": {"c": "game.user_action", "r": 100, "p": {"data": '{"userAction":4,"betAmount":0}'}}})

    def test_build_game_user_action_packet(self):
        pkt = build_game_user_action_packet(42, ACTION_CALL, 0.0)
        self.assertTrue(pkt.startswith(b'\x80'))
        decoded = decode_packet(pkt)
        self.assertEqual(decoded["p"]["p"]["data"], '{"userAction":4,"betAmount":0}')

        pkt2 = build_game_user_action_packet(42, ACTION_RAISE, 1.5)
        decoded2 = decode_packet(pkt2)
        self.assertIn("betAmount", decoded2["p"]["p"]["data"])
        self.assertIn("1.5", decoded2["p"]["p"]["data"])

    def test_frame_metadata(self):
        pkt = build_game_user_action_packet(42, ACTION_FOLD, 0.0)
        meta = frame_metadata(pkt)
        self.assertIn("length", meta)
        self.assertIn("header", meta)
        self.assertFalse(meta["compressed"])  # game.user_action is not compressed

    def test_resolve_fold_check_call(self):
        for typ, code in [("FOLD", ACTION_FOLD), ("CHECK", ACTION_CHECK), ("CALL", ACTION_CALL)]:
            ra = resolve_eye_cc_action({"type": typ})
            self.assertEqual(ra.coin_code, code)
            self.assertEqual(ra.bet_amount, 0.0)

    def test_resolve_raise(self):
        ra = resolve_eye_cc_action(
            {"type": "RAISE", "amount": 50},
            user_turn_options={"5": [100, 300]},
            current_street_bet=200, chip_scale=100,
        )
        self.assertEqual(ra.name, "RAISE")
        self.assertEqual(ra.coin_code, ACTION_RAISE)
        # desired = 200 + 50/100 = 200.5; min/lo=100, max/hi=300; target = max(100, min(300, 200.5)) = 200.5
        self.assertAlmostEqual(ra.bet_amount, 200.5, places=6)

    def test_resolve_allin_as_raise(self):
        ra = resolve_eye_cc_action(
            {"type": "ALLIN", "amount": 500},
            user_turn_options={"5": [100, 1000]},
            current_street_bet=200, chip_scale=100,
        )
        self.assertEqual(ra.name, "RAISE")
        self.assertEqual(ra.coin_code, ACTION_RAISE)
        # desired = 200 + 500/100 = 205; target = max(100, min(1000, 205)) = 205
        self.assertAlmostEqual(ra.bet_amount, 205.0, places=6)

    def test_resolve_falls_to_call_when_closer(self):
        # option 4 (call) = [50] -> call_target = 200 + 50 = 250
        # option 5 (raise) = [100, 300] -> target = 250
        # Both distances are 0; call has priority index 1, raise 0 → raise wins
        ra = resolve_eye_cc_action(
            {"type": "RAISE", "amount": 50},
            user_turn_options={"4": [50], "5": [200, 400]},
            current_street_bet=200, chip_scale=100,
        )
        # Both have distance 0; raise (index 0) wins
        self.assertEqual(ra.name, "RAISE")

    def test_unsupported_action_raises(self):
        with self.assertRaises(ValueError):
            resolve_eye_cc_action({"type": "DEAL"}, user_turn_options={"4": [50]})


class CoinWireEdgeCaseTests(unittest.TestCase):
    def test_call_zero_with_check_legal_is_handled_by_anomaly_module(self):
        """The wire resolver itself only projects raises; call_need=0 detection is in anomalies.py."""
        ra = resolve_eye_cc_action({"type": "CHECK"})
        self.assertEqual(ra.coin_code, ACTION_CHECK)
        self.assertEqual(ra.bet_amount, 0.0)
        # A CALL forced through resolver with type=CALL → code 4, amount 0
        ra2 = resolve_eye_cc_action({"type": "CALL"})
        self.assertEqual(ra2.coin_code, ACTION_CALL)
        self.assertEqual(ra2.bet_amount, 0.0)
        # The anomaly module must intercept the decision before the CALL 0 is sent.


if __name__ == "__main__":
    unittest.main()