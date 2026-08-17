"""Regression tests for the hint→action policy layer (call=0 guard included)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.policy import ActionDecision, decide_cc_action
from core.state import TableState


def hero_table():
    ts = TableState("d1", "t1")
    ts.street_state.ensure_seat(1)  # opponent
    ts.street_state.ensure_seat(2)  # hero
    return ts


class PolicyTests(unittest.TestCase):
    def test_normal_check_decision(self):
        ts = hero_table()
        d = decide_cc_action({"type": "CHECK"}, table_state=ts, hero_seat=2,
                             user_turn_options={"3": [], "4": [50]})
        self.assertEqual(d.status, "decided")
        self.assertEqual(d.action.name, "CHECK")
        self.assertEqual(d.action.coin_code, 3)

    def test_call_after_raise_uses_call_code(self):
        ts = hero_table()
        ts.street_state.contribute(1, 200)  # opponent raised 200
        d = decide_cc_action({"type": "CALL"}, table_state=ts, hero_seat=2,
                             user_turn_options={"4": [200], "5": [400, 800]})
        self.assertEqual(d.status, "decided")
        self.assertEqual(d.action.name, "CALL")
        self.assertEqual(d.action.coin_code, 4)
        self.assertEqual(d.action.bet_amount, 0.0)

    def test_call_zero_with_check_legal_prefers_check(self):
        """Regression: call=0 -> backend CHECK 0 -> Coin CALL 0 must never repeat."""
        ts = hero_table()
        d = decide_cc_action({"type": "CALL"}, table_state=ts, hero_seat=2,
                             user_turn_options={"3": [], "4": [50]})
        self.assertEqual(d.status, "state_gap_check")
        self.assertEqual(d.action.name, "CHECK")
        self.assertEqual(d.action.coin_code, 3)

    def test_call_zero_without_check_needs_operator(self):
        ts = hero_table()
        d = decide_cc_action({"type": "CALL"}, table_state=ts, hero_seat=2,
                             user_turn_options={"4": [50]})
        self.assertEqual(d.status, "needs_operator")
        self.assertIsNone(d.action)

    def test_raise_with_options(self):
        ts = hero_table()
        ts.street_state.contribute(1, 200)  # opponent raised
        # chip_scale=1: CC amount is in chip units (matches state integers)
        d = decide_cc_action({"type": "RAISE", "amount": 400},
                             table_state=ts, hero_seat=2, chip_scale=1,
                             user_turn_options={"4": [200], "5": [400, 800]})
        self.assertEqual(d.status, "decided")
        self.assertEqual(d.action.name, "RAISE")
        self.assertEqual(d.action.coin_code, 5)

    def test_unsupported_cc_is_error_not_action(self):
        ts = hero_table()
        ts.street_state.contribute(1, 200)
        d = decide_cc_action({"type": "DEAL"}, table_state=ts, hero_seat=2,
                             user_turn_options={"4": [200]})
        self.assertEqual(d.status, "error")
        self.assertIsNone(d.action)

    def test_fold_always_allowed(self):
        ts = hero_table()
        ts.street_state.contribute(1, 200)
        d = decide_cc_action({"type": "FOLD"}, table_state=ts, hero_seat=2,
                             user_turn_options={"4": [200]})
        self.assertEqual(d.status, "decided")
        self.assertEqual(d.action.coin_code, 7)

    def test_state_gap_has_anomaly_diagnostics(self):
        ts = hero_table()
        d = decide_cc_action({"type": "CALL"}, table_state=ts, hero_seat=2,
                             user_turn_options={"3": []}, source="regression", seq=5)
        self.assertIsNotNone(d.anomaly)
        self.assertEqual(d.anomaly.verdict.value, "STATE_GAP")
        self.assertEqual(d.anomaly.diagnostics["seq"], 5)
        self.assertEqual(d.anomaly.diagnostics["source"], "regression")


if __name__ == "__main__":
    unittest.main()
