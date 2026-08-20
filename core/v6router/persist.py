"""Automation policy + runtime snapshot. Redis first, JSON file fallback."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .lobby_scene import MIN_PLAYERS


ALLOWED_BB = (0.02, 0.05, 0.1, 0.2, 0.5, 1.0)
DEFAULT_BB = 0.02
DEFAULT_TABLES = 5
COIN_MAX_TABLES = 5
LEAVE_BELOW_BB = 79.0
OPEN_IF_FREE_BB = 100.0

_RUNTIME_PREFIX = "pokereye:v1:device:"
_POLICY_KEY = "pokereye:v1:policies"
_CATALOG_KEY = "pokereye:v1:catalog"


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _closest_bb(value: Any) -> float:
    number = _finite(value)
    if number is None:
        return DEFAULT_BB
    return min(ALLOWED_BB, key=lambda item: abs(item - number))


def _policy_path() -> Path:
    raw = os.getenv("POKEREYE_AUTO_FILE", "").strip()
    if raw:
        return Path(raw)
    data = Path("/opt/pokereye/data/device_automation.json")
    if data.parent.is_dir():
        return data
    return Path(__file__).resolve().parents[2] / "config" / "device_automation.json"


class AutoPolicy:
    enabled: bool = False
    play_enabled: bool = True
    table_count: int = DEFAULT_TABLES
    bb: float = DEFAULT_BB
    watch_balance: bool = True
    watch_players: bool = True
    min_players: int = MIN_PLAYERS
    leave_below_bb: float = LEAVE_BELOW_BB
    open_if_free_bb: float = OPEN_IF_FREE_BB

    def __init__(self, **kwargs: Any) -> None:
        self.enabled = bool(kwargs.get("enabled", False))
        self.play_enabled = bool(kwargs.get("play_enabled", True))
        self.table_count = int(kwargs.get("table_count", DEFAULT_TABLES))
        self.bb = float(kwargs.get("bb", DEFAULT_BB))
        self.watch_balance = bool(kwargs.get("watch_balance", True))
        self.watch_players = bool(kwargs.get("watch_players", True))
        self.min_players = int(kwargs.get("min_players", MIN_PLAYERS))
        self.leave_below_bb = float(kwargs.get("leave_below_bb", LEAVE_BELOW_BB))
        self.open_if_free_bb = float(kwargs.get("open_if_free_bb", OPEN_IF_FREE_BB))

    @classmethod
    def from_mapping(cls, raw: Any) -> "AutoPolicy":
        data = dict(raw or {}) if isinstance(raw, dict) else {}
        tables = int(data.get("table_count") or DEFAULT_TABLES)
        tables = max(1, min(COIN_MAX_TABLES, tables))
        min_players = int(data.get("min_players") or MIN_PLAYERS)
        min_players = max(2, min(9, min_players))
        leave_below = _finite(data.get("leave_below_bb")) or LEAVE_BELOW_BB
        open_free = _finite(data.get("open_if_free_bb")) or OPEN_IF_FREE_BB
        play = data.get("play_enabled")
        return cls(
            enabled=bool(data.get("enabled")),
            play_enabled=True if play is None else bool(play),
            table_count=tables,
            bb=_closest_bb(data.get("bb")),
            watch_balance=bool(data.get("watch_balance", True)),
            watch_players=bool(data.get("watch_players", True)),
            min_players=min_players,
            leave_below_bb=max(1.0, float(leave_below)),
            open_if_free_bb=max(1.0, float(open_free)),
        )

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "play_enabled": self.play_enabled,
            "table_count": self.table_count,
            "bb": self.bb,
            "watch_balance": self.watch_balance,
            "watch_players": self.watch_players,
            "min_players": self.min_players,
            "leave_below_bb": self.leave_below_bb,
            "open_if_free_bb": self.open_if_free_bb,
        }


class _RedisClient:
    """Minimal Redis GET/SET/HGETALL/HSET. Optional redis-py, else raw RESP."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._py = None
        self._sock_lock = threading.Lock()
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = int(parsed.port or 6379)
        self.db = int((parsed.path or "/0").strip("/") or 0)
        try:
            import redis  # type: ignore
            self._py = redis.Redis.from_url(url, decode_responses=True, socket_timeout=0.4)
            self._py.ping()
        except Exception:
            self._py = None
            self._ping_raw()

    def _ping_raw(self) -> None:
        import socket
        sock = socket.create_connection((self.host, self.port), timeout=0.4)
        try:
            if self.db:
                sock.sendall(f"*2\r\n$6\r\nSELECT\r\n${len(str(self.db))}\r\n{self.db}\r\n".encode())
                sock.recv(64)
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            sock.recv(64)
        finally:
            sock.close()

    def get(self, key: str) -> Optional[str]:
        if self._py is not None:
            value = self._py.get(key)
            return str(value) if value is not None else None
        return self._raw("GET", key)

    def set(self, key: str, value: str) -> None:
        if self._py is not None:
            self._py.set(key, value)
            return
        self._raw("SET", key, value)

    def hgetall(self, key: str) -> dict[str, str]:
        if self._py is not None:
            data = self._py.hgetall(key) or {}
            return {str(k): str(v) for k, v in data.items()}
        blob = self._raw_array("HGETALL", key)
        out: dict[str, str] = {}
        for i in range(0, len(blob) - 1, 2):
            out[blob[i]] = blob[i + 1]
        return out

    def hset(self, key: str, field: str, value: str) -> None:
        if self._py is not None:
            self._py.hset(key, field, value)
            return
        self._raw("HSET", key, field, value)

    def _raw(self, *parts: str) -> Optional[str]:
        items = self._raw_array(*parts)
        return items[0] if items else None

    def _raw_array(self, *parts: str) -> list[str]:
        import socket
        chunks = [f"*{len(parts)}\r\n".encode()]
        for part in parts:
            encoded = part.encode("utf-8")
            chunks.append(f"${len(encoded)}\r\n".encode() + encoded + b"\r\n")
        with self._sock_lock:
            sock = socket.create_connection((self.host, self.port), timeout=0.4)
            try:
                if self.db:
                    db = str(self.db)
                    sock.sendall(f"*2\r\n$6\r\nSELECT\r\n${len(db)}\r\n{db}\r\n".encode())
                    sock.recv(64)
                sock.sendall(b"".join(chunks))
                return _read_resp(sock)
            finally:
                sock.close()


