"""Per-device lobby automation: join N NLH tables, keep 79–100 BB, leave short tables."""
from __future__ import annotations

import base64
import json
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..verified_v1.coin_action_wire import (
    build_game_leave_seat_packet,
    build_game_quit_table_packet,
    build_game_reserve_seat_packet,
    build_game_take_seat_packet,
    build_lobby_join_game_packet,
)
from .router import RoutedEvent, _decode_event, _forward


ALLOWED_BB = (0.02, 0.05, 0.1, 0.2, 0.5, 1.0)
DEFAULT_BB = 0.02
DEFAULT_TABLES = 5
COIN_MAX_TABLES = 5
LEAVE_BELOW_BB = 79.0
OPEN_IF_FREE_BB = 100.0
MIN_PLAYERS = 3
JOIN_GAP_SECONDS = 8.0
WATCHDOG_SECONDS = 2.0
EMPTY_GRACE_SECONDS = 25.0
TAB_COOLDOWN_SECONDS = 25.0
JOIN_REJECT_COOLDOWN = 20.0


def _policy_path() -> Path:
    raw = os.getenv("POKEREYE_AUTO_FILE", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "config" / "device_automation.json"


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


@dataclass
class AutoPolicy:
    enabled: bool = False
    table_count: int = DEFAULT_TABLES
    bb: float = DEFAULT_BB
    watch_balance: bool = True
    watch_players: bool = True
    min_players: int = MIN_PLAYERS
    leave_below_bb: float = LEAVE_BELOW_BB
    open_if_free_bb: float = OPEN_IF_FREE_BB

    @classmethod
    def from_mapping(cls, raw: Any) -> "AutoPolicy":
        data = dict(raw or {}) if isinstance(raw, dict) else {}
        tables = int(data.get("table_count") or DEFAULT_TABLES)
        tables = max(1, min(COIN_MAX_TABLES, tables))
        min_players = int(data.get("min_players") or MIN_PLAYERS)
        min_players = max(2, min(9, min_players))
        leave_below = _finite(data.get("leave_below_bb")) or LEAVE_BELOW_BB
        open_free = _finite(data.get("open_if_free_bb")) or OPEN_IF_FREE_BB
        return cls(
            enabled=bool(data.get("enabled")),
            table_count=tables,
            bb=_closest_bb(data.get("bb")),
            watch_balance=bool(data.get("watch_balance", True)),
            watch_players=bool(data.get("watch_players", True)),
            min_players=min_players,
            leave_below_bb=max(1.0, float(leave_below)),
            open_if_free_bb=max(1.0, float(open_free)),
        )

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoomConfig:
    config_id: int
    big_blind: float
    min_buyin: float
    max_buyin: float
    mini_game_type: int = 1
    table_size: int = 6
    name: str = ""


@dataclass
class PendingCommand:
    command: str
    room_id: int
    raw: bytes
    token: str
    due_at: float
    ws_kind: str
    table_id: int = 0
    data: dict[str, Any] = field(default_factory=dict)


class DeviceAutomation:
    """Observe lobby dummy, join Ring NLH tables, and keep the fleet at policy."""

    def __init__(
        self,
        device_id: str,
        *,
        sink: Optional[Callable[..., None]] = None,
        store: Optional["AutomationStore"] = None,
    ) -> None:
        self.device_id = str(device_id)
        self._sink = sink or (lambda *_args, **_kwargs: None)
        self._store = store
        self.policy = AutoPolicy()
        self.lobby_ws = ""
        self.lobby_url = ""
        self.game_ws: dict[int, str] = {}
        self.room_of_table: dict[int, int] = {}
        self.table_of_room: dict[int, int] = {}
        self.configs: dict[int, RoomConfig] = {}
        self.wallet_cash: Optional[float] = None
        self._queue: list[PendingCommand] = []
        self._joining = False
        self._last_join_at = 0.0
        self._gradual: list[tuple[int, float, str]] = []
        self._leave_reasons: dict[int, str] = {}
        self._closed_reasons: dict[int, str] = {}
        self._seated: set[int] = set()
        self._players: dict[int, int] = {}
        self._stack_bb: dict[int, float] = {}
        self._sitout: set[int] = set()
        self._want_seat: set[int] = set()
        self._seated_at: dict[int, float] = {}
        self._hand_live: dict[int, bool] = {}
        self._table_bb: dict[int, float] = {}
        self._leave_inflight: set[int] = set()
        self._tabs: dict[int, float] = {}
        self._join_block_until = 0.0
        self._status = "idle"
        self._counter = 0
        if store is not None:
            saved = store.get(self.device_id)
            if saved is not None:
                self.policy = saved

    def snapshot(self) -> dict[str, Any]:
        bbs = sorted({round(row.big_blind, 4) for row in self.configs.values() if row.mini_game_type == 1})
        return {
            "enabled": self.policy.enabled,
            "policy": self.policy.public(),
            "status": self._status,
            "wallet_cash": self.wallet_cash,
            "wallet_bb": (
                None if self.wallet_cash is None or self.policy.bb <= 0
                else round(self.wallet_cash / self.policy.bb, 1)
            ),
            "catalog_bb": bbs or list(ALLOWED_BB),
            "joining": self._joining,
            "gradual_leave": len(self._gradual),
            "sitout_tables": sorted(self._sitout),
            "coin_tabs": len(self._tabs),
            "coin_tab_cap": COIN_MAX_TABLES,
        }

    def apply_policy(self, raw: dict[str, Any], *, enable: Optional[bool] = None) -> AutoPolicy:
        policy = AutoPolicy.from_mapping({**self.policy.public(), **dict(raw or {})})
        if enable is not None:
            policy.enabled = bool(enable)
        self.policy = policy
        if not policy.enabled:
            self._joining = False
            self._queue = [item for item in self._queue if item.command not in {"lobby.join_game", "game.reserve_Seat", "game.take_Seat"}]
            self._status = "paused"
        else:
            self._status = "armed"
        if self._store is not None:
            self._store.put(self.device_id, policy)
        self._note("automation.policy", f"auto {'on' if policy.enabled else 'off'} · {policy.table_count}×{policy.bb:g}")
        return policy

    def schedule_leave_all(self, table_ids: list[int], *, gradual: bool) -> int:
        now = time.monotonic()
        delay = 0.0
        queued = 0
        for table_id in table_ids:
            table_id = int(table_id)
            if table_id <= 0:
                continue
            if gradual:
                delay += random.uniform(120.0, 600.0)
            self._gradual.append((table_id, now + delay, "operator leave-all"))
            queued += 1
            if not gradual:
                delay += 0.4
        self._status = "leaving"
        self._note(
            "automation.leave_all",
            f"покинуть все столы · {'постепенно' if gradual else 'сразу'} · {queued}",
        )
        return queued

    def note_table_closed(self, table_id: int, reason: str = "") -> None:
        table_id = int(table_id)
        why = str(self._leave_reasons.pop(table_id, "") or reason or "closed")
        self._closed_reasons[table_id] = why
        stack_bb = self._stack_bb.pop(table_id, None)
        bb = self._table_bb.pop(table_id, self.policy.bb)
        if stack_bb and bb and self.wallet_cash is not None:
            self.wallet_cash = float(self.wallet_cash) + float(stack_bb) * float(bb)
        self._seated.discard(table_id)
        self._sitout.discard(table_id)
        self._players.pop(table_id, None)
        self._seated_at.pop(table_id, None)
        self._hand_live.pop(table_id, None)
        self._leave_inflight.discard(table_id)
        self._want_seat.discard(table_id)
        self._tabs[table_id] = time.monotonic() + TAB_COOLDOWN_SECONDS
        self._gradual = [row for row in self._gradual if row[0] != table_id]
        self._queue = [item for item in self._queue if item.table_id != table_id]
        if self.policy.enabled:
            self._status = "refill"

    def drain_leaves(self) -> list[tuple[int, str]]:
        now = time.monotonic()
        due = [row for row in self._gradual if row[1] <= now]
        self._gradual = [row for row in self._gradual if row[1] > now]
        out: list[tuple[int, str]] = []
        for table_id, _when, reason in due:
            table_id = int(table_id)
            if table_id in self._leave_inflight:
                continue
            self._leave_inflight.add(table_id)
            out.append((table_id, reason))
        for table_id, reason in list(self._leave_reasons.items()):
            table_id = int(table_id)
            if table_id in self._leave_inflight:
                continue
            self._leave_inflight.add(table_id)
            out.append((table_id, reason))
        return out

    def coin_tab_count(self) -> int:
        now = time.monotonic()
        keep: dict[int, float] = {}
        for table_id, expiry in self._tabs.items():
            if expiry == 0 or expiry > now:
                keep[int(table_id)] = float(expiry)
        self._tabs = keep
        return len(keep)

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        try:
            routed = _decode_event(event)
        except Exception:
            return _forward(event)
        self._observe(routed)
        if routed.is_dummy:
            injected = self._inject(routed)
            if injected is not None:
                return injected
            self.tick(seated_tables=len(self._seated))
            injected = self._inject(routed)
            if injected is not None:
                return injected
        return _forward(event)

    def tick(self, *, seated_tables: int, live_table_ids: Optional[list[int]] = None) -> None:
        now = time.monotonic()
        live = set(int(item) for item in (live_table_ids or self._seated))
        self._seated = {tid for tid in self._seated if tid in live}
        if not self.policy.enabled:
            return
        pending_joins = sum(1 for item in self._queue if item.command == "lobby.join_game")
        tabs = self.coin_tab_count()
        open_count = max(len(live), tabs) + pending_joins + (1 if self._joining else 0)
        target = min(int(self.policy.table_count), COIN_MAX_TABLES)
        if open_count < target and self._can_open(open_count=open_count):
            self._queue_join()
        for table_id in list(live):
            players = int(self._players.get(table_id) or 0)
            in_hand = bool(self._hand_live.get(table_id))
            age = now - float(self._seated_at.get(table_id) or now)
            if self.policy.watch_players and players and players < int(self.policy.min_players):
                if in_hand:
                    self._request_leave(table_id, f"мало игроков в раздаче ({players})")
                elif age >= EMPTY_GRACE_SECONDS:
                    self._request_leave(table_id, f"пусто {players} игроков после {int(age)}с")
            if self.policy.watch_balance:
                stack = self._stack_bb.get(table_id)
                if stack is not None and stack < float(self.policy.leave_below_bb) and not in_hand:
                    self._request_leave(table_id, f"стек {stack:.1f} BB < {self.policy.leave_below_bb:g}")
            if table_id in self._sitout and not in_hand:
                self._request_leave(table_id, "sitout")

    def _can_open(self, *, open_count: int = 0) -> bool:
        now = time.monotonic()
        if self._joining:
            return False
        if now < float(self._join_block_until or 0.0):
            self._status = "join-cooldown"
            return False
        if now - self._last_join_at < JOIN_GAP_SECONDS:
            return False
        if open_count >= min(int(self.policy.table_count), COIN_MAX_TABLES):
            self._status = f"cap {open_count}/{COIN_MAX_TABLES}"
            return False
        if self.coin_tab_count() >= COIN_MAX_TABLES:
            self._status = "coin 5 tabs"
            return False
        if not self._pick_config():
            self._status = "waiting-catalog"
            return False
        if not self.lobby_ws:
            self._status = "waiting-lobby"
            return False
        if self.policy.watch_balance and self.wallet_cash is not None:
            free_bb = self.wallet_cash / max(self.policy.bb, 1e-9)
            if free_bb < float(self.policy.open_if_free_bb):
                self._status = f"wallet {free_bb:.0f} BB"
                return False
        return True

    def _pick_config(self) -> Optional[RoomConfig]:
        target = float(self.policy.bb)
        candidates = [
            row for row in self.configs.values()
            if row.mini_game_type == 1 and abs(row.big_blind - target) <= max(0.0005, target * 0.05)
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: (abs(row.big_blind - target), row.config_id))[0]

    def _buyin(self, config: RoomConfig) -> float:
        want = float(self.policy.open_if_free_bb) * float(config.big_blind)
        amount = min(float(config.max_buyin or want), want)
        if config.min_buyin:
            amount = max(float(config.min_buyin), amount)
        return amount

    def _queue_join(self) -> None:
        config = self._pick_config()
        if config is None:
            return
        buyin = self._buyin(config)
        self._counter += 1
        token = f"autojoin:{self.device_id}:{self._counter}"
        raw = build_lobby_join_game_packet(
            config_id=config.config_id,
            big_blind=config.big_blind,
            buyin=buyin,
        )
        self._queue.append(PendingCommand(
            command="lobby.join_game",
            room_id=-1,
            raw=raw,
            token=token,
            due_at=time.monotonic(),
            ws_kind="lobby",
            data={"configId": config.config_id, "buyin": buyin},
        ))
        self._joining = True
        self._last_join_at = time.monotonic()
        self._status = f"join {config.big_blind:g}"
        self._note("automation.join", f"ищем стол NLH {config.big_blind:g} · buyin {buyin:g}")

    def _request_leave(self, table_id: int, reason: str) -> None:
        table_id = int(table_id)
        if table_id in self._leave_reasons:
            return
        self._leave_reasons[table_id] = reason
        self._note("automation.leave", f"стол {table_id}: {reason}")

    def _observe(self, routed: RoutedEvent) -> None:
        if routed.is_dummy:
            url = str(routed.event.get("url") or "")
            ws = str(routed.websocket_id or "")
            if ws and ("mainlobby" in url.lower() or routed.room_id in {None, -1, 0}):
                self.lobby_ws = ws
                self.lobby_url = url
            if ws and routed.room_id:
                table_id = self.table_of_room.get(int(routed.room_id), 0)
                if table_id:
                    self.game_ws[table_id] = ws
            return
        data = routed.data if isinstance(routed.data, dict) else {}
        if routed.decoded_body:
            self._ingest_catalog(routed)
        if routed.command == "lobby.join_game" and routed.direction == "in":
            failed = data.get("isSuccess") is False or int(data.get("errorCode") or 0) != 0
            if failed:
                self._joining = False
                self._join_block_until = time.monotonic() + JOIN_REJECT_COOLDOWN
                self._status = "join-rejected"
                self._note("automation.join_rejected", "Coin отклонил поиск стола · пауза", severity="WARN")
        if routed.command == "lobby.join_game_table" and routed.direction == "in":
            self._joining = False
            for tid in routed.table_ids:
                self._tabs[int(tid)] = 0.0
            for row in data.get("tablesToJoin") or ():
                if not isinstance(row, dict):
                    continue
                props = row.get("roomProperties") if isinstance(row.get("roomProperties"), dict) else {}
                self._remember_config(props, name=str(row.get("tableName") or ""))
                balance = _finite(data.get("balance") or props.get("balance"))
                if balance is not None:
                    self.wallet_cash = balance
        if routed.command in {"game.game_init", "game.wait_list_data"} and routed.table_ids:
            table_id = int(routed.table_ids[0])
            if routed.room_id:
                self.room_of_table[table_id] = int(routed.room_id)
                self.table_of_room[int(routed.room_id)] = table_id
            if routed.websocket_id:
                self.game_ws[table_id] = str(routed.websocket_id)
            if routed.command == "game.game_init":
                self._want_seat.add(table_id)
        if routed.command == "game.reserve_Seat" and routed.direction == "in":
            balance = _finite(data.get("balance"))
            if balance is not None:
                self.wallet_cash = balance
            if data.get("isReserved") is True or int(data.get("errorCode") or 0) == 0:
                table_id = int(self.table_of_room.get(int(routed.room_id or 0), 0) or (routed.table_ids[0] if routed.table_ids else 0))
                seat = int(data.get("seatId") or 0)
                buyin = _finite(data.get("maxBuyin") or data.get("maxbuyin"))
                if table_id and seat and routed.room_id:
                    amount = buyin or self._buyin(self._pick_config() or RoomConfig(0, self.policy.bb, 0, buyin or 0))
                    self._enqueue(
                        "game.take_Seat",
                        int(routed.room_id),
                        build_game_take_seat_packet(int(routed.room_id), seat, amount),
                        ws_kind="game",
                        table_id=table_id,
                    )
        if routed.command in {"game.take_Seat", "game.seatInfo", "game.seat"}:
            self._observe_seats(routed)
        if routed.command == "game.quit_table" and routed.direction == "in":
            table_id = int(self.table_of_room.get(int(routed.room_id or 0), 0) or (routed.table_ids[0] if routed.table_ids else 0))
            if table_id:
                self.note_table_closed(table_id, "quit")

    def _observe_seats(self, routed: RoutedEvent) -> None:
        data = routed.data if isinstance(routed.data, dict) else {}
        rows = data.get("seatResponseDataList")
        if not isinstance(rows, list):
            if routed.command in {"game.take_Seat", "game.seat"}:
                rows = [data]
            else:
                return
        table_id = int(self.table_of_room.get(int(routed.room_id or 0), 0) or (routed.table_ids[0] if routed.table_ids else 0))
        occupied = []
        hero = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                seat = int(row.get("seatId") or 0)
            except (TypeError, ValueError):
                continue
            uid = 0
            try:
                uid = int(row.get("userId") or 0)
            except (TypeError, ValueError):
                uid = 0
            if seat and (uid or row.get("userName") or row.get("isPlaying") is True):
                occupied.append(row)
            if row.get("_hero") or False:
                hero = row
        if table_id:
            self._players[table_id] = len(occupied)
            if occupied and table_id not in self._want_seat:
                self._seated.add(table_id)
            if routed.websocket_id:
                self.game_ws[table_id] = str(routed.websocket_id)
            if routed.room_id:
                self.room_of_table[table_id] = int(routed.room_id)
                self.table_of_room[int(routed.room_id)] = table_id
        if routed.command == "game.take_Seat" and routed.direction == "in":
            seated = data.get("isSeated") is True or data.get("isSuccess") is not False
            if seated and table_id:
                self._want_seat.discard(table_id)
                self._seated.add(table_id)
        if routed.command == "game.seatInfo" and table_id and self.policy.enabled and table_id in self._want_seat:
            self._maybe_reserve(table_id, routed, occupied)

    def _maybe_reserve(self, table_id: int, routed: RoutedEvent, occupied: list[dict[str, Any]]) -> None:
        room = int(routed.room_id or 0)
        if room <= 0:
            return
        if any(item.command in {"game.reserve_Seat", "game.take_Seat"} and item.table_id == table_id for item in self._queue):
            return
        taken = set()
        for row in occupied:
            try:
                taken.add(int(row.get("seatId") or 0))
            except (TypeError, ValueError):
                continue
        size = 6
        config = self._pick_config()
        if config and config.table_size:
            size = int(config.table_size)
        free = [seat for seat in range(1, size + 1) if seat not in taken]
        if not free:
            return
        seat = free[0]
        self._enqueue(
            "game.reserve_Seat",
            room,
            build_game_reserve_seat_packet(room, seat),
            ws_kind="game",
            table_id=table_id,
        )
        self._note("automation.reserve", f"стол {table_id} место {seat}")

    def _ingest_catalog(self, routed: RoutedEvent) -> None:
        try:
            from ..verified_v1.coin_ppp_bridge import extract_all_json_after
        except Exception:
            return
        for props in extract_all_json_after(routed.decoded_body, b"GameRoomProperties"):
            if isinstance(props, dict):
                self._remember_config(props)

    def _remember_config(self, props: dict[str, Any], *, name: str = "") -> None:
        try:
            config_id = int(props.get("configId") or props.get("id") or 0)
        except (TypeError, ValueError):
            return
        if config_id <= 0:
            return
        bb = _finite(props.get("bigBlind") or props.get("bbAmount") or props.get("bigblind"))
        if bb is None:
            return
        try:
            mini = int(props.get("miniGameTypeId") or props.get("miniGameType") or 1)
        except (TypeError, ValueError):
            mini = 1
        min_buyin = _finite(props.get("minbuyin") or props.get("minBuyIn") or props.get("minBuyin")) or 0.0
        max_buyin = _finite(props.get("maxbuyin") or props.get("maxBuyIn") or props.get("maxBuyin")) or 0.0
        try:
            size = int(props.get("tableSize") or props.get("maxSize") or 6)
        except (TypeError, ValueError):
            size = 6
        self.configs[config_id] = RoomConfig(
            config_id=config_id,
            big_blind=float(bb),
            min_buyin=float(min_buyin),
            max_buyin=float(max_buyin),
            mini_game_type=mini,
            table_size=size,
            name=name or str(props.get("roomName") or ""),
        )

    def _enqueue(self, command: str, room_id: int, raw: bytes, *, ws_kind: str, table_id: int = 0) -> None:
        self._counter += 1
        self._queue.append(PendingCommand(
            command=command,
            room_id=int(room_id),
            raw=raw,
            token=f"auto:{command}:{self.device_id}:{self._counter}",
            due_at=time.monotonic(),
            ws_kind=ws_kind,
            table_id=int(table_id),
        ))

    def _inject(self, routed: RoutedEvent) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        ws = str(routed.websocket_id or "")
        for index, item in enumerate(self._queue):
            if item.due_at > now:
                continue
            if item.ws_kind == "lobby":
                if self.lobby_ws and ws and ws != self.lobby_ws:
                    continue
            elif item.ws_kind == "game":
                want = self.game_ws.get(item.table_id, "")
                if want and ws and ws != want:
                    continue
                if item.room_id and routed.room_id not in {None, 0, -1} and int(routed.room_id) != int(item.room_id):
                    continue
            self._queue.pop(index)
            decision = {
                "id": routed.event.get("id", ""),
                "action": "replace",
                "text": False,
                "payload_b64": base64.b64encode(item.raw).decode(),
                "token": item.token,
                "ws_id": ws,
                "_operator_action": {
                    "table_id": item.table_id,
                    "action": item.command.split(".")[-1].upper(),
                    "token": item.token,
                    "automation": True,
                },
            }
            try:
                decision["_ws_u32"] = int(ws, 16)
            except (TypeError, ValueError):
                pass
            return decision
        return None

    def mark_sitout(self, table_id: int, sitting_out: bool) -> None:
        table_id = int(table_id)
        if sitting_out:
            self._sitout.add(table_id)
        else:
            self._sitout.discard(table_id)

    def mark_stack(self, table_id: int, stack_bb: float, players: int, seated: bool) -> None:
        table_id = int(table_id)
        self._stack_bb[table_id] = float(stack_bb)
        if players:
            self._players[table_id] = int(players)
        if seated:
            if table_id not in self._seated:
                self._seated_at.setdefault(table_id, time.monotonic())
            self._seated.add(table_id)
            self._tabs.setdefault(table_id, 0.0)
        else:
            self._seated.discard(table_id)

    def mark_hand(self, table_id: int, live: bool) -> None:
        self._hand_live[int(table_id)] = bool(live)

    def mark_bb(self, table_id: int, bb: float) -> None:
        value = _finite(bb)
        if value:
            self._table_bb[int(table_id)] = float(value)

    def mark_wallet(self, cash: float) -> None:
        value = _finite(cash)
        if value is not None:
            self.wallet_cash = value

    def _note(self, event: str, text: str, *, severity: str = "INFO") -> None:
        try:
            self._sink(event, text, severity)
        except TypeError:
            try:
                self._sink(event)
            except Exception:
                pass
        except Exception:
            pass


class AutomationStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _policy_path()
        self._lock = threading.Lock()
        self._rows: dict[str, AutoPolicy] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, dict):
            return
        for key, value in devices.items():
            self._rows[str(key)] = AutoPolicy.from_mapping(value)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "devices": {key: policy.public() for key, policy in self._rows.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get(self, device_id: str) -> Optional[AutoPolicy]:
        with self._lock:
            return self._rows.get(str(device_id))

    def put(self, device_id: str, policy: AutoPolicy) -> None:
        with self._lock:
            self._rows[str(device_id)] = policy
            self._save()
