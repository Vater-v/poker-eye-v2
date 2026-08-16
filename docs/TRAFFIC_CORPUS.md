# External traffic corpus

Traffic captures remain outside Git and outside the v2 runtime tree, in a restricted directory such as `D:\pokereye-corpus`, with a manifest and SHA-256.

Canonical IDs are ASCII uppercase, hyphen-separated semantic names:

```text
PPP-PLO4B-0.05BB-HU-PREFLOP-RAW.pcap
PPP-PLO4B-0.05BB-HU-PREFLOP-HOOK.ndjson
PPP-PLO6-ROOM-SNAPSHOT.ndjson
EYE-LOCAL-FULLSESSION-RAW.pcap
```

Do not place account/emulator/timestamp/table/hand IDs in canonical names; keep those in restricted manifest metadata. Derived decoded artifacts reference `parent_id`; deduplicate by SHA-256. Existing workspace captures should be classified rather than copied into Git.

Always-on per-table PCAP is reasonable only with a BPF filter and bounded ring: 4 x 64 MiB per active table, 256 MiB cap, 3 completed captures retained. Unrestricted full-device capture is not a default.
