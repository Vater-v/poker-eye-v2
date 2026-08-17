"""Unit tests for the hint lifecycle watchdog."""
import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.hints import HintWatchdog, OutstandingHint


class HintWatchdogTests(unittest.TestCase):
    def test_accepts_first_hint(self):
        w = HintWatchdog(timeout=10.0, clock=time.monotonic)
        accepted, transitions = w.observe_hint("t1", "h1", 0.0)
        self.assertTrue(accepted)
        self.assertEqual(transitions[0].event, "hint.armed")

    def test_holds_second_hint(self):
        w = HintWatchdog(timeout=10.0, clock=time.monotonic)
        w.observe_hint("t1", "h1", 0.0)
        accepted, transitions = w.observe_hint("t1", "h2", 1.0)
        self.assertFalse(accepted)
        self.assertEqual(transitions[0].event, "hint.held")

    def test_finish_releases_and_promotes_successor(self):
        w = HintWatchdog(timeout=10.0, clock=time.monotonic)
        w.observe_hint("t1", "h1", 0.0)
        w.observe_hint("t1", "h2", 1.0)
        transitions = w.observe_finish("t1")
        events = [t.event for t in transitions]
        self.assertIn("hint.finished", events)
        self.assertIn("hint.promoted", events)

    def test_timeout_once_then_recycle(self):
        clock = [0.0]

        def fake_clock():
            return clock[0]

        w = HintWatchdog(timeout=5.0, clock=fake_clock)
        w.observe_hint("t1", "h1", 0.0)
        clock[0] = 6.0
        transitions1 = w.poll(6.0)
        self.assertEqual(transitions1[0].event, "hint.timeout_once")
        self.assertIn("t1", w.outstanding())

        clock[0] = 12.0
        transitions2 = w.poll(12.0)
        self.assertEqual(transitions2[0].event, "hint.recycled")
        self.assertNotIn("t1", w.outstanding())

    def test_recycles_with_successor(self):
        clock = [0.0]

        def fake_clock():
            return clock[0]

        w = HintWatchdog(timeout=5.0, clock=fake_clock)
        w.observe_hint("t1", "h1", 0.0)
        w.observe_hint("t1", "h2", 1.0)
        clock[0] = 6.0
        w.poll(6.0)  # first timeout: timeout_once
        clock[0] = 12.0
        transitions = w.poll(12.0)  # second timeout: recycle
        events = [t.event for t in transitions]
        self.assertIn("hint.recycled", events)
        self.assertIn("hint.promoted", events)

    def test_rejects_negative_timeout(self):
        with self.assertRaises(ValueError):
            HintWatchdog(timeout=-1.0)


if __name__ == "__main__":
    unittest.main()