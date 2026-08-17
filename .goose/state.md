# State

## Reliability hardening subagent (2026-08-17)
- Updated only `core/trainer.py` and `core/actions.py`.
- Timeout workers now capture the attempt number and refuse to finalize/retry a later attempt.
- Malformed/text/undecodable frames do not consume attempts (existing gate retained).
- Actions capture the pre-action turn identity; same/replayed `game.user_turn` cannot ACK. Turn identity is tracked per table.
- Callback routing requires explicit `table_id`; device identity is kept distinct from table identity. Missing/ambiguous callback identity is logged as unrouted/uncertain rather than guessed.
- Action scheduler key uses callback physical device identity when available, preserving one action per physical device while table state remains separate.
- Focused trainer tests passed. Full suite passed on rerun: 96 tests. First full run had one transient callback socket race; isolated bootstrap and rerun passed.

## Bootstrap lease lifecycle implementation (2026-08-17)
- Updated only core/bootstrap.py and tests/test_bootstrap.py.
- Force reallocation closes prior listener/connections, releases old lease, and allocates fresh token/generation.
- Callback hello requires non-empty table_id; welcome includes it; 3-argument handlers receive (device_id, table_id, message), with legacy 2-argument compatibility.
- Authenticated callback disconnect releases current lease and listener. Failed callback bind rolls back reservation. stop() closes listeners/connections and releases leases.
- Verification: python -m unittest -v tests.test_bootstrap (9 passed); bootstrap + trainer E2E (16 passed).

## Android network stall hardening (2026-08-18)
- Updated only `android/HmuriyBridge.java`. First-channel bootstrap/discovery is fail-open with one attempt, 1s bootstrap/connect timeouts, and 15s bootstrap retry backoff; established channels retain 3x/3s reconnect behavior.
- Production diagnostics are controlled by `PRODUCTION_DIAGNOSTICS`, default false; existing diagnostic hooks remain available.
- Verification: `javac -source 8 -target 8 -cp C:\Android\platforms\android-35\android.jar` passed (4 standard warnings). d8/APK build not run.
