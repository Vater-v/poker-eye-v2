# Evidence logging and anomaly cases

## Case: `call=0` after prior raises

The available schema and bridge evidence establish that `ActionNotifyBRC.CallNeedChips=0` is a valid wire value when the hero is already matched and `CHECK` is legal. However, the target capture also shows a separate suspicious pattern:

```text
call=0 -> backend CHECK amount=0 -> Coin CALL betAmount=0
```

This repeated pattern is an action-policy/ordering inconsistency. It is not proof of internet loss, and it is not yet proof that a raise was lost. The plausible causes are stale contribution/current-max state, missing or late `ActionBRC`, duplicate suppression, generation reset, or an event arriving out of order. The current text logs do not include raw protobuf bytes, sequence IDs, or body hashes, so source attribution is not possible from text alone.

For this case, retain observation but add diagnostics and a guard: if `call_need == 0` and CHECK is legal, never silently normalize the selected action to `CALL amount=0`; log a policy/state-gap event and prefer CHECK or require reconciliation according to the action policy.

Required fields: hand, room/table, street, seat, event sequence, source, raw body hash, decoded `CallNeedChips`, contribution/current max/last full raise, legal actions, selected action, bridge generation, backend health.

## Case: double-board / `dealerCardsRit`

The supplied second-board lines are not tied by existing evidence to the target hand capture. The schema has second-board-related fields, and selftests contain `dealerCardsRit`, but the target JSONL does not retain enough board-source/index data to prove whether the live hand was parsed correctly. Hand `112595900229` is also not a clean reproduction because `GAME_IS_BROKEN` was already present.

Do not diagnose this as internet failure yet. Preserve a clean PPP/production double-board reference or a raw capture around `RoundStartBRC`, `ZoomFoldBRC`, dealer-card payloads, `WinnerRSP`, and `RoundOverBRC`. The useful reference must include raw frame order, board index/source, hand/table ID, seat mapping, and expected state transitions.

## Do we need PCAP?

Not for every run. Full PCAP is expensive and difficult to retain. Use three evidence levels:

1. **Normal:** compact stdout + append-only `events.jsonl` with event type, direction, lengths, hashes, sequence/correlation IDs, decoded allowlisted fields, state before/after, and error class.
2. **Targeted forensic:** bounded raw envelope/frames for one selected session/hand, ideally encrypted or access-restricted, with a manifest and retention limit.
3. **Full PCAP:** only for a short, explicitly enabled reproduction window when source attribution cannot be resolved from level 1/2.

A raw frame body hash is usually enough to correlate normalized events without logging the full body. PCAP should be a diagnostic escalation, not the default.

## Proposed layout

```text
logs/
  run_<run_id>/
    manifest.json
    operator.txt
    events.jsonl
    devices/<emulator_name>/
      session_<session_id>/
        events.jsonl
        tables/table_01/events.jsonl
    forensic/              # only explicit debug mode, bounded/optional
```

Technical IDs and human display numbers must be separate. `table_01` is a recyclable operator label; every event also carries stable `table_id` and `table_generation`. Before hero identity is known, use `device/session/connection` paths and add `hero_ref` later in manifest/events; never rename or move the active session based on late identity.

## Event categories

- `trainer.ready`, `broadcast.sent`, `broadcast.received`, `broadcast.ignored`
- `tcp.accept`, `hello.authenticated`, `slot.reserved`, `slot.released`, `heartbeat.missed`, `reconnect.*`
- `coin.frame.in`, `coin.frame.out`, `eye.frame.in`, `eye.frame.out` (metadata/hash, not raw body by default)
- `state.before`, `state.after`, `state.gap`, `board.changed`, `hand.started`, `hand.ended`
- `hint.requested`, `hint.received`, `action.created`, `action.attempt_sent`, `action.ack`, `action.timeout`, `action.reconcile`, `action.finalized`

Critical action/auth/slot events flush immediately. Logging failures must not silently mutate action state; on disk pressure drop forensic/debug events first and keep safety/accounting events.
