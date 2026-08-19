# poker-eye-v2 — production native runtime

Runtime path only:

`Coin RealWebSocket → libhmuriy.so → TCP/19037 → verified v6 DeviceIngressRouter → verified v1 Coin/PPP/PokerEYE bridge`.

Properties:

- one persistent authenticated TCP connection per physical Android process;
- Coin WebSocket thread never waits for Trainer/network and does no Base64/JSON;
- real SmartFox room/table routing is performed before mutable table state;
- `game.game_init` is the table-admission edge; waitlist/preview rooms do not lease accounts;
- one backend account per admitted table;
- one v6 `ActionArbiter` per physical device serializes actions across tables;
- passive frames are one-way; Trainer only sends heartbeat ACK, action, or cancel;
- native `Hmuriy` perf counters and Perfetto profiling are available.

Production secrets remain local under `secrets/` and are not committed or packaged by the installer.
