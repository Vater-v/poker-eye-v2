from __future__ import annotations

import unittest

from core.v6router.action_arbiter import (
    ActionArbiter,
    ActionArbiterConfig,
    ActionOffer,
    ActionState,
)


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class ActionArbiterFailsafeTests(unittest.TestCase):
    def test_host_delay_on_one_table_does_not_block_other_table(self):
        clock = _Clock(100.0)
        arbiter = ActionArbiter("dev", config=ActionArbiterConfig(), clock=clock)
        delayed = ActionOffer(
            action_id="check",
            table_id=1,
            ready_at=104.5,
            deadline_at=110.0,
            supports_delayed_send=False,
        )
        due = ActionOffer(
            action_id="fold",
            table_id=2,
            ready_at=100.0,
            deadline_at=112.0,
            bypass_gap=True,
            supports_delayed_send=False,
        )
        arbiter.offer(delayed)
        arbiter.offer(due)
        plan = arbiter.dispatch_next(now=100.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.action_id, "fold")
        check = arbiter.record("check")
        self.assertEqual(check.state, ActionState.QUEUED)
        self.assertIsNotNone(check.scheduled_at)
        self.assertGreater(check.scheduled_at, 100.0)

    def test_extra_urgent_clears_host_delay_and_dispatches_now(self):
        """TelImit CHECK 0.0 4509 / 0 of 0: extraTimer must not wait HOST_DELAY."""
        clock = _Clock(100.0)
        arbiter = ActionArbiter("dev", config=ActionArbiterConfig(), clock=clock)
        delayed = ActionOffer(
            action_id="check",
            table_id=1,
            ready_at=104.509,
            deadline_at=116.0,
            supports_delayed_send=False,
        )
        arbiter.offer(delayed)
        self.assertIsNone(arbiter.dispatch_next(now=100.0))
        check = arbiter.record("check")
        self.assertEqual(check.state, ActionState.QUEUED)
        self.assertIsNotNone(check.scheduled_at)
        self.assertGreater(check.scheduled_at, 100.0)
        clock.now = 103.0
        urgent = ActionOffer(
            action_id="check",
            table_id=1,
            ready_at=103.0,
            deadline_at=116.0,
            bypass_gap=True,
            supports_delayed_send=False,
        )
        arbiter.offer(urgent)
        plan = arbiter.dispatch_next(now=103.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.action_id, "check")
        self.assertEqual(plan.dispatch_delay_seconds, 0.0)
        self.assertEqual(arbiter.record("check").state, ActionState.DISPATCHED)

    def test_skips_ineligible_head_to_send_other_table(self):
        clock = _Clock()
        arbiter = ActionArbiter("dev", config=ActionArbiterConfig(), clock=clock)
        head = ActionOffer(
            action_id="a", table_id=1, ready_at=100.0, deadline_at=112.0,
        )
        other = ActionOffer(
            action_id="b", table_id=2, ready_at=100.0, deadline_at=112.0,
            bypass_gap=True,
        )
        arbiter.offer(head)
        arbiter.offer(other)
        plan = arbiter.dispatch_next(["b"], now=100.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.action_id, "b")

    def test_deadline_force_sends_instead_of_cancel(self):
        clock = _Clock(200.0)
        arbiter = ActionArbiter(
            "dev",
            config=ActionArbiterConfig(deadline_safety_margin=0.55),
            clock=clock,
        )
        offer = ActionOffer(
            action_id="fold",
            table_id=3,
            ready_at=100.0,
            deadline_at=200.2,
            bypass_gap=True,
        )
        arbiter.offer(offer)
        arbiter._last_scheduled_at = 199.9
        arbiter._last_scheduled_table_id = 9
        plan = arbiter.dispatch_next(now=200.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.action_id, "fold")
        record = arbiter.record("fold")
        self.assertEqual(record.state, ActionState.DISPATCHED)
        self.assertIn("DEADLINE_FORCE", record.reasons)
        self.assertNotEqual(record.cancellation_reason, "DEADLINE_EXPIRED")

    def test_bypass_gap_is_immediate(self):
        clock = _Clock(50.0)
        arbiter = ActionArbiter("dev", config=ActionArbiterConfig(), clock=clock)
        arbiter._last_scheduled_at = 49.0
        arbiter._last_scheduled_table_id = 1
        offer = ActionOffer(
            action_id="check",
            table_id=2,
            ready_at=50.0,
            deadline_at=62.0,
            bypass_gap=True,
        )
        arbiter.offer(offer)
        plan = arbiter.dispatch_next(now=50.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.dispatch_delay_seconds, 0.0)

    def test_expired_queued_action_is_not_cancelled(self):
        clock = _Clock(300.0)
        arbiter = ActionArbiter("dev", config=ActionArbiterConfig(), clock=clock)
        arbiter.offer(ActionOffer(
            action_id="late", table_id=4, ready_at=280.0, deadline_at=290.0,
            bypass_gap=True,
        ))
        arbiter._cancel_expired_locked(300.0)
        record = arbiter.record("late")
        self.assertEqual(record.state, ActionState.QUEUED)


class HumanTimingTests(unittest.TestCase):
    def test_default_gaps_are_wider_than_the_robot_band(self):
        config = ActionArbiterConfig()
        self.assertGreaterEqual(config.inter_action_gap_min, 0.8)
        self.assertGreaterEqual(config.inter_action_gap_max, 2.0)
        self.assertGreaterEqual(config.cross_table_gap_min, 1.1)
        self.assertGreaterEqual(config.cross_table_gap_max, 2.4)
        self.assertGreater(
            config.cross_table_gap_min - config.inter_action_gap_min, 0.2,
        )

    def test_human_gap_sample_stays_in_range_and_is_not_flat(self):
        from core.v6router.action_arbiter import sample_human_gap

        class _Rng:
            def __init__(self) -> None:
                self.i = 0

            def uniform(self, a, b):
                self.i += 1
                return a if self.i % 2 else b

        rng = _Rng()
        low = sample_human_gap(rng, 1.2, 2.8)
        high = sample_human_gap(rng, 1.2, 2.8)
        self.assertGreaterEqual(low, 1.2)
        self.assertLessEqual(high, 2.8)
        self.assertNotAlmostEqual(low, high)

    def test_prefold_with_time_left_takes_human_gap(self):
        from core.v6router.action_arbiter import should_bypass_action_gap

        self.assertFalse(should_bypass_action_gap(
            fallback=False, extra_urgent=False, retry=False,
            prefold=True, remaining_seconds=8.0,
        ))
        self.assertFalse(should_bypass_action_gap(
            fallback=False, extra_urgent=False, retry=False,
            prefold=True, remaining_seconds=None,
        ))

    def test_prefold_near_deadline_and_failsafe_still_bypass(self):
        from core.v6router.action_arbiter import should_bypass_action_gap

        self.assertTrue(should_bypass_action_gap(
            fallback=False, extra_urgent=False, retry=False,
            prefold=True, remaining_seconds=2.4,
        ))
        self.assertTrue(should_bypass_action_gap(
            fallback=True, extra_urgent=False, retry=False,
            prefold=False, remaining_seconds=12.0,
        ))
        self.assertTrue(should_bypass_action_gap(
            fallback=False, extra_urgent=True, retry=False,
            prefold=False, remaining_seconds=12.0,
        ))
        self.assertTrue(should_bypass_action_gap(
            fallback=False, extra_urgent=False, retry=True,
            prefold=False, remaining_seconds=12.0,
        ))


if __name__ == "__main__":
    unittest.main()
