# External traffic corpus

Traffic captures remain outside Git and outside the v2 runtime tree. Keep immutable raw artifacts in a separate restricted directory, for example `D:\pokereye-corpus`, with a manifest and SHA-256.

## Canonical IDs

Use ASCII uppercase, hyphen-separated semantic names:

```text
PPP-PLO4B-0.05BB-HU-PREFLOP-RAW.pcap
PPP-PLO4B-0.05BB-HU-PREFLOP-HOOK.ndjson
PPP-PLO6-ROOM-SNAPSHOT.ndjson
EYE-LOCAL-FULLSESSION-RAW.pcap
```

Do not put account IDs, emulator IDs, timestamps, table IDs or hand IDs in the canonical name. Store those in restricted manifest metadata.

## Manifest fields

```json
{"id":"PPP-PLO4B-0.05BB-HU-PREFLOP-RAW","kind":"capture","format":"pcap","path":"D:/pokereye-corpus/raw/...","sha256":"...","bytes":0,"family":"PPP","game":"PLO4B","stakes_bb":0.05,"scenario":"HU_PREFLOP","stream":"raw","status":"complete","tags":["reference"]}
```

Derived decoded files reference their parent with `parent_id`. Duplicate content is recorded once by SHA-256 while original paths remain in metadata.

## Current inventory notes

The existing workspace contains exploratory captures, manual EYE captures, account/table captures, PPP/COIN fixtures, and derived NDJSON. Do not copy the bulk corpus into Git. First classify it and create only a manifest/tooling entry later.

## PCAP ring

Always-on per-table PCAP is reasonable only with a BPF filter and bounded ring: 4 × 64 MiB per active table, 256 MiB cap, 3 completed captures retained. Unrestricted full-device capture is not acceptable as a default.
