"""RED tests for last-2-session bugs: ledger wallet, street leak, no stack add."""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class WalletCashTests(unittest.TestCase):
    def test_table_gold_is_not_lobby_cash(self):
        from core.v6router.wallet_cash import wallet_from_payload

        table = {"gold": 12.47, "leftGold": 7.66, "userChips": 1.6}
        self.assertIsNone(wallet_from_payload(table, source="table"))

    def test_lobby_payload_uses_cash_not_bonus_gold_first(self):
        from core.v6router.wallet_cash import wallet_from_payload

        lobby = {"gold": 99.0, "balance": 12.4662, "leftGold": 7.0}
        self.assertAlmostEqual(wallet_from_payload(lobby, source="lobby"), 12.4662)

    def test_closing_a_table_does_not_invent_cash(self):
        from core.v6router.wallet_cash import apply_table_close

        live = apply_table_close(wallet=12.4662, stack_bb=80.0, bb=0.02)
        self.assertAlmostEqual(live, 12.4662)


class StreetLeakTests(unittest.TestCase):
    def test_leaked_flop_mark_without_board_is_preflop(self):
        from core.verified_v1.prefold import street_from_board

        street = street_from_board(
            hand_id="",
            board_count=0,
            emitted_stages=("FLOP", "TURN"),
            street_hand_id="old-hand",
        )
        self.assertEqual(street, "PREFLOP")

    def test_same_hand_with_flop_mark_stays_postflop(self):
        from core.verified_v1.prefold import street_from_board

        street = street_from_board(
            hand_id="116303101044",
            board_count=3,
            emitted_stages=("FLOP",),
            street_hand_id="116303101044",
        )
        self.assertEqual(street, "FLOP")


class LedgerSeedTests(unittest.TestCase):
    def test_seed_without_lobby_is_fallback_not_fact(self):
        from core.v6router.history_ledger import seed_balance_st, seed_source

        rows = [
            ["Date", "Nickname"],
            ["", "Weedman834", "", "", "", "", "16.48", "16.48", "0", "NL", "0,01/0,02", "0", "0", "0"],
        ]
        value = seed_balance_st(rows, "Weedman834", wallet_cash=None)
        self.assertEqual(seed_source(rows, "Weedman834", wallet_cash=None), "fallback")
        self.assertAlmostEqual(value, 16.48)

    def test_lobby_cash_beats_previous_end(self):
        from core.v6router.history_ledger import seed_balance_st, seed_source

        rows = [
            ["Date", "Nickname"],
            ["", "Weedman834", "", "", "", "", "16.48", "16.48", "0", "NL", "0,01/0,02", "0", "0", "0"],
        ]
        self.assertAlmostEqual(seed_balance_st(rows, "Weedman834", wallet_cash=12.4662), 12.47)
        self.assertEqual(seed_source(rows, "Weedman834", wallet_cash=12.4662), "lobby")

    def test_end_prefers_lobby_not_reconstructed_wallet(self):
        from core.v6router.history_ledger import close_balance_end

        end, profit = close_balance_end(
            balance_st=12.4662,
            hand_profit=0.12,
            live_lobby=12.58,
            reconstructed_wallet=16.476172331051497,
        )
        self.assertAlmostEqual(end, 12.58)
        self.assertAlmostEqual(profit, 0.1138, places=3)

    def test_end_falls_back_to_st_plus_hands_not_stack_sum(self):
        from core.v6router.history_ledger import close_balance_end

        end, profit = close_balance_end(
            balance_st=12.4662,
            hand_profit=-0.20,
            live_lobby=None,
            reconstructed_wallet=16.48,
        )
        self.assertAlmostEqual(end, 12.2662)
        self.assertAlmostEqual(profit, -0.20)


if __name__ == "__main__":
    unittest.main()
