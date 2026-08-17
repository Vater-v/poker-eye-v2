import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from discovery import SlotPool
from protocol import frame, recv_frame, proof
from transport import TrainerServer
from core.discovery import DEFAULT_INTERVAL


class PrototypeTests(unittest.TestCase):
    def test_default_interval_and_advertisements_do_not_reserve_slots(self):
        from discovery import Broadcaster, DEFAULT_INTERVAL
        self.assertEqual(DEFAULT_INTERVAL, 1.25)
        b = Broadcaster("127.0.0.1", 1234, b"x", slots=1)
        self.assertEqual(b.advertisement()["metadata"]["available"], 1)

    def test_slot_reservation_and_duplicate_claim(self):
        pool = SlotPool(2)
        self.assertEqual(pool.reserve("a"), 1)
        self.assertEqual(pool.reserve("a"), 1)
        self.assertEqual(pool.reserve("b"), 2)
        self.assertIsNone(pool.reserve("c"))
        self.assertEqual(pool.release("a"), 1)
        self.assertEqual(pool.reserve("c"), 1)

    def test_handshake_framing_handles_partial_reads(self):
        left, right = socket.socketpair()
        try:
            payload = {"type": "hello", "table_id": "t", "proof": "p"}
            data = frame(payload)
            threading.Thread(target=lambda: [left.send(bytes([b])) for b in data], daemon=True).start()
            self.assertEqual(recv_frame(right), payload)
        finally: left.close(); right.close()

    def test_authenticated_connection_duplicate_and_reconnect(self):
        secret, session, nonce = b"secret", "s", "n"
        pool = SlotPool(1); server = TrainerServer(secret, session, nonce, pool)
        port = server.start()
        def connect():
            sock = socket.create_connection(("127.0.0.1", port))
            from protocol import send_frame
            send_frame(sock, {"type":"hello", "version":2, "device_id":"device", "table_id":"table", "proof":proof(secret, nonce, "table", session)})
            return sock, recv_frame(sock)
        first, welcome = connect(); self.assertEqual(welcome["slot"], 1)
        duplicate, reply = connect(); self.assertEqual(reply["error"], "duplicate_connection"); duplicate.close()
        first.close(); time.sleep(.1)
        second, welcome2 = connect(); self.assertEqual(welcome2["slot"], 1); second.close(); server.stop()

if __name__ == "__main__": unittest.main()
