# Current
- v2 core is stdlib-only and green: 65 unit tests pass, compileall clean, trainer smoke prints READY and writes logs/run_<id>/{manifest.json,operator.txt,events.jsonl}.
- New core modules: coin_wire (ported verified SFS2X + CC resolver), state (street/table state + turn_identity), events (normalized Coin/EYE metadata model + eye-channel CC parsing), anomalies (call=0 guard), policy (hint->action decision layer with state-gap), hints (hint watchdog), ledger (JSONL accounting), pcap_ring (4x64MiB ring, 256MiB cap, BPF), logging (run/device/session/table hierarchy), actions (+ HumanDelay for 60BB+).
- Two read-only analyst reports integrated into docs/: PROTOCOL_REFERENCE_PCAP.md (reference PCAP = pppoker hook channel to PokerEYE on 127.0.0.1:17770, 4-byte BE length + JSON framing; CC schema confirmed; single-board NLH HU; NO SmartFox 0x80 frames in this capture — that framing belongs to CoinPoker, verified separately in legacy selftests) and MIGRATION_MAP.md (port-as-is / port-with-adaptation / do-not-port lists + 10 invariants).

# Verification
- `python -m unittest discover`: 65 tests OK (was 9).
- Fixed during build: pcap_ring RLock deadlock (rotate under write lock), close() self-deadlock, bpf interface string, ledger read_text newline kwarg (Python 3.10), policy guard only fires for paid CC types.
- README.md updated.

# Not done / blocked
- No Android client/build source in v2; no v2 APK built/signed/installed. No real UDP discovery + TCP LAN + hook/hint/CC/ACK/ledger acceptance yet.
- EYE backend client (the trainer-side piece that connects to PokerEYE and forwards CCs) not yet implemented.
- Double-board explicitly unsupported (docs/CAPABILITY_GAPS.md).
- PCAP ring has no live capture driver yet (policy/rotation is unit-tested; tcpdump driver is a future step for the Linux target).

# Next
1. Implement trainer-side EYE backend client (lp_pack/lp_read frames, SCLogin-lite, cc reception) as core/eye_backend.py with tests.
2. Wire the full local loop: TCP client sim (tests) -> hint request -> cc -> policy -> action schedule -> attempts -> ACK -> ledger -> operator lines, as an integration test.
3. Android broadcast/TCP client + isolated APK build (biggest remaining milestone).
4. Real emulator acceptance: 1 table -> 2 -> 4 -> 5, then multiple emulators; 10/10 launches.

# Decisions
- Keep root main.py + small core modules; stdlib only. Port from legacy per MIGRATION_MAP, never copy LiveCoinBridge wholesale.
- Normal logs carry frame metadata (direction/type/length/seq/correlation/SHA-256) and allowlisted decoded fields only; no payloads.
- The call=0 guard lives in core/policy.py (decide_cc_action): paid CC + call_need==0 + CHECK legal -> state_gap_check (CHECK), never CALL 0; without CHECK legal -> needs_operator.
