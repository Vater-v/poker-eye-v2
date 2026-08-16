# Evidence logging and anomaly cases

## `call=0` after raises

`ActionNotifyBRC.CallNeedChips=0` is a valid wire value when the hero is matched and CHECK is legal. The target capture also demonstrates a suspicious repeated policy pattern: `call=0 -> backend CHECK amount=0 -> Coin CALL betAmount=0`. This is not proof of internet loss, but it is an action/order/state inconsistency. A high raise being lost is plausible but unproven because current text logs lack raw protobuf bytes, sequence IDs and body hashes.

Potential causes: stale contribution/current-max state, missing/late `ActionBRC`, duplicate suppression, generation reset, or out-of-order notify. Add a guard: if `call_need=0` and CHECK is legal, never silently normalize to `CALL amount=0`; emit `state.gap` and prefer CHECK or reconcile according to policy.

Required fields: hand/table/street/seat, source sequence, raw body hash, call/min/max/can, contribution/current max/last full raise, legal actions, selected action, bridge generation, backend health.

## Double-board

The supplied `dealerCardsRit` lines are not tied by evidence to the target hand. Selftests contain second-board markers, but target JSONL lacks board source/index and raw dealer-card payload. Hand `112595900229` is not a clean reproduction because `GAME_IS_BROKEN` preceded it. A clean PPP/raw reference should include `RoundStartBRC`, `ZoomFoldBRC`, dealer-card payloads, `WinnerRSP`, `RoundOverBRC`, IDs and ordering.

## PCAP policy

Do not write full PCAP by default. Normal logs use compact decoded allowlisted metadata, frame lengths, hashes, sequence/correlation IDs, state before/after and errors. Targeted forensic mode stores bounded raw frames for one session/hand. Full PCAP is an explicit escalation only when source attribution remains unresolved.

## Layout

```text
logs/run_<run_id>/manifest.json
logs/run_<run_id>/operator.txt
logs/run_<run_id>/events.jsonl
logs/run_<run_id>/devices/<emulator>/session_<session>/events.jsonl
logs/run_<run_id>/devices/<emulator>/session_<session>/tables/table_01/events.jsonl
```

Technical IDs and recyclable human table numbers are separate. Before hero identity is known use device/session/connection; add hero_ref later without renaming the active session.

Event categories cover broadcast, TCP/auth/slot/heartbeat/reconnect, Coin/EYE frame metadata, state/board/hand, hint, action attempts/ACK/timeout/reconcile/finalize. Critical events flush immediately; drop forensic events first under disk pressure.
