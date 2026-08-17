"""Unit tests for state machine (street contributions, call_need, min_raise)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.state import StreetState, TableState, SeatState


class StreetStateTests(unittest.TestCase):
    def test_initial_call_zero(self):
        st = StreetState()
        st.ensure_seat(1)
        st.ensure_seat(2)
        self.assertEqual(st.call_need(1), 0)
        self.assertEqual(st.call_need(2), 0)
        self.assertEqual(st.current_max, 0)

    def test_contribute_and_call_need(self):
        st = StreetState()
        st.ensure_seat(1)
        st.ensure_seat(2)
        st.contribute(1, 200)
        self.assertEqual(st.current_max, 200)
        self.assertEqual(st.call_need(2), 200)
        self.assertEqual(st.call_need(1), 0)

    def test_set_raise_updates_current_max_and_last_full_raise(self):
        st = StreetState()
        st.ensure_seat(1)
        st.set_raise(400)
        self.assertEqual(st.current_max, 400)
        self.assertEqual(st.last_full_raise, 400)

    def test_min_raise_to(self):
        st = StreetState()
        st.ensure_seat(1)
        st.ensure_seat(2)
        st.contribute(1, 200)
        st.set_raise(200)
        # hero (seat 2): put=0, current_max=200, last_full_raise=200, 2 can act → min = 200+200-0 = 400
        self.assertEqual(st.min_raise_to(2), 400)

    def test_min_raise_zero_when_only_one_can_act(self):
        st = StreetState()
        st.ensure_seat(1)
        st.ensure_seat(2)
        st.fold(2)
        self.assertEqual(st.min_raise_to(1), 0)

    def test_max_call_or_raise(self):
        st = StreetState()
        s1 = st.ensure_seat(1); s1.remaining_stack = 5000
        s2 = st.ensure_seat(2); s2.remaining_stack = 3000
        st.contribute(1, 200)  # remaining becomes 4800
        # cover of seat 1 = street 200 + remaining 4800 = 5000; hero put 0 → 5000
        self.assertEqual(st.max_call_or_raise_to(2), 5000)

    def test_reset_street_clears_contributions(self):
        st = StreetState()
        st.ensure_seat(1)
        st.contribute(1, 200)
        st.set_raise(200)
        st.reset_street()
        self.assertEqual(st.current_max, 0)
        self.assertEqual(st.last_full_raise, 0)
        self.assertEqual(st.call_need(1), 0)


class TableStateTests(unittest.TestCase):
    def test_new_hand_advances_generation(self):
        ts = TableState("d1", "t1", generation=5)
        self.assertEqual(ts.generation, 5)
        ts.new_hand("hand_42")
        self.assertEqual(ts.generation, 6)
        self.assertEqual(ts.hand_id, "hand_42")

    def test_new_street_resets(self):
        ts = TableState("d1", "t1")
        ts.street_state.contribute(1, 100)
        ts.new_street(1)  # flop
        self.assertEqual(ts.street, 1)
        self.assertEqual(ts.street_state.current_max, 0)


if __name__ == "__main__":
    unittest.main()