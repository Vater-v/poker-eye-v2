from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
