# Current
- Repo: `C:\projects\pokereye\poker-eye-v2`; clean Git state after commits `987e322` and `cf4f65b`, pushed to origin/main.
- Full suite: 88 tests pass; compileall previously passed.
- v2 Android APK source is `android/HmuriyBridge.java`; latest isolated build/sign artifact (outside Git) SHA-256 `ED22901EF22EEC562949441AB2F7F00A6B454FAD1988CF7054335DEC51A5709D`, apksigner v2/v3 verified.
- Baseline APK was pulled before install and matched SHA `73E2113...A11AC7C3`. Baseline source was not modified.
- Local ignored files now exist: `secrets/eye.agent` copied from ready_v6, `secrets/trainer.secret` generated locally, `config/backend_accounts.local.json` copied, `config/config.example.toml` tracked. No secret is tracked.
- RUN.cmd now reads ignored secrets/trainer.secret or requires POKEREYE_V2_SECRET; no insecure default.

# Real acceptance finding
- Target LDPlayer9 instance index 2 / emulator-5558 was tested with Coin installed/launched and SocksDroid VPN active. Coin reached authenticated home; no active table in final rebuilt run.
- Before Wi-Fi binding patch, logcat repeatedly showed `Hmuriy: bridge fail-open` / `IllegalStateException: no trainer discovered`; trainer had only `trainer.ready`, no hello/auth/transport events.
- Root cause observed: LDPlayer NAT guest wlan0 `172.16.1.49`/`.22`, host Ethernet `192.168.0.132`; guest-to-host broadcast/TCP did not reach host. ADB is not used as fallback. Android bridge now explicitly selects non-VPN Wi-Fi network for UDP bind and TCP socket and logs discovery bind/valid ads.
- Reversible LDPlayer bridged-network experiment was performed and NAT config restored. No legacy/emulator data was intentionally deleted.
- Final report: `docs/REAL_ACCEPTANCE_2026-08-17.md`. Production claim remains blocked; no real hook→EYE→CC→action→ACK claim.

# Technical audit findings applied / remaining
- Fixed numeric-key `userTurnOptions` in Coin resolver.
- Fixed negative protobuf varint compatibility.
- Fixed failed EYE hint delivery leaving watchdog permanently armed; turn is only marked seen after successful delivery.
- Remaining major gaps: production PPP/protobuf adapter and hero identity/routing, EYE logical session replay/auth, reconcile-before-retry semantics, true per-device serialization/queue behavior, real network acceptance.

# Next
1. Rebuild/install latest APK after Wi-Fi patch, ensure Coin is in an active table, run host trainer, inspect `logcat -s Hmuriy` for `discovery UDP bound`, `trainer discovered`, TCP generation/handshake.
2. If still no discovery, configure LDPlayer reachable bridged LAN using its supported UI/service, not ADB reverse; prove with UDP packet capture and TCP HMAC event logs.
3. Implement actual PPP protobuf parser/adapter and EYE session replay before any production acceptance claim.
4. Perform one table, reconnect, 2/4/5 table, multi-emulator and 10/10 launch gates.
