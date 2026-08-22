from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core.v6router.history_ledger import (
    COLUMNS,
    HISTORY_HEADERS,
    DrySheetsTransport,
    FakeSheetsTransport,
    HistoryLedger,
    OpenSession,
    attach_session_docs,
    attach_session_profit,
    assemble_close_row,
    assemble_open_row,
    assemble_rakeback_row,
    default_credential_path,
    euro_amount,
    find_last_matching_row,
    is_open_history_row,
    ledger_limit,
    ledger_type,
    novosibirsk_now,
    normalize_limit,
    open_transport,
    rakeback_delta,
    seed_balance_st,
    session_key,
)


NOW = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone(timedelta(hours=3)))
NL = "0,01/0,02"
PLO = "0,01/0,02"


def _ledger(rows=None, enabled="dev") -> tuple[HistoryLedger, FakeSheetsTransport]:
    transport = FakeSheetsTransport(rows)
    ledger = HistoryLedger(transport, now_factory=lambda: NOW)
    if enabled:
        ledger.set_device_enabled(str(enabled), True)
    return ledger, transport


class FormatTests(unittest.TestCase):
    def test_history_clock_is_novosibirsk(self):
        now = novosibirsk_now()
        offset = now.utcoffset()
        self.assertIsNotNone(offset)
        self.assertEqual(offset, timedelta(hours=7))

    def test_type_and_limit_never_alias_across_families(self):
        self.assertEqual(ledger_type("NLH"), "NL")
        self.assertEqual(ledger_type("RING"), "NL")
        self.assertEqual(ledger_type("Ring"), "NL")
        self.assertEqual(ledger_type("CASH"), "NL")
        self.assertEqual(ledger_type("PLO"), "PLO4")
        self.assertEqual(ledger_type("PLO5"), "PLO5")
        self.assertEqual(ledger_type("bombpot"), "NLB")
        self.assertEqual(ledger_limit(0.02), "0,01/0,02")
        self.assertEqual(ledger_limit(0.05), "0,02/0,05")
        self.assertNotEqual(ledger_type("NLH"), ledger_type("PLO"))

    def test_cgm_history_limit_strings_strip_trailing_zeros(self):
        # Exact History Limit cells from CGM (2).xlsx shared strings.
        self.assertEqual(ledger_limit(0.02), "0,01/0,02")
        self.assertEqual(ledger_limit(0.05), "0,02/0,05")
        self.assertEqual(ledger_limit(0.1), "0,05/0,1")
        self.assertEqual(ledger_limit(0.2), "0,1/0,2")
        self.assertEqual(ledger_limit(0.4), "0,2/0,4")
        self.assertEqual(ledger_limit(0.5), "0,25/0,5")
        self.assertEqual(ledger_limit(1.0), "0,5/1")
        self.assertEqual(euro_amount(0.10), "0,1")
        self.assertEqual(euro_amount(1.0), "1")
        self.assertEqual(normalize_limit("0,05/0,10"), "0,05/0,1")
        self.assertEqual(normalize_limit("0,50/1,00"), "0,5/1")
        found, open_row = find_last_matching_row(
            [
                list(HISTORY_HEADERS),
                assemble_open_row(
                    nickname="Vaterv", game_type="NL", limit="0,05/0,1",
                    started_at=NOW, balance_st=19.47, hands_s=10,
                ),
            ],
            nickname="Vaterv",
            game_type="NLH",
            limit=ledger_limit(0.1),
        )
        self.assertEqual(found, 2)
        self.assertTrue(open_row)


