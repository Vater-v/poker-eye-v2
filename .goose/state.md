# Current
- Local `poker-eye-v2` exists but is not a Git worktree yet. It contains an early stdlib UDP/TCP prototype (11 source/test files), no Android source/build project, and a reference PCAP under `raw/`.
- LDPlayer9 is installed at `I:\progs\LDPlayer\LDPlayer9`; test device `emulator-5558` is online (transport id 44).
- Initial inspection found `CONCEPT.md` still describes forbidden ADB-reverse fallback and must be corrected before any implementation claim.

# Next
- Inspect remote Git state and available legacy/source artifacts.
- Replace prototype with a testable v2 core: authenticated discovery/TCP handshake, lifecycle/session/action safety and observability.
- Validate with unit/integration tests, then assess Android APK availability and emulator gates without modifying baseline.

# Decisions
- Double-board is explicitly out of scope; v2 accepts NLH single-board only.
- Runtime transport is UDP IPv4 broadcast discovery plus authenticated outbound TCP; ADB may be used only for lab control and observation.
- No raw payload in ordinary logs; PCAP artifacts remain outside Git.

# Journal
- 2026-08-17: Started v2 task; created project state file. Existing local v2 is an untracked prototype and has no working Android client source.