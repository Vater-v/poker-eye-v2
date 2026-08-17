# Production readiness / real acceptance report

## Scope
Target: LDPlayer9 instance index 2 (`emulator-5558`, CoinPoker, root/ADB lab
control only). Baseline `ready_v6` and baseline APK were not modified.
Double-board/PLO remains explicitly unsupported; the reference PCAP is NLH
single-board only.

## Verified

- v2 source repository is `C:\projects\pokereye\poker-eye-v2`.
- Current full suite: **88 tests pass**, `compileall` passes.
- Local end-to-end test proves: authenticated HMAC TCP handshake, slot
  reservation after handshake, heartbeat, ws_message response, EYE CC,
  action `schedule_send`, action packet generation, ACK/reconciliation,
  JSONL ledger success, exactly three failed attempts, and logs.
- Android isolated build completed with apktool, javac, d8, zipalign and
  apksigner. Latest artifact is outside Git:

  ```text
  .dist/v2workspace/out/coinpoker-v2-debug.apk
  SHA-256: ED22901EF22EEC562949441AB2F7F00A6B454FAD1988CF7054335DEC51A5709D
  ```

  Signature verification observed: APK Signature Scheme v2/v3 true.
- Baseline rollback APK was pulled before v2 installation and SHA-256 matched
  the recorded baseline (`73E2113...A11AC7C3`).
- Local v2 secret is stored only in ignored `secrets/trainer.secret`; legacy
  `.eye` and `backend_accounts.local.json` are copied into ignored v2 paths.
- Android bridge now binds discovery/TCP to the non-VPN Wi-Fi network where
  Android exposes `wlan0`, and logs discovery bind/valid advertisements.
- LDPlayer NAT configuration was restored after a reversible bridge experiment;
  backup is not retained in the project tree.

## Real emulator result: BLOCKED before v2 transport

Observed on the target emulator:

- Coin starts and reaches the authenticated home screen (`Vaterv`, online).
- Before Wi-Fi binding fix, logcat repeatedly showed:

  ```text
  Hmuriy: bridge fail-open
  java.lang.IllegalStateException: no trainer discovered
  ```

- No `trainer.discovered`, `hello.authenticated`, or `transport.connected` event
  was recorded by the trainer during the real run.
- LDPlayer NAT guest address was `172.16.1.49`/later `172.16.1.22`; host has
  `192.168.0.132`. Broadcast/TCP from the guest to host did not establish a
  connection. ADB connectivity is not used as runtime transport.
- The emulator had SocksDroid VPN `tun0` active during the first run; v2 now
  deliberately selects Wi-Fi for discovery/TCP instead of VPN.
- After reinstall/relaunch with the rebuilt APK, Coin was at home rather than
  inside an active table, so no real hook frame was generated in the final
  run. Thus real hint→CC→action→ACK is **not claimed**.

## Technical gaps still blocking production claim

1. LDPlayer must provide a reachable host/guest LAN or bridged adapter. NAT
   broadcast isolation is an environment limitation, not solved by ADB reverse
   (ADB reverse is prohibited by the v2 contract).
2. Trainer's live Coin path still needs a PPP/protobuf adapter: the reference
   capture is 4-byte-JSON + `pb.*`, while CoinPoker live hook frames are SFS2X.
   Both codecs exist, but automatic production routing/hero identity is not
   yet complete.
3. Real EYE session replay/authentication and direct backend credential use are
   not yet fully wired into the trainer runtime.
4. Final real gates remain open: 1 table, reconnect/no duplicate, 2/4/5 tables,
   multiple emulators, and 10/10 launch soak.

## Do not interpret as passed

Screenshots, `adb devices`, a listener, a successful APK install, or local fake
TCP tests are not proof of real v2 transport. The release remains **not
production-ready** until the real acceptance sequence records UDP discovery,
HMAC TCP, hook metadata, EYE CC, actual Coin action, reconciliation/ACK, ledger,
and reconnect evidence in one run directory.
