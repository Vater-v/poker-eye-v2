# Plan for the next test session

## Current verdict

The `call=0` incident is not closed as harmless CHECK. The available capture proves a repeated inconsistency (`call=0` -> backend CHECK 0 -> Coin CALL 0), but does not prove whether a raise was lost. Do not change action policy blindly before a clean reference is correlated.

The double-board hand is not a clean reproduction because `GAME_IS_BROKEN` preceded it. A clean PPP reference is required.

## What to provide

Provide a short clean PCAP for a normal single-board hand containing a raise before the hero decision, and/or a clean PPP double-board PCAP. Note approximate time, emulator, profile/stakes, PPP vs Coin/EYE, expected result, and whether it is raw device or host/EYE traffic. No credentials or unrelated captures are needed.

## Test progression

1. Start v2 from local disk; confirm READY and run manifest/operator log.
2. One emulator/one table; save the run directory.
3. Reproduce a known raise and correlate frame metadata/hash, decoded CallNeedChips/min/max/can, state before/after, hint, action and ACK.
4. Use a clean double-board hand from RoundStartBRC through RoundOverBRC, with board source/index and WinnerRSP correlation.
5. Progress: 1 device/1 table -> 1/2 -> 1/4 -> 1/5 -> 2/2 each -> 2/4–5 each. Check no duplicate action IDs, stale ACK mutations or global blocking.

## PCAP policy

Always-on per-table PCAP is acceptable only as a bounded filtered ring: `tcp port 17770` (or exact verified port), 4 x 64 MiB segments/table, 256 MiB/table cap, 3 completed captures retained. JSONL metadata remains primary. Do not use unrestricted `tcpdump -i any -s 0 -w`.

## Implementation order

Keep root `main.py` and small `core/` modules. Wire session logging, add pure state/hint/action modules, port tested EYE framing helpers, add Android discovery in an isolated APK workspace, then run one-table LAN canary and multi-table soak. Legacy baseline stays untouched. Do not add GUI/web/admin/Telegram or commit raw captures/decompiled APK/.dist/secrets.
