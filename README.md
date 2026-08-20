# poker-eye-v2

```
Coin RealWebSocket → libhmuriy.so → TCP :19037 → v6 router → Coin/PPP/PokerEYE bridge
```

## Layout

| path | what |
|---|---|
| `android/` | APK bridge (`HmuriyBridge.java`, native, patches) |
| `core/` | Trainer: `v6router/` + `verified_v1/` |
| `vps/` | systemd units, `install.sh`, built console (`web-dist/`) |
| `web/console/` | Nuxt operator console source |
| `docs/` | live docs (`PEYE_VIEW_IDS.md`, `BUILD.md`) |
| `docs/archive/` | historical notes |

## Operator

Manual sit. Play is protocol CC. No lobby janitor, no auto-join, no auto-leave.

Trainer live: `V7.4.48-HMN1-VPS`. Phone APK: `CoinPoker-PokerEye-V7.4.46-HMN1-VPS.apk` (room-force is trainer-side; no APK bump for 7.4.47/48).

1. Install that APK. Log into Coin on the phone.
2. Sit the tables yourself.
3. Console: **Play** on. Trainer sends FOLD/CHECK/RAISE.
4. **Покинуть все столы** when done. Deposit/withdraw stay in Coin.

| cmd | |
|---|---|
| `RUN.cmd` | local trainer |
| `BUILD_APK.cmd` | Coin APK with current `BUILD_ID` |
| `INSTALL_APK.cmd [serial]` | adb install |
| `DEPLOY_VPS.cmd` | pack `core` + `vps` → VPS |
| `VPS_STATUS.cmd` / `VPS_LOGS.cmd` | remote health |

Diagnostics: `scripts\vps.cmd pool|snapshot|ingress|probe-coin|probe-eye|probe-known`

Captures: `captures/` (meaning-named pcaps). Secrets stay in `secrets/`.
