# poker-eye-v2 — minimal trainer concept

## Goal

Preserve the currently useful behavior—start one CMD, connect EYE/LDPlayer manually, receive hints, and automatically execute verified CC actions—while removing GUI/web/admin/Telegram complexity from the core runtime.

The v2 core must be understandable, observable, restartable, and safe. It must not silently claim success. A real success means: transport connected, hook traffic observed, EYE/backend healthy, hint received, CC acknowledged, and accounting event persisted.

## Operator experience

One entry point:

```cmd
RUN.cmd
```

The console is the source of truth. It prints short, rate-limited, human-readable lifecycle lines:

```text
[+] Trainer корректно запущен, ожидаю подключений.
[+] Новое подключение #1 от Player 4!
[+] Player 4 подключен: transport=tcp-authenticated
[+] Player 4 зашел за новый стол #1
- Ход Player 4 за столом #1! Запрашиваем подсказку...
- Пришла подсказка для Player 4, стол #1: (CALL, 0.02)
[+] Player 4 за столом #1 успешно исполнил CC: (CALL, 0.02)
[-] Подсказка Player 4 / стол #1 не выполнена: timeout; повтор 1/3...
[+] Повтор успешен: Player 4 / стол #1: (CALL, 0.02)
```

Every line has a structured event behind it (JSONL file), but stdout stays compact. Secrets, raw packets, and full traffic dumps never go to normal logs.

## Explicit lifecycle

```text
STARTING
  -> READY (listener/runtime alive)
  -> CONNECTED (emulator transport authenticated)
  -> HOOK_READY (hook handshake and heartbeat)
  -> TABLE_ACTIVE (table identified)
  -> HINT_PENDING
  -> ACTION_SENT
  -> ACTION_ACKED
  -> DEGRADED (bounded recovery)
  -> STOPPING
  -> STOPPED
```

No `RUN_REQUESTED` state may remain indefinitely. Every pending state has a deadline and ends as success, retry, or explicit failure.

## Minimal modules

```text
poker-eye-v2/
  RUN.cmd                 # only operator entry point
  trainer.py              # bounded main loop and signal handling
  config.toml             # explicit safe defaults
  transport.py            # authenticated TCP after UDP IPv4 broadcast discovery
  protocol.py             # framed hook/EYE messages and sequence IDs
  sessions.py             # emulator/table identity and generations
  hints.py                # hint request, validation, deduplication
  actions.py              # CC send, ACK, timeout, retry policy
  ledger.py               # append-only local JSONL/SQLite accounting
  logging.py              # compact stdout + structured JSONL
  tests/
  docs/
```

No GUI, HTTP server, browser admin, Sheets client, or Telegram dependency belongs in the core. External notification/export can be a separate optional process later.

## Reliability contract

- Target scale: multiple emulators with 4–5+ tables per device; capacity is bounded by measured resources, not an arbitrary single-table default.
- Every connection/table/action has a stable ID and generation; stdout uses a recyclable human table number (`стол #1`, `стол #2`) allocated per device and reused after close.
- One in-flight CC per device across all active tables; actions are separated by a sampled human-like delay range.
- Human-like timing: actions involving elements of 60 BB or more may add a configured probability of an extra delay; all delays remain bounded by the server/action deadline.
- When EYE returns a CC, make exactly three explicit attempts: attempt 1 after the calculated delay, attempt 2 after 1 second, attempt 3 after another 1 second. Never create an unbounded retry loop. If the result is uncertain, reconcile before resending.
- Retry only idempotent/replay-safe commands; never blindly duplicate an uncertain action.
- On uncertain timeout: reconcile state first, then retry or mark `NEEDS_OPERATOR`.
- Per-stage deadlines: connect, handshake, hook heartbeat, hint, action ACK, table teardown.
- Exponential backoff with cap and jitter; no infinite hot loop.
- TCP/emulator transport loss is a transport event, not a generic backend error; ADB is never a runtime transport.
- Recovery sequence: mark degraded → stop affected worker → remove mapping → wait for same serial → reinstall mapping → handshake → resume only after fresh state.
- Crash-safe local ledger and startup reconciliation.
- One broken emulator/table cannot stop others.
- Normal logs are bounded and never contain raw payload dumps.

## Runtime transport contract

The only v2 runtime transport is UDP IPv4 broadcast discovery (default interval 1.25 seconds) followed by authenticated TCP LAN. Broadcast advertises trainer endpoint, session nonce, protocol version, and free slots; it never reserves slots and connected clients ignore repeated advertisements. ADB is lab control/observation only, never a runtime fallback. The first implementation uses one TCP connection per table; a later milestone may add logical table channels over one TCP connection per device.

Do not implement unbounded port+1 as the long-term contract. If a temporary port is needed, allocate from a bounded OS-selected range and persist device-to-channel identity separately.

## Acceptance gates

A v2 build is not called ready until:

1. 10/10 starts from local disk succeed and print READY within the budget.
2. One emulator connects, handshakes, and produces hook traffic.
3. One table is discovered and identified.
4. A hint is received, validated, sent, acknowledged, and journaled.
5. A timeout/reconnect does not duplicate an action.
6. Stop produces a final ledger event exactly once.
7. Authenticated TCP loss/reconnect recovers without duplicating an action; ADB remains lab-only.
8. Logs remain readable under multi-table traffic.
9. The current baseline remains runnable unchanged until v2 passes the same evidence gates.

## Non-goals for v2 core

- Web UI, GUI trainer, browser control, Telegram, Google Sheets, raw PCAP/log streaming, and speculative gRPC are not core requirements.
- They may consume the structured event journal in separate adapters after the core is stable.
