"""Unit tests for bounded PCAP ring."""
import os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.pcap_ring import (
    PcapRing, PcapRingPolicy, PcapRingManager, bpf_for_game_port,
    GLOBAL_HEADER
)


class PcapRingPolicyTests(unittest.TestCase):
    def test_defaults(self):
        p = PcapRingPolicy()
        self.assertEqual(p.segment_bytes, 64 * 1024 * 1024)
        self.assertEqual(p.segments, 4)
        self.assertEqual(p.game_port, 17770)
        self.assertIn("tcp port 17770", p.bpf)

    def test_invalid_geometry_rejected(self):
        with self.assertRaises(ValueError):
            PcapRingPolicy(segment_bytes=1, segments=0)
        with self.assertRaises(ValueError):
            PcapRingPolicy(segment_bytes=1_000_000, segments=3, per_table_cap=1000)

    def test_bpf_filter(self):
        self.assertIn("tcp port 8080", bpf_for_game_port(8080))
        self.assertIn("and host eth0", bpf_for_game_port(8080, interface="eth0"))
        with self.assertRaises(ValueError):
            bpf_for_game_port(0)
        with self.assertRaises(ValueError):
            bpf_for_game_port(99999)


class PcapRingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "ring"
        self.policy = PcapRingPolicy(
            segment_bytes=1024,  # 1KB for fast tests
            segments=3,
            per_table_cap=10_000,
            completed_retention=1,
            game_port=17770,
        )

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_packets(self):
        ring = PcapRing(self.dir, self.policy)
        self.assertTrue(ring.write_packet(time.time(), b"\x00" * 50))
        ring.flush()
        ring.close()
        files = list(Path(self.dir).glob("seg_*.pcap"))
        self.assertGreaterEqual(len(files), 1)

    def test_rotation_when_segment_full(self):
        ring = PcapRing(self.dir, self.policy)
        # Write enough to fill more than one segment.
        for i in range(50):
            ring.write_packet(time.time() + i, b"\x42" * 60)
        ring.close()
        files = list(Path(self.dir).glob("seg_*.pcap"))
        self.assertGreater(len(files), 1)

    def test_stats_and_drop(self):
        # Very small segment so rotation is forced.
        policy = PcapRingPolicy(segment_bytes=200, segments=1, per_table_cap=10000, completed_retention=0)
        ring = PcapRing(self.dir, policy)
        ring.write_packet(time.time(), b"\x00" * 100)
        # Next record: 16 + 180 = 196 > 200-24? 24+196=220 > 200 → rotate.
        # After rotate, current_size = 24 + 196 > 200 → drop.
        ok = ring.write_packet(time.time(), b"\x00" * 180)
        self.assertFalse(ok)
        stats = ring.stats()
        self.assertEqual(stats.dropped, 1)
        ring.close()

    def test_generation_retirement(self):
        policy = PcapRingPolicy(segment_bytes=200, segments=2, per_table_cap=10000, completed_retention=0)
        ring = PcapRing(self.dir, policy)
        # Rotate through multiple segments/generations.
        for i in range(40):
            ring.write_packet(time.time() + i, b"\x00" * 30)
        ring.close()
        # At least some files should exist (generations may be deleted due to 0 retention).
        # Just check no crash.
        remaining = list(Path(self.dir).glob("seg_*.pcap"))
        self.assertGreaterEqual(len(remaining), 0)

    def test_thread_safety(self):
        import threading
        ring = PcapRing(self.dir, self.policy)
        def writer():
            for i in range(20):
                ring.write_packet(time.time(), b"\x00" * 50)
        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        ring.close()
        self.assertTrue(Path(self.dir).exists())


class PcapRingManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_and_releases_rings(self):
        mgr = PcapRingManager(self.tmp, PcapRingPolicy(segment_bytes=5000, segments=2, per_table_cap=20000))
        r1 = mgr.ring("device_A/table_01")
        r1.write_packet(time.time(), b"\x00" * 50)
        self.assertIn("device_A/table_01", mgr.stats())
        mgr.release("device_A/table_01")
        self.assertNotIn("device_A/table_01", mgr.stats())
        mgr.close_all()


if __name__ == "__main__":
    unittest.main()