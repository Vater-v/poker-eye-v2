# v2 isolated APK build records

The Android client (UDP broadcast discovery + authenticated TCP handshake +
per-table connections) is `android/HmuriyBridge.java`. It is compiled into
`classes8.dex` and injected into an isolated copy of the Coin APK; the baseline
tree `C:\projects\pokereye\coin` is never modified.

## Build 2026-08-17 (v2 bridge, discovery + handshake)

Artifact (outside Git):

```text
.dist/v2workspace/out/coinpoker-v2-debug.apk
```

- Size: 213,051,259 bytes
- SHA-256: `ED22901EF22EEC562949441AB2F7F00A6B454FAD1988CF7054335DEC51A5709D`
- Bridge source SHA-256: `214920A13176DE2901F435B2EEDCC03DD1458DE1CCC8A7818705B008A65A89A2`
- `apksigner verify`: APK Signature Scheme v2 and v3 verified.
- classes8.dex = 15,624 bytes; contains HmacSHA256 hello/proof, UDP discovery
  listener (port 37020), heartbeat, idle sweep, per-WebSocket connections.
- Built with apktool 3.0.1, javac/d8 (android-35), zipalign + debug keystore.
- The Coin custom Hermes update check is disabled in the isolated copy
  (same verified patch as the baseline build).

## Baseline record (unchanged, for rollback)

```text
.dist/coinpoker-hmuriy-body-disabled-debug.apk
```

- SHA-256: `73E2113CC28C282C506A2FF9CD43FCDFC7032F45B0550A23B0292A11A11AC7C3`
- Size: 213,043,067 bytes

The pristine baseline source remains at `C:\projects\pokereye\coin` and must
never be built destructively. Builds use a fresh isolated copy under
`.dist/v2workspace/`.
