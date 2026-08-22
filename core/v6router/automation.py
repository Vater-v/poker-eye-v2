"""Per-device lobby automation: join N NLH tables, keep 79–100 BB, leave short tables."""
from __future__ import annotations

import base64
import json
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from ..verified_v1.coin_action_wire import (
    build_game_leave_seat_packet,
    build_game_quit_table_packet,
    build_game_reserve_seat_packet,
    build_game_sitout_packet,
    build_game_take_seat_packet,
    build_lobby_join_game_packet,
)
from .lobby_scene import (
    MIN_PLAYERS,
    MIN_TABLE_SIZE,
    LobbyPlan,
    UiDump,
    UiNodeRow,
    _is_hu_label,
    _ui_compact,
    classify_lobby_scene,
    parse_ui_dump,
    plan_lobby_join_tap,
)
from .persist import (
    ALLOWED_BB,
    COIN_MAX_TABLES,
    DEFAULT_BB,
    DEFAULT_TABLES,
    LEAVE_BELOW_BB,
    OPEN_IF_FREE_BB,
    AutoPolicy,
    AutomationStore,
    _closest_bb,
    _finite,
    _policy_path,
)
from .router import RoutedEvent, _decode_event, _forward
from .wallet_cash import apply_table_close, is_lobby_command, wallet_from_payload


def _wallet_from_payload(data: Any, *, source: str = "lobby") -> Optional[float]:
    return wallet_from_payload(data, source=source)


