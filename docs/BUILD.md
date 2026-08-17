# v2 isolated APK build

The isolated Hmuriy BODY-logging patch was built and signed before the APK workspace was removed from this working tree.

Artifact:

```text
.dist/coinpoker-hmuriy-body-disabled-debug.apk
```

SHA-256:

```text
73E2113CC28C282C506A2FF9CD43FCDFC7032F45B0550A23B0292A11A11AC7C3
```

Size: 213,043,067 bytes.

`apksigner verify --verbose` passed with APK Signature Scheme v2 and v3. This proves packaging/signature integrity only; it does not prove runtime hook, discovery, hint, CC, or reconnect behavior.

The decompiled workspace and tooling were intentionally kept out of Git and removed from the v2 working tree after the build. The pristine source remains in the original baseline location and must not be built destructively. A future rebuild should use a fresh isolated copy and record its source hash first.
