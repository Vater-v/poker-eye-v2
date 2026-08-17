# Current
- Read-only Android/network audit: current HmuriyBridge.java and core discovery/transport use matching UDP 37020 JSON advertisement and TCP v2 HMAC protocol; no source files modified.
- v2 core is stdlib-only and green: 88 unit tests pass, compileall clean, trainer smoke prints READY and writes logs/run_<id>/{manifest.json,operator.txt,events.jsonl}.
- New core modules: coin_wire, state, events, anomalies, policy, hints, ledger, pcap_ring, logging, actions.

# Verification
- `python -m unittest discover`: 88 tests OK.
- Real trainer stdout artifact `.goose-tmp/real_trainer_stdout.txt`: ready tcp=62214 udp=37020; no device discovery/connection evidence.
- Existing logs contain only trainer.ready in inspected runs; no broadcast.received or hello.authenticated events.
- APK build record docs/BUILD.md confirms signed artifact and bridge source hash, but no current ADB/logcat dump exists in repository.

# Audit findings
- Java discovery binds UDP/IPv4 0.0.0.0:37020 and accepts only JSON type=trainer, version=2, tcp_port, nonce; it does not log ignored/malformed advertisements. It starts in static initialization, so discovery itself is not gated by ws traffic.
- Trainer advertises to 255.255.255.255:37020 with host exactly the configured `host`. Default host 0.0.0.0 is intentionally replaced by the Android receiver with packet source IP. UDP/TCP protocol fields and HMAC proof formula match core/protocol.py.
- Most likely real-emulator failure is network topology/firewall: LDPlayer NAT/isolated mode commonly does not deliver host LAN IPv4 broadcasts to guest; ADB is not a runtime fallback. Also verify Windows firewall permits inbound UDP 37020 and advertised TCP ephemeral port (trainer output was 62214 in the captured run).
- Minimal protocol-side hardening (if needed after network test): emit Java logs for bind/valid/ignored discovery and trainer broadcast send errors; optionally advertise a fixed LAN host/IP rather than 0.0.0.0. Do not replace UDP broadcast with ADB or unicast if broadcast-only contract is required.
- Separate race: if UDP discovery succeeds just before TCP, trainerNonce/sessionId are volatile and updated from each ad; otherwise hello should authenticate. A TCP refusal/timeout after “discovered” indicates firewall/route or advertised wrong host/port, not parsing.

# Not done / blocked
- No files modified. No real ADB/logcat/network capture was available in repository, so exact emulator topology cannot be proven from files alone.
- Double-board unsupported; real acceptance still needs discovery, TCP, hook/hint/CC/ACK/ledger evidence.

# Next
1. Run host-side UDP listener and packet capture while trainer is running; verify advertisement source/destination and emulator visibility.
2. Check LDPlayer network mode/route and Windows firewall; test TCP to the exact advertised port from emulator only after UDP visibility.
3. Add only diagnostic logging first, then any topology-specific minimal fix.
