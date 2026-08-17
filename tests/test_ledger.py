"""Unit tests for append-only ledger."""
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.ledger import Ledger, LedgerStatus


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "ledger.jsonl"

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finalize_once(self):
        l = Ledger(self.path)
        self.assertTrue(l.finalize("test:1", LedgerStatus.SUCCESS, action="CALL", amount=0.02))
        self.assertEqual(l.count(), 1)
        self.assertFalse(l.finalize("test:1", LedgerStatus.SUCCESS))
        self.assertEqual(l.count(), 1)

    def test_multiple_keys(self):
        l = Ledger(self.path)
        for i in range(5):
            self.assertTrue(l.finalize(f"test:{i}", LedgerStatus.SUCCESS))
        self.assertEqual(l.count(), 5)

    def test_persistence(self):
        l1 = Ledger(self.path)
        l1.finalize("a", LedgerStatus.SUCCESS)
        l2 = Ledger(self.path)
        self.assertTrue(l2.has("a"))
        self.assertFalse(l2.finalize("a", LedgerStatus.FAILED))

    def test_extra_fields(self):
        l = Ledger(self.path)
        l.finalize("x", LedgerStatus.NEEDS_OPERATOR, action="RAISE", amount=1.5, extra={"reason": "timeout"})
        self.assertTrue(l.has("x"))


if __name__ == "__main__":
    unittest.main()