# Hmuriy traffic/logging audit

The main high-volume logger was an unconditional OkHttp `HttpLoggingInterceptor` at `BODY` level in `coin/smali_classes6/okhttp3/OkHttpClient$Builder.smali`. The isolated build removed only that insertion. HmuriyBridge WebSocket hooks and frame transformation behavior were preserved.

Synchronous BODY Logcat writes and binary-to-hex conversion can increase allocations, CPU and Logcat contention. They are a plausible latency contributor, but ADB transport loss is a separate failure mode. Do not globally remove Logcat calls or bypass HmuriyBridge without runtime evidence.