JOIN_GAP_MIN_SECONDS = 25.0
JOIN_GAP_MAX_SECONDS = 40.0
JOIN_HANDS_BEFORE_NEXT = 1
WATCHDOG_SECONDS = 2.0
# Real table that emptied: wait 20s (seat_map flicker / next deal).
# Junk tab that never got a roster/hand/cards: do not wait 20s.
EMPTY_IDLE_SECONDS = 20.0
JUNK_SECONDS = 3.0
STACK_GRACE_SECONDS = 20.0
TAB_COOLDOWN_SECONDS = 45.0
JOIN_REJECT_COOLDOWN = 30.0
LOBBY_STEP_SECONDS = 1.8


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
        self.lobby_wallet_cash: Optional[float] = None
        self._queue: list[PendingCommand] = []
        self._joining = False
        self._last_join_at = 0.0
        self._last_opened_table = 0
        self._gradual: list[tuple[int, float, str]] = []
        self._leave_reasons: dict[int, str] = {}
        self._closed_reasons: dict[int, str] = {}
        self._seated: set[int] = set()
        self._players: dict[int, int] = {}
        self._stack_bb: dict[int, float] = {}
        self._stack_positive: set[int] = set()
        self._sitout: set[int] = set()
        self._sitout_since: dict[int, float] = {}
        self._want_seat: set[int] = set()
        self._seated_at: dict[int, float] = {}
        self._hand_live: dict[int, bool] = {}
        self._hand_ended_at: dict[int, float] = {}
        self._hands_done: dict[int, int] = {}
        self._ui_players: dict[int, int] = {}
        self._ui_closed: set[int] = set()
        self._ui_loading: set[int] = set()
        self._ui_waitlist: set[int] = set()
        self._short_since: dict[int, float] = {}
        self._peak_players: dict[int, int] = {}
        self._table_bb: dict[int, float] = {}
        self._leave_inflight: set[int] = set()
        self._sit_in_at: dict[int, float] = {}
        self._tabs: dict[int, float] = {}
        self._join_block_until = 0.0
        self._next_join_gap = JOIN_GAP_MIN_SECONDS
        self._leaving_all = False
        self._status = "idle"
        self._counter = 0
        self._ui_last: dict[str, Any] = {}
        self._ui_join_armed = False
        self._last_ui_join_at = 0.0
        self._lobby_dump_key = ""
        self._ui_boot_since = 0.0
        self._table_size: dict[int, int] = {}
        if store is not None:
            saved = store.get(self.device_id)
            if saved is not None:
                self.policy = saved
            self._restore_runtime()

    def persist_runtime(self) -> None:
        if self._store is None:
            return
        self._store.put_runtime(self.device_id, {
            "seated_tables": sorted(int(tid) for tid in self._seated if int(tid) > 0),
            "play_enabled": bool(self.policy.play_enabled),
            "policy": self.policy.public(),
            "lobby_wallet_cash": self.lobby_wallet_cash,
            "wallet_cash": self.wallet_cash,
        })

    def _restore_runtime(self) -> None:
        if self._store is None:
            return
        blob = self._store.get_runtime(self.device_id) or {}
        if not isinstance(blob, dict):
            return
        # Never restore seated tables from disk. A leftover id (Vaterv 1166102)
        # looks like a live sit after everyone stood up and blocks deploy.
        play = blob.get("play_enabled")
        if play is not None:
            self.policy.play_enabled = bool(play)
        # Never restore wallet from disk. run_7e1d persisted leftover+stacks
        # (16.476/23.405) and the next sit wrote that lie as History St/End.

    def snapshot(self) -> dict[str, Any]:
        bbs = sorted({round(row.big_blind, 4) for row in self.configs.values() if row.mini_game_type == 1})
        return {
            "enabled": self.policy.enabled,
            "play_enabled": bool(self.policy.play_enabled),
            "policy": self.policy.public(),
            "status": self._status,
            "wallet_cash": self.wallet_cash,
            "wallet_bb": (
                None if self.wallet_cash is None or self.policy.bb <= 0
                else round(self.wallet_cash / self.policy.bb, 1)
            ),
            "catalog_bb": bbs or list(ALLOWED_BB),
            "joining": self._joining,
            "leaving_all": self._leaving_all,
            "gradual_leave": len(self._gradual),
            "sitout_tables": sorted(self._sitout),
            "coin_tabs": self.coin_tab_count(),
            "coin_tab_cap": COIN_MAX_TABLES,
            "ui": {
                key: value
                for key, value in dict(self._ui_last).items()
                if key != "rows"
            },
        }

    def apply_policy(self, raw: dict[str, Any], *, enable: Optional[bool] = None) -> AutoPolicy:
        policy = AutoPolicy.from_mapping({**self.policy.public(), **dict(raw or {})})
        if enable is not None:
            policy.enabled = bool(enable)
        self.policy = policy
        if not policy.enabled:
            self._joining = False
            self._want_seat.clear()
            self._queue = [
                item for item in self._queue
                if item.command not in {"lobby.join_game", "game.reserve_Seat", "game.take_Seat"}
            ]
            if not self._leaving_all:
                self._status = "paused"
        else:
            self._leaving_all = False
            self._status = "armed"
        if self._store is not None:
            self._store.put(self.device_id, policy)
            self.persist_runtime()
        self._note(
            "automation.policy",
            f"auto {'on' if policy.enabled else 'off'} · play {'on' if policy.play_enabled else 'off'} · {policy.table_count}×{policy.bb:g}",
            severity="INFO" if policy.enabled or policy.play_enabled else "WARN",
        )
        return policy

    def _halt_joins(self) -> bool:
        return (not self.policy.enabled) or self._leaving_all

    def schedule_leave_all(self, table_ids: list[int], *, gradual: bool) -> int:
        self._leaving_all = True
        self._joining = False
        self._want_seat.clear()
        self._queue = [
            item for item in self._queue
            if item.command not in {"lobby.join_game", "game.reserve_Seat", "game.take_Seat"}
        ]
        if self.policy.enabled:
            self.policy = AutoPolicy.from_mapping({**self.policy.public(), "enabled": False})
            if self._store is not None:
                self._store.put(self.device_id, self.policy)
        now = time.monotonic()
        delay = 0.0
        queued = 0
        seen: set[int] = set()
        for table_id in table_ids:
            table_id = int(table_id)
            if table_id <= 0 or table_id in seen:
                continue
            seen.add(table_id)
            if gradual:
                delay += random.uniform(120.0, 600.0)
            # Sequential even for "сразу": next table starts only after this
            # one has stood up and the Coin tab actually closed.
            self._gradual.append((table_id, now + delay, "operator leave-all"))
            queued += 1
        self._status = "leaving"
        self._note(
            "automation.leave_all",
            f"покидаем все столы · по одному · {'пауза 2–10 мин' if gradual else 'сразу после закрытия'} · {queued}",
            severity="WARN",
        )
        return queued

    def note_table_closed(self, table_id: int, reason: str = "") -> None:
        table_id = int(table_id)
        why = str(self._leave_reasons.pop(table_id, "") or reason or "closed")
        self._closed_reasons[table_id] = why
        stack_bb = self._stack_bb.pop(table_id, None)
        bb = self._table_bb.pop(table_id, self.policy.bb)
        self.wallet_cash = apply_table_close(
            wallet=self.wallet_cash, stack_bb=stack_bb, bb=bb,
        )
        self._seated.discard(table_id)
        self._sitout.discard(table_id)
        self._sitout_since.pop(table_id, None)
        self._players.pop(table_id, None)
        self._seated_at.pop(table_id, None)
        self._hand_live.pop(table_id, None)
        self._hand_ended_at.pop(table_id, None)
        self._hands_done.pop(table_id, None)
        self._ui_players.pop(table_id, None)
        self._ui_closed.discard(table_id)
        self._ui_loading.discard(table_id)
        self._ui_waitlist.discard(table_id)
        self._short_since.pop(table_id, None)
        self._peak_players.pop(table_id, None)
        self._stack_positive.discard(table_id)
        self._leave_inflight.discard(table_id)
        self._want_seat.discard(table_id)
        self._table_size.pop(table_id, None)
        self._tabs[table_id] = time.monotonic() + TAB_COOLDOWN_SECONDS
        self._gradual = [row for row in self._gradual if row[0] != table_id]
        self._queue = [item for item in self._queue if item.table_id != table_id]
        self.persist_runtime()
        if self._leaving_all:
            self._status = "leaving"
        elif self.coin_tab_count() >= min(int(self.policy.table_count), COIN_MAX_TABLES):
            self._status = f"cap {self.coin_tab_count()}/{COIN_MAX_TABLES}"
        elif self.policy.enabled:
            self._status = "refill"
        else:
            self._status = "paused"

    def _extend_ghost_tabs(self, extra_seconds: float) -> None:
        """Keep protocol-closed Coin tabs occupying the 5-slot cap.

        quit_table ACKs the trainer session but the RN tab stays until the APK
        janitor walks overflow → leave table → confirm. Joining during that
        window hits Maximum Tables Opened and the new table is invisible.
        """
        now = time.monotonic()
        hold = now + max(1.0, float(extra_seconds))
        for table_id, expiry in list(self._tabs.items()):
            current = float(expiry or 0.0)
            # Live tabs use expiry 0. Only stretch already-closed ghosts.
            if current > 0:
                self._tabs[int(table_id)] = max(current, hold)

    def drain_leaves(self) -> list[tuple[int, str]]:
        now = time.monotonic()
        if self._leaving_all:
            if self._leave_inflight:
                return []
            due = [row for row in self._gradual if row[1] <= now]
            if not due:
                return []
            table_id, _when, reason = due[0]
            table_id = int(table_id)
            self._gradual = [row for row in self._gradual if int(row[0]) != table_id]
            self._leave_inflight.add(table_id)
            self._leave_reasons[table_id] = reason
            return [(table_id, reason)]
        if self._leave_inflight:
            return []
        due = [row for row in self._gradual if row[1] <= now]
        self._gradual = [row for row in self._gradual if row[1] > now]
        for table_id, _when, reason in due:
            table_id = int(table_id)
            if table_id in self._leave_inflight:
                continue
            self._leave_inflight.add(table_id)
            return [(table_id, str(reason))]
        for table_id, reason in list(self._leave_reasons.items()):
            table_id = int(table_id)
            if table_id in self._leave_inflight:
                continue
            self._leave_inflight.add(table_id)
            return [(table_id, str(reason))]
        return []

    def coin_tab_count(self) -> int:
        return len(self._live_tabs())

    def coin_tab_ids(self) -> list[tuple[int, str]]:
        """Coin UI tabs the trainer still owes the operator, live or ghost."""
        rows: list[tuple[int, str]] = []
        for table_id, expiry in self._live_tabs().items():
            kind = "live" if float(expiry or 0.0) == 0.0 else "ghost"
            rows.append((int(table_id), kind))
        rows.sort(key=lambda item: item[0])
        return rows

    def _live_tabs(self) -> dict[int, float]:
        now = time.monotonic()
        keep: dict[int, float] = {}
        for table_id, expiry in self._tabs.items():
            if expiry == 0 or expiry > now:
                keep[int(table_id)] = float(expiry)
        self._tabs = keep
        return keep

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
        live = set(int(item) for item in (live_table_ids or self._seated))
        self._seated = {tid for tid in self._seated if tid in live}
        # Auto-off: operator sits and leaves. Auto-on + watch_players: leave a
        # ring that finished a hand below min_players. Sitout never leaves.
        if self.policy.enabled and self.policy.watch_players:
            self._watch_short_handed(live)

    def _watch_short_handed(self, live: set[int]) -> None:
        min_n = int(self.policy.min_players)
        for table_id in list(live):
            if bool(self._hand_live.get(table_id)):
                continue
            if table_id in self._leave_reasons or self._leaving_all:
                continue
            hands = int(self._hands_done.get(table_id) or 0)
            players = int(self._players.get(table_id) or 0)
            if hands > 0 and players > 0 and players < min_n:
                self._request_leave(table_id, f"пусто {players} игроков")

    def request_sit_in(self, table_id: int, *, room: int, ws_id: str = "") -> bool:
        """Queue captured game.sitout sitOutNextHand=false. Never leave_Seat."""
        table_id = int(table_id)
        if not bool(self.policy.play_enabled):
            return False
        if self._leaving_all or table_id in self._leave_reasons or table_id in self._leave_inflight:
            return False
        if table_id not in self._sitout:
            return False
        if table_id not in self._seated:
            return False
        try:
            room_id = int(room)
        except (TypeError, ValueError):
            room_id = 0
        if room_id <= 0:
            return False
        now = time.monotonic()
        last = float(self._sit_in_at.get(table_id) or 0.0)
        if now - last < 20.0:
            return False
        if any(item.table_id == table_id and item.command == "game.sitout" for item in self._queue):
            return False
        if ws_id:
            self.game_ws[table_id] = str(ws_id)
        self.room_of_table[table_id] = room_id
        self._sit_in_at[table_id] = now
        self._enqueue(
            "game.sitout",
            room_id,
            build_game_sitout_packet(room_id, sit_out_next_hand=False),
            ws_kind="game",
            table_id=table_id,
        )
        self._note("automation.sit_in", f"возвращаемся из ситаута · стол {table_id}")
        return True

    def _watch_tables(self, live: set[int], now: float) -> None:
        for table_id in list(live):
            players = int(self._players.get(table_id) or 0)
            in_hand = bool(self._hand_live.get(table_id))
            age = now - float(self._seated_at.get(table_id) or now)
            if in_hand:
                # Never stand up mid-hand. Do not clear a short-table leave
                # and do not reset idle — a 2-player ring used to play
                # forever because each deal wiped the 20s timer.
                continue
            if self.policy.watch_players:
                ui_n = self._ui_players.get(table_id)
                seat_n = int(players or 0)
                peak = max(int(self._peak_players.get(table_id) or 0), seat_n, int(ui_n or 0))
                self._peak_players[table_id] = peak
                min_n = int(self.policy.min_players)
                loading = table_id in self._ui_loading
                closed = table_id in self._ui_closed
                useful = peak >= 2 or int(self._hands_done.get(table_id) or 0) > 0
                if useful:
                    self.retract_false_leave(table_id)
                if not useful:
                    if closed or table_id in self._ui_waitlist:
                        self._request_leave(table_id, "uidump TABLE CLOSED")
                        continue
                    if loading:
                        self._short_since.pop(table_id, None)
                        continue
                    # Missing uidump/roster is normal while game_init lands.
                    # Never stand up on the first 0-player dump.
                    if age >= JUNK_SECONDS:
                        why = "нет данных" if ui_n is None else f"uidump {ui_n} игроков"
                        self._request_leave(table_id, why)
                    continue
                short = False
                if ui_n is not None:
                    short = int(ui_n) < min_n
                elif seat_n < min_n:
                    short = True
                if short:
                    hands = int(self._hands_done.get(table_id) or 0)
                    if hands > 0:
                        why = f"uidump {ui_n} игроков" if ui_n is not None else f"пусто {seat_n} игроков"
                        self._request_leave(table_id, why)
                    else:
                        started = float(self._short_since.get(table_id) or 0.0)
                        if started <= 0:
                            self._short_since[table_id] = now
                            started = now
                        if (now - started) >= EMPTY_IDLE_SECONDS:
                            why = f"uidump {ui_n} игроков" if ui_n is not None else f"пусто {seat_n} игроков"
                            self._request_leave(table_id, why)
                else:
                    self._short_since.pop(table_id, None)
            if self.policy.watch_balance:
                stack = self._stack_bb.get(table_id)
                if (
                    stack is not None
                    and stack < float(self.policy.leave_below_bb)
                    and age >= STACK_GRACE_SECONDS
                ):
                    if stack <= 0.05 and table_id not in self._stack_positive:
                        pass
                    else:
                        self._request_leave(
                            table_id,
                            f"стек {stack:.1f} BB < {self.policy.leave_below_bb:g}",
                        )
        for tid, expiry in list(self._live_tabs().items()):
            if float(expiry or 0.0) != 0.0 or tid in live or tid in self._leave_reasons:
                continue
            # Observing/pending tabs are not junk. Only a focused TABLE CLOSED
            # / waitlist dump may close a tab that has not seated yet.
            if tid in self._ui_closed or tid in self._ui_waitlist:
                self._request_leave(int(tid), "uidump TABLE CLOSED")

    def _newest_has_enough_hands(self) -> bool:
        waiting = [
            tid for tid in self._seated
            if int(self._hands_done.get(tid, 0)) < JOIN_HANDS_BEFORE_NEXT
            and tid not in self._leave_reasons
            and tid not in self._ui_closed
        ]
        if waiting:
            return False
        for tid, expiry in self._live_tabs().items():
            if float(expiry or 0.0) != 0.0:
                continue
            if tid in self._leave_reasons or tid in self._ui_closed:
                continue
            if tid not in self._seated:
                self._status = "wait sit"
                return False
        tid = int(self._last_opened_table or 0)
        if tid <= 0 or tid in self._seated or tid in self._closed_reasons or tid in self._ui_closed:
            return True
        expiry = float(self._tabs.get(tid) or 0.0)
        if expiry == 0.0:
            self._status = "wait sit"
            return False
        return True

    def _can_open(self, *, open_count: int = 0) -> bool:
        now = time.monotonic()
        if self._halt_joins():
            self._status = "leaving" if self._leaving_all else "paused"
            return False
        if self._joining:
            if now - float(self._last_join_at or 0.0) < 10.0:
                return False
            self._joining = False
            self._queue = [item for item in self._queue if item.command != "lobby.join_game"]
            self._note("automation.join_timeout", "join_game не открыл стол · открываем из лобби")
        if self.coin_tab_count() == 0 and not self._seated:
            self._status = "ui-join"
            return False
        if now < float(self._join_block_until or 0.0):
            self._status = "join-cooldown"
            return False
        if now - self._last_join_at < float(self._next_join_gap or JOIN_GAP_MIN_SECONDS):
            return False
        if not self._newest_has_enough_hands():
            if self._status != "wait sit":
                self._status = "wait 1 hand"
            return False
        if open_count >= min(int(self.policy.table_count), COIN_MAX_TABLES):
            self._status = f"cap {open_count}/{COIN_MAX_TABLES}"
            return False
        if self.coin_tab_count() >= COIN_MAX_TABLES:
            self._status = "coin 5 tabs"
            return False
        if not self._pick_config():
            self._status = "ui-join"
            return False
        if not self.lobby_ws:
            self._status = "ui-join" if self.needs_ui_join() else "waiting-lobby"
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

    def _open_table_count(self) -> int:
        return max(len(self._seated), self.coin_tab_count())

    def _want_more_tables(self) -> bool:
        if self._halt_joins():
            return False
        return self._open_table_count() < min(int(self.policy.table_count), COIN_MAX_TABLES)

    def needs_ui_join(self) -> bool:
        return False

    def lobby_join_command(self) -> Optional[dict[str, Any]]:
        return None


    def _buyin(self, config: RoomConfig) -> float:
        want = float(self.policy.open_if_free_bb) * float(config.big_blind)
        amount = min(float(config.max_buyin or want), want)
        if config.min_buyin:
            amount = max(float(config.min_buyin), amount)
        return amount

    def _queue_join(self) -> None:
        return

        if self._halt_joins():
            return
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
        self._next_join_gap = random.uniform(JOIN_GAP_MIN_SECONDS, JOIN_GAP_MAX_SECONDS)
        self._status = f"join {config.big_blind:g}"
        self._note(
            "automation.join",
            f"ищем стол NLH {config.big_blind:g} · buyin {buyin:g} · пауза {self._next_join_gap:.0f}с",
        )

    def _request_leave(self, table_id: int, reason: str) -> None:
        table_id = int(table_id)
        if table_id in self._leave_reasons:
            return
        self._leave_reasons[table_id] = reason
        self._want_seat.discard(table_id)
        self._note("automation.leave", f"стол {table_id}: {reason}")

    def retract_false_leave(self, table_id: int) -> Optional[str]:
        """Drop a junk/uidump standup if the table is actually playable."""
        table_id = int(table_id)
        why = str(self._leave_reasons.get(table_id) or "")
        if not why:
            return None
        if not (
            why.startswith("нет данных")
            or why.startswith("uidump")
            or why.startswith("пусто")
        ):
            return None
        peak = max(
            int(self._peak_players.get(table_id) or 0),
            int(self._players.get(table_id) or 0),
            int(self._ui_players.get(table_id) or 0),
        )
        if peak < int(self.policy.min_players):
            return None
        self._leave_reasons.pop(table_id, None)
        self._leave_inflight.discard(table_id)
        self._note("automation.leave_abort", f"стол {table_id}: отмена стендапа · стол живой")
        return why

    def _observe(self, routed: RoutedEvent) -> None:
        if routed.is_dummy:
            url = str(routed.event.get("url") or "")
            ws = str(routed.websocket_id or "")
            if ws and ("mainlobby" in url.lower() or "lobby" in url.lower()):
                self.lobby_ws = ws
                self.lobby_url = url
            if ws and routed.room_id:
                table_id = self.table_of_room.get(int(routed.room_id), 0)
                if table_id:
                    self.game_ws[table_id] = ws
            return
        data = routed.data if isinstance(routed.data, dict) else {}
        self._ingest_catalog(routed)
        if routed.direction == "in" and is_lobby_command(routed.command):
            self._capture_start_wallet(data, source="lobby")
        if routed.command == "lobby.join_game" and routed.direction == "in":
            failed = data.get("isSuccess") is False or int(data.get("errorCode") or 0) != 0
            if failed:
                self._joining = False
                self._join_block_until = time.monotonic() + JOIN_REJECT_COOLDOWN
                self._extend_ghost_tabs(JOIN_REJECT_COOLDOWN)
                self._status = "join-rejected"
                self._note("automation.join_rejected", "Coin отклонил поиск стола · пауза", severity="WARN")
        if routed.command == "lobby.join_game_table" and routed.direction == "in":
            self._joining = False
            self._capture_start_wallet(data, source="lobby")
            for tid in routed.table_ids:
                self._tabs[int(tid)] = 0.0
                self._last_opened_table = int(tid)
            for row in data.get("tablesToJoin") or ():
                if not isinstance(row, dict):
                    continue
                props = row.get("roomProperties") if isinstance(row.get("roomProperties"), dict) else {}
                self._remember_config(props, name=str(row.get("tableName") or ""))
                self._capture_start_wallet(row, source="table")
                self._capture_start_wallet(props, source="table")
        if routed.command in {"game.game_init", "game.wait_list_data"} and routed.table_ids:
            table_id = int(routed.table_ids[0])
            if routed.room_id:
                self.room_of_table[table_id] = int(routed.room_id)
                self.table_of_room[int(routed.room_id)] = table_id
            if routed.websocket_id:
                self.game_ws[table_id] = str(routed.websocket_id)
            self._capture_start_wallet(data, source="table")
            if routed.command == "game.game_init":
                size = 0
                init = data.get("gameInitResponseData") if isinstance(data.get("gameInitResponseData"), dict) else data
                self._capture_start_wallet(init, source="table")
                try:
                    size = int((init or {}).get("maxSize") or (init or {}).get("tableSize") or 0)
                except (TypeError, ValueError):
                    size = 0
                if size <= 0:
                    cfg = self._pick_config()
                    size = int(cfg.table_size) if cfg and cfg.table_size else 0
                if size:
                    self._table_size[table_id] = size
                hu_name = str((init or {}).get("tableName") or (init or {}).get("roomName") or "")
                if (size and size < max(3, int(self.policy.min_players))) or _is_hu_label(_ui_compact(hu_name)):
                    self._want_seat.discard(table_id)
                    self._request_leave(table_id, f"стол {size or 'HU'}-max")
                elif (
                    self.policy.enabled
                    and not self._leaving_all
                    and table_id not in self._leave_reasons
                    and table_id not in self._closed_reasons
                ):
                    self._want_seat.add(table_id)
        if routed.command == "game.reserve_Seat" and routed.direction == "in":
            self._capture_start_wallet(data, source="table")
            if data.get("isReserved") is True or int(data.get("errorCode") or 0) == 0:
                table_id = int(self.table_of_room.get(int(routed.room_id or 0), 0) or (routed.table_ids[0] if routed.table_ids else 0))
                seat = int(data.get("seatId") or 0)
                buyin = _finite(data.get("maxBuyin") or data.get("maxbuyin"))
                if table_id and seat and routed.room_id:
                    if (
                        self._leaving_all
                        or table_id in self._leave_reasons
                        or table_id in self._closed_reasons
                    ):
                        return
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
        if self._leaving_all or table_id in self._leave_reasons or table_id in self._closed_reasons:
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
        blobs: list[bytes] = []
        if routed.decoded_body:
            blobs.append(bytes(routed.decoded_body))
        raw = routed.raw if isinstance(routed.raw, (bytes, bytearray)) else b""
        if raw:
            raw_b = bytes(raw)
            if raw_b not in blobs:
                blobs.append(raw_b)
        for blob in blobs:
            if b"GameRoomProperties" not in blob:
                continue
            for props in extract_all_json_after(blob, b"GameRoomProperties"):
                if isinstance(props, dict):
                    self._remember_config(props)
        data = routed.data if isinstance(routed.data, dict) else {}
        for key in ("roomProperties", "GameRoomProperties"):
            props = data.get(key)
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
        cfg = RoomConfig(
            config_id=config_id,
            big_blind=float(bb),
            min_buyin=float(min_buyin),
            max_buyin=float(max_buyin),
            mini_game_type=mini,
            table_size=size,
            name=name or str(props.get("roomName") or ""),
        )
        self.configs[config_id] = cfg

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
        if self._halt_joins():
            self._queue = [
                item for item in self._queue
                if item.command not in {"lobby.join_game", "game.reserve_Seat", "game.take_Seat"}
            ]
            self._joining = False
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
            self._sitout_since.setdefault(table_id, time.monotonic())
        else:
            self._sitout.discard(table_id)
            self._sitout_since.pop(table_id, None)

    def mark_stack(
        self,
        table_id: int,
        stack_bb: Optional[float],
        players: int,
        seated: bool,
        *,
        stack_known: bool = True,
    ) -> None:
        table_id = int(table_id)
        if stack_known and stack_bb is not None:
            value = float(stack_bb)
            self._stack_bb[table_id] = value
            if value > 0.05:
                self._stack_positive.add(table_id)
        if players:
            self._players[table_id] = int(players)
            self._peak_players[table_id] = max(int(self._peak_players.get(table_id) or 0), int(players))
        if seated:
            if table_id not in self._seated:
                self._seated_at.setdefault(table_id, time.monotonic())
            self._seated.add(table_id)
            self._tabs.setdefault(table_id, 0.0)
            self.persist_runtime()
        else:
            self._seated.discard(table_id)
            self.persist_runtime()

    def mark_hand(self, table_id: int, live: bool) -> None:
        table_id = int(table_id)
        was = bool(self._hand_live.get(table_id))
        self._hand_live[table_id] = bool(live)
        if was and not live:
            self._hand_ended_at[table_id] = time.monotonic()
            self._hands_done[table_id] = int(self._hands_done.get(table_id, 0)) + 1
        elif live:
            self._hand_ended_at.pop(table_id, None)

    def ingest_ui_dump(self, xml: str, *, table_id: int = 0) -> dict[str, Any]:
        stats = parse_ui_dump(xml)
        self._ui_last = {
            "closed": bool(stats.get("closed")),
            "waitlist": bool(stats.get("waitlist")),
            "loading": bool(stats.get("loading")),
            "players": stats.get("players"),
            "timer": stats.get("shortest_timer"),
            "janitor": bool(stats.get("janitor")),
            "tap": str(stats.get("tap") or ""),
            "tap_age_ms": stats.get("tap_age_ms"),
            "nodes": int(stats.get("nodes") or 0),
            "ts": time.time(),
            "rows": list(stats.get("rows") or []),
            "live": bool(stats.get("live")),
        }
        tid = int(table_id or 0)
        # Unscoped dumps are the focused Coin window, not last_opened.
        # Mutating per-table maps from them closed/cleared the wrong tab.
        if tid <= 0:
            return stats
        seated_here = tid in self._seated
        live_here = bool(self._hand_live.get(tid))
        peak = int(self._peak_players.get(tid) or 0)
        hands = int(self._hands_done.get(tid) or 0)
        live_roster = live_here or peak >= 2 or hands > 0
        # Focused Coin window. Leftover TABLE CLOSED / waitlist / loading
        # must not be applied to a table that already played or has a roster.
        if stats.get("closed") or stats.get("waitlist"):
            if seated_here and live_roster:
                return stats
            self._ui_closed.add(tid)
            if stats.get("waitlist"):
                self._ui_waitlist.add(tid)
            self._ui_players[tid] = 0
            self._ui_loading.discard(tid)
            return stats
        self._ui_waitlist.discard(tid)
        self._ui_closed.discard(tid)
        if stats.get("loading"):
            self._ui_loading.add(tid)
            return stats
        self._ui_loading.discard(tid)
        if stats.get("players") is not None:
            counted = int(stats["players"])
            if live_roster and counted < 2:
                return stats
            self._ui_players[tid] = counted
            self._peak_players[tid] = max(peak, counted)
        return stats

    def mark_bb(self, table_id: int, bb: float) -> None:
        value = _finite(bb)
        if value:
            self._table_bb[int(table_id)] = float(value)

    def mark_wallet(self, cash: float) -> None:
        self._capture_start_wallet({"balance": cash}, source="lobby")

    def _capture_start_wallet(self, data: Any, *, source: str = "lobby") -> None:
        """Freeze Coin lobby cash from lobby traffic. Never a table stack."""
        value = _wallet_from_payload(data, source=source)
        if value is None:
            return
        previous = self.wallet_cash
        self.wallet_cash = value
        first = self.lobby_wallet_cash is None
        if first:
            self.lobby_wallet_cash = value
            self._note(
                "automation.lobby_wallet",
                f"стартовый баланс лобби {value:g}",
                wallet_cash=value,
                wallet_source="lobby",
            )
            self.persist_runtime()
            return
        if source == "lobby" and (
            previous is None or abs(float(previous) - float(value)) >= 0.005
        ):
            self._note(
                "automation.lobby_wallet",
                f"баланс лобби {value:g}",
                wallet_cash=value,
                wallet_source="lobby",
            )

    def _note(self, event: str, text: str, *, severity: str = "INFO", **extra) -> None:
        try:
            self._sink(event, text, severity, extra)
        except TypeError:
            try:
                self._sink(event, text, severity)
            except TypeError:
                try:
                    self._sink(event)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass


