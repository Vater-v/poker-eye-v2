#!/usr/bin/env python3
"""Replace the old Hmuriy RealWebSocket hook with a minimal native tap."""
from __future__ import annotations

import argparse
from pathlib import Path


METHODS = {
".method public onReadMessage(Ljava/lang/String;)V": r'''.method public onReadMessage(Ljava/lang/String;)V
    .locals 4
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    const-string v0, "text"

    invoke-static {p1, v0}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V

    .line 333
    iget-object v0, p0, Lokhttp3/internal/ws/RealWebSocket;->listener:Lokhttp3/WebSocketListener;

    move-object v1, p0

    check-cast v1, Lokhttp3/WebSocket;

    invoke-virtual {v0, v1, p1}, Lokhttp3/WebSocketListener;->onMessage(Lokhttp3/WebSocket;Ljava/lang/String;)V

    return-void
.end method''',

".method public onReadMessage(Lokio/ByteString;)V": r'''.method public onReadMessage(Lokio/ByteString;)V
    .locals 4
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    const-string v0, "bytes"

    invoke-static {p1, v0}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V

    invoke-static {p0, p1}, Lcom/hmuriy/HmuriyBridge;->tapInBinary(Ljava/lang/Object;Ljava/lang/Object;)V

    .line 338
    iget-object v0, p0, Lokhttp3/internal/ws/RealWebSocket;->listener:Lokhttp3/WebSocketListener;

    move-object v1, p0

    check-cast v1, Lokhttp3/WebSocket;

    invoke-virtual {v0, v1, p1}, Lokhttp3/WebSocketListener;->onMessage(Lokhttp3/WebSocket;Lokio/ByteString;)V

    return-void
.end method''',

".method public send(Ljava/lang/String;)Z": r'''.method public send(Ljava/lang/String;)Z
    .locals 4

    const-string v0, "text"

    invoke-static {p1, v0}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V

    .line 393
    sget-object v0, Lokio/ByteString;->Companion:Lokio/ByteString$Companion;

    invoke-virtual {v0, p1}, Lokio/ByteString$Companion;->encodeUtf8(Ljava/lang/String;)Lokio/ByteString;

    move-result-object p1

    const/4 v0, 0x1

    invoke-direct {p0, p1, v0}, Lokhttp3/internal/ws/RealWebSocket;->send(Lokio/ByteString;I)Z

    move-result p1

    return p1
.end method''',

".method public send(Lokio/ByteString;)Z": r'''.method public send(Lokio/ByteString;)Z
    .locals 4

    const-string v0, "bytes"

    invoke-static {p1, v0}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V

    invoke-static {p0, p1}, Lcom/hmuriy/HmuriyBridge;->tapOutBinary(Ljava/lang/Object;Ljava/lang/Object;)V

    const/4 v0, 0x2

    .line 397
    invoke-direct {p0, p1, v0}, Lokhttp3/internal/ws/RealWebSocket;->send(Lokio/ByteString;I)Z

    move-result p1

    return p1
.end method''',
}


def replace_method(source: str, signature: str, replacement: str) -> str:
    start = source.find(signature)
    if start < 0:
        raise RuntimeError(f"method not found: {signature}")
    end = source.find(".end method", start)
    if end < 0:
        raise RuntimeError(f"unterminated method: {signature}")
    end += len(".end method")
    return source[:start] + replacement + source[end:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("smali", type=Path)
    args = ap.parse_args()

    path = args.smali
    source = path.read_text(encoding="utf-8")

    for signature, replacement in METHODS.items():
        source = replace_method(source, signature, replacement)

    forbidden = [
        "ByteString;->hex()Ljava/lang/String;",
        "[WS IN BINARY hex]",
        "[WS OUT BINARY hex]",
        "HmuriyBridge;->wsBinary",
    ]
    for value in forbidden:
        if value in source:
            raise RuntimeError(f"obsolete expensive hook remains: {value}")

    if source.count("HmuriyBridge;->tapInBinary") != 1:
        raise RuntimeError("expected exactly one native inbound tap")
    if source.count("HmuriyBridge;->tapOutBinary") != 1:
        raise RuntimeError("expected exactly one native outbound tap")

    path.write_text(source, encoding="utf-8", newline="\n")
    print("[+] RealWebSocket patched: zero-copy native tap, payload diagnostics removed")


if __name__ == "__main__":
    main()
