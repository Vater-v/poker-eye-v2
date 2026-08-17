"""Unit tests for normalized event model and hierarchical logging."""
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.events import Direction, FrameFamily, coin_frame, eye_frame
from core.logging import SessionLogger, DeviceLogger, TableLogger


class EventModelTests(unittest.TestCase):
    def test_coin_frame_metadata_only(self):
        ev = coin_frame(Direction.IN, b"raw sfs bytes", decoded={"cmd": "game.user_turn", "secret_field": "SHOULD NOT LEAK"}, seq=1, correlation_id="abc")
        d = ev.as_dict()
        self.assertEqual(d["family"], "coin")
        self.assertEqual(d["direction"], "in")
        self.assertEqual(d["type"], "game.user_turn")
        self.assertIn("sha256", d)
        self.assertNotIn("secret_field", d["decoded"])

    def test_eye_frame_allowlist(self):
        ev = eye_frame(Direction.OUT, b"eyeframe", decoded={"cmd": "pb.ActionNotifyBRC", "call": 0, "secret": "x"})
        d = ev.as_dict()
        self.assertEqual(d["family"], "eye")
        self.assertEqual(d["decoded"], {"cmd": "pb.ActionNotifyBRC", "call": 0})

    def test_length_and_seq(self):
        ev = coin_frame(Direction.OUT, b"12345", seq=9)
        d = ev.as_dict()
        self.assertEqual(d["length"], 5)
        self.assertEqual(d["seq"], 9)


class LoggingHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_device_table_hierarchy(self):
        logger = SessionLogger(self.root, run_id="testrun")
        dev = logger.device("LDPlayer-5558")
        dev.emit("device.connected", flush=True)
        tbl = dev.table("table_01")
        tbl.emit("hint.received", flush=True, table_id="t1", generation=3)
        logger.close()

        run_dir = self.root / "run_testrun"
        self.assertTrue((run_dir / "manifest.json").exists())
        self.assertTrue((run_dir / "operator.txt").exists())
        self.assertTrue((run_dir / "events.jsonl").exists())
        self.assertTrue((run_dir / "devices" / "LDPlayer-5558" / "events.jsonl").exists())
        self.assertTrue((run_dir / "devices" / "LDPlayer-5558" / "tables" / "table_01" / "events.jsonl").exists())

    def test_close_emits_stop_once(self):
        logger = SessionLogger(self.root, run_id="testrun")
        logger.close()
        logger.close()  # idempotent
        content = (self.root / "run_testrun" / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(content.count("trainer.stopped"), 1)

    def test_operator_file_contains_messages(self):
        logger = SessionLogger(self.root, run_id="testrun")
        logger.emit("trainer.ready", message="[+] Trainer корректно запущен", flush=True)
        logger.close()
        op = (self.root / "run_testrun" / "operator.txt").read_text(encoding="utf-8")
        self.assertIn("[+] Trainer корректно запущен", op)


if __name__ == "__main__":
    unittest.main()