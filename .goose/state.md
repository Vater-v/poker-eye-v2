# Current
- Repo: `C:\projects\pokereye\poker-eye-v2`; clean Git state, latest commit `5007c50`, pushed to origin/main.
- Full suite: 88 tests pass.
- 10/10 local trainer launch smoke test passed; each printed READY and exited cleanly.
- Latest isolated Android build from project-local build assets completed with apksigner v2/v3 verification. Artifact is outside Git at `.dist/v2workspace/out/coinpoker-v2-debug.apk`; SHA-256 `6546972B6814175A26A1669C2B660D64F353FA69A53299C9381781819EA9848A`.
- Baseline APK rollback evidence remains SHA `73E2113...A11AC7C3`; baseline source was not changed.
- Project-local ignored runtime files: `secrets/eye.agent`, `secrets/trainer.secret`, `config/backend_accounts.local.json`.

# Real acceptance
- Target LDPlayer index 2 is NAT (`nic1=nat`), guest `172.16.1.x`, host Ethernet `192.168.0.132`.
- Controlled guest-to-host TCP probes do not establish; trainer gets only `trainer.ready`; no `hello.authenticated` or `transport.connected`.
- Coin launches/authenticates to home, but final test did not enter an active table, so no real hook→EYE→CC→action→ACK evidence exists.
- Android logcat previously proved `Hmuriy: no trainer discovered`; Wi-Fi-bound discovery/TCP diagnostics are in the Android bridge.
- Reversible bridged-mode experiment was attempted and NAT was restored. No ADB reverse/fallback is used.
- Report: `docs/REAL_ACCEPTANCE_2026-08-17.md`.

# Remaining blockers
- Reachable LDPlayer network mode and real UDP/TCP acceptance.
- Active real Coin table hook; PPP/protobuf production adapter and hero identity/routing.
- EYE logical session replay/authentication.
- Reconcile-before-retry/no duplicate action semantics.
- 2/4/5 tables, multi-emulator, reconnect and 10/10 real-device gates.
- Double-board/PLO remains explicitly unsupported.

# Read-only LDPlayer/network investigation 2026-08-17 21:52 +07:00
- Host Ethernet is `192.168.0.132/24` with gateway `192.168.0.1`; target process is `I:\progs\LDPlayer\LDPlayer9\dnplayer.exe index=2|`.
- ADB is present only as lab control (`C:\platform-tools\adb.exe`, server `127.0.0.1:5037`); one device is attached as `emulator-5558` / `PJH110`.
- Guest network is NAT-style `wlan0=172.16.1.37/24`, broadcast `172.16.1.255`; no route/ARP exists from host to `172.16.1.37`. Guest ping to host Ethernet `192.168.0.132` loses 2/2 packets.
- LDPlayer `vms/config/leidian2.config` confirms network enabled but no static address/interface and does not expose a non-ADB UDP/TCP forwarding or bridge setting. `dnconsole help` exposes only lifecycle, `adb`, app, property, and backup commands; no network/port-forward command.
- Project runtime implementation supports UDP IPv4 broadcast `255.255.255.255:37020` plus authenticated TCP trainer endpoint (default EYE `127.0.0.1:17770`), with no ADB fallback. This path cannot be established from current NAT guest to host without changing emulator networking or adding a forwarding mechanism.
- Read-only checks made no emulator/project changes. Safest next action: schedule a reversible LDPlayer bridged-network experiment (with capture/rollback and firewall review), or place trainer/EYE on a guest-reachable endpoint; do not edit config or use ADB reverse during acceptance.

# Read-only audit 2026-08-17
- Audited core/trainer.py, core/transport.py, core/discovery.py, android/HmuriyBridge.java, android/build_v2.ps1 after 5007c50.
- Top correctness gaps: scheduled action consumed on text/malformed WS message (trainer.py:192-208); broad stale/unrelated turn ACK (trainer.py:233-257); Android trusts unauthenticated discovery/welcome (HmuriyBridge.java:331-375, 216-222); Android schedule/connection lifecycle races and null-WS action loss (HmuriyBridge.java:162-170, 184-191, 273-290, 395-400); broadcaster uses disconnected SlotPool (discovery.py:41-53 vs transport.py:33-38, 97-103).
- Additional build risk: plaintext secret embedded in APK and debug keystore (build_v2.ps1:65-70, 96-102).
- Focused unittest: 12 passed; pytest unavailable. No source changes made (state update only).