class RowTests(unittest.TestCase):
    def test_first_hand_appends_open_row_for_nickname(self):
        ledger, transport = _ledger()
        session = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=694,
            wallet_cash=19.47,
        )
        self.assertIsNotNone(session)
        self.assertEqual(session.row_number, 2)
        row = transport.rows[1]
        self.assertEqual(row[1], "Vaterv")
        self.assertEqual(row[2], "coin")
        self.assertEqual(row[9], "NL")
        self.assertEqual(row[10], NL)
        self.assertEqual(row[11], 694)
        self.assertEqual(row[6], 19.47)
        self.assertEqual(row[4], "")
        self.assertEqual(row[12], "")
        self.assertTrue(is_open_history_row(row))
        self.assertEqual(assemble_open_row(
            nickname="Vaterv", game_type="NLH", limit=NL,
            started_at=NOW, balance_st=19.47, hands_s=694,
        )[:4], row[:4])

    def test_last_open_row_is_filled_not_appended(self):
        existing = [
            list(HISTORY_HEADERS),
            assemble_open_row(
                nickname="Vaterv", game_type="NL", limit=NL,
                started_at=NOW - timedelta(hours=1), balance_st=4.0, hands_s=694,
            ),
        ]
        ledger, transport = _ledger(existing)
        session = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=700,
        )
        self.assertEqual(session.row_number, 2)
        self.assertEqual(session.hands_s, 694)
        self.assertEqual(len(transport.rows), 2)
        self.assertEqual(transport.rows[1][11], 694)
        self.assertEqual(transport.rows[1][6], 4.0)

    def test_finished_row_is_never_rewritten(self):
        closed = assemble_close_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=2.0, profit=0.4, hands_s=600, hands=20,
        )
        ledger, transport = _ledger([list(HISTORY_HEADERS), closed])
        session = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=620,
        )
        self.assertEqual(session.row_number, 3)
        self.assertEqual(transport.rows[1], closed[:COLUMNS] if len(closed) >= COLUMNS else closed)
        self.assertTrue(is_open_history_row(transport.rows[2]))
        self.assertEqual(session.hands_s, 620)

    def test_nl_and_plo_same_limit_are_two_rows(self):
        ledger, transport = _ledger()
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=10,
        )
        ledger.on_hand_started(
            device_id="dev", table_id=12, nickname="Vaterv",
            game_type="PLO4", coin_bb=0.02, stack=2.0, session_hands=10,
        )
        self.assertEqual(len(transport.rows), 3)
        self.assertEqual(transport.rows[1][9], "NL")
        self.assertEqual(transport.rows[2][9], "PLO4")
        self.assertEqual(transport.rows[1][10], transport.rows[2][10])

    def test_two_nl_tables_sum_into_one_row(self):
        ledger, transport = _ledger()
        a = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=10,
            wallet_cash=19.47,
        )
        writes_before = len(transport.ops)
        b = ledger.on_hand_started(
            device_id="dev", table_id=12, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=10,
            wallet_cash=19.47,
        )
        self.assertIs(a, b)
        self.assertEqual(len(transport.rows), 2)
        self.assertEqual(a.balance_st, 19.47)
        self.assertEqual(transport.rows[1][6], 19.47)
        self.assertEqual(len(transport.ops), writes_before)
        self.assertEqual(sorted(a.tables), ["dev:11", "dev:12"])

    def test_balance_st_is_last_nick_end_not_stack_sum(self):
        # CGM rows 395/399/402: Vaterv End 18.86 -> rakeback End 19.47 -> open NL St 19.47.
        closed_nl = assemble_close_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=18.34, profit=0.52, hands_s=0, hands=694,
        )
        rakeback = [
            46255, "Vaterv", "coin", 0.0, 0.0, 0,
            18.86, 19.47, 0.61, "rakeback", "", 0, 0, 0,
        ]
        self.assertEqual(seed_balance_st([list(HISTORY_HEADERS), closed_nl, rakeback], "Vaterv"), 19.47)
        ledger, transport = _ledger([list(HISTORY_HEADERS), closed_nl, rakeback])
        first = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=694,
        )
        self.assertEqual(first.balance_st, 19.47)
        self.assertEqual(transport.rows[3][6], 19.47)
        second = ledger.on_hand_started(
            device_id="dev", table_id=12, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=694,
        )
        self.assertIs(first, second)
        self.assertEqual(second.balance_st, 19.47)
        self.assertEqual(transport.rows[3][6], 19.47)
        self.assertEqual(len(transport.rows), 4)

    def test_open_row_402_st_kept_when_sibling_sits(self):
        open_nl = assemble_open_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW - timedelta(minutes=10), balance_st=19.47, hands_s=694,
        )
        ledger, transport = _ledger([list(HISTORY_HEADERS), open_nl])
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=800,
        )
        ledger.on_hand_started(
            device_id="dev", table_id=12, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=800,
        )
        self.assertEqual(len(transport.rows), 2)
        self.assertEqual(transport.rows[1][6], 19.47)

    def test_wallet_cash_seeds_when_history_has_no_nick(self):
        ledger, transport = _ledger()
        session = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=0,
            wallet_cash=19.47,
        )
        self.assertEqual(session.balance_st, 19.47)
        self.assertEqual(transport.rows[1][6], 19.47)

    def test_two_nl_02_one_nl_05_two_plo5_are_three_rows(self):
        ledger, transport = _ledger()
        for tid in (11, 12):
            ledger.on_hand_started(
                device_id="dev", table_id=tid, nickname="Vaterv",
                game_type="NLH", coin_bb=0.2, stack=20.0, session_hands=0,
            )
        ledger.on_hand_started(
            device_id="dev", table_id=13, nickname="Vaterv",
            game_type="NLH", coin_bb=0.5, stack=50.0, session_hands=0,
        )
        for tid in (21, 22):
            ledger.on_hand_started(
                device_id="dev", table_id=tid, nickname="Vaterv",
                game_type="PLO5", coin_bb=0.2, stack=20.0, session_hands=0,
            )
        data = [row for row in transport.rows[1:] if any(str(c).strip() for c in row)]
        self.assertEqual(len(data), 3)
        families = {(str(row[9]), str(row[10])) for row in data}
        self.assertEqual(
            families,
            {("NL", "0,1/0,2"), ("NL", "0,25/0,5"), ("PLO5", "0,1/0,2")},
        )


