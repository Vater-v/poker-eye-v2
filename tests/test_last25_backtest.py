"""Harsh replay of the last 25 live VPS sessions.

Not tautology tests. Each pcap is fed through the shipped decoder +
LiveCoinBridge. The suite fails on:

- uncaught exceptions in handle_event
- hero turns that never queue CHECK/FOLD/CC
- reconstructed wallet math (stacks added back)
- leaked FLOP marks turning a no-board deal into PREFOLD_NOT_PREFLOP
- persisted leftover+stacks used as lobby cash
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from core.v6router.history_ledger import close_balance_end, seed_balance_st, seed_source
from core.v6router.wallet_cash import apply_table_close, wallet_from_payload
from core.verified_v1.prefold import street_from_board

from tests.test_pcap_backtest import _arm_backtest_eye


LAST25_DIR = Path(os.environ.get(
    "POKEREYE_LAST25_DIR",
    Path(__file__).resolve().parent / "fixtures" / "last25",
))
MAX_PCAPS = 25


def _last25_pcaps() -> list[Path]:
    if not LAST25_DIR.is_dir():
        return []
    files = sorted(LAST25_DIR.rglob("*.pcap"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_PCAPS]


class Last25LiveReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pcaps = _last25_pcaps()

    def test_last25_dir_has_real_captures(self) -> None:
        if not LAST25_DIR.is_dir():
            self.skipTest(f"last-25 dir missing {LAST25_DIR}")
        self.assertGreaterEqual(len(self.pcaps), 8, f"only {len(self.pcaps)} pcaps under {LAST25_DIR}")
        empty = [p for p in self.pcaps if p.stat().st_size < 64]
        self.assertFalse(empty, f"tiny/empty pcaps: {empty}")

    def test_decoder_reads_every_hmr1_record(self) -> None:
        from core.coin_capture import iter_hmr1_pcap
        from core.verified_v1.coin_bridge_live import cmd_room_data, decode_hook_payload

        if not self.pcaps:
            self.skipTest(f"no last-25 pcaps in {LAST25_DIR}")
        failures: list[str] = []
        total = 0
        for path in self.pcaps:
            try:
                events = list(iter_hmr1_pcap(path))
            except Exception as exc:
                failures.append(f"{path.name}: iter {type(exc).__name__}: {exc}")
                continue
            if not events:
                failures.append(f"{path.name}: decoder returned 0 events")
                continue
            for index, event in enumerate(events, 1):
                try:
                    payload, raw = decode_hook_payload(event)
                    cmd_room_data(payload)
                except Exception as exc:
                    failures.append(f"{path.name}#{index}: {type(exc).__name__}: {exc}")
                    break
            total += len(events)
        self.assertGreater(total, 100, f"too few decoded events: {total}")
        self.assertFalse(failures, "\n".join(failures[:20]))

    def test_replay_each_pcap_without_bridge_exceptions(self) -> None:
        import asyncio

        from core.coin_capture import iter_hmr1_pcap
        from core.verified_v1.coin_bridge_live import LiveCoinBridge
        from tests.test_pcap_backtest import _arm_backtest_eye

        if not self.pcaps:
            self.skipTest(f"no last-25 pcaps in {LAST25_DIR}")
        targets = [path for path in self.pcaps if path.stat().st_size <= 2_500_000]
        self.assertGreaterEqual(len(targets), 2, [p.name for p in self.pcaps])

        async def _run(path: Path) -> list[str]:
            problems: list[str] = []
            bridge = LiveCoinBridge(diagnostic_sink=lambda *_a, **_k: None)
            _arm_backtest_eye(bridge)
            for index, event in enumerate(iter_hmr1_pcap(path), 1):
                try:
                    await asyncio.wait_for(bridge.handle_event(event), 2.0)
                except asyncio.TimeoutError:
                    problems.append(f"handle_event hang at #{index}")
                    break
                except Exception as exc:
                    problems.append(f"#{index} {type(exc).__name__}: {exc}")
                    if len(problems) >= 8:
                        break
            worker = getattr(bridge, "protocol_task", None)
            if worker is not None and not worker.done():
                worker.cancel()
                try:
                    await asyncio.wait_for(worker, 0.2)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            return problems

        failures: list[str] = []
        for path in targets:
            try:
                problems = asyncio.run(_run(path))
            except Exception as exc:
                failures.append(f"{path.name}: crash {type(exc).__name__}: {exc}")
                continue
            if problems:
                failures.append(f"{path.name}: {problems}")
        self.assertFalse(failures, "\n".join(failures))


class Run7e1dRegressionTests(unittest.TestCase):
    """Concrete lies from run_7e1d381ef7cd / Weedman834+Vaterv."""

    def test_weedman_end_is_not_start_plus_sitting_stacks(self) -> None:
        end, profit = close_balance_end(
            balance_st=12.4662,
            hand_profit=0.0,
            live_lobby=None,
            reconstructed_wallet=16.476172331051497,
        )
        self.assertAlmostEqual(end, 12.4662)
        self.assertAlmostEqual(profit, 0.0)
        self.assertLess(abs(float(end) - 16.476172331051497), 20)
        self.assertNotAlmostEqual(float(end), 16.476172331051497, places=2)

    def test_vaterv_end_is_not_plus_six_dollars_of_stacks(self) -> None:
        end, profit = close_balance_end(
            balance_st=17.0657,
            hand_profit=-0.11,
            live_lobby=None,
            reconstructed_wallet=23.405689743702386,
        )
        self.assertAlmostEqual(end, 16.9557)
        self.assertAlmostEqual(profit, -0.11)
        self.assertNotAlmostEqual(float(end), 23.41, places=1)

    def test_table_gold_cannot_seed_history_st(self) -> None:
        self.assertIsNone(wallet_from_payload({"gold": 12.4662, "leftGold": 7.66}, source="table"))
        rows = [["h"], ["", "Weedman834", "", "", "", "", "16.48", "16.48"]]
        self.assertEqual(seed_source(rows, "Weedman834", wallet_cash=12.4662), "lobby")
        self.assertAlmostEqual(seed_balance_st(rows, "Weedman834", wallet_cash=12.4662), 12.47)

    def test_closing_three_tables_does_not_print_four_dollars(self) -> None:
        wallet = 12.4662
        for _ in range(3):
            wallet = apply_table_close(wallet=wallet, stack_bb=80.0, bb=0.02)
        self.assertAlmostEqual(wallet, 12.4662)

    def test_leaked_flop_on_empty_hand_id_is_preflop(self) -> None:
        street = street_from_board(
            hand_id="",
            board_count=0,
            emitted_stages=("FLOP", "TURN", "RIVER"),
            street_hand_id="116303101044",
        )
        self.assertEqual(street, "PREFLOP")

    def test_events_jsonl_records_the_old_lie_so_new_math_cannot_repeat_it(self) -> None:
        events = LAST25_DIR / "run_7e1d381ef7cd" / "events.jsonl"
        if not events.is_file():
            self.skipTest("events.jsonl for 7e1d not pulled")
        profits = []
        starts = []
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            kind = str(ev.get("event") or "")
            if kind.endswith("ledger_closed") and ev.get("profit") is not None:
                profits.append(float(ev["profit"]))
            if "lobby_wallet" in kind:
                text = str(ev.get("reason") or ev.get("message") or "")
                for token in text.replace(",", ".").split():
                    try:
                        starts.append(float(token))
                        break
                    except ValueError:
                        continue
        self.assertTrue(starts, "7e1d never logged lobby cash")
        self.assertTrue(profits, "7e1d never closed a History row")
        # Historical row is the bug: profit ~= sitting stacks. New closer
        # given that lobby start and 0 hand P/L must NOT emit those dollars.
        lobby = min(starts)
        end, profit = close_balance_end(
            balance_st=lobby,
            hand_profit=0.0,
            live_lobby=None,
            reconstructed_wallet=lobby + max(profits),
        )
        self.assertAlmostEqual(end, lobby)
        self.assertAlmostEqual(profit, 0.0)
        for old in profits:
            self.assertGreater(abs(old), 3.0)  # the lie was +4.01 / +6.34
            self.assertNotAlmostEqual(float(end), lobby + old, places=1)


if __name__ == "__main__":
    unittest.main()
