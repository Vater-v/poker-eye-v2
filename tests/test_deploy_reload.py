from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.deploy_reload import (
    annotate_snapshot,
    clear_reload_request,
    install_trainer_action,
    read_disk_build,
    request_path,
    should_apply_deferred_reload,
    watcher_action,
    write_reload_request,
)


class DeployReloadDecisionTests(unittest.TestCase):
    def test_force_and_inactive_always_restart(self):
        sitting = {"ok": True, "seated_tables": 2, "live_tables": 2, "build": "old"}
        self.assertEqual(
            install_trainer_action(sitting, force=True, trainer_active=True),
            "restart",
        )
        self.assertEqual(
            install_trainer_action(None, force=False, trainer_active=False),
            "restart",
        )

    def test_hold_when_seated_or_live(self):
        self.assertEqual(
            install_trainer_action(
                {"ok": True, "seated_tables": 2, "live_tables": 2, "build": "old"},
                force=False,
                trainer_active=True,
            ),
            "hold",
        )
        self.assertEqual(
            install_trainer_action(
                {"ok": True, "seated_tables": 0, "live_tables": 1, "build": "old"},
                force=False,
                trainer_active=True,
            ),
            "hold",
        )

    def test_restart_when_fleet_empty(self):
        self.assertEqual(
            install_trainer_action(
                {"ok": True, "seated_tables": 0, "live_tables": 0, "build": "old"},
                force=False,
                trainer_active=True,
            ),
            "restart",
        )

    def test_hold_when_snapshot_unknown_or_stale(self):
        self.assertEqual(
            install_trainer_action(None, force=False, trainer_active=True),
            "hold",
        )
        self.assertEqual(
            install_trainer_action(
                {"ok": True, "stale": True, "seated_tables": 0, "live_tables": 0},
                force=False,
                trainer_active=True,
            ),
            "hold",
        )
        self.assertEqual(
            install_trainer_action(
                {"ok": True, "build": "old"},
                force=False,
                trainer_active=True,
            ),
            "hold",
        )

    def test_deferred_apply_only_on_empty_pending(self):
        self.assertFalse(should_apply_deferred_reload(
            seated_tables=2, live_tables=2, reload_pending=True,
        ))
        self.assertFalse(should_apply_deferred_reload(
            seated_tables=0, live_tables=0, reload_pending=False,
        ))
        self.assertTrue(should_apply_deferred_reload(
            seated_tables=0, live_tables=0, reload_pending=True,
        ))

    def test_watcher_waits_while_sitting_then_restarts(self):
        sitting = {"ok": True, "seated_tables": 1, "live_tables": 1, "build": "old"}
        self.assertEqual(
            watcher_action(sitting, disk_build="new", request_exists=True),
            "wait",
        )
        empty = {"ok": True, "seated_tables": 0, "live_tables": 0, "build": "old"}
        self.assertEqual(
            watcher_action(empty, disk_build="new", request_exists=True),
            "restart",
        )
        live = {"ok": True, "seated_tables": 0, "live_tables": 0, "build": "new"}
        self.assertEqual(
            watcher_action(live, disk_build="new", request_exists=True),
            "stop",
        )
        self.assertEqual(
            watcher_action(empty, disk_build="new", request_exists=False),
            "stop",
        )
        self.assertEqual(
            watcher_action(None, disk_build="new", request_exists=True),
            "wait",
        )
        self.assertEqual(
            watcher_action({"ok": True, "build": "old"}, disk_build="new", request_exists=True),
            "wait",
        )

    def test_annotate_pending_on_build_mismatch(self):
        snap = annotate_snapshot(
            {"ok": True, "build": "V7.4.72-HMN1-VPS"},
            disk_build="V7.4.73-HMN1-VPS",
            request_exists=False,
        )
        self.assertTrue(snap["reload_pending"])
        self.assertEqual(snap["staged_build"], "V7.4.73-HMN1-VPS")
        same = annotate_snapshot(
            {"ok": True, "build": "V7.4.73-HMN1-VPS"},
            disk_build="V7.4.73-HMN1-VPS",
            request_exists=False,
        )
        self.assertFalse(same["reload_pending"])
        requested = annotate_snapshot(
            {"ok": True, "build": "V7.4.73-HMN1-VPS"},
            disk_build="V7.4.73-HMN1-VPS",
            request_exists=True,
        )
        self.assertTrue(requested["reload_pending"])

    def test_request_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "BUILD_ID").write_text("staged-build\n", encoding="utf-8")
            self.assertEqual(read_disk_build(root), "staged-build")
            path = write_reload_request("staged-build", root)
            self.assertEqual(path, request_path(root))
            self.assertTrue(path.is_file())
            clear_reload_request(root)
            self.assertFalse(path.is_file())
            clear_reload_request(root)

    def test_install_sh_does_not_unconditionally_restart_trainer(self):
        text = Path(__file__).resolve().parents[1].joinpath("vps", "install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("core.deploy_reload", text)
        self.assertIn("POKEREYE_FORCE_RESTART", text)
        self.assertIn("deferred_reload.sh", text)
        self.assertIn('"$ACTION" != "hold"', text)
        self.assertNotIn("Always restart after replacing runtime files", text)

    def test_live_pressure_is_conservative_max(self):
        from core.production_runtime import RouterService

        svc = RouterService.__new__(RouterService)
        auto = SimpleNamespace(_seated={11})
        router = SimpleNamespace(automation=auto, active_table_ids=(11, 22))
        svc._routers = {"dev": router}
        svc.automation_store = SimpleNamespace(
            all_runtime=lambda: {"dev": {"seated_tables": [11]}}
        )
        svc._last_snapshot = {"seated_tables": 3, "live_tables": 4}
        seated, live = svc.live_pressure()
        self.assertEqual(seated, 3)
        self.assertEqual(live, 4)

    def test_deferred_exec_skipped_while_sitting(self):
        from core.production_runtime import ProductionTrainer

        trainer = ProductionTrainer.__new__(ProductionTrainer)
        trainer._reload_flag = threading.Event()
        trainer._reload_flag.set()
        trainer.router_service = SimpleNamespace(live_pressure=lambda: (2, 2))
        trainer.logger = SimpleNamespace(emit=lambda *a, **k: None)
        called = {"shutdown": False}

        def boom():
            called["shutdown"] = True
            raise AssertionError("must not exec while sitting")

        trainer.shutdown = boom
        with patch("core.production_runtime.read_disk_build", return_value="staged"):
            with patch("core.production_runtime.BUILD_ID", "live"):
                self.assertFalse(trainer._maybe_deferred_exec())
        self.assertFalse(called["shutdown"])

    def test_console_restart_is_eye_lease_not_trainer(self):
        router = Path(__file__).resolve().parents[1].joinpath(
            "core", "v6router", "router.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Recycle only the PokerEYE side for one admitted table", router)
        vue = Path(__file__).resolve().parents[1].joinpath(
            "web", "console", "pages", "index.vue"
        ).read_text(encoding="utf-8")
        self.assertIn("table/restart", vue)
        self.assertIn("↻ PokerEYE", vue)


if __name__ == "__main__":
    unittest.main()
