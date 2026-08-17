"""Tests for public bootstrap and lowest-free callback leases."""
import os, socket, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.bootstrap import BootstrapServer, CallbackAllocator
from core.protocol import frame, recv_frame, proof


class CallbackAllocatorTests(unittest.TestCase):
    def test_lowest_free_and_reuse(self):
        a = CallbackAllocator(54300, 54302)
        x = a.allocate("a", "192.168.0.1")
        y = a.allocate("b", "192.168.0.2")
        self.assertEqual((x.callback_port, y.callback_port), (54300, 54301))
        a.release("a", x.generation)
        z = a.allocate("c", "192.168.0.3")
        self.assertEqual(z.callback_port, 54300)

    def test_duplicate_returns_same_lease(self):
        a = CallbackAllocator(54300, 54399)
        x = a.allocate("a", "192.168.0.1")
        y = a.allocate("a", "192.168.0.9")
        self.assertEqual(x, y)
        self.assertEqual(a.available(), 99)

    def test_force_reallocation_invalidates_generation(self):
        a = CallbackAllocator(54300, 54301)
        x = a.allocate("a", "192.168.0.1")
        y = a.allocate("a", "192.168.0.2", force_new=True)
        self.assertNotEqual(x.token, y.token)
        self.assertEqual(y.generation, 2)
        self.assertEqual(y.callback_port, x.callback_port)

    def test_generation_rejects_stale_release(self):
        a = CallbackAllocator(54300, 54300)
        x = a.allocate("a", "192.168.0.1")
        a.release("a", x.generation)
        y = a.allocate("a", "192.168.0.1")
        self.assertEqual(y.generation, 2)
        self.assertFalse(a.release("a", 1))
        self.assertIsNotNone(a.get("a"))

    def test_capacity_sixty(self):
        a = CallbackAllocator(54300, 54399)
        leases = [a.allocate(f"d{i}", "192.168.0.1") for i in range(60)]
        self.assertEqual(len({x.callback_port for x in leases}), 60)
        self.assertEqual(a.available(), 40)


class BootstrapIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"bootstrap-test-secret"
        self.server = BootstrapServer(self.secret, host="127.0.0.1", bootstrap_port=0,
                                      callback_start=0 if False else 54300,
                                      callback_end=54305, advertised_host="127.0.0.1")
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_registration_then_callback_auth(self):
        nonce = "client-nonce"
        sock = socket.create_connection(("127.0.0.1", self.server.bootstrap_port), timeout=2)
        send = {"type": "bootstrap_hello", "version": 2, "device_id": "emu-1",
                "local_ipv4": "192.168.0.139", "nonce": nonce,
                "session_id": "bootstrap",
                "proof": proof(self.secret, nonce, "emu-1", "bootstrap")}
        sock.sendall(frame(send))
        reply = recv_frame(sock)
        sock.close()
        self.assertEqual(reply["type"], "bootstrap_ok")
        self.assertEqual(reply["callback_port"], 54300)
        self.assertEqual(reply["generation"], 1)

        cb = socket.create_connection(("127.0.0.1", 54300), timeout=2)
        cb.sendall(frame({"type": "callback_hello", "version": 2,
                          "device_id": "emu-1", "table_id": "table-1", "generation": 1,
                          "token": reply["callback_token"]}))
        welcome = recv_frame(cb)
        self.assertEqual(welcome["type"], "callback_welcome")
        cb.sendall(frame({"type": "heartbeat", "sequence": 9}))
        self.assertEqual(recv_frame(cb)["sequence"], 9)
        cb.close()

    def test_bad_bootstrap_proof_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.server.bootstrap_port), timeout=2)
        sock.sendall(frame({"type": "bootstrap_hello", "version": 2,
                            "device_id": "evil", "local_ipv4": "192.168.0.1",
                            "nonce": "x", "session_id": "bootstrap", "proof": "0" * 64}))
        reply = recv_frame(sock)
        self.assertEqual(reply["type"], "error")
        sock.close()

    def test_callback_disconnect_releases_lease_and_listener(self):
        sock = socket.create_connection(("127.0.0.1", self.server.bootstrap_port), timeout=2)
        nonce = "release"
        sock.sendall(frame({"type": "bootstrap_hello", "version": 2, "device_id": "emu-r",
                            "local_ipv4": "192.168.0.1", "nonce": nonce, "session_id": "bootstrap",
                            "proof": proof(self.secret, nonce, "emu-r", "bootstrap")}))
        reply = recv_frame(sock); sock.close()
        cb = socket.create_connection(("127.0.0.1", reply["callback_port"]), timeout=2)
        cb.sendall(frame({"type": "callback_hello", "version": 2, "device_id": "emu-r",
                          "table_id": "t-r", "generation": 1, "token": reply["callback_token"]}))
        self.assertEqual(recv_frame(cb)["type"], "callback_welcome")
        cb.close()
        for _ in range(20):
            if self.server.allocator.get("emu-r") is None:
                break
            time.sleep(.02)
        self.assertIsNone(self.server.allocator.get("emu-r"))

    def test_bad_callback_token_rejected(self):
        nonce = "x"
        sock = socket.create_connection(("127.0.0.1", self.server.bootstrap_port), timeout=2)
        sock.sendall(frame({"type": "bootstrap_hello", "version": 2, "device_id": "emu-2",
                            "local_ipv4": "192.168.0.140", "nonce": nonce,
                            "session_id": "bootstrap", "proof": proof(self.secret, nonce, "emu-2", "bootstrap")}))
        reply = recv_frame(sock); sock.close()
        cb = socket.create_connection(("127.0.0.1", reply["callback_port"]), timeout=2)
        cb.sendall(frame({"type": "callback_hello", "version": 2, "device_id": "emu-2",
                          "table_id": "table-2", "generation": 1, "token": "wrong"}))
        with self.assertRaises((ConnectionError, OSError, TimeoutError)):
            cb.settimeout(.5); recv_frame(cb)
        cb.close()


if __name__ == "__main__":
    unittest.main()
