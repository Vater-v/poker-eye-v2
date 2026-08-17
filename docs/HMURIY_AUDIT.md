# Hmuriy traffic/logging audit

Date: 2026-08-16
Source: `C:\projects\pokereye\coin` (read-only audit)

## Findings

The main unconditional high-volume logger is:

```text
coin/smali_classes6/okhttp3/OkHttpClient$Builder.smali
method <init>()V
```

It constructs `com.hmuriy.HmuriyLogger`, creates an OkHttp `HttpLoggingInterceptor`, sets level `BODY`, and adds it to the client. This logs request/response headers and bodies for every client created through that constructor.

`coin/smali_classes7/com/hmuriy/HmuriyLogger.smali` synchronously chunks messages into approximately 3584-character pieces and calls `Log.d("Hmuriy", ...)`.

Additional WebSocket blocks in:

```text
coin/smali_classes6/okhttp3/internal/ws/RealWebSocket.smali
```

log inbound/outbound text and convert binary frames to hex before logging. These blocks must not be removed blindly: `HmuriyBridge.wsText/wsBinary` also transforms or drops frames and are behaviorally significant.

## Safe patch order

1. First disable only the unconditional BODY interceptor insertion in an isolated copy.
2. Preserve all HmuriyBridge calls and frame decision behavior.
3. Build/sign the isolated APK.
4. Verify `apksigner`, install only on a disposable/test emulator, and compare:
   - app launch;
   - EYE/hook connection;
   - no `Hmuriy` BODY flood;
   - hint arrival and CC execution;
   - reconnect behavior.
5. Only then consider gating/removing WebSocket text/binary display logs.

## Stability hypothesis

Synchronous Logcat BODY writes and binary-to-hex allocations plausibly increase CPU, allocation, and Logcat contention. They can worsen timing, but the audit found no Hmuriy file/network dump sink. ADB transport loss remains a separate failure mode and must be measured independently.

## Build safety

Never run the destructive build script in the baseline `coin` tree. Use only `C:\projects\poker-eye-v2\coin`. Preserve baseline APK/archive and record hashes before/after.
