"""End-to-end trainer integration test: fake bridge device + fake EYE backend.

Proves the real v2 loop without an emulator:
  discovery broadcast -> authenticated TCP handshake (HMAC proof) ->
  ws_message exchange -> hint -> cc -> action schedule_send (exactly 3
  attempts when unacknowledged) -> server turn ACK -> ledger finalize.
"""
import base64
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.coin_wire import build_game_user_action_packet
from core.protocol import proof, recv_frame, send_frame
from core.trainer import Trainer


def lp_pack(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


def _read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf.extend(chunk)
    return bytes(buf)


def lp_read(sock):
    n = struct.unpack(">I", _read_exact(sock, 4))[0]
    return json.loads(_read_exact(sock, n).decode("utf-8"))


def coin_turn_frame(room_id=1, whose="hero", options=None, turn_time=15):
    """A Coin game.user_turn SFS2X frame (server -> client)."""
    data = json.dumps({
        "whoseTurn": whose, "turnTime": turn_time,
        "callAmount": 0, "userTurnOptions": options or {"3": [], "4": [50]},
    }, separators=(",", ":"))
    from core.coin_wire import _Byte, _Int, _Obj, _Short, _Str, encode_packet
    root = {"c": _Byte(1), "a": _Short(13),
            "p": _Obj({"c": _Str("game.user_turn"), "r": _Int(room_id),
                       "p": _Obj({"data": _Str(data)})})}
    return encode_packet(root)


def lobby_dummy_frame(room_id=1):
    """A neutral periodic game frame (lobby.dummy equivalent) used for pacing."""
    from core.coin_wire import _Byte, _Int, _Obj, _Short, _Str, encode_packet
    root = {"c": _Byte(1), "a": _Short(13),
            "p": _Obj({"c": _Str("lobby.dummy"), "r": _Int(room_id),
                       "p": _Obj({"data": _Str("{}")})})}
    return encode_packet(root)


class FakeEyeServer:
    """Serves the EYE channel (lp_pack JSON frames) and sends one cc on demand."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._conn = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conn = conn
            try:
                while not self._stop.is_set():
                    raw = lp_read(conn)
                    self.received.append(raw)
            except (ConnectionError, OSError):
                return

    def send_cc(self, cc: dict):
        deadline = time.time() + 5.0
        while self._conn is None and time.time() < deadline:
            time.sleep(0.02)
        if self._conn is None:
            raise RuntimeError("no eye client connected")
        self._conn.sendall(lp_pack({
            "tag": "cc",
            "data": json.dumps(cc),
            "packageName": "com.lein.pppoker.android",
        }))

    def close(self):
        self._stop.set()
        try:
            if self._conn:
                self._conn.close()
            self.sock.close()
        except OSError:
            pass


class FakeDevice:
    """Emulates the v2 Android bridge: hello handshake + ws_message exchange."""

    def __init__(self, host, port, secret, nonce, session_id, table_id, device_id):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.table_id, self.device_id = table_id, device_id
        send_frame(self.sock, {
            "type": "hello", "version": 2,
            "device_id": device_id, "table_id": table_id,
            "proof": proof(secret.encode(), nonce, table_id, session_id),
        })
        self.welcome = recv_frame(self.sock)

    def ws_message(self, payload: bytes, direction="in", url="wss://host/room=1", text=False):
        msg = {
            "type": "ws_message", "v": 3, "id": f"id-{time.time_ns()}",
            "kind": "ws_message", "direction": direction, "text": text,
            "url": url, "ws_id": "ab12",
            "payload_b64": base64.b64encode(payload).decode(),
        }
        send_frame(self.sock, msg)
        return recv_frame(self.sock)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TrainerEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eye = FakeEyeServer()
        self.trainer = Trainer(
            secret="test-secret",
            tcp_port=0,
            log_dir=Path(self.tmp) / "logs",
            eye_host="127.0.0.1",
            eye_port=self.eye.port,
            ack_timeout_s=0.4,
        )
        self.trainer.start()
        self.device = FakeDevice(
            "127.0.0.1", self.trainer.server.port, "test-secret",
            self.trainer.advertised_nonce, "trainer", "t1", "emulator-1")

    def tearDown(self):
        try:
            self.device.close()
        except Exception:
            pass
        self.trainer.shutdown()
        self.eye.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── handshake ─────────────────────────────────────────────────────
    def test_handshake_welcome_and_slot(self):
        self.assertEqual(self.device.welcome["type"], "welcome")
        self.assertEqual(self.device.welcome["slot"], 1)
        self.assertEqual(self.device.welcome["table_id"], "t1")

    def test_bad_proof_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.trainer.server.port))
        send_frame(sock, {"type": "hello", "version": 2, "device_id": "evil",
                          "table_id": "tx", "proof": "0" * 64})
        reply = recv_frame(sock)
        self.assertIn("error", reply)
        sock.close()

    # ── forward pass-through ──────────────────────────────────────────
    def test_ws_message_forwarded_by_default(self):
        payload = coin_turn_frame()
        resp = self.device.ws_message(payload)
        self.assertEqual(resp["action"], "forward")
        self.assertEqual(resp["id"], resp["id"])  # id echoed

    # ── cc -> schedule_send -> ack -> ledger ──────────────────────────
    def test_hint_cc_action_ack_ledger(self):
        # 1. Hero turn arrives from the device (server frame).
        self.device.ws_message(coin_turn_frame())
        time.sleep(0.1)
        # 2. EYE sends a cc.
        self.eye.send_cc({"type": "CHECK", "delay": 0, "lifetime": 4000, "amount": 0.0})
        deadline = time.time() + 3.0
        while time.time() < deadline and self.trainer.pending.get("t1") is None:
            time.sleep(0.02)
        self.assertIsNotNone(self.trainer.pending.get("t1"), "cc did not create a pending action")
        # 3. Next ws_message from the device (lobby.dummy pacing) triggers
        #    schedule_send (attempt 1).
        resp = self.device.ws_message(lobby_dummy_frame())
        self.assertEqual(resp["action"], "schedule_send")
        self.assertIn("payload_b64", resp)
        action_pkt = base64.b64decode(resp["payload_b64"])
        from core.coin_wire import decode_packet
        decoded = decode_packet(action_pkt)
        self.assertEqual(decoded["p"]["p"]["data"], '{"userAction":3,"betAmount":0}')
        # 4. Server confirms with a turn advance -> trainer acknowledges.
        self.device.ws_message(coin_turn_frame(whose="villain"))
        deadline = time.time() + 3.0
        while time.time() < deadline and self.trainer.ledger.count() == 0:
            time.sleep(0.02)
        self.assertEqual(self.trainer.ledger.count(), 1)
        with open(self.trainer.ledger.path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["action"], "CHECK")

    # ── exactly 3 attempts then failed ────────────────────────────────
    def test_three_attempts_then_failed(self):
        self.device.ws_message(coin_turn_frame())
        time.sleep(0.1)
        self.eye.send_cc({"type": "CHECK", "delay": 0})
        deadline = time.time() + 3.0
        while time.time() < deadline and self.trainer.pending.get("t1") is None:
            time.sleep(0.02)
        # No ACK ever: 3 schedule_send attempts (first due immediately,
        # retries +1s), then failed ledger entry. Neutral lobby.dummy frames
        # pace the exchanges (as in the real hook flow).
        actions = []
        # Attempt 1: first ws_message after cc is due.
        resp = self.device.ws_message(lobby_dummy_frame())
        actions.append(resp["action"])
        self.assertEqual(resp["action"], "schedule_send")
        time.sleep(1.2)  # retry gap
        resp = self.device.ws_message(lobby_dummy_frame())
        actions.append(resp["action"])
        self.assertEqual(resp["action"], "schedule_send")
        time.sleep(1.2)
        resp = self.device.ws_message(lobby_dummy_frame())
        actions.append(resp["action"])
        self.assertEqual(resp["action"], "schedule_send")
        # No more attempts: next message is forward.
        resp = self.device.ws_message(lobby_dummy_frame())
        actions.append(resp["action"])
        self.assertEqual(resp["action"], "forward")
        sends = [a for a in actions if a == "schedule_send"]
        self.assertEqual(len(sends), 3, f"expected 3 schedule_send, got {actions}")
        deadline = time.time() + 3.0
        while time.time() < deadline and self.trainer.ledger.count() == 0:
            time.sleep(0.02)
        with open(self.trainer.ledger.path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["status"], "failed")

    # ── heartbeat ─────────────────────────────────────────────────────
    def test_heartbeat_ack(self):
        send_frame(self.device.sock, {"type": "heartbeat", "sequence": 7})
        reply = recv_frame(self.device.sock)
        self.assertEqual(reply["type"], "heartbeat_ack")
        self.assertEqual(reply["sequence"], 7)

    # ── logs ──────────────────────────────────────────────────────────
    def test_run_logs_exist(self):
        self.device.ws_message(coin_turn_frame())
        time.sleep(0.1)
        self.eye.send_cc({"type": "CHECK", "delay": 0})
        time.sleep(0.3)
        run_dir = Path(self.tmp) / "logs" / f"run_{self.trainer.logger.run_id}"
        self.assertTrue((run_dir / "manifest.json").exists())
        self.assertTrue((run_dir / "operator.txt").exists())
        self.assertTrue((run_dir / "events.jsonl").exists())
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("trainer.ready", events)
        self.assertIn("hello.authenticated", events)
        self.assertIn("slot.reserved", events)


if __name__ == "__main__":
    unittest.main()