class RakebackTests(unittest.TestCase):
    def test_positive_end_to_st_gap_writes_rakeback_then_game(self):
        closed = assemble_close_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=18.34, profit=0.52, hands_s=0, hands=694,
        )
        self.assertEqual(closed[7], 18.86)
        self.assertEqual(rakeback_delta([list(HISTORY_HEADERS), closed], "Vaterv", 19.47), 0.61)
        ledger, transport = _ledger([list(HISTORY_HEADERS), closed])
        session = ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=694,
            wallet_cash=19.47,
        )
        self.assertEqual(transport.rows[2][9], "rakeback")
        self.assertEqual(transport.rows[2][6], 18.86)
        self.assertEqual(transport.rows[2][7], 19.47)
        self.assertEqual(transport.rows[2][8], 0.61)
        self.assertEqual(session.balance_st, 19.47)
        self.assertEqual(transport.rows[3][9], "NL")
        self.assertEqual(transport.rows[3][6], 19.47)
        expected = assemble_rakeback_row(
            nickname="Vaterv", started_at=NOW, balance_st=18.86, profit=0.61,
        )
        self.assertEqual(transport.rows[2], expected)

    def test_negative_gap_is_not_rakeback(self):
        closed = assemble_close_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=18.34, profit=0.52, hands_s=0, hands=10,
        )
        self.assertIsNone(rakeback_delta([list(HISTORY_HEADERS), closed], "Vaterv", 18.00))
        ledger, transport = _ledger([list(HISTORY_HEADERS), closed])
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0,
            wallet_cash=18.00,
        )
        types = [row[9] for row in transport.rows[1:]]
        self.assertNotIn("rakeback", types)
        self.assertEqual(types[-1], "NL")

    def test_deposit_or_withdrawal_between_is_not_rakeback(self):
        closed = assemble_close_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=18.34, profit=0.52, hands_s=0, hands=10,
        )
        deposit = [
            46255, "Vaterv", "coin", 0.0, 0.0, 0,
            18.86, 30.00, 11.14, "deposit", "", 0, 0, 0,
        ]
        rows = [list(HISTORY_HEADERS), closed, deposit]
        self.assertIsNone(rakeback_delta(rows, "Vaterv", 30.00))
        ledger, transport = _ledger(rows)
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0,
            wallet_cash=30.00,
        )
        types = [row[9] for row in transport.rows[1:]]
        self.assertEqual(types.count("rakeback"), 0)
        self.assertEqual(types[-1], "NL")
        self.assertEqual(transport.rows[-1][6], 30.00)


