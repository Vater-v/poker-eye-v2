"""Regression tests for the call=0 anomaly guard."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.anomalies import (
    AnomalyVerdict, assess_call_zero, select_action_for_call_zero,
)
from core.state import StreetState, TableState


class AnomalyTests(unittest.TestCase):
    def setUp(self):
        self.st = StreetState()
        # Two active seats, no contributions yet.
        self.st.ensure_seat(1)
        self.st.ensure_seat(2)

    def test_call_need_zero_check_legal_gives_state_gap(self):
        """call_need==0 with CHECK available → STATE_GAP, prefer CHECK."""
        res = assess_call_zero(
            call_need=0,
            street_state=self.st,
            user_turn_options={"3": [], "4": [50], "5": [100, 300]},
        )
        self.assertEqual(res.verdict, AnomalyVerdict.STATE_GAP)
        self.assertTrue(res.check_legal)
        self.assertIn("STATE_GAP", res.reason)
        action = select_action_for_call_zero(res)
        self.assertEqual(action, {"type": "CHECK"})

    def test_call_need_zero_check_not_legal_needs_operator(self):
        """call_need==0 but CHECK not offered → contradictory state."""
        res = assess_call_zero(
            call_need=0,
            street_state=self.st,
            user_turn_options={"4": [50], "5": [100, 300]},  # no option 3
        )
        self.assertEqual(res.verdict, AnomalyVerdict.NEEDS_OPERATOR)
        self.assertFalse(res.check_legal)
        self.assertIsNone(select_action_for_call_zero(res))

    def test_call_need_nonzero_is_ok(self):
        self.st.contribute(2, 100)
        res = assess_call_zero(
            call_need=50,
            street_state=self.st,
            user_turn_options={"3": [], "4": [50]},
        )
        self.assertEqual(res.verdict, AnomalyVerdict.OK)

    def test_body_hash_in_diagnostics(self):
        res = assess_call_zero(
            call_need=0,
            street_state=self.st,
            user_turn_options={"3": []},
            raw_frame=b"test frame data",
            seq=1,
            source="test",
        )
        self.assertIn("body_sha256", res.diagnostics)
        self.assertEqual(res.diagnostics["body_sha256"], res.body_sha256)

    def test_call_zero_regression_scenario(self):
        """Exact regression: call=0 → backend CHECK 0 → Coin CALL 0.
        When computed call_need==0 AND CHECK is legal, the action policy must
        emit STATE_GAP and prefer CHECK, NOT silently send CALL amount=0.
        """
        # Simulate: there was a raise preflop by opponent, hero on the button.
        # Hero's street contribution = 0, current max = opponent's raise = 200.
        # Computed call_need = 200 - 0 = 200 (not zero yet).
        st = StreetState()
        st.ensure_seat(1)  # opponent
        st.ensure_seat(2)  # hero
        st.contribute(1, 200)
        st.set_raise(200)
        call_need = st.call_need(2)  # 200 - 0 = 200 → non-zero, OK
        self.assertEqual(call_need, 200)

        # But if a state bug causes call_need=0 when it should be 200:
        # and CHECK is legal (option 3 available):
        res = assess_call_zero(
            call_need=0,  # bug: should be 200
            street_state=st,
            user_turn_options={"3": [], "4": [200], "5": [400, 800]},
            source="regression",
        )
        self.assertEqual(res.verdict, AnomalyVerdict.STATE_GAP)
        self.assertIn("CHECK legal", res.reason)


if __name__ == "__main__":
    unittest.main()