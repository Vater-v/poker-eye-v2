"""Tests for the trainer-side PokerEYE channel client."""
import os, socket, sys, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.eye_backend import EyeChannelClient, lp_pack, lp_read_sock
from core.events import parse_cc_data


class FakeEyeServer:
    """Minimal fake PokerEYE endpoint: accepts one client, echoes cc frames."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._stop = threading.Event()
        self._connections = set()
        self._connection_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            self.sock.settimeout(0.2)
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._connection_lock:
                self._connections.add(conn)
            try:
                self._serve(conn)
            finally:
                with self._connection_lock:
                    self._connections.discard(conn)

    def _serve(self, conn):
        try:
            while not self._stop.is_set():
                try:
                    raw = lp_read_sock(conn)
                except (ConnectionError, OSError, ValueError):
                    return
                if raw is None:
                    return
                import json
                frame = json.loads(raw.decode("utf-8"))
                self.received.append(frame)
                if frame.get("tag") == "hint_ping":
                    conn.sendall(lp_pack({
                        "tag": "cc",
                        "data": '{"type":"CHECK","subtype":0,"delay":200,"lifetime":4000,"amount":0.0,"message":"check"}',
                        "packageName": "com.lein.pppoker.android",
                    }))
                elif frame.get("tag") == "stop":
                    return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        with self._connection_lock:
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self.thread.join(timeout=1.0)


class EyeChannelTests(unittest.TestCase):
    def test_connect_send_and_receive_cc(self):
        server = FakeEyeServer()
        try:
            client = EyeChannelClient("127.0.0.1", server.port, on_state=lambda *a: None)
            client.start()
            deadline = time.time() + 5.0
            while not client.connected.is_set() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(client.connected.is_set(), "client did not connect")
            self.assertTrue(client.send("hint_ping", ""))
            cc = client.cc_queue.get(timeout=5.0)
            self.assertEqual(cc.tag, "cc")
            data = parse_cc_data(cc.data)
            self.assertEqual(data["type"], "CHECK")
            self.assertEqual(data["delay"], 200)
            client.stop()
        finally:
            server.close()

    def test_reconnect_after_server_restart(self):
        server1 = FakeEyeServer()
        try:
            client = EyeChannelClient("127.0.0.1", server1.port, reconnect_base=0.05, reconnect_max=0.2)
            client.start()
            deadline = time.time() + 5.0
            while not client.connected.is_set() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(client.connected.is_set())
            # Kill the first server.
            server1.close()
            deadline = time.time() + 3.0
            while client.connected.is_set() and time.time() < deadline:
                time.sleep(0.05)
            # Start a second server on the same port.
            server2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server2.bind(("127.0.0.1", server1.port))
            server2.listen(1)
            try:
                deadline = time.time() + 5.0
                while not client.connected.is_set() and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(client.connected.is_set(), "client did not reconnect")
            finally:
                server2.close()
            client.stop()
        finally:
            try:
                server1.close()
            except OSError:
                pass

    def test_send_returns_false_when_disconnected(self):
        client = EyeChannelClient("127.0.0.1", 1, reconnect_base=0.05)
        self.assertFalse(client.send("traffic", ""))
        client.stop()


class LpFrameTests(unittest.TestCase):
    def test_lp_pack_roundtrip(self):
        import json
        data = lp_pack({"tag": "cc", "data": "x"})
        body = json.dumps({"tag": "cc", "data": "x"}, separators=(",", ":")).encode()
        self.assertEqual(data[:4], len(body).to_bytes(4, "big"))
        left, right = socket.socketpair()
        try:
            left.sendall(data)
            self.assertEqual(lp_read_sock(right), data[4:])
        finally:
            left.close(); right.close()


if __name__ == "__main__":
    unittest.main()