class CloseTests(unittest.TestCase):
    def test_close_by_family_writes_only_finished_type(self):
        ledger, transport = _ledger()
        for tid in (11, 12, 13):
            ledger.on_hand_started(
                device_id="dev", table_id=tid, nickname="Vaterv",
                game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=10,
            )
        for tid in (21, 22):
            ledger.on_hand_started(
                device_id="dev", table_id=tid, nickname="Vaterv",
                game_type="PLO4", coin_bb=0.02, stack=2.0, session_hands=10,
            )
        ledger.on_hand_completed(device_id="dev", table_id=11, hero_profit=0.10)
        ledger.on_hand_completed(device_id="dev", table_id=21, hero_profit=-0.04)
        ledger.on_table_left(device_id="dev", table_id=11)
        ledger.on_table_left(device_id="dev", table_id=12)
        self.assertTrue(is_open_history_row(transport.rows[1]))
        ledger.on_table_left(device_id="dev", table_id=13)
        nl = transport.rows[1]
        plo = transport.rows[2]
        self.assertFalse(is_open_history_row(nl))
        self.assertEqual(nl[9], "NL")
        self.assertEqual(nl[13], 1)
        self.assertTrue(is_open_history_row(plo))
        self.assertEqual(plo[9], "PLO4")
        ledger.on_table_left(device_id="dev", table_id=21)
        self.assertTrue(is_open_history_row(transport.rows[2]))
        ledger.on_table_left(device_id="dev", table_id=22)
        self.assertFalse(is_open_history_row(transport.rows[2]))
        self.assertEqual(transport.rows[2][13], 1)

    def test_flush_device_closes_leftovers(self):
        ledger, transport = _ledger()
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0,
        )
        ledger.on_hand_started(
            device_id="dev", table_id=21, nickname="Vaterv",
            game_type="PLO4", coin_bb=0.02, stack=2.0,
        )
        closed = ledger.flush_device("dev")
        self.assertEqual(len(closed), 2)
        self.assertFalse(is_open_history_row(transport.rows[1]))
        self.assertFalse(is_open_history_row(transport.rows[2]))


class ConsoleTests(unittest.TestCase):
    def test_hands_and_profit_are_independent_per_family(self):
        ledger, _transport = _ledger()
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0,
        )
        ledger.on_hand_started(
            device_id="dev", table_id=21, nickname="Vaterv",
            game_type="PLO4", coin_bb=0.02, stack=2.0,
        )
        ledger.on_hand_completed(device_id="dev", table_id=11, hero_profit=0.20)
        ledger.on_hand_completed(device_id="dev", table_id=11, hero_profit=0.10)
        ledger.on_hand_completed(device_id="dev", table_id=21, hero_profit=-0.06)
        nl = ledger._sessions[next(k for k in ledger._sessions if "|NL|" in k)]
        plo = ledger._sessions[next(k for k in ledger._sessions if "|PLO4|" in k)]
        self.assertEqual(nl.hands, 2)
        self.assertAlmostEqual(nl.profit, 0.30, places=4)
        self.assertEqual(plo.hands, 1)
        self.assertAlmostEqual(plo.profit, -0.06, places=4)
        self.assertAlmostEqual(ledger.table_profit("dev", 11) or 0, 0.30, places=4)
        self.assertAlmostEqual(ledger.table_profit("dev", 21) or 0, -0.06, places=4)

    def test_compact_snapshot_exposes_profit_only_when_uchet_on(self):
        row = {"table_id": 11, "hero_sitting": True}
        on = attach_session_profit(row, 0.42, enabled=True)
        off = attach_session_profit(row, 0.42, enabled=False)
        self.assertEqual(on["session_profit"], 0.42)
        self.assertNotIn("session_profit", off)
        ledger, _transport = _ledger()
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0,
        )
        ledger.on_hand_completed(device_id="dev", table_id=11, table_profit=0.12)
        self.assertEqual(ledger.table_profit("dev", 11), 0.12)
        ledger.set_device_enabled("dev", False)
        self.assertIsNone(ledger.table_profit("dev", 11))

    def test_uchet_off_never_enqueues_sheet_write(self):
        ledger, transport = _ledger(enabled="")
        ledger.set_device_enabled("dev", False)
        self.assertIsNone(
            ledger.on_hand_started(
                device_id="dev", table_id=11, nickname="Vaterv",
                game_type="NLH", coin_bb=0.02, stack=2.0,
            )
        )
        ledger.on_hand_completed(device_id="dev", table_id=11, hero_profit=1.0)
        ledger.on_table_left(device_id="dev", table_id=11)
        self.assertEqual(transport.ops, [])
        self.assertEqual(len(transport.rows), 1)


