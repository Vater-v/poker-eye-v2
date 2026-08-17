import os, socket, sys, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.actions import Action, ActionScheduler, ActionStatus
from core.discovery import Broadcaster, SlotPool
from core.protocol import PROTOCOL_VERSION, proof, recv_frame, send_frame
from core.sessions import SessionRegistry
from core.transport import TrainerServer


class V2CoreTests(unittest.TestCase):
    def test_advertisement_is_v2_and_never_claims(self):
        b = Broadcaster("0.0.0.0", 1234, b"x", slots=5)
        a = b.advertisement()
        self.assertEqual(a["version"], PROTOCOL_VERSION)
        self.assertEqual(a["metadata"]["available"], 5)
        self.assertEqual(b.slot_pool.claimed(), {})

    def test_session_labels_recycle_and_generations_advance(self):
        r = SessionRegistry(); a = r.open_table("d", "a"); b = r.open_table("d", "b")
        self.assertEqual((a.number, b.number), (1, 2)); self.assertTrue(r.close_table(a))
        again = r.open_table("d", "a")
        self.assertEqual((again.number, again.generation), (1, 2))

    def test_actions_three_attempts_and_stale_ack_guard(self):
        s = ActionScheduler(); a = Action("d", "t", 7, "CHECK", 0)
        self.assertTrue(s.create(a))
        self.assertEqual([s.next_attempt("d")[0].attempt for _ in range(3)], [1, 2, 3])
        self.assertIsNone(s.next_attempt("d")); self.assertEqual(a.status, ActionStatus.FAILED)
        b = Action("d2", "t", 3, "CALL", 1); s.create(b)
        self.assertFalse(s.acknowledge("d2", b.correlation_id, 2)); self.assertTrue(s.acknowledge("d2", b.correlation_id, 3))

    def test_unknown_timeout_never_retries_blindly(self):
        s = ActionScheduler(); a = Action("d", "t", 1, "CALL", 1); s.create(a); s.next_attempt("d")
        self.assertTrue(s.timeout_unknown("d", a.correlation_id)); self.assertIsNone(s.next_attempt("d")); self.assertEqual(a.status, ActionStatus.NEEDS_OPERATOR)

    def test_authenticated_reservation_happens_after_handshake_and_releases(self):
        secret, session, nonce = b"secret", "s", "n"; pool = SlotPool(1)
        server = TrainerServer(secret, session, nonce, pool); port = server.start()
        def connect(device, table):
            sock = socket.create_connection(("127.0.0.1", port)); send_frame(sock, {"type":"hello","version":PROTOCOL_VERSION,"device_id":device,"table_id":table,"proof":proof(secret, nonce, table, session)}); return sock, recv_frame(sock)
        try:
            first, welcome = connect("d", "t"); self.assertEqual(welcome["slot"], 1); self.assertEqual(pool.metadata()["available"], 0)
            dup, reply = connect("d", "t"); self.assertEqual(reply["error"], "duplicate_connection"); dup.close()
            first.close(); time.sleep(.2); self.assertEqual(pool.metadata()["available"], 1)
        finally: server.stop()

if __name__ == "__main__": unittest.main()
