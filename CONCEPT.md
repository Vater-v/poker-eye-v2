# poker-eye-v2 — minimal trainer concept

## Goal

Preserve the currently useful behavior—start one CMD, connect EYE/LDPlayer manually, receive hints, and automatically execute verified CC actions—while removing GUI/web/admin/Telegram complexity from the core runtime.

The v2 core must be understandable, observable, restartable, and safe. It must not silently claim success. A real success means: transport connected, hook traffic observed, EYE/backend healthy, hint received, CC acknowledged, and accounting event persisted.

## Operator experience

One entry point: `RUN.cmd`.

The console is the source of truth and prints short, rate-limited, human-readable lifecycle lines such as:

```text
[+] Trainer корректно запущен, ожидаю подключений.
[+] Новое подключение #1 от Player 4!
[+] Player 4 подключен: transport=lan-tcp
[+] Player 4 зашел за новый стол #1
- Ход Player 4 за столом #1! Запрашиваем подсказку...
- Пришла подсказка для Player 4, стол #1: (CALL, 0.02)
[+] Player 4 за столом #1 успешно исполнил CC: (CALL, 0.02)
[-] Подсказка Player 4 / стол #1 не выполнена: timeout; повтор 1/3...
[+] Повтор успешен: Player 4 / стол #1: (CALL, 0.02)
```

Every line has a structured event behind it. Raw packets and full traffic dumps never go to normal logs.

## Scale and action policy

Target scale is multiple emulators with 4–5+ tables per device. Capacity is bounded by measured resources.

- Human-readable table numbers are allocated per device and reused after a table closes.
- One in-flight CC per device across active tables; actions are separated by a sampled human-like delay range.
- Actions involving 60 BB or more may add a configured probability of extra delay, bounded by the server/action deadline.
- Every EYE CC has exactly three explicit attempts: first after calculated delay, second after 1 second, third after another 1 second. No unbounded retry loop.
- On uncertain timeout, reconcile before resending; never blindly duplicate an uncertain action.

## Lifecycle

```text
STARTING -> READY -> DISCOVERED -> AUTHENTICATED -> HOOK_READY -> TABLE_ACTIVE
-> HINT_PENDING -> ACTION_SENT -> ACTION_ACKED -> DEGRADED -> STOPPING -> STOPPED
```

No pending state may remain indefinitely: it ends in success, bounded retry, or explicit failure.

## Minimal core

`RUN.cmd`, `main.py`, `transport.py`, `protocol.py`, `sessions.py`, `hints.py`, `actions.py`, `ledger.py`, `logging.py`.

No GUI, web admin, browser panel, Telegram or Sheets dependency belongs in the core.

## Broadcast/TCP transport

UDP IPv4 broadcast is discovery only. Every 0.5 seconds during bootstrap, the trainer advertises a short-lived slot and metadata. After discovery, the emulator initiates authenticated TCP in the LAN. One TCP connection is created per new table; a slot is reserved immediately and another slot is advertised. Slots are bounded (default 1–3) and released on clean close or heartbeat timeout.

Long-term, prefer one authenticated multiplexed TCP connection per device with logical table channels; retain one-connection-per-table during the first prototype because it matches the requested observable behavior. Do not use unbounded port+1 as an application contract.

Discovery packet contains version, trainer instance, LAN endpoint, nonce/TTL and capabilities only. Commands and hook payloads stay on authenticated TCP. Use per-device credentials, replay protection, sequence/correlation IDs, frame limits and heartbeats.

Migration phases: trainer prototype -> authenticated client in APK -> one-table canary -> 4–5 tables/device -> multiplexing. No ADB fallback is planned for v2; `ready_v6` remains the separate fallback baseline until acceptance is proven.

## Acceptance gates

1. 10/10 starts from local disk print READY.
2. One emulator discovers, authenticates and produces hook traffic.
3. One table is identified and a hint/CC is acknowledged and journaled.
4. Three-attempt retry never duplicates an uncertain action.
5. ADB is not required for v2 transport.
6. Loss/reconnect recovers with fresh handshake and no stale ACK mutation.
7. 4–5+ tables per device pass soak and readable-log tests.
8. Baseline remains runnable unchanged until all gates pass.
