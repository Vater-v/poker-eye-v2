# poker-eye-v2

Minimal trainer prototype and migration plan.

- `CONCEPT.md` — minimal CLI, multi-table, reliability and broadcast/TCP architecture.
- `main.py`, `discovery.py`, `transport.py`, `protocol.py` — standard-library trainer-side UDP discovery/TCP prototype.
- `tests/` — unittest coverage for slots, framing, authentication and reconnect.
- Decompiled APK/build workspace and `.dist` artifacts remain local and are excluded from Git.

The existing `ready_v6` baseline remains the fallback until v2 passes real emulator and multi-table acceptance gates.
