# v2 capability gaps and milestones

## Deliberately unsupported: double-board

The v2 pipeline is **NLH single-board only**. It must not enter, play, or advertise double-board/PLO/multi-board support. The supplied `raw/NLH4 HU 2 hands.pcap` is reference material for the NLH4 HU single-board flow only; it is not a double-board reference. A clean double-board capture with explicit board index/source, hand generation, and expected transitions is required before any future work.

## Proven locally (2026-08-19 corpus)

- HMN1 APK exists: `CoinPoker-PokerEye-V7.4.2-HMN1-VPS.apk`. Live hellos used `V7.4.0-HMN1-PUBLIC` against trainers `V7.4.2-HMN1-VPS` / `dev-unversioned` (mismatch is allowed, logged).
- Hook framing is integrated: v2 HMR1 captures are SmartFox `0x80` and decode `game.*` commands.
- `ready_v6/captures` replay through current v2: 31911 events, 265/266 hero-turn hands synthesized, 0 complete-capture failures.
- `port_18010.pcap` (Coin SFS on 18010): 423 events, 1/1 hand built.
- `raw/NLH4 HU 2 hands.pcap` is **not** Coin SFS. It is loopback PokerEYE JSON-LP / pppoker `pb.*`. The 18010 loader correctly yields 0 events.

## Live evidence still missing / broken

- One real v2 hand exists (`run_09461f05840f`): mid-hand join → fallback CHECK (no Eye CC) → manual cancel. Not a clean hint→CC→ACK.
- Transport flap: 2666 `peer disconnected` + 856 WinError 10054 across v2 logs; many runs never sit a table.
- Eye gRPC `UNAVAILABLE` after hours on the one leased session.
- Reconnect / 4–5 tables / multi-emulator gates remain open as *live* claims.
- Bounded PCAP ring exists (HMR1 USER0), but most runs capture only handshake crumbs.

## Evidence rule

Unit tests demonstrate protocol/core invariants only. A release claim requires real emulator evidence for discovery, authenticated TCP, hook traffic, hint/action/ACK, reconnect, and multitable progression, with raw artifacts kept outside Git.
