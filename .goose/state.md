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

## Cleanup/build/install (2026-08-18)
- Cleaned generated artifacts: removed `.v2-repair-backup-20260818-010414`, root `logs`, Python `__pycache__`, disposable `.dist\\v2workspace` before rebuild, `.dist\\phone_logs`, and phone acceptance output files. Preserved `.dist\\baseline`, `.dist\\tooling`, and `.dist\\latest`.
- Built isolated APK from `.dist\\baseline\\coin` using `android\\build_v2.ps1`; apktool, d8, zipalign, apksigner verification passed (v2/v3).
- Latest APK: `.dist\\latest\\poker-eye-v2-latest.apk`; SHA-256: E3AC2650B40E0F74B1910B43C4C53E24685E83E97EFDB3A099E874DBBECD8DD7
; versionName 1.27.0, versionCode 2.
- Installed successfully via ADB on device `8a0d2d77` and launched `com.coingames.coinpoker/.MainActivity`; no crash was observed in the checked logcat window.
- Activity launch returned “current task brought to front”; Android focus later moved to another app, so Coin process/activity launch is verified but foreground persistence is not.


## Android EPERM discovery fix (2026-08-18)
- Root cause: Network 250 bindSocket rejected with EPERM; Android/VPN policy, not trainer protocol.
- Discovery now falls back to default network and concise status.
- Public 37.192.228.101:19037 unreachable while local 0.0.0.0:19037 listens; forwarding remains blocker.
- APK rebuilt/installed; SHA256 1C4C887A04A69C550753F0D2A91B4F391189432E6F8B8A801CB46F43AA1E2D73.

## Callback EPERM fallback (2026-08-18)
- Confirmed VPN is not required: EPERM occurred because callback connect used Network.getSocketFactory() without fallback.
- Callback TCP now retries through the default system route when preferred Network binding is rejected.
- Rebuilt/installed APK SHA256 3981057E85D79104C4CEBDC7453B6B7DC6D3A8DAB12EEBCBB4AC9ABBCFC44391.

## HmuriyBridge duplicate-connection diagnosis (2026-08-18)
- No code edited. Current global IO_LOCK serializes dispatch/ensureConn, but heartbeat and idle sweep can act on a stale Conn after replacement and unconditionally remove the wsId mapping, enabling reconnect churn.
- Minimal lifecycle fix: use a per-ws lock/in-flight connect guard; recheck CONNS under that lock; publish a new Conn only if the old mapping is still the expected one, close/remove with compare-and-remove, and never replace a healthy Conn.
- READ_TIMEOUT_MS=220 is too short for framed trainer exchanges and heartbeat replies; SocketTimeoutException closes the Conn and next frame reconnects. Use ~5-10s read timeout (or separate handshake/read policy), and heartbeat only under the same per-Conn lock/current-map check.

## Reconnect storm and identity fix (2026-08-18)
- Root cause: READ_TIMEOUT_MS was 220ms, so normal/heartbeat replies timed out; cleanup could remove a newer connection and concurrent lifecycle paths were not identity-safe.
- Raised callback read timeout to 5s, added per-WebSocket lifecycle lock, and made cleanup compare-and-remove.
- Trainer connect callback now receives and prints separate device_id and table_id; previous logs mislabeled table as device because callback signature omitted device_id.
- 97 tests pass; APK rebuilt/installed, SHA256 79083BD0F1B3B275C937E991B7658C09D781ECBB23BDCE2D74604112AE2EDA4A.

## Direct public single-port mode (2026-08-18)
- Removed Android UDP discovery and dynamic callback runtime path; Android now targets fixed public 37.192.228.101:19037 with authenticated direct_hello per device/table.
- Trainer direct_public mode is default, starts one authenticated TCP listener on configured public port and does not start bootstrap callback allocator or UDP broadcaster.
- Direct hello HMAC uses device_id|table_id|session_id|protocol_version; duplicate table/device ownership is rejected.
- 99 tests pass; javac passes; APK installed on 8a0d2d77, SHA256 E564C68232690A73825FB9CA2CBE9DFA4F82885DB2A1B1A32551F99585736A3D.
