"""In-process safe-module reload for the live trainer."""
from __future__ import annotations

import importlib
import sys
from typing import Any


SAFE_MODULES = (
    "core.verified_v1.prefold",
    "core.v6router.wallet_cash",
    "core.v6router.history_ledger",
    "core.hot_reload",
)


def reload_safe_modules() -> list[str]:
    reloaded: list[str] = []
    for name in SAFE_MODULES:
        module = sys.modules.get(name)
        if module is None:
            try:
                importlib.import_module(name)
            except Exception:
                continue
            module = sys.modules.get(name)
        if module is None:
            continue
        try:
            importlib.reload(module)
        except Exception:
            continue
        reloaded.append(name)
    return reloaded


def refresh_runtime(runtime: Any) -> dict[str, Any]:
    names = reload_safe_modules()
    cleared = 0
    service = getattr(runtime, "router_service", runtime)
    routers = getattr(service, "_routers", {}) or {}
    if isinstance(routers, dict):
        for router in routers.values():
            sessions = getattr(router, "_sessions", None) or getattr(router, "sessions", None) or {}
            if not isinstance(sessions, dict):
                continue
            for item in sessions.values():
                bridge = getattr(item, "_bridge", None)
                if bridge is not None and hasattr(bridge, "_prefold_config"):
                    bridge._prefold_config = None
                    cleared += 1
    return {"reloaded": names, "prefold_caches": cleared}
