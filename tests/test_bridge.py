"""Integration tests for the hint->CC->ACK->ledger orchestration bridge.

Uses fake eye_sender and device_sender so the full lifecycle is testable
without any real sockets.
"""
import os, sys, tempfile, threading, time, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.bridge import BridgeContext
from core.ledger import Ledger, LedgerStatus


class BridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = Ledger(Path(self.tmp) / "ledger.jsonl")
        self.eye_sent: list = []
        self.device_sent: list = []
        self.events: list = []
        self.bridge = BridgeContext(
            device_id="test-device",
            ledger=self.ledger,
            eye_sender=lambda f: self.eye_sent.append(f) or True,
            device_sender=lambda tid, f: self.device_sent.append((tid, f)) or True,
            on_event=lambda e: self.events.append(e),
            chip_scale=100,
            watchdog_timeout=1.0,
            action_ack_timeout=0.3,
        )

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    # ── hint request ────────────────────────────────────────────────
    def test_hint_request_sends_to_eye(self):
        ok = self.bridge.on_hero_turn("t1", {"initTimeStamp": 100},
                                      hand_id="h1", user_turn_options={"3": [], "4": [50]})
        self.assertTrue(ok)
        self.assertEqual(len(self.eye_sent), 1)
        hint_event = next((e for e in self.events if e.event == "hint.requested"), None)
        self.assertIsNotNone(hint_event)
        self.assertEqual(hint_event.table_id, "t1")

    def test_duplicate_turn_does_not_resend_hint(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 100}, hand_id="h1")
        self.assertEqual(len(self.eye_sent), 1)
        ok = self.bridge.on_hero_turn("t1", {"initTimeStamp": 100}, hand_id="h1")
        self.assertFalse(ok)
        self.assertEqual(len(self.eye_sent), 1)  # no duplicate

    # ── CC reception ────────────────────────────────────────────────
    def test_cc_check_creates_action_and_schedules(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": [], "4": [50]})
        cid = self.bridge.on_cc("t1", {"type": "CHECK", "delay": 100}, hero_seat=2)
        self.assertIsNotNone(cid)
        self.assertEqual(len(self.device_sent), 0)  # action worker hasn't fired yet (delay)
        created = next((e for e in self.events if e.event == "action.created"), None)
        self.assertIsNotNone(created)
        self.assertEqual(created.fields["command"], "CHECK")

    def test_cc_call_with_call_zero_check_legal_prefers_check(self):
        """Regression: CC says CALL, call_need==0, CHECK legal -> state_gap -> CHECK sent."""
        # No contributions: call_need = 0. CHECK legal (option 3 present).
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": [], "4": [50]})
        cid = self.bridge.on_cc("t1", {"type": "CALL", "delay": 100}, hero_seat=2)
        self.assertIsNotNone(cid)
        state_gap = next((e for e in self.events if e.event == "state.gap"), None)
        self.assertIsNotNone(state_gap, "state.gap event was not emitted")
        created = next((e for e in self.events if e.event == "action.created"), None)
        self.assertEqual(created.fields["command"], "CHECK")

    def test_cc_call_after_raise_uses_call_code(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"4": [200], "5": [400, 800]})
        state = self.bridge.table("t1")
        state.street_state.contribute(1, 200)  # opponent raised
        cid = self.bridge.on_cc("t1", {"type": "CALL", "delay": 100}, hero_seat=2)
        self.assertIsNotNone(cid)
        created = next((e for e in self.events if e.event == "action.created"), None)
        self.assertEqual(created.fields["command"], "CALL")

    # ── ACK ─────────────────────────────────────────────────────────
    def test_ack_finalizes_success(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"4": [200]})
        state = self.bridge.table("t1")
        state.street_state.contribute(1, 200)
        cid = self.bridge.on_cc("t1", {"type": "CALL", "delay": 0}, hero_seat=2)
        self.assertIsNotNone(cid)
        time.sleep(0.01)
        ok = self.bridge.on_ack(cid, status="ok")
        self.assertTrue(ok)
        self.assertGreater(self.ledger.count(), 0)
        finalized = next((e for e in self.events if e.event == "action.finalized"), None)
        self.assertIsNotNone(finalized)
        self.assertEqual(finalized.fields["status"], "success")

    def test_stale_ack_rejected(self):
        ok = self.bridge.on_ack("nonexistent-correlation", status="ok")
        self.assertFalse(ok)
        stale_event = next((e for e in self.events if e.event == "action.stale_ack"), None)
        self.assertIsNotNone(stale_event)

    def test_wrong_generation_ack_rejected(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": []})
        cid = self.bridge.on_cc("t1", {"type": "CHECK", "delay": 0}, hero_seat=2)
        # ACK with a different generation:
        ok = self.bridge.on_ack(cid, generation=999)
        self.assertFalse(ok)
        stale = next((e for e in self.events if e.event == "action.stale_ack"), None)
        self.assertIsNotNone(stale)

    # ── three attempts ──────────────────────────────────────────────
    def test_three_attempts_triggered(self):
        self.bridge.ack_timeout = 0.2
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": []})
        self.bridge.on_cc("t1", {"type": "CHECK", "delay": 0}, hero_seat=2)
        # attempt 1 at 0s, +1s, +1s; ACK wait 0.2s each -> ~2.6s total.
        # Wait for all 3 attempts to fire, then for finalization.
        deadline = time.time() + 5.0
        while time.time() < deadline and len(
                [e for e in self.events if e.event == "action.attempt_sent"]) < 3:
            time.sleep(0.05)
        attempts = [e for e in self.events if e.event == "action.attempt_sent"]
        self.assertEqual(len(attempts), 3, f"expected 3 attempts, saw {len(attempts)}")
        self.assertEqual([a.fields["attempt"] for a in attempts], [1, 2, 3])
        # Wait for finalization after 3 failed attempts.
        deadline = time.time() + 3.0
        while time.time() < deadline and not any(
                e for e in self.events if e.event == "action.finalized"):
            time.sleep(0.05)
        finalized = next((e for e in self.events if e.event == "action.finalized"), None)
        self.assertIsNotNone(finalized, "action.finalized event never emitted")
        self.assertEqual(finalized.fields["status"], "failed")

    # ── one in-flight CC ────────────────────────────────────────────
    def test_second_cc_blocked_while_action_in_flight(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": []})
        cid1 = self.bridge.on_cc("t1", {"type": "CHECK", "delay": 1000}, hero_seat=2)
        self.assertIsNotNone(cid1)
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 2}, hand_id="h2",
                                 user_turn_options={"3": []})
        cid2 = self.bridge.on_cc("t1", {"type": "CHECK", "delay": 0}, hero_seat=2)
        self.assertIsNone(cid2)
        skipped = next((e for e in self.events if e.event == "action.skipped"), None)
        self.assertIsNotNone(skipped)

    # ── uncertain ───────────────────────────────────────────────────
    def test_uncertain_action_no_retry(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": []})
        cid = self.bridge.on_cc("t1", {"type": "CHECK", "delay": 0}, hero_seat=2)
        ok = self.bridge.on_action_uncertain(cid)
        self.assertTrue(ok)
        self.assertGreater(self.ledger.count(), 0)
        needs_op = next((e for e in self.events if e.event == "action.finalized" and "needs_operator" in e.fields.get("status", "")), None)
        self.assertIsNotNone(needs_op)

    # ── watchdog ────────────────────────────────────────────────────
    def test_hint_watchdog_timeout_recycle(self):
        self.bridge.on_hero_turn("t1", {"initTimeStamp": 1}, hand_id="h1",
                                 user_turn_options={"3": []})
        time.sleep(1.2)  # timeout=1.0 -> first timeout
        self.bridge.poll()  # monotonic clock internally
        time.sleep(1.2)
        self.bridge.poll()
        recycled = next((e for e in self.events if e.event == "hint.recycled"), None)
        self.assertIsNotNone(recycled)


if __name__ == "__main__":
    unittest.main()