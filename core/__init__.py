"""Minimal v2 core modules.

    protocols    — versioned authenticated length-prefixed JSON framing + HMAC
    discovery    — UDP IPv4 broadcast discovery (1.25 s interval)
    transport    — threaded TCP trainer endpoint
    sessions     — recyclable table labels and generation-safe identities
    actions      — per-device CC scheduler (exactly 3 attempts) + human delays
    coin_wire    — CoinPoker SFS2X encoding and EYE→Coin CC resolution
    state        — per-table poker action state (street contributions, etc.)
    events       — normalized Coin/EYE event model (metadata only, no payload)
    hints        — hint lifecycle watchdog
    anomalies    — call=0 anomaly detection and state-gap guard
    ledger       — append-only crash-safe JSONL accounting
    pcap_ring    — bounded per-table PCAP ring
    logging      — per-run/per-device/per-session/per-table log hierarchy
"""