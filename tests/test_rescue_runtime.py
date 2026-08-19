import base64
import json
import socket
import unittest

from core.rescue_runtime import DirectTableServer, direct_proof, recv_frame, send_frame
from core.verified_v1.coin_action_wire import decode_packet
from core.verified_v1.coin_autoplay import CoinAutoplayCoordinator


class DirectTransportTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"transport-test"
        self.server = DirectTableServer(
            self.secret,
            host="127.0.0.1",
            port=0,
            on_message=lambda d, t, m: {"id": m.get("id"), "action": "forward"},
        )
        self.port = self.server.start()

    def tearDown(self):
        self.server.stop()

    def connect(self, device, table):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        sock.settimeout(2)
        send_frame(sock, {
            "type": "direct_hello",
            "version": 2,
            "device_id": device,
            "table_id": table,
            "proof": direct_proof(self.secret, device, table),
        })
        return sock, recv_frame(sock)

    def test_exact_android_direct_hello(self):
        sock, welcome = self.connect("android-a", "android-a-ws1")
        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(welcome["device_id"], "android-a")
        sock.close()

    def test_multitable_same_device(self):
        a, _ = self.connect("android-a", "t1")
        b, _ = self.connect("android-a", "t2")
        self.assertEqual(self.server.counts(), (1, 2))
        a.close(); b.close()

    def test_multidevice(self):
        a, _ = self.connect("android-a", "a1")
        b, _ = self.connect("android-b", "b1")
        self.assertEqual(self.server.counts(), (2, 2))
        a.close(); b.close()

    def test_bad_proof_fails_immediately(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        sock.settimeout(2)
        send_frame(sock, {
            "type": "direct_hello", "version": 2,
            "device_id": "android-a", "table_id": "x", "proof": "0" * 64,
        })
        self.assertEqual(recv_frame(sock)["error"], "bad_proof")
        sock.close()

    def test_transport_accepts_frame_over_64k(self):
        sock, _ = self.connect("android-a", "large")
        msg = {
            "type": "ws_message", "kind": "ws_message", "id": "large-1",
            "direction": "in", "text": False, "url": "wss://example",
            "ws_id": "x", "payload_b64": base64.b64encode(b"x" * 100_000).decode(),
        }
        send_frame(sock, msg)
        reply = recv_frame(sock)
        self.assertEqual(reply["id"], "large-1")
        self.assertEqual(reply["action"], "forward")
        sock.close()


class VerifiedBusinessWireTests(unittest.TestCase):
    def test_v1_room_and_chip_scale_100(self):
        coordinator = CoinAutoplayCoordinator(100)
        state = {
            "user_name": "hero", "user_id": 1,
            "_hook_room": 49453, "hand_id": "h1"
        }
        turn = {
            "whoseTurn": "hero",
            "turnTime": 15,
            "callAmount": 0,
            "userTurnOptions": {"3": [], "4": [0.02], "5": [0.04, 0.20]},
            "initTimeStamp": 123,
        }
        payload = {
            "p": {
                "c": "game.user_turn",
                "r": 49453,
                "p": {"data": json.dumps(turn)},
            }
        }
        coordinator.observe(
            {"direction": "in", "ws_id": "ws", "url": "wss://coin"},
            payload, b"x", state,
        )
        coordinator.schedule_cc(
            {"type": "RAISE", "amount": 8.0, "delay": 0, "lifetime": 4000},
            state,
        )
        decoded = decode_packet(coordinator.pending["raw"])
        self.assertEqual(decoded["p"]["r"], 49453)
        body = json.loads(decoded["p"]["p"]["data"])
        self.assertEqual(body["userAction"], 5)
        self.assertGreater(body["betAmount"], 0)

    def test_check_stays_free(self):
        coordinator = CoinAutoplayCoordinator(100)
        state = {
            "user_name": "hero", "user_id": 1,
            "_hook_room": 7, "hand_id": "h2"
        }
        turn = {
            "whoseTurn": "hero",
            "turnTime": 15,
            "callAmount": 0,
            "userTurnOptions": {"3": []},
            "initTimeStamp": 456,
        }
        payload = {"p": {"c": "game.user_turn", "r": 7, "p": {"data": json.dumps(turn)}}}
        coordinator.observe(
            {"direction": "in", "ws_id": "ws2", "url": "wss://coin"},
            payload, b"x", state,
        )
        coordinator.schedule_cc(
            {"type": "CHECK", "amount": 0.0, "delay": 0, "lifetime": 4000},
            state,
        )
        decoded = decode_packet(coordinator.pending["raw"])
        body = json.loads(decoded["p"]["p"]["data"])
        self.assertEqual(body, {"userAction": 3, "betAmount": 0})

if __name__ == "__main__":
    unittest.main()
