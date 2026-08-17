"""Full v2 trainer process: broadcast + TCP + per-table state + operator status.

Single stdlib process, no GUI/web/admin/Telegram. Ties together UDP broadcast
discovery, authenticated TCP (one connection per table), Coin SFS2X decode,
per-table state tracking, EYE channel (hint/cc), action scheduling with the
hook PULL model (every ws_message exchange is answered immediately; a due
action is attached to the response as ``schedule_send``), retry, ledger,
bounded PCAP ring and hierarchical logging.

Operator view: short status lines on stdout; evidence: JSONL under
``logs/run_<run_id>/``.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .actions import Action, ActionScheduler, ActionStatus, HumanDelay
from .coin_wire import (
    ACTION_CHECK, ACTION_CALL, ACTION_FOLD, ACTION_RAISE,
    build_game_user_action_packet, decode_packet,
)
from .discovery import Broadcaster, SlotPool
from .events import parse_cc_data
from .eye_backend import EyeChannelClient, EyeFrame
from .eye_frames import action_notify_brc, eye_envelope, round_hint_multiple_table
from .ledger import Ledger, LedgerStatus
from .logging import SessionLogger
from .pcap_ring import PcapRingManager, PcapRingPolicy
from .policy import decide_cc_action
from .sessions import SessionRegistry
from .state import TableState, turn_identity
from .transport import TrainerServer

COIN_CODES = {"CHECK": ACTION_CHECK, "CALL": ACTION_CALL,
              "FOLD": ACTION_FOLD, "RAISE": ACTION_RAISE, "ALLIN": ACTION_RAISE}


class Trainer:
    def __init__(
        self,
        *,
        secret: str,
        host: str = "0.0.0.0",
        tcp_port: int = 0,
        broadcast_port: int = 37020,
        slots: int = 16,
        interval: float = 1.25,
        session_id: str = "trainer",
        log_dir: str | Path = "logs",
        eye_host: Optional[str] = None,
        eye_port: Optional[int] = None,
        eye_package: str = "com.lein.pppoker.android",
        game_port: int = 17770,
        chip_scale: int = 100,
        ack_timeout_s: float = 2.5,
    ) -> None:
        self.secret = secret.encode("utf-8")
        self.session_id = session_id
        self.chip_scale = chip_scale
        self.ack_timeout_s = ack_timeout_s

        self.logger = SessionLogger(log_dir)
        self.slot_pool = SlotPool(slots)

        self.broadcaster = Broadcaster(host, 0, self.secret, slots, interval,
                                       broadcast_port, session_id)
        self.broadcaster.advertised_nonce = secrets.token_hex(16)
        self.advertised_nonce = self.broadcaster.advertised_nonce

        self.server = TrainerServer(
            self.secret, session_id, self.advertised_nonce, self.slot_pool,
            host, tcp_port,
            on_connect=self._on_device_connect,
            on_event=self._on_transport_event,
            ws_handler=self._ws_handler,
        )

        self.tables: Dict[str, TableState] = {}
        self._tables_lock = threading.Lock()
        self.pending: Dict[str, Action] = {}          # table_id -> action awaiting send
        self.pending_due: Dict[str, float] = {}       # table_id -> due monotonic
        self._pending_lock = threading.Lock()
        self.scheduler = ActionScheduler()
        self._acked: set = set()                      # correlation ids confirmed
        self._acked_lock = threading.Lock()

        self.ledger = Ledger(Path(log_dir) / f"run_{self.logger.run_id}" / "ledger.jsonl")
        self.pcap = PcapRingManager(
            Path(log_dir) / f"run_{self.logger.run_id}" / "pcap",
            PcapRingPolicy(game_port=game_port))

        self.eye_client: Optional[EyeChannelClient] = None
        self.eye_host, self.eye_port = eye_host, eye_port
        if eye_host and eye_port:
            self.eye_client = EyeChannelClient(
                eye_host, eye_port, package_name=eye_package,
                on_frame=self._on_eye_frame, on_state=self._on_eye_state)
            self.eye_client.start()

        self._stop = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self) -> None:
        port = self.server.start()
        self.broadcaster.tcp_port = port
        threading.Thread(target=self.broadcaster.run, daemon=True).start()
        self.logger.emit("trainer.ready", flush=True, tcp_port=port,
                         broadcast_port=self.broadcaster.broadcast_port,
                         slots=self.slot_pool.metadata()["available"])
        print(f"[+] Trainer v2 ready  tcp={port} udp={self.broadcaster.broadcast_port} "
              f"slots={self.slot_pool.metadata()['available']}", flush=True)
        print(f"[+] run={self.logger.run_id} dir={self.logger.directory}", flush=True)
        if self.eye_client:
            print(f"[+] EYE target {self.eye_host}:{self.eye_port}", flush=True)

    def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        self.broadcaster.stop()
        self.server.stop()
        if self.eye_client:
            self.eye_client.stop()
        self.pcap.close_all()
        self.logger.close()

    # ── transport callbacks ───────────────────────────────────────────
    def _on_device_connect(self, table_id, slot, conn, address):
        print(f"[+] table connected {table_id} slot={slot} peer={address[0]}:{address[1]}", flush=True)
        self.logger.emit("transport.connected", table_id=table_id, slot=slot,
                         peer=str(address), flush=True)

    def _on_transport_event(self, event, **fields):
        sev = fields.pop("severity", "INFO")
        self.logger.emit(f"transport.{event}", severity=sev, **fields)

    def _table(self, table_id: str) -> TableState:
        with self._tables_lock:
            ts = self.tables.get(table_id)
            if ts is None:
                ts = TableState("device", table_id)
                self.tables[table_id] = ts
            return ts

    # ── ws_message handler (runs in the connection thread; must be fast) ──
    def _ws_handler(self, table_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        mid = message.get("id", "")
        text = bool(message.get("text", False))
        direction = str(message.get("direction", ""))
        url = str(message.get("url", ""))
        payload_b64 = str(message.get("payload_b64", ""))

        raw: Optional[bytes] = None
        decoded: Optional[Dict[str, Any]] = None
        if payload_b64 and not text:
            try:
                raw = base64.b64decode(payload_b64)
                decoded = decode_packet(raw)
            except Exception:
                raw = None
                decoded = None

        # PCAP ring: capture raw game traffic bytes (bounded, BPF-filtered policy)
        if raw:
            try:
                self.pcap.ring(table_id).write_packet(time.time(), raw)
            except Exception:
                pass

        state = self._table(table_id)

        if decoded:
            self._observe_frame(table_id, state, decoded, direction, url)

        # PULL: answer a due pending action with schedule_send.
        due = self._take_due_action(table_id)
        if due is not None and raw is not None:
            action = due
            room = self._room_from_url(url)
            packet = build_game_user_action_packet(room, COIN_CODES.get(action.command, ACTION_CHECK),
                                                   action.amount)
            self._ack_deadline_thread(table_id, action)
            self.logger.emit("action.attempt_sent", table_id=table_id, flush=True,
                             command=action.command, attempt=action.attempt,
                             correlation_id=action.correlation_id,
                             message=f"attempt {action.attempt}/3 {action.command}")
            print(f"[!] {action.command} -> {table_id} attempt {action.attempt}/3 "
                  f"cc={action.correlation_id[:8]}", flush=True)
            return {"id": mid, "action": "schedule_send",
                    "payload_b64": base64.b64encode(packet).decode(),
                    "delay_ms": 0, "token": action.correlation_id}

        return {"id": mid, "action": "forward"}

    # ── inbound/outbound observation ──────────────────────────────────
    def _observe_frame(self, table_id: str, state: TableState,
                       decoded: Dict[str, Any], direction: str, url: str) -> None:
        p = decoded.get("p")
        if not isinstance(p, dict):
            return
        cmd = str(p.get("c", ""))
        body = p.get("p")
        data: Dict[str, Any] = {}
        if isinstance(body, dict):
            ds = body.get("data", body)
            if isinstance(ds, str):
                try:
                    parsed = json.loads(ds)
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    pass
            elif isinstance(ds, dict):
                data = ds

        # ACK detection: server confirms the action with game.user_turn for
        # the next actor (or a stack update) after our synthetic send.
        if cmd == "game.user_turn" and direction == "in":
            self._mark_acked_if_pending(table_id, data)

        if cmd == "game.user_turn" and direction == "in":
            whose = str(data.get("whoseTurn") or data.get("userName") or "")
            hero = str(getattr(self, "hero_name", "") or "")
            if hero and whose == hero:
                state.user_turn_options = data.get("userTurnOptions") or {}
                state.source = "coin.turn"
                self._request_hint(table_id, state, data)

    def _mark_acked_if_pending(self, table_id: str, data: Dict[str, Any]) -> None:
        with self._pending_lock:
            action = self.pending.get(table_id)
        if action is None:
            return
        # Only a sent action can be confirmed: a turn frame that arrives
        # before the first schedule_send must never consume the action.
        if action.attempt < 1:
            return
        # A fresh turn means our action was accepted and the hand advanced.
        tid = turn_identity(data)
        self._confirm_action(table_id, action, f"turn-{tid}")

    def _confirm_action(self, table_id: str, action: Action, evidence: str) -> None:
        ok = self.scheduler.acknowledge(action.device_id, action.correlation_id, action.generation)
        if not ok:
            return
        with self._pending_lock:
            self.pending.pop(table_id, None)
            self.pending_due.pop(table_id, None)
        key = f"{table_id}:{action.generation}:{action.command}:{action.correlation_id}"
        self.ledger.finalize(key, LedgerStatus.SUCCESS, action=action.command,
                             amount=action.amount, extra={"evidence": evidence})
        self.logger.emit("action.ack", table_id=table_id, flush=True,
                         command=action.command, correlation_id=action.correlation_id,
                         message=f"{action.command} acknowledged ({evidence})")
        print(f"[+] {action.command} ACKED on {table_id} ({evidence})", flush=True)

    # ── action PULL scheduling ────────────────────────────────────────
    def _enqueue(self, table_id: str, action: Action, delay_ms: int) -> None:
        with self._pending_lock:
            self.pending[table_id] = action
            self.pending_due[table_id] = time.monotonic() + delay_ms / 1000.0

    def _take_due_action(self, table_id: str) -> Optional[Action]:
        with self._pending_lock:
            action = self.pending.get(table_id)
            due = self.pending_due.get(table_id, 0.0)
            if action is None or time.monotonic() < due:
                return None
            scheduled = self.scheduler.next_attempt(action.device_id)
            if scheduled is None:
                self.pending.pop(table_id, None)
                self.pending_due.pop(table_id, None)
                return None
            act, extra = scheduled
            # Retry gap: attempt 2 and 3 come +1 s after the previous send.
            self.pending_due[table_id] = time.monotonic() + (extra if extra else 1.0)
            return act

    def _ack_deadline_thread(self, table_id: str, action: Action) -> None:
        threading.Thread(target=self._ack_deadline, args=(table_id, action),
                         daemon=True).start()

    def _ack_deadline(self, table_id: str, action: Action) -> None:
        time.sleep(self.ack_timeout_s)
        if not self.scheduler.has_active(action.device_id):
            return
        with self._pending_lock:
            cur = self.pending.get(table_id)
            if cur is not action:
                return
            # If attempts remain, the next ws_message triggers the retry send.
            # If exhausted, finalize failed exactly once.
            if cur.attempt >= self.scheduler.MAX_ATTEMPTS:
                self.pending.pop(table_id, None)
                self.pending_due.pop(table_id, None)
                self.scheduler.finish_failed(action.device_id)
                key = f"{table_id}:{action.generation}:{action.command}:{action.correlation_id}"
                self.ledger.finalize(key, LedgerStatus.FAILED, action=action.command,
                                     amount=action.amount,
                                     extra={"reason": "3 attempts exhausted without ACK"})
                self.logger.emit("action.failed", table_id=table_id, severity="WARN", flush=True,
                                 command=action.command,
                                 message=f"{action.command} failed after 3 attempts")
                print(f"[-] {action.command} on {table_id} FAILED (3 attempts, no ACK)", flush=True)

    # ── EYE channel ────────────────────────────────────────────────────
    def _on_eye_frame(self, frame: EyeFrame) -> None:
        if frame.tag != "cc":
            return
        cc = parse_cc_data(frame.data)
        if not cc:
            return
        # Route the cc to the table with an outstanding turn (single-table first).
        table_id = self._cc_target_table()
        if table_id is None:
            self.logger.emit("hint.cc_unrouted", severity="WARN",
                             message="cc arrived with no active table")
            return
        self._on_cc(table_id, cc)

    def _cc_target_table(self) -> Optional[str]:
        with self._tables_lock:
            for tid, ts in self.tables.items():
                if ts.source == "coin.turn":
                    return tid
        with self._tables_lock:
            if self.tables:
                return next(iter(self.tables))
        return None

    def _on_cc(self, table_id: str, cc: Dict[str, Any]) -> None:
        state = self._table(table_id)
        decision = decide_cc_action(
            cc, table_state=state, hero_seat=1,
            user_turn_options=state.user_turn_options, chip_scale=self.chip_scale)
        if decision.action is None or decision.status in ("needs_operator", "error"):
            self.logger.emit("action.mapping_error", table_id=table_id, severity="WARN",
                             status=decision.status, reason=decision.reason, flush=True)
            return
        if decision.status == "state_gap_check":
            self.logger.emit("state.gap", table_id=table_id, severity="WARN", flush=True,
                             message=decision.reason)
        action = Action(
            device_id=f"device:{table_id}",
            table_id=table_id,
            generation=state.generation,
            command=decision.action.name,
            amount=decision.action.bet_amount,
        )
        if not self.scheduler.create(action):
            self.logger.emit("action.skipped", table_id=table_id, severity="WARN",
                             message="another CC already in flight")
            return
        delay_ms = HumanDelay.compute(
            int(cc.get("delay") or 0),
            turn_time_s=float(state.user_turn_options.get("turnTime") or 0.0),
            current_pot_bb=float(cc.get("pot_bb") or 0.0),
        )
        self._enqueue(table_id, action, delay_ms)
        self.logger.emit("action.created", table_id=table_id, flush=True,
                         command=action.command, delay_ms=delay_ms,
                         correlation_id=action.correlation_id,
                         message=f"scheduled {action.command} delay={delay_ms}ms")
        print(f"[*] cc -> {action.command} on {table_id} delay={delay_ms}ms "
              f"cc={action.correlation_id[:8]}", flush=True)
        state.source = "eye.cc"

    def _on_eye_state(self, event: str, fields: Dict[str, Any]) -> None:
        if event == "eye.connected":
            self.logger.emit("eye.connected", flush=True, **fields)
            print(f"[+] EYE connected gen={fields.get('generation')}", flush=True)
        elif event == "eye.disconnected":
            self.logger.emit("eye.disconnected", severity="WARN", **fields)
            print("[-] EYE disconnected (will reconnect)", flush=True)

    # ── hint request ───────────────────────────────────────────────────
    def _request_hint(self, table_id: str, state: TableState, turn: Dict[str, Any]) -> None:
        if self.eye_client is None or not self.eye_client.connected.is_set():
            return
        street = state.street_state
        call_need = street.call_need(1)
        min_r = street.min_raise_to(1)
        max_r = street.max_call_or_raise_to(1)
        can = sum(1 for s in street.seats.values() if not s.folded and not s.all_in)
        notify = eye_envelope("pb.ActionNotifyBRC",
                              action_notify_brc(1, call_need, min_r, max_r, max(1, can)))
        self.eye_client.send_outer(notify)
        hint = eye_envelope("pb.RoundHintMultipleTableRSP", round_hint_multiple_table(
            turn_time=int(turn.get("turnTime") or 15), room_name="Table"))
        self.eye_client.send_outer(hint)
        self.logger.emit("hint.requested", table_id=table_id, flush=True, call_need=call_need)
        print(f"[*] hint requested {table_id} call={call_need} min={min_r} max={max_r}", flush=True)

    # ── misc ───────────────────────────────────────────────────────────
    @staticmethod
    def _room_from_url(url: str) -> int:
        import re
        m = re.search(r"room[/=](\d+)", url or "")
        return int(m.group(1)) if m else 1