class AuthTests(unittest.TestCase):
    def test_missing_credential_uses_dry_transport(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no-such.json"
            transport = open_transport(missing)
            self.assertEqual(type(transport).__name__, "DrySheetsTransport")
            cells = assemble_open_row(
                nickname="Vaterv", game_type="NL", limit=NL,
                started_at=NOW, balance_st=2.0, hands_s=1,
            )
            row_number = transport.append_row(cells)
            self.assertEqual(row_number, 2)
            self.assertEqual(transport.get_history()[1][:11], cells[:11])

    def test_fake_cells_match_assembler(self):
        ledger, transport = _ledger()
        ledger.on_hand_started(
            device_id="dev", table_id=11, nickname="Vaterv",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=5,
            wallet_cash=19.47,
        )
        ledger.on_hand_completed(device_id="dev", table_id=11, hero_profit=0.08)
        ledger.on_table_left(device_id="dev", table_id=11, wallet_cash=19.55)
        expected = assemble_close_row(
            nickname="Vaterv", game_type="NL", limit=NL,
            started_at=NOW, ended_at=NOW, balance_st=19.47,
            profit=0.08, hands_s=5, hands=1, balance_end=19.55,
        )
        self.assertEqual(transport.rows[1], expected)

    def test_default_credential_path_points_at_secrets(self):
        path = default_credential_path()
        self.assertEqual(path.name, "google-sheets.json")
        self.assertIn("secrets", str(path).replace("\\", "/"))

    def test_observe_uses_shipped_hand_events(self):
        ledger, transport = _ledger()
        started = SimpleNamespace(
            kind="hand_started",
            device_id="dev",
            table_id=11,
            game_type="NLH",
            coin_bb=0.02,
            detail={"nickname": "Vaterv", "stack": 2.0, "session_hands": 3},
        )
        ledger.observe(started)
        finished = SimpleNamespace(
            kind="hand_completed",
            device_id="dev",
            table_id=11,
            game_type="NLH",
            coin_bb=0.02,
            detail={"table_profit": 0.05},
        )
        ledger.observe(finished)
        left = SimpleNamespace(
            kind="table_close",
            device_id="dev",
            table_id=11,
            game_type="NLH",
            coin_bb=0.02,
            detail={},
        )
        ledger.observe(left)
        self.assertFalse(is_open_history_row(transport.rows[1]))
        found, open_row = find_last_matching_row(
            transport.rows, nickname="Vaterv", game_type="NL", limit=NL,
        )
        self.assertEqual(found, 2)
        self.assertFalse(open_row)


class FrozenObservationRoutingTests(unittest.TestCase):
    def test_hand_started_does_not_mutate_frozen_router_observation(self):
        from core.production_runtime import OperatorConsole
        from core.v6router.router import RouterObservation

        class _Log:
            def emit(self, *args, **kwargs):
                return {}

        ledger, _transport = _ledger(enabled="dev-a")
        console = OperatorConsole(_Log(), 1)
        console.history_ledger = ledger
        console._device_nick["dev-a"] = "Vaterv"
        obs = RouterObservation(
            kind="hand_started",
            device_id="dev-a",
            table_id=11,
            game_type="NLH",
            coin_bb=0.02,
            hand_id="h1",
            detail={"stack": 2.0},
        )
        console.observation(obs)
        console.observation(
            RouterObservation(
                kind="hand_completed",
                device_id="dev-a",
                table_id=11,
                game_type="NLH",
                coin_bb=0.02,
                hand_id="h1",
                detail={"table_profit": 0.04},
            )
        )
        console.observation(
            RouterObservation(
                kind="table_close",
                device_id="dev-a",
                table_id=11,
                detail={},
            )
        )

    def test_failsafe_and_sitout_emit_explicit_nonscript_lines(self):
        from core.production_runtime import OperatorConsole
        from core.v6router.router import RouterObservation

        class _Log:
            def __init__(self):
                self.events = []

            def emit(self, event, **kwargs):
                self.events.append((event, kwargs.get("message"), kwargs))
                return {}

        log = _Log()
        console = OperatorConsole(log, 1)
        console.observation(
            RouterObservation(
                kind="bridge_diag",
                device_id="dev-a",
                table_id=11,
                detail={
                    "tag": "fallback_ready",
                    "action": "FOLD",
                    "reason": "CC_TIMEOUT",
                    "hand": "TT",
                    "position": "BTN",
                    "facing": "RERAISE",
                },
            )
        )
        console.observation(
            RouterObservation(
                kind="bridge_diag",
                device_id="dev-a",
                table_id=11,
                detail={"tag": "action_confirmed", "action": "FOLD", "source": "failsafe", "hand": "TT"},
            )
        )
        console.observation(
            RouterObservation(
                kind="bridge_diag",
                device_id="dev-a",
                table_id=0,
                detail={"tag": "sitout_detected"},
            )
        )
        events = [row[0] for row in log.events]
        self.assertIn("operator.nonscript_fold", events)
        self.assertIn("operator.nonscript_ack", events)
        self.assertIn("operator.nonscript_sitout", events)


class SitOpenAndLobbyProfitTests(unittest.TestCase):
    def test_sit_opens_unclosed_row_before_first_hand(self):
        ledger, transport = _ledger()
        session = ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, stack=2.0, wallet_cash=19.47,
        )
        self.assertIsNotNone(session)
        self.assertEqual(session.row_number, 2)
        self.assertEqual(session.hands, 0)
        self.assertTrue(is_open_history_row(transport.rows[1]))
        self.assertEqual(transport.rows[1][1], "Weedman834")
        self.assertEqual(transport.rows[1][6], 19.47)
        docs = ledger.session_docs("dev", 11)
        self.assertEqual(docs["status"], "written")
        self.assertEqual(docs["hands"], 0)
        row = attach_session_docs({}, docs, enabled=True)
        self.assertEqual(row["docs_status"], "written")
        missing = attach_session_docs({}, None, enabled=True)
        self.assertEqual(missing["docs_status"], "missing")
        off = attach_session_docs({}, None, enabled=False)
        self.assertNotIn("docs_status", off)

    def test_coin_ring_writes_nl_not_ring(self):
        ledger, transport = _ledger()
        session = ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="RING", coin_bb=0.02, wallet_cash=19.47,
        )
        self.assertIsNotNone(session)
        self.assertEqual(session.game_type, "NL")
        self.assertEqual(transport.rows[1][9], "NL")
        self.assertNotEqual(transport.rows[1][9], "RING")

    def test_table_stack_is_never_balance_st(self):
        ledger, transport = _ledger()
        session = ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=0,
        )
        self.assertIsNotNone(session)
        self.assertNotEqual(session.balance_st, 2.0)
        self.assertEqual(transport.rows[1][6], "")
        self.assertEqual(transport.rows[1][11], 0)

    def test_previous_end_and_hands_e_seed_when_lobby_unknown(self):
        closed = assemble_close_row(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=18.34, profit=0.52, hands_s=0, hands=694,
        )
        self.assertEqual(closed[7], 18.86)
        ledger, transport = _ledger([list(HISTORY_HEADERS), closed])
        session = ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, stack=2.0, session_hands=0,
        )
        self.assertEqual(session.balance_st, 18.86)
        self.assertEqual(session.hands_s, 694)
        self.assertEqual(transport.rows[2][6], 18.86)
        self.assertEqual(transport.rows[2][11], 694)

    def test_close_without_lobby_does_not_write_stack_delta(self):
        ledger, transport = _ledger()
        ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, stack=2.0, wallet_cash=20.00,
        )
        ledger.on_hand_completed(device_id="dev", table_id=11, table_profit=-0.07)
        ledger.on_table_left(device_id="dev", table_id=11)
        row = transport.rows[1]
        self.assertFalse(is_open_history_row(row))
        self.assertEqual(row[6], 20.0)
        self.assertEqual(row[7], 19.93)
        self.assertNotEqual(row[7], 1.93)
        self.assertEqual(row[13], 1)

    def test_ru_formulas_use_semicolon_not_comma_args(self):
        from core.v6router.history_ledger import (
            PROFIT_FORMULA, google_duration_formula, google_time_formula,
        )
        self.assertIn(";", PROFIT_FORMULA)
        self.assertNotIn("INDEX(G:G,ROW())", PROFIT_FORMULA)
        self.assertEqual(google_time_formula(NOW), "=TIME(18;0;0)")
        self.assertTrue(google_duration_formula(NOW, NOW).startswith("=TIME("))
        self.assertIn(";", google_duration_formula(NOW, NOW))

    def test_late_lobby_wallet_writes_rakeback(self):
        closed = assemble_close_row(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=18.34, profit=0.52, hands_s=0, hands=10,
        )
        ledger, transport = _ledger([list(HISTORY_HEADERS), closed])
        first = ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02,
        )
        self.assertEqual(first.balance_st, 18.86)
        self.assertNotIn("rakeback", [row[9] for row in transport.rows[1:]])
        ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, wallet_cash=19.47,
        )
        types = [row[9] for row in transport.rows[1:]]
        self.assertEqual(types.count("rakeback"), 1)
        self.assertEqual(transport.rows[2][9], "NL")
        self.assertEqual(transport.rows[2][6], 19.47)
        self.assertEqual(transport.rows[3][9], "rakeback")
        self.assertEqual(transport.rows[3][6], 18.86)
        self.assertEqual(transport.rows[3][7], 19.47)

    def test_close_uses_lobby_end_not_table_stack_deltas(self):
        ledger, transport = _ledger()
        ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, wallet_cash=20.00,
        )
        ledger.on_hand_completed(device_id="dev", table_id=11, table_profit=-0.03)
        ledger.on_table_left(device_id="dev", table_id=11, wallet_cash=19.50)
        row = transport.rows[1]
        self.assertFalse(is_open_history_row(row))
        self.assertEqual(row[6], 20.0)
        self.assertEqual(row[7], 19.5)

    def test_dry_transport_is_visible_as_docs_missing(self):
        ledger = HistoryLedger(DrySheetsTransport(reason="missing_credential"), now_factory=lambda: NOW)
        ledger.set_device_enabled("dev", True)
        ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="NLH", coin_bb=0.02, wallet_cash=10.0,
        )
        docs = ledger.session_docs("dev", 11)
        self.assertEqual(docs["status"], "dry")

    def test_persist_reloads_open_sit_row(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "history_ledger.json"
            first = HistoryLedger(FakeSheetsTransport(), now_factory=lambda: NOW, persist_path=path)
            first.set_device_enabled("dev", True)
            first.on_table_sat(
                device_id="dev", table_id=11, nickname="Weedman834",
                game_type="NLH", coin_bb=0.02, wallet_cash=10.0,
            )
            self.assertTrue(path.is_file())
            second = HistoryLedger(FakeSheetsTransport(), now_factory=lambda: NOW, persist_path=path)
            second.set_device_enabled("dev", True)
            docs = second.session_docs("dev", 11)
            self.assertIsNotNone(docs)
            self.assertEqual(docs["hands"], 0)
            self.assertEqual(docs["status"], "missing")

    def test_stale_ring_persist_opens_nl_row_and_rakeback(self):
        closed = assemble_close_row(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=3), ended_at=NOW - timedelta(hours=2),
            balance_st=18.34, profit=0.52, hands_s=0, hands=10,
        )
        junk = assemble_close_row(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
            balance_st=2.0, profit=-0.07, hands_s=0, hands=11,
        )
        junk[9] = "RING"
        transport = FakeSheetsTransport([list(HISTORY_HEADERS), closed, junk])
        ledger = HistoryLedger(transport, now_factory=lambda: NOW)
        ledger.set_device_enabled("dev", True)
        ledger._sessions[session_key("Weedman834", "NL", NL)] = OpenSession(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=2), balance_st=2.0,
            row_number=3, sheet_confirmed=False,
        )
        session = ledger.on_table_sat(
            device_id="dev", table_id=11, nickname="Weedman834",
            game_type="RING", coin_bb=0.02, wallet_cash=19.47,
        )
        types = [row[9] for row in transport.rows[1:]]
        self.assertEqual(types[-2], "rakeback")
        self.assertEqual(types[-1], "NL")
        self.assertEqual(transport.rows[-2][6], 18.86)
        self.assertEqual(transport.rows[-2][7], 19.47)
        self.assertEqual(session.game_type, "NL")
        self.assertTrue(session.sheet_confirmed)
        self.assertEqual(ledger.session_docs("dev", 11)["status"], "written")

    def test_ring_end_is_not_rakeback_baseline(self):
        closed = assemble_close_row(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=3), ended_at=NOW - timedelta(hours=2),
            balance_st=18.34, profit=0.52, hands_s=0, hands=10,
        )
        junk = assemble_close_row(
            nickname="Weedman834", game_type="NL", limit=NL,
            started_at=NOW - timedelta(hours=1), ended_at=NOW,
            balance_st=2.0, profit=-0.07, hands_s=0, hands=11,
        )
        junk[9] = "RING"
        rows = [list(HISTORY_HEADERS), closed, junk]
        self.assertIsNone(rakeback_delta(rows, "Weedman834", 12.40))
        self.assertEqual(rakeback_delta(rows, "Weedman834", 19.47), 0.61)


if __name__ == "__main__":
    unittest.main()