def _read_resp(sock: Any) -> list[str]:
    def line() -> bytes:
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                break
            buf += chunk
        return buf[:-2]

    first = line()
    if not first:
        return []
    kind = first[:1]
    if kind == b"$":
        n = int(first[1:] or -1)
        if n < 0:
            return []
        data = b""
        while len(data) < n + 2:
            data += sock.recv(n + 2 - len(data))
        return [data[:n].decode("utf-8", "replace")]
    if kind == b"+":
        return [first[1:].decode("utf-8", "replace")]
    if kind == b"*":
        count = int(first[1:] or 0)
        out: list[str] = []
        for _ in range(max(0, count)):
            out.extend(_read_resp(sock))
        return out
    if kind == b":":
        return [first[1:].decode("utf-8", "replace")]
    return []


def _connect_redis(url: str) -> Optional[_RedisClient]:
    try:
        return _RedisClient(url)
    except Exception:
        return None


class AutomationStore:
    def __init__(self, path: Optional[Path] = None, redis_url: Optional[str] = None) -> None:
        self.path = path or _policy_path()
        self._lock = threading.Lock()
        self._rows: dict[str, AutoPolicy] = {}
        self._catalog: dict[int, dict[str, Any]] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        if redis_url is None:
            url = (os.getenv("POKEREYE_REDIS") or "redis://127.0.0.1:6379/0").strip()
        else:
            url = str(redis_url).strip()
        self._redis = _connect_redis(url) if url else None
        self._load()
        self._pull_redis()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        devices = raw.get("devices")
        if isinstance(devices, dict):
            for key, value in devices.items():
                self._rows[str(key)] = AutoPolicy.from_mapping(value)
        catalog = raw.get("catalog")
        if isinstance(catalog, dict):
            for key, value in catalog.items():
                if not isinstance(value, dict):
                    continue
                try:
                    config_id = int(value.get("config_id") or key)
                except (TypeError, ValueError):
                    continue
                if config_id > 0:
                    self._catalog[config_id] = dict(value)
        runtime = raw.get("runtime")
        if isinstance(runtime, dict):
            for key, value in runtime.items():
                if isinstance(value, dict):
                    self._runtime[str(key)] = dict(value)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 2,
            "devices": {key: policy.public() for key, policy in self._rows.items()},
            "catalog": {str(key): dict(value) for key, value in self._catalog.items()},
            "runtime": {key: dict(value) for key, value in self._runtime.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _pull_redis(self) -> None:
        client = self._redis
        if client is None:
            return
        try:
            policies = client.hgetall(_POLICY_KEY)
            for key, blob in policies.items():
                self._rows[str(key)] = AutoPolicy.from_mapping(json.loads(blob))
            catalog = client.get(_CATALOG_KEY)
            if catalog:
                parsed = json.loads(catalog)
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        if isinstance(value, dict):
                            self._catalog[int(value.get("config_id") or key)] = dict(value)
        except Exception:
            return

    def get(self, device_id: str) -> Optional[AutoPolicy]:
        with self._lock:
            return self._rows.get(str(device_id))

    def put(self, device_id: str, policy: AutoPolicy) -> None:
        with self._lock:
            self._rows[str(device_id)] = policy
            self._save()
            if self._redis is not None:
                try:
                    self._redis.hset(_POLICY_KEY, str(device_id), json.dumps(policy.public()))
                except Exception:
                    pass

    def put_catalog(self, cfg: Any) -> None:
        row = asdict(cfg) if hasattr(cfg, "__dataclass_fields__") else dict(cfg)
        with self._lock:
            prev = self._catalog.get(int(row["config_id"]))
            if prev == row:
                return
            self._catalog[int(row["config_id"])] = row
            self._save()
            if self._redis is not None:
                try:
                    self._redis.set(_CATALOG_KEY, json.dumps(self._catalog))
                except Exception:
                    pass

    def catalog(self) -> list[Any]:
        from .automation import RoomConfig
        with self._lock:
            rows = list(self._catalog.values())
        out: list[Any] = []
        for value in rows:
            try:
                config_id = int(value.get("config_id") or 0)
                bb = float(value.get("big_blind") or 0)
            except (TypeError, ValueError):
                continue
            if config_id <= 0 or bb <= 0:
                continue
            try:
                mini = int(value.get("mini_game_type") or 1)
            except (TypeError, ValueError):
                mini = 1
            try:
                size = int(value.get("table_size") or 6)
            except (TypeError, ValueError):
                size = 6
            out.append(RoomConfig(
                config_id=config_id,
                big_blind=bb,
                min_buyin=float(value.get("min_buyin") or 0),
                max_buyin=float(value.get("max_buyin") or 0),
                mini_game_type=mini,
                table_size=size,
                name=str(value.get("name") or ""),
            ))
        return out

    def get_runtime(self, device_id: str) -> Optional[dict[str, Any]]:
        key = str(device_id)
        with self._lock:
            local = dict(self._runtime.get(key) or {})
        if self._redis is not None:
            try:
                blob = self._redis.get(_RUNTIME_PREFIX + key)
                if blob:
                    parsed = json.loads(blob)
                    if isinstance(parsed, dict):
                        return parsed
            except Exception:
                pass
        return local or None

    def all_runtime(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = {str(key): dict(value) for key, value in self._runtime.items()}
        client = self._redis
        if client is None:
            return rows
        try:
            for key in list(rows):
                blob = client.get(_RUNTIME_PREFIX + key)
                if blob:
                    parsed = json.loads(blob)
                    if isinstance(parsed, dict):
                        rows[key] = parsed
        except Exception:
            pass
        return rows

    def put_runtime(self, device_id: str, payload: dict[str, Any]) -> None:
        key = str(device_id)
        row = dict(payload or {})
        row["ts"] = time.time()
        with self._lock:
            self._runtime[key] = row
            try:
                self._save()
            except Exception:
                pass
        if self._redis is not None:
            try:
                self._redis.set(_RUNTIME_PREFIX + key, json.dumps(row))
            except Exception:
                pass
