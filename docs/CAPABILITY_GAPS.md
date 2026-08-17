# v2 capability gaps and milestones

## Deliberately unsupported: double-board

The v2 pipeline is **NLH single-board only**. It must not enter, play, or advertise double-board/PLO/multi-board support. The supplied `raw/NLH4 HU 2 hands.pcap` is reference material for the NLH4 HU single-board flow only; it is not a double-board reference. A clean double-board capture with explicit board index/source, hand generation, and expected transitions is required before any future work.

## Not yet proven

- Android UDP discovery client and authenticated TCP client APK are not present in this repository; no v2 APK has been built or installed.
- Coin/EYE hook framing and normalized business-event bridge are not integrated into the new runtime.
- One-table live NLH4 HU hint → CC → ACK → ledger has not been demonstrated.
- Reconnect, 4–5 tables per emulator, and multiple-emulator acceptance gates remain open.
- Bounded filtered PCAP ring implementation remains open; normal logs currently retain metadata only.

## Evidence rule

Unit tests demonstrate protocol/core invariants only. A release claim requires real emulator evidence for discovery, authenticated TCP, hook traffic, hint/action/ACK, reconnect, and multitable progression, with raw artifacts kept outside Git.
