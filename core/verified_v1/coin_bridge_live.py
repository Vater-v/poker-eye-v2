#!/usr/bin/env python3
from __future__ import annotations

import argparse, asyncio, base64, collections, json, os, re, struct, time
from typing import Any, Optional

from . import coin_ppp_bridge as core
from .coin_action_wire import build_game_user_action_packet, decode_packet
from .coin_autoplay import CoinAutoplayCoordinator
from .bombpot_support import BombpotTracker

ACTION = {"FOLD":1,"CHECK":2,"CALL":3,"RAISE":4,"BET":7,"SB":8,"BB":9,"ANTE":10,"FORCE_BB":11,"STRADDLE":14}
STAGE = {"PREFLOP":1,"FLOP":2,"TURN":3,"RIVER":4}
# A successful Coin quit ACK in the supplied multitable trace took 10.769s.
# Keep the logical PPP context alive for the real ACK; the ingress router prevents
# late table frames from reopening a closing session during this interval.
QUIT_ACK_TIMEOUT_SECONDS = 15.0
HAND_TYPE = {
    "HIGH CARD":1,"HIGHCARD":1,"PAIR":2,"ONE PAIR":2,"TWO PAIR":3,"TWO PAIRS":3,
    "THREE OF A KIND":4,"THREE KIND":4,"TRIPS":4,"STRAIGHT":5,"FLUSH":6,
    "FULL HOUSE":7,"FOUR OF A KIND":8,"FOUR KIND":8,"QUADS":8,
    "STRAIGHT FLUSH":9,"ROYAL FLUSH":10,
}


def log(tag: str, msg: str):
    if os.getenv("POKEREYE_BIZ_VERBOSE", "").strip() == "1":
        print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


def lp_pack(obj: dict) -> bytes:
    raw=json.dumps(obj,ensure_ascii=False,separators=(",",":")).encode()
    return struct.pack(">I",len(raw))+raw

async def lp_read(reader: asyncio.StreamReader) -> Optional[bytes]:
    try: h=await reader.readexactly(4)
    except (asyncio.IncompleteReadError,ConnectionError): return None
    n=struct.unpack(">I",h)[0]
    if not 0<n<=20_000_000: raise ValueError(f"bad frame {n}")
    return await reader.readexactly(n)


def decode_hook_payload(event: dict) -> tuple[Any,bytes]:
    raw_value=event.get("_raw")
    if isinstance(raw_value,(bytes,bytearray,memoryview)):
        raw=bytes(raw_value)
    else:
        try: raw=base64.b64decode(event.get("payload_b64","") or "")
        except Exception: raw=b""
    if event.get("text"):
        try: return json.loads(raw.decode("utf8")),raw
        except Exception: return raw.decode("utf8","replace"),raw
    try: return decode_packet(raw),raw
    except Exception: return None,raw


def cmd_room_data(payload: Any):
    if not isinstance(payload,dict): return "",None,{}
    p=payload.get("p") if isinstance(payload.get("p"),dict) else {}
    cmd=str(p.get("c") or ""); room=p.get("r")
    body=p.get("p") if isinstance(p.get("p"),dict) else {}
    data=body.get("data",body)
    if isinstance(data,str):
        try:data=json.loads(data)
        except Exception:pass
    return cmd, int(room) if isinstance(room,(int,float)) else None, data if isinstance(data,dict) else {}


_SEAT_ACTION_ALIASES={
    "AUTOBB":"FORCE_BB","AUTO_BB":"FORCE_BB","POST_BB":"FORCE_BB",
    "ALL_IN":"ALLIN","ALLIN":"ALLIN",
}
_SEAT_ACTIONS={
    "FOLD","CHECK","CALL","RAISE","BET","ALLIN",
    "SB","BB","ANTE","FORCE_BB","STRADDLE",
}

def normalized_coin_seat_action(data:dict[str,Any]) -> str:
    """Return the current action, never a stale ``lastAction`` refresh.

    The legacy PCAP contains rows such as ``newCaption=Inuse``,
    ``lastAction=Raise``, ``betAmout=0``.  Falling back to lastAction in that
    case invents a second villain raise.  Conversely, real postflop open bets
    appear as ``newCaption=Bet`` while lastAction remains ``Raise``.
    """
    def norm(value:Any) -> str:
        text=re.sub(r"[^A-Z0-9]+","_",str(value or "").upper()).strip("_")
        return _SEAT_ACTION_ALIASES.get(text,text)

    # newCaption/caption describe this mutation.  If either is explicitly a
    # non-action state (INUSE, PLAYING, ...), lastAction is historical and must
    # not be reused.
    saw_current=False
    for key in ("newCaption","caption"):
        raw=data.get(key)
        if raw not in (None,""):
            saw_current=True
            action=norm(raw)
            if action in _SEAT_ACTIONS:return action
    if saw_current:return ""

    action=norm(data.get("lastAction"))
    return action if action in _SEAT_ACTIONS else ""




def compute_net_profits(
    *,
    hand_participants: set[int] | list[int] | tuple[int, ...],
    hand_contrib: dict[int, int],
    raw_winner_net: dict[int, int],
    payout_by_seat: dict[int, int],
    forced_adjustment_by_seat: dict[int, int],
) -> dict[int, int]:
    """Return one net result per participating zero-based seat.

    Coin's atomic cumulative winner response can omit folded/non-winning seats,
    while winner fragments expose gross payouts.  This helper keeps the exact
    existing wire semantics but makes the accounting independently testable:
    gross payout minus committed chips for fragment-backed winners, Coin's net
    fallback with the one forced-blind adjustment removed, and negative committed
    chips for every remaining participant.
    """
    seats = {
        int(seat1) - 1
        for seat1 in hand_participants
        if int(seat1) > 0
    }
    seats.update(int(seat) for seat in hand_contrib)
    seats.update(int(seat) for seat in raw_winner_net)
    seats.update(int(seat) for seat in payout_by_seat)

    profits: dict[int, int] = {}
    for seat in seats:
        if seat in payout_by_seat:
            profits[seat] = int(payout_by_seat[seat]) - int(hand_contrib.get(seat, 0))
        elif seat in raw_winner_net:
            profits[seat] = int(raw_winner_net[seat]) - int(
                forced_adjustment_by_seat.get(seat, 0)
            )
        else:
            profits[seat] = -int(hand_contrib.get(seat, 0))
    return profits


def core_hook_empty():
    # Avoid importing HookResult here; all callers only access these four attributes.
    from .coin_autoplay import HookResult
    return HookResult()


class LiveCoinBridge:
    def __init__(self, eye_host="127.0.0.1", eye_port=17770, scale=100, frame_delay=0.004, broadcast=False, diagnostic_sink=None):
        self.eye_host=eye_host; self.eye_port=eye_port; self.base_scale=int(scale); self.scale=self.base_scale
        self.frame_delay=frame_delay; self.broadcast=broadcast
        self.diagnostic_sink=diagnostic_sink
        self.events=collections.deque(maxlen=6000)
        self.autoplay=CoinAutoplayCoordinator(self.base_scale); self.autoplay.hold_window=0.0
        self.money_profiles_by_table={}; self.active_money_profile=None; self.money_profile_table_id=0
        self.money_profile_log_keys=set()
        self.money_blocked_rooms=set()
        self.bombpots={}
        self.state={"user_name":"","user_id":0,"hero_seat":0,"hand_id":"","table_id":0,"_hook_room":None,"_pending_finish_hint":None}
        self.eye_r=None; self.eye_w=None; self.eye_reader_task=None; self.eye_lock=asyncio.Lock(); self.cc_queue=asyncio.Queue()
        self.eye_generation=0; self.eye_ready=asyncio.Event(); self.eye_resync_lock=asyncio.Lock()
        self.hint_lock=asyncio.Lock(); self.context_lock=asyncio.Lock(); self.context_active=False; self.context_table_id=0; self.context_hand=None
        self.login_sent=False; self.enter_request_table_id=0; self.leave_request_sent=False; self.stand_request_sent=False
        self.lifecycle_phase="offline"; self.hero_departing=False
        self.leaving_hook_room=None; self.leave_timeout_task=None; self.quit_ack_timeout=QUIT_ACK_TIMEOUT_SECONDS
        self.cold_hands=set(); self.current_hand=None
        self.finish_frame_by_table={}; self.active_seats=set(); self.all_in_seats=set(); self.street_contrib=collections.defaultdict(int)
        self.hand_contrib=collections.defaultdict(int); self.remaining_stack=collections.defaultdict(int)
        self.forced_adjustment_by_seat=collections.defaultdict(int)
        self.forced_raw_remaining_by_seat=collections.defaultdict(int)
        self.hand_participants=set(); self.session_profit=collections.defaultdict(int)
        self.action_seen=set(); self.notify_seen=set(); self.last_full_raise=0
        self.street_generation=0
        self.seat_map={}; self.announced_seats={}; self.hero_sitting=False; self.hero_total_buyin=0; self.pending_buyin_by_room={}; self.last_winner_info=[]
        self.deferred_seat_snapshot=None; self.chipsback_refresh_seats=set(); self.inject_claim_id=None
        self.pending_action_ack=None; self.settlement_round_over_sent=False; self.last_pools=[]
        self.round_over_seen=set(); self.round_zoom_seen=set(); self.pending_round_over=None; self.round_boundary_open=False; self.runout_closed=False; self.last_round_name=""; self.last_gross_pools=[]
        self.winner_fragments_seen=set(); self.winner_pot_gross={}; self.winner_rsp_sent=False
        self.show_hole_cards={}; self.show_hand_sent=False; self.is_doing_evchop=False
        self.wait_blind_cancelled=False
        self.rabbit_cards=[]; self.rabbit_second_board=[]; self.rabbit_finish_stage=None
        self.schedule_finish_tasks={}
        self.game_id_prefix_by_table={}
        self.room_session_timestamp_by_table={}
        # Persistent facts survive the bounded replay deque.  More importantly, the
        # SmartFox room id is the isolation boundary between simultaneously open Coin
        # table sockets; table events must never be merged merely because they arrived
        # close together in time.
        self.identity={"user_id":0,"user_name":""}
        self.room_to_table={}; self.table_to_room={}; self.table_to_config={}
        self.room_props_by_config={}; self.room_name_by_table={}
        self.active_hook_room=None; self.context_hook_room=None; self.requested_table_id=0; self.requested_config_id=0
        self.closing_rooms=set()
        self.mid_hand_wait_notified=set()
        self.protocol_queue=asyncio.Queue(); self.protocol_task=None
        self.emitted_primary_stages=set(); self.emitted_second_boards=set()
        self.pending_actor_seat=None
        self.manual_action_event=asyncio.Event(); self.event_count=0; self.inject_count=0; self.cc_count=0; self.heartbeat_count=0
        self.state_error_count=0; self.protocol_error_count=0
        self._bridge_error_occurrences=collections.Counter()
        # Operator policy: up to three actual sends. Coin confirmation gets a
        # one-second window between attempts; semantic turn dedupe prevents an
        # extraTimer refresh from spawning a second independent decision.
        self.action_retry_delay=max(0.5,float(os.getenv("POKER_ACTION_RETRY_DELAY_SECONDS","1.0")))
        self.action_max_attempts=max(1,int(os.getenv("POKER_ACTION_MAX_ATTEMPTS","3")))
        self.cc_timeout_seconds=max(2.0,float(os.getenv("POKER_CC_TIMEOUT_SECONDS","7.0")))
        self.cc_fallback_margin_seconds=max(1.0,float(os.getenv("POKER_CC_FALLBACK_MARGIN_SECONDS","2.0")))
        # Router/session replay and simultaneous WebSocket channels can deliver a
        # hero turn a few milliseconds before its seed/card frames are visible to
        # the bridge model.  Give the raw-event cache one bounded grace window.
        self.mid_hand_recovery_grace=max(0.0,float(os.getenv("POKER_MID_HAND_RECOVERY_GRACE_SECONDS","2.0")))
        self.mid_hand_recovery_attempts=max(1,min(8,int(os.getenv("POKER_MID_HAND_RECOVERY_ATTEMPTS","4"))))
        self.awaiting_cc=False
        self.last_eye_rx=time.monotonic(); self.last_hook_rx=time.monotonic()
        self._prefold_config=None
        self._prefold_config_loaded=False
        self.cc_miss_streak=0
        self._hand_cc_failed=False
        self._hero_turn_this_hand=False
        self.hero_sitting_out=False

    def _is_hero_turn(self, data: dict) -> bool:
        """Hero turn is the Coin actor matching login name or numeric uid."""
        if not isinstance(data, dict):
            return False
        hero_name=str(self.state.get("user_name") or self.identity.get("user_name") or "").strip()
        whose=str(data.get("whoseTurn") or data.get("userName") or "").strip()
        if hero_name and whose and whose.casefold()==hero_name.casefold():
            return True
        try:hero_id=int(self.state.get("user_id") or self.identity.get("user_id") or 0)
        except (TypeError,ValueError):hero_id=0
        try:whose_id=int(data.get("userId") or data.get("whoseTurnUserId") or 0)
        except (TypeError,ValueError):whose_id=0
        return bool(hero_id and whose_id and hero_id==whose_id)

    def _hand_in_progress(self) -> bool:
        """True while Coin still has us in the current hand.

        A standup click is queued by Coin until this hand ends. Eye must keep
        the hero seated and still answer CC until the real leave_Seat lands.
        """
        return bool(self.current_hand) or bool(str(self.state.get("hand_id") or "").strip())

    def _lifecycle_aborts_cc(self, cmd: str) -> bool:
        if cmd in ("game.quit_table", "game.reset_data", "lobby.join_game_table", "lobby.join_game"):
            return True
        if cmd in ("game.leave_Seat", "game.leave_seat"):
            return not self._hand_in_progress()
        return False

    def _should_announce_standup_to_eye(self) -> bool:
        return bool(self.hero_sitting) and not self._hand_in_progress()

    def _diagnostic(self, tag, message, detail=None):
        """Forward one safe bridge diagnostic to the owning table recorder."""
        sink=self.diagnostic_sink
        if sink is None:return
        clean=" ".join(str(message or "").replace("\r"," ").replace("\n"," ").split())[:1200]
        safe_detail=dict(detail or {}) if isinstance(detail,dict) else {}
        try:sink(str(tag or "bridge"),clean,safe_detail)
        except TypeError:
            try:sink(str(tag or "bridge"),clean)
            except Exception:pass
        except Exception:pass

    def _record_bridge_error(self, tag:str, command:str, exc:BaseException, *, room:Optional[int]=None):
        """Make ordered-protocol failures visible without flooding the console."""
        clean_command=str(command or "<unknown>")[:160]
        error_type=type(exc).__name__
        clean_error=" ".join(str(exc or "").replace("\r"," ").replace("\n"," ").split())[:800]
        key=(str(tag),clean_command,error_type,clean_error)
        self._bridge_error_occurrences[key]+=1
        count=int(self._bridge_error_occurrences[key])
        if tag=="state_error":self.state_error_count+=1
        else:self.protocol_error_count+=1
        # First occurrence is immediate; repeats are exponentially sampled.
        if count not in {1,2,5,10,25} and count%100!=0:
            return
        detail={
            "command":clean_command,
            "room":room,
            "error_type":error_type,
            "error":clean_error,
            "count":count,
            "state_errors":int(self.state_error_count),
            "protocol_errors":int(self.protocol_error_count),
        }
        if count in {1,10}:
            try:detail["telemetry"]=self._error_snapshot(reason=f"{tag}:{clean_command}")
            except Exception:pass
        self._diagnostic(tag,f"{clean_command}: {error_type}: {clean_error or '<no message>'}",detail)

    @staticmethod
    def _sanitize_error_value(value:Any, depth:int=0) -> Any:
        if depth>6:return "<depth-limit>"
        if isinstance(value,dict):
            out={}
            for key,item in value.items():
                name=str(key)
                lower=name.lower()
                if (
                    lower in {"sessionid","session_id","password","passwd","credential","credentials","secret","jwt","jwttoken","accesstoken","refreshtoken","authorization"}
                    or lower.endswith("_secret")
                    or lower.endswith("_password")
                    or ("token" in lower and lower not in {"turntoken","actiontoken"})
                ):
                    out[name]="<redacted>"
                else:
                    out[name]=LiveCoinBridge._sanitize_error_value(item,depth+1)
            return out
        if isinstance(value,(list,tuple)):
            return [LiveCoinBridge._sanitize_error_value(item,depth+1) for item in value[:128]]
        if isinstance(value,(bytes,bytearray,memoryview)):
            return f"<{len(value)} raw bytes; see PCAP>"
        if isinstance(value,str) and len(value)>1000:
            return value[:1000]+"…"
        return value

    def _error_snapshot(self, *, reason:str, limit:int=48) -> dict:
        """Sanitized high-signal state; raw frames live in the session PCAP."""
        room=self.context_hook_room or self.active_hook_room
        turn=dict(self.autoplay.turn_by_room.get(int(room),{}) if room is not None else {})
        turn.pop('_url',None)
        recent=[]
        for ev in list(self.events)[-max(1,int(limit)):]:
            try:
                payload=decode_packet(core.coin_payload_bytes(ev))
                cmd,r,data=cmd_room_data(payload)
                recent.append({
                    "id":str(ev.get("id") or ""),
                    "direction":str(ev.get("direction") or ""),
                    "ws_id":str(ev.get("ws_id") or ""),
                    "command":cmd,
                    "room":r,
                    "data":self._sanitize_error_value(data if isinstance(data,dict) else {}),
                })
            except Exception:
                recent.append({
                    "id":str(ev.get("id") or ""),
                    "direction":str(ev.get("direction") or ""),
                    "ws_id":str(ev.get("ws_id") or ""),
                    "command":"<raw>",
                })
        pending=dict(self.autoplay.pending or {})
        pending.pop('raw',None); pending.pop('url',None)
        ack=dict(self.pending_action_ack or {})
        ack.pop('raw',None); ack.pop('url',None)
        return {
            "reason":str(reason or ""),
            "table_id":int(self.context_table_id or self.state.get("table_id") or 0),
            "room":room,
            "hand_id":str(self.state.get("hand_id") or ""),
            "phase":str(self.lifecycle_phase),
            "hero_seat":int(self.state.get("hero_seat") or 0),
            "context_active":bool(self.context_active),
            "hero_sitting":bool(self.hero_sitting),
            "event_count":int(self.event_count),
            "cc_count":int(self.cc_count),
            "inject_count":int(self.inject_count),
            "state_error_count":int(self.state_error_count),
            "protocol_error_count":int(self.protocol_error_count),
            "hook_age_s":round(max(0.0,time.monotonic()-self.last_hook_rx),3),
            "eye_age_s":round(max(0.0,time.monotonic()-self.last_eye_rx),3),
            "turn":turn,
            "pending_action":pending,
            "pending_ack":ack,
            "recent_events":recent,
            "pcap":"see captures/<device>/coin_*.pcap in the same run directory",
        }

    def seed_router_context(self,events,*,requested_table_id=0,requested_config_id=0):
        """Import immutable lobby/identity facts into one table-isolated bridge.

        The multitable ingress router calls this before the session sees its first
        table event.  It intentionally does *not* run autoplay or protocol/lifecycle
        handlers: a lobby packet may seed many future sessions, but may never cause
        duplicated PPP traffic or an action side effect.
        """
        for event in events:
            try:
                payload,raw=decode_hook_payload(event); cmd,room,data=cmd_room_data(payload)
                if cmd=="lobby.dummy" or cmd.startswith("game."):continue
                self.events.append(event)
                self._remember_event(event,cmd,room,data)
            except Exception:
                continue
        self.requested_table_id=max(0,int(requested_table_id or 0))
        self.requested_config_id=max(0,int(requested_config_id or 0))

    async def ensure_eye(self):
        delay=.25
        while True:
            writer=self.eye_w
            if writer and not writer.is_closing():
                generation=self.eye_generation
            else:
                async with self.eye_lock:
                    writer=self.eye_w
                    if writer and not writer.is_closing():
                        generation=self.eye_generation
                    else:
                        try:
                            reader,writer=await asyncio.open_connection(self.eye_host,self.eye_port)
                            self.eye_generation+=1; generation=self.eye_generation
                            self.eye_r=reader; self.eye_w=writer; self.eye_ready.clear()
                            self.eye_reader_task=asyncio.create_task(self._eye_reader(reader,writer,generation))
                            log("EYE",f"connected {self.eye_host}:{self.eye_port} generation={generation}")
                        except Exception as e:
                            log("EYE",f"connect failed: {e}; retry {delay:.2f}s")
                            await asyncio.sleep(delay); delay=min(3.0,delay*1.7)
                            continue
            try:
                await self._ensure_eye_resynced(generation)
            except Exception as e:
                log("EYE",f"generation={generation} resync failed: {e}")
                await asyncio.sleep(delay); delay=min(3.0,delay*1.7)
                continue
            if (generation==self.eye_generation and self.eye_w is writer
                    and writer and not writer.is_closing() and self.eye_ready.is_set()):
                return

    def _reset_eye_wire_state(self):
        # Logical Coin/table/hand state survives. These flags describe only what the
        # current EYE TCP generation has already observed.
        self.login_sent=False; self.leave_request_sent=False; self.stand_request_sent=False
        self.eye_ready.clear()

    def _invalidate_eye_generation(self,generation:int,writer=None):
        if generation!=self.eye_generation:return
        if writer is not None and self.eye_w is not writer:return
        old=self.eye_w
        self.eye_r=None; self.eye_w=None; self._reset_eye_wire_state()
        try:
            if old and not old.is_closing():old.close()
        except Exception:pass

    async def _eye_reader(self,reader:asyncio.StreamReader,writer:asyncio.StreamWriter,generation:int):
        try:
            while True:
                raw=await lp_read(reader)
                if raw is None: break
                # A write failure can retire this generation before its reader sees
                # EOF.  Never accept a late cc/control frame from that stale socket.
                if generation!=self.eye_generation or self.eye_w is not writer:break
                self.last_eye_rx=time.monotonic()
                try:z=json.loads(raw)
                except Exception: continue
                tag=z.get("tag","?")
                if tag=="cc":
                    self.cc_count+=1
                    raw_data=z.get("data","")
                    try:parsed=json.loads(raw_data) if isinstance(raw_data,str) else (raw_data or {})
                    except Exception:parsed={}
                    action=str((parsed or {}).get("action") or (parsed or {}).get("message") or "").strip()
                    amount=(parsed or {}).get("amount",(parsed or {}).get("bet"))
                    if self.awaiting_cc:
                        await self.cc_queue.put(z)
                        self._diagnostic("cc_received","PokerEYE cc received",{
                            "action":action or None,
                            "amount":amount,
                        })
                    else:
                        self._diagnostic("late_cc_ignored","PokerEYE CC arrived without an active hint and was ignored")
                    log("EYE<<",f"cc action={action or '-'} amount={amount} {str(raw_data)[:240]}")
                elif tag in ("game_mode","browser_profile"):
                    log("EYE<<",f"{tag}={str(z.get('data',''))[:100]}")
                else: log("EYE<<",f"{tag}")
        except Exception as e: log("EYE",f"reader generation={generation} stopped: {e}")
        finally:
            try:
                if not writer.is_closing():writer.close()
            except Exception: pass
            self._invalidate_eye_generation(generation,writer)
            log("EYE",f"disconnected generation={generation}")

    def _eye_outer_for_cmd(self,cmd:str,body:bytes,location="TABLE",envelope_uid:Optional[int]=None):
        envelope_hand=self.current_hand or self.context_hand
        if envelope_uid is not None:
            uid=str(int(envelope_uid))
            m={"timestamp":int(time.time()*1000),"pid":uid,"uid":uid,"cmd":cmd,"direction":"ServerToClient","data":base64.b64encode(body).decode(),"location":location,"seq":0}
            return {"data":"","msg":json.dumps(m,separators=(",",":")),"packageName":core.PPP_PACKAGE,"tag":"traffic"}
        if envelope_hand:return core.PPPBuilder(envelope_hand,self.scale)._outer(cmd,body,location)
        uid=str(self.state.get("user_id") or 0)
        m={"timestamp":int(time.time()*1000),"pid":uid,"uid":uid,"cmd":cmd,"direction":"ServerToClient","data":base64.b64encode(body).decode(),"location":location,"seq":0}
        return {"data":"","msg":json.dumps(m,separators=(",",":")),"packageName":core.PPP_PACKAGE,"tag":"traffic"}

    async def _eye_send_outer_generation(self,o:dict,label:str,generation:int):
        raw=json.dumps(core.restamp_outer(o),ensure_ascii=False,separators=(",",":")).encode()
        writer=None
        try:
            async with self.eye_lock:
                writer=self.eye_w
                if (generation!=self.eye_generation or writer is None or writer.is_closing()):
                    raise ConnectionError("stale EYE generation")
                await core.send_lp(writer,raw)
        except Exception:
            self._invalidate_eye_generation(generation,writer)
            raise
        if label:log("EYE>>",label)

    async def _eye_send_cmd_generation(self,cmd:str,body:bytes,location:str,generation:int,*,envelope_uid:Optional[int]=None,label:Optional[str]=None):
        await self._eye_send_outer_generation(self._eye_outer_for_cmd(cmd,body,location,envelope_uid),label or cmd,generation)

    def _login_wire_bodies(self):
        uid=int(self.state.get("user_id") or 0); device_id=f"{uid:032x}"[-32:]
        request=b"".join([
            core.p_int(1,uid),core.p_str(2,device_id),core.p_str(3,core.PPP_CLIENT_VERSION),
            core.p_str(4,core.PPP_CLIENT_IP),core.p_int(6,0),core.p_str(7,core.PPP_CLIENT_PLATFORM),
            core.p_int(8,0),core.p_str(9,core.PPP_SERVER_ENDPOINT),core.p_str(10,core.PPP_CLIENT_COUNTRY),
        ])
        response=core.p_int(1,0)+core.p_str(3,core.PPP_SILENT_VERSION)+core.p_int(4,0)
        return request,response

    async def _replay_eye_wire_session(self,generation:int):
        uid=int(self.state.get("user_id") or 0)
        if not uid:return
        request,login=self._login_wire_bodies()
        await self._eye_send_cmd_generation("pb.UserLoginREQ",request,"OTHERS",generation,envelope_uid=0)
        await self._eye_send_cmd_generation("pb.UserLoginRSP",login,"OTHERS",generation)
        self.login_sent=True
        if self.lifecycle_phase=="offline":self.lifecycle_phase="lobby"

        if not self.context_active and self.lifecycle_phase=="pending":
            tid=int(self.enter_request_table_id or self.requested_table_id or 0)
            if tid:
                body=b"".join([core.p_int(1,core.ppp_table_id(tid)),core.p_int(2,-1),core.p_int(3,0),
                               core.p_int(4,core.PPP_CLUB_ID),core.p_int(7,0),core.p_int(8,0)])
                await self._eye_send_cmd_generation("pb.EnterRoomREQ",body,"OTHERS",generation)
                self.enter_request_table_id=tid
            return

        if not self.context_active:return
        h=self.context_hand or self._build_context_hand()
        if not h:return
        self._activate_money_profile(h,"reconnect")
        tid=int(self.context_table_id or h.table_id)
        enter=b"".join([core.p_int(1,core.ppp_table_id(tid)),core.p_int(2,-1),core.p_int(3,0),
                         core.p_int(4,core.PPP_CLUB_ID),core.p_int(7,0),core.p_int(8,0)])
        await self._eye_send_cmd_generation("pb.EnterRoomREQ",enter,"OTHERS",generation)
        builder=core.PPPBuilder(h,self.scale)
        await self._eye_send_outer_generation(builder._outer("pb.EnterRoomRSP",builder._enter_room_rsp(),"OTHERS"),"pb.EnterRoomRSP [resync]",generation)
        self.enter_request_table_id=0

        if self.hero_sitting:
            seat=int(self.state.get("hero_seat") or h.hero_seat or 0)
            row=self.seat_map.get(seat)
            if not row:
                row=next((r for r in h.roster if int(r.get("seatId") or 0)==seat),None)
            if row and seat:
                row=dict(row); row.setdefault("userId",h.hero_id); row.setdefault("userName",h.hero_name)
                chips=core.money(row.get("userChips",0),self.scale)
                sit_req=b"".join([core.p_int(1,seat-1),core.p_int(2,chips),core.p_str(3,core.PPP_CLIENT_IP),
                                   core.p_int(4,0),core.p_int(5,0),core.p_int(6,0),
                                   core.p_str(7,core.PPP_SIT_EMAIL),core.p_int(8,1)])
                await self._eye_send_cmd_generation("pb.SitDownREQ",sit_req,"TABLE",generation)
                await self._eye_send_cmd_generation("pb.SitDownBRC",builder._sitdown_brc(row,wait_blind=False,in_game=bool(self.current_hand)),"TABLE",generation)
                await self._eye_send_cmd_generation("pb.SitDownRSP",core.p_int(1,0)+core.p_int(3,seat-1)+core.p_int(4,chips)+core.p_int(5,10),"TABLE",generation)
                total=int(self.hero_total_buyin or chips)
                await self._eye_send_cmd_generation("pb.TotalBuyinBRC",core.p_int(1,int(row.get("userId") or h.hero_id))+core.p_int(2,total)+core.p_int(3,0),"TABLE",generation)

        if self.lifecycle_phase=="leaving":
            if self.hero_sitting and self.state.get("hero_seat") and not self._hand_in_progress():
                await self._eye_send_cmd_generation("pb.StandUpREQ",b"","TABLE",generation)
                self.stand_request_sent=True
            body=core.p_int(1,0)+core.p_int(2,0)+core.p_int(3,0)
            await self._eye_send_cmd_generation("pb.LeaveRoomREQ",body,"TABLE",generation)
            self.leave_request_sent=True
        elif self.hero_departing and self.hero_sitting and not self._hand_in_progress():
            await self._eye_send_cmd_generation("pb.StandUpREQ",b"","TABLE",generation)
            self.stand_request_sent=True

    async def _ensure_eye_resynced(self,generation:int):
        async with self.eye_resync_lock:
            if generation!=self.eye_generation:return
            if self.eye_ready.is_set():return
            await self._replay_eye_wire_session(generation)
            if generation==self.eye_generation and self.eye_w and not self.eye_w.is_closing():
                self.eye_ready.set()

    async def eye_send_outer(self,o:dict, label=""):
        await self.ensure_eye()
        await self._eye_send_outer_generation(o,label,self.eye_generation)

    async def eye_send_cmd(self,cmd:str,body:bytes,location="TABLE",envelope_uid:Optional[int]=None):
        await self.eye_send_outer(self._eye_outer_for_cmd(cmd,body,location,envelope_uid),cmd)

    async def _ensure_login_identity(self):
        if self.login_sent or not int(self.state.get("user_id") or 0):return
        while True:
            # ensure_eye may itself replay a known logical session on a fresh socket.
            # Recheck afterwards so the caller never emits a second login pair.
            await self.ensure_eye()
            if self.login_sent:return
            async with self.eye_resync_lock:
                if self.login_sent:return
                generation=self.eye_generation
                # The socket can predate Coin identity (bridge startup).  Temporarily
                # close its ready gate so no table frame can overtake the login pair.
                self.eye_ready.clear()
                request,login=self._login_wire_bodies()
                try:
                    # Official UserLoginREQ precedes identity assignment in the outer
                    # envelope.  Send both on one generation; never reconnect between
                    # the request and response and emit a lone duplicate response.
                    await self._eye_send_cmd_generation("pb.UserLoginREQ",request,"OTHERS",generation,envelope_uid=0)
                    await self._eye_send_cmd_generation("pb.UserLoginRSP",login,"OTHERS",generation)
                except Exception:
                    continue
                if (generation!=self.eye_generation or self.eye_w is None
                        or self.eye_w.is_closing()):
                    continue
                self.login_sent=True; self.eye_ready.set()
                if self.lifecycle_phase=="offline":self.lifecycle_phase="lobby"
                log("EYE",f"PPP identity online uid={self.state.get('user_id')}")
                return

    async def _send_enter_room_req(self,table_id:int):
        table_id=int(table_id or 0)
        if table_id<=0 or self.enter_request_table_id==table_id:return
        body=b"".join([core.p_int(1,core.ppp_table_id(table_id)),core.p_int(2,-1),core.p_int(3,0),
                       core.p_int(4,core.PPP_CLUB_ID),core.p_int(7,0),core.p_int(8,0)])
        await self.eye_send_cmd("pb.EnterRoomREQ",body,"OTHERS")
        self.enter_request_table_id=table_id; self.lifecycle_phase="pending"
        log("TABLE",f"enter pending coinTable={table_id} pppTable={core.ppp_table_id(table_id)}")

    async def _send_leave_room_req(self):
        if self.leave_request_sent:return
        await self.eye_send_cmd("pb.LeaveRoomREQ",core.p_int(1,0)+core.p_int(2,0)+core.p_int(3,0))
        self.leave_request_sent=True; self.lifecycle_phase="leaving"

    def _stamp_game_id_context(self,h):
        tid=int(h.table_id)
        prefix=self.game_id_prefix_by_table.get(tid)
        if not prefix:
            # Coin has no PPP room-creation timestamp. The first timestamp observed
            # while binding this SmartFox room is the closest truthful stand-in.
            stamp=self.room_session_timestamp_by_table.get(tid,h.timestamp_ms)
            sec=max(0,int(stamp)//1000)
            prefix=time.strftime("%y%m%d%H%M%S",time.gmtime(sec+core.PPP_GAME_ID_UTC_OFFSET_SECONDS))
            self.game_id_prefix_by_table[tid]=prefix
        h.room.props["_gameIdPrefix"]=prefix
        return h

    @staticmethod
    def _money_profile_signature(profile):
        return (profile.coin_small_blind,profile.coin_big_blind,profile.chip_scale)

    def _activate_money_profile(self,h,reason="hand"):
        """Pin one dynamic money profile to the selected Coin table.

        Hand models and SmartFox turn/action state remain in raw Coin currency.
        Only PPP wire/accounting uses ``self.scale``; autoplay uses the exact same
        scale in reverse when converting backend cc amounts back to Coin.
        """
        tid=int(h.table_id or 0)
        if tid<=0:raise ValueError("cannot activate money profile without table id")
        try:
            derived=core.money_profile_for_hand(h,self.base_scale)
        except core.UnrepresentableMoneyProfile as exc:
            room=self.table_to_room.get(tid,self.context_hook_room or self.active_hook_room)
            self.autoplay.clear_room(room); self.state["_pending_finish_hint"]=None
            self.enter_request_table_id=0; self.requested_table_id=0; self.requested_config_id=0
            key=(room,tid,str(exc))
            if key not in self.money_blocked_rooms:
                self.money_blocked_rooms.add(key)
                log("MONEY_UNREPRESENTABLE",f"table={tid} room={room} {exc}; EYE admission/autoplay disabled")
            raise
        self.money_blocked_rooms={key for key in self.money_blocked_rooms if key[1]!=tid}
        cached=self.money_profiles_by_table.get(tid)
        if not derived.valid_blinds:
            profile=cached or derived
        else:
            profile=derived
            if cached and self._money_profile_signature(cached)!=self._money_profile_signature(derived):
                pinned=(self.current_hand is not None and int(self.current_hand.table_id)==tid)
                if pinned:
                    raise RuntimeError(
                        f"Coin blinds changed during hand table={tid}: "
                        f"{cached.coin_small_blind}/{cached.coin_big_blind} -> "
                        f"{derived.coin_small_blind}/{derived.coin_big_blind}")
            self.money_profiles_by_table[tid]=profile

        self.active_money_profile=profile; self.money_profile_table_id=tid
        self.scale=profile.chip_scale; self.autoplay.chip_scale=profile.chip_scale
        self.state["_money_scale"]=profile.chip_scale
        self.state["_money_factor"]=profile.factor
        key=(tid,*self._money_profile_signature(profile))
        if key not in self.money_profile_log_keys:
            self.money_profile_log_keys.add(key)
            if profile.valid_blinds:
                dashboard_bb=float(profile.ppp_big_blind/profile.base_scale)
                inverse_bb=float(profile.ppp_big_blind/profile.chip_scale)
                coin_cash_bb=float(profile.coin_big_blind*core.COIN_CHIP_USDT*core.USD_RUB)
                target_cash_bb=float(profile.target_big_blind*core.TARGET_PPP_CHIP_RUB)
                log("MONEY",f"table={tid} reason={reason} Coin SB/BB={profile.coin_small_blind}/{profile.coin_big_blind} "
                    f"CoinCashBB~{coin_cash_bb:g}RUB -> targetBB={profile.target_big_blind} "
                    f"targetCashBB~{target_cash_bb:g}RUB PPP SB/BB={profile.ppp_small_blind}/{profile.ppp_big_blind} "
                    f"scale={profile.chip_scale:g} factor={profile.factor:.6g} dashboardBB={dashboard_bb:g} inverseBB={inverse_bb:g} "
                    f"CoinQuantum={profile.coin_quantum}")
            else:
                log("MONEY",f"table={tid} reason={reason} blinds unavailable; temporary base scale={profile.chip_scale}")
        return profile

    def _restore_active_money_profile(self):
        """Restore the selected table's scale before resolving a backend action."""
        tid=int(self.state.get("table_id") or self.context_table_id or 0)
        profile=self.money_profiles_by_table.get(tid)
        if profile is None:
            hand=self.current_hand or self.context_hand
            if hand and int(hand.table_id)==tid:
                profile=self._activate_money_profile(hand,"cc")
        if profile is None or not profile.valid_blinds:
            raise RuntimeError(f"no valid Coin money profile for active table={tid}")
        self.active_money_profile=profile; self.money_profile_table_id=tid
        self.scale=profile.chip_scale; self.autoplay.chip_scale=profile.chip_scale
        return profile

    def _quantize_pending_cc_amount(self,cc:dict,profile) -> str:
        """Map PPP's additional chip-in to one legal Coin total-bet target.

        SCAction.amount follows ActionBRC.Chips: it is the amount added by this
        action, not the player's resulting street total.  Coin's user_action wire,
        conversely, wants that resulting raw total.  Use the live PPP contribution
        ledger as the bridge between those two semantics; this also absorbs an
        outstanding explicit-SB correction exactly once.
        """
        requested=cc.get("amount")
        try:requested=float(requested)
        except (TypeError,ValueError):requested=0.0
        with self.autoplay.lock:
            pending=self.autoplay.pending
            if not pending or str(pending.get("action") or "").upper() not in ("RAISE","BET"):
                return ""
            room=int(pending.get("room") or 0)
            turn=self.autoplay.turn_by_room.get(room) or {}
            options=turn.get("userTurnOptions") or {}
            rr=options.get("5") or options.get(5)
            if not rr or len(rr)<2:return ""
            lo,hi=float(rr[0]),float(rr[1])
            current=float(self.autoplay.hero_bet_by_room.get(room,0.0))
            raw_before=float(pending.get("bet_amount") or 0.0)
            quantum=core.coin_money_quantum(
                profile.coin_small_blind,profile.coin_big_blind,lo,hi,current,
                fallback_scale=profile.base_scale)
            hero=max(0,int(self.state.get("hero_seat") or 1)-1)
            forced=int(self.forced_adjustment_by_seat.get(hero,0))
            current_wire=int(self.street_contrib.get(hero,0))
            if current_wire<=0 and current>0:
                # Defensive fallback before a cold accounting snapshot is available.
                current_wire=profile.to_ppp(current)+forced
            desired_total=current_wire+int(round(requested))
            raw_after=profile.ppp_to_legal_coin(
                desired_total,minimum=lo,maximum=hi,quantum=quantum)
            roundtrip=profile.to_ppp(raw_after)-current_wire
            if not lo-1e-9<=raw_after<=hi+1e-9:
                raise ValueError(f"quantized Coin target {raw_after} outside [{lo},{hi}]")
            pending["bet_amount"]=raw_after
            pending["raw"]=build_game_user_action_packet(room,5,raw_after)
        return (f"PPP amount={requested:g} / scale={profile.chip_scale} -> Coin {raw_before:g}"
                f" -> {raw_after:g} quantum={quantum} range=[{lo:g},{hi:g}]"
                f" mode=additional currentPPP={current_wire} desiredTotal={desired_total}"
                f" roundtripAdditional={roundtrip}")

    def _outstanding_forced_adjustment(self) -> int:
        return sum(int(value) for value in self.forced_adjustment_by_seat.values())

    def _wire_small_blind(self,h=None) -> int:
        hand=h or self.current_hand or self.context_hand
        profile=self.active_money_profile
        if profile is not None and hand is not None and self.money_profile_table_id==int(hand.table_id):
            return profile.ppp_small_blind
        if hand is None:return 0
        return core.money(hand.pre.get("sbAmount") or hand.room.props.get("smallBlind",0),self.scale)

    def _wire_big_blind(self,h=None) -> int:
        hand=h or self.current_hand or self.context_hand
        profile=self.active_money_profile
        if profile is not None and hand is not None and self.money_profile_table_id==int(hand.table_id):
            return profile.ppp_big_blind
        if hand is None:return 0
        return core.money(hand.pre.get("bbAmount") or hand.room.props.get("bigBlind",0),self.scale)

    def _wire_remaining_from_coin(self,seat:int,raw_amount:Any) -> int:
        # Until the normalized SB takes another voluntary action, its PPP stack is
        # lower/higher than raw*scale by the forced-post override. Once absorbed,
        # both ledgers converge again.
        return max(0,core.money(raw_amount,self.scale)-int(self.forced_adjustment_by_seat.get(seat,0)))

    def _absorb_forced_adjustment(self,seat:int):
        if self.forced_adjustment_by_seat.get(seat):
            self.forced_adjustment_by_seat[seat]=0
        self.forced_raw_remaining_by_seat.pop(seat,None)

    def _wire_chipsback_amount(self,seat:int,raw_amount:Any) -> tuple[int,int]:
        """Scale a Coin return and release the explicit-SB delta on its final tick."""
        chips=max(0,core.money(raw_amount,self.scale))
        released_adjustment=0
        raw_remaining=max(0,int(self.forced_raw_remaining_by_seat.get(seat,0)))
        if raw_remaining:
            consumed=min(raw_remaining,chips)
            raw_remaining-=consumed
            if raw_remaining:
                self.forced_raw_remaining_by_seat[seat]=raw_remaining
            else:
                self.forced_raw_remaining_by_seat.pop(seat,None)
                released_adjustment=int(self.forced_adjustment_by_seat.get(seat,0))
                chips+=released_adjustment
                self.forced_adjustment_by_seat[seat]=0
        return max(0,chips),released_adjustment

    def _normalize_gross_pools(self,pools:list[int]) -> list[int]:
        result=[max(0,int(value)) for value in pools]
        adjustment=self._outstanding_forced_adjustment()
        if result and adjustment:
            result[0]=max(0,result[0]+adjustment)
        return result

    def _decoded_events(self):
        return core.decode_coin_events(list(self.events))

    @staticmethod
    def _table_from_data(data:dict) -> int:
        for key in ("tableId","gameTableId","tableID","tid"):
            try:
                value=int(data.get(key) or 0)
                if value:return value
            except Exception:pass
        return 0

    def _remember_join_allocation(self,data:dict) -> int:
        """Bind Coin's nested lobby allocation before its game socket exists.

        `lobby.join_game` only carries a config id.  The following inbound
        `lobby.join_game_table` is the first message that exposes the concrete table
        id, inside tablesToJoin[].tableName.  That is the truthful point at which a
        PPP EnterRoomREQ can be emitted and remain Pending until game_alldata arrives.
        """
        if not (self.requested_table_id or self.requested_config_id):return 0
        rows=data.get("tablesToJoin") or []
        for row in rows:
            if not isinstance(row,dict):continue
            props=row.get("roomProperties") if isinstance(row.get("roomProperties"),dict) else {}
            try:cid=int(props.get("id") or props.get("configId") or row.get("configId") or 0)
            except Exception:cid=0
            name=str(row.get("tableName") or "")
            tid=self._table_from_data(row)
            if not tid:
                match=re.search(r"\b(\d{6,})\s*$",name)
                if match:tid=int(match.group(1))
            if not tid:continue
            if self.requested_table_id and tid!=self.requested_table_id:continue
            if self.requested_config_id and cid!=self.requested_config_id:continue
            self.requested_table_id=tid
            if cid:
                normalized=dict(props); normalized.setdefault("configId",cid)
                # Coin's allocation object uses different casing from its older
                # GameRoomProperties snapshots.  Normalize only aliases; values stay
                # fully dynamic and table-specific.
                for canonical,alias in (("minBuyIn","minbuyin"),("maxBuyIn","maxbuyin"),("maxSize","tableSize")):
                    if normalized.get(canonical) is None and normalized.get(alias) is not None:
                        normalized[canonical]=normalized[alias]
                self.table_to_config[tid]=cid; self.room_props_by_config[cid]=normalized
            if name:self.room_name_by_table[tid]=name
            return tid
        return 0

    def _promote_identity(self,name:Any=None,uid:Any=None,source:str="") -> bool:
        """Learn Coin identity in two phases: login name first, numeric uid later.

        Current Coin login requests may truthfully carry userName/sessionId while
        userId is still 0.  Seat snapshots then provide the stable numeric uid.
        Never throw away the name just because the first frame has uid=0.
        """
        name=str(name or "").strip()
        try:uid_i=int(uid or 0)
        except (TypeError,ValueError):uid_i=0
        old_name=str(self.identity.get("user_name") or "")
        try:old_uid=int(self.identity.get("user_id") or 0)
        except (TypeError,ValueError):old_uid=0

        if old_name and name and old_name!=name:
            return False
        if old_uid and uid_i and old_uid!=uid_i:
            return False

        changed=False
        if name and not old_name:
            self.identity["user_name"]=name
            self.state["user_name"]=name
            changed=True
            self._diagnostic(
                "identity_name",
                f"Coin login name captured; uid={'ready' if uid_i>0 else 'pending'}",
                {"name": name},
            )
        if uid_i>0 and not old_uid:
            self.identity["user_id"]=uid_i
            self.state["user_id"]=uid_i
            if name:self.identity["user_name"]=name; self.state["user_name"]=name
            changed=True
            self._diagnostic("identity_uid",f"Coin numeric identity resolved from {source or 'event'}")
        return changed

    @staticmethod
    def _identity_rows(cmd:str,data:dict) -> list[dict]:
        if not isinstance(data,dict):return []
        if cmd=="game.seatInfo":
            rows=data.get("seatResponseDataList") or []
            return [dict(row) for row in rows if isinstance(row,dict)]
        if cmd=="game.game_alldata":
            nested=data.get("seatInfoRsponseData") or data.get("seatInfoResponseData") or {}
            rows=nested.get("seatResponseDataList") if isinstance(nested,dict) else []
            return [dict(row) for row in (rows or []) if isinstance(row,dict)]
        if cmd in ("game.seat","game.take_Seat","game.take_seat") and data.get("userName"):
            return [dict(data)]
        return []

    def _remember_event(self,event:dict,cmd:str,room:Optional[int],data:dict):
        """Persist identity/config plus the exact SmartFox-room -> Coin-table binding."""
        try:
            ce=core.decode_coin_events([event])[0]
            d=ce.data
            if isinstance(d,dict) and d.get("userName") and "sessionId" in d:
                # Coin currently sends uid=0 in the authenticated login request.
                # Keep the truthful name now; promote numeric uid from seat data.
                self._promote_identity(
                    d.get("userName"), d.get("userId"), "login"
                )
            for props in core.extract_all_json_after(ce.body,b"GameRoomProperties"):
                if isinstance(props,dict) and isinstance(props.get("configId"),int):
                    self.room_props_by_config[int(props["configId"])]=dict(props)
            # Lobby snapshots encode RoomName/default/activeGameInfo/GameRoomProperties
            # as adjacent SFS values. Pair them once so a table opened later already has
            # its exact limits/game type before wait_list_data arrives.
            strings=core.printable_strings(ce.body)
            for i,s in enumerate(strings):
                if s!="GameRoomProperties" or i+1>=len(strings):continue
                try:props=json.loads(strings[i+1])
                except Exception:continue
                if not isinstance(props,dict) or not isinstance(props.get("configId"),int):continue
                for prior in reversed(strings[max(0,i-8):i]):
                    match=re.search(r"\b(\d{6,})$",prior)
                    if not match:continue
                    tid0=int(match.group(1)); cid0=int(props["configId"])
                    self.table_to_config[tid0]=cid0; self.room_name_by_table[tid0]=prior.lstrip("!\"#'()+./")
                    break
            tid=self._table_from_data(data)
            if room is not None and ce.table_id:
                self.room_to_table[room]=int(ce.table_id); self.table_to_room[int(ce.table_id)]=room
                tid=tid or int(ce.table_id)
            if room is not None and tid and cmd in ("game.wait_list_data","game.game_init","game.game_alldata"):
                self.room_to_table[room]=tid; self.table_to_room[tid]=room
                try:
                    stamp=int(str(data.get("initTimeStamp") or 0))
                    if stamp>0:self.room_session_timestamp_by_table.setdefault(tid,stamp)
                except Exception:pass
                if data.get("configId") is not None:
                    try:self.table_to_config[tid]=int(data["configId"])
                    except Exception:pass
                candidates=[s for s in core.printable_strings(ce.body)
                            if re.search(rf"\b{tid}$",s) and not s.strip().isdigit()]
                if candidates:self.room_name_by_table[tid]=min(candidates,key=len).lstrip("!\"#'()")
                cid=int(data.get("configId") or self.table_to_config.get(tid) or 0)
                if self.requested_table_id==tid or (self.requested_config_id and self.requested_config_id==cid):
                    self.active_hook_room=room; self.closing_rooms.discard(room); self.requested_table_id=0; self.requested_config_id=0
                elif self.active_hook_room is None and room not in self.closing_rooms:
                    self.active_hook_room=room
            identity_rows=self._identity_rows(cmd,data)
            if identity_rows:
                hero_id=int(self.identity.get("user_id") or 0)
                hero_name=str(self.identity.get("user_name") or "")
                hero=next((r for r in identity_rows if (
                    (hero_id and int(r.get("userId") or 0)==hero_id)
                    or (hero_name and str(r.get("userName") or "")==hero_name)
                )),None)
                if hero:
                    self._promote_identity(
                        hero.get("userName") or hero_name,
                        hero.get("userId"),
                        "seat snapshot",
                    )
                    try:
                        seat=int(hero.get("seatId") or 0)
                    except (TypeError,ValueError):
                        seat=0
                    if seat>0:self.state["hero_seat"]=seat
                    if (room is not None and float(hero.get("userChips") or 0)>0
                            and room not in self.closing_rooms):
                        self.active_hook_room=room
                        if not self.state.get("_operator_seated"):
                            self.state["_operator_seated"]=True
                            self._diagnostic("seated","hero seated",{"seat":seat,"room":room})
        except Exception as e:
            log("CACHE",f"event cache warning: {e}")

    def _capture_model(self):
        model=core.CoinCaptureModel(self._decoded_events())
        if self.identity.get("user_id"):
            model.hero_id=int(self.identity["user_id"]); model.hero_name=str(self.identity["user_name"])
        for cid,props in self.room_props_by_config.items():model.room_props_by_config[cid]=dict(props)
        for tid,cid in self.table_to_config.items():
            room=model.rooms_by_table.setdefault(tid,core.RoomMeta(tid,cid))
            room.config_id=cid; room.props=dict(self.room_props_by_config.get(cid,room.props))
            room.room_name=self.room_name_by_table.get(tid,room.room_name)
        for room,tid in self.room_to_table.items():model.hook_rooms_by_table[tid].add(room)
        return model

    def _is_active_room(self,room:Optional[int]) -> bool:
        active=self.context_hook_room if self.context_active else self.active_hook_room
        return active is not None and room==active and room not in self.closing_rooms

    def _bombpot_for(self,room:Optional[int]) -> BombpotTracker:
        key=int(room) if room is not None else -1
        if key not in self.bombpots:self.bombpots[key]=BombpotTracker()
        return self.bombpots[key]

    def _build_context_hand(self):
        """Build table/roster context without requiring hero cards or a hero turn."""
        evs=self._decoded_events(); model=self._capture_model()
        hook_room=self.active_hook_room
        tid=int(self.room_to_table.get(hook_room,0)); game_init={}; roster=[]; ts=int(time.time()*1000)
        if not hook_room or not tid:return None
        # Only events from the selected SmartFox room may contribute context.
        for e in evs:
            if e.hook_room!=hook_room:continue
            if e.name=="game.game_init" and isinstance(e.data,dict) and e.data.get("tableId"):
                tid=int(e.data["tableId"]); game_init=dict(e.data)
                try: ts=int(str(e.data.get("initTimeStamp") or ts))
                except Exception: pass
        # Latest seat snapshot for this table.
        for e in evs:
            if e.hook_room==hook_room and e.name=="game.seatInfo" and isinstance(e.data,dict):
                rows=e.data.get("seatResponseDataList")
                if rows: roster=[dict(r) for r in rows if isinstance(r,dict)]
        room=model.rooms_by_table.get(tid)
        if room is None:
            room=core.RoomMeta(tid)
            # Recover config binding if wait_list_data was seen before model discovery.
            for e in evs:
                if e.name=="game.wait_list_data" and isinstance(e.data,dict) and int(e.data.get("tableId") or 0)==tid:
                    room.config_id=e.data.get("configId")
            if room.config_id in model.room_props_by_config: room.props=model.room_props_by_config[room.config_id]
        hero_id=int(model.hero_id or self.identity.get("user_id") or self.state.get("user_id") or 0); hero_name=str(model.hero_name or self.identity.get("user_name") or self.state.get("user_name") or "HERO")
        hero_row=next((r for r in roster if int(r.get("userId") or 0)==hero_id or r.get("userName")==hero_name),None)
        hero_seat=int((hero_row or {}).get("seatId") or 1)
        pre={
            "dealerSeatId":int(game_init.get("dealerSeatId") or 1),
            "sbSeatId":int(game_init.get("smallBlindSeatId") or 1),
            "bbSeatId":int(game_init.get("bigBlindSeatId") or 1),
            "sbAmount":game_init.get("smallBlind", room.props.get("smallBlind",0)),
            "bbAmount":game_init.get("bigBlind", room.props.get("bigBlind",0)),
            "anteAmount":game_init.get("ante", room.props.get("ante",0)),
        }
        hid=int(game_init.get("gameId") or (tid*100000))
        h=core.HandModel(hid,tid,hero_id,hero_name,hero_seat,roster,pre,[],{},room,[],ts)
        self.state.update(user_name=hero_name,user_id=hero_id,hero_seat=(hero_seat if hero_row else 0),table_id=tid,_hook_room=hook_room)
        return self._stamp_game_id_context(h)

    async def ensure_observer_context(self):
        async with self.context_lock:
            try:
                await self._ensure_observer_context_unlocked()
            except core.UnrepresentableMoneyProfile:
                # A protocol-domain validation failure must never tear down the hook
                # TCP client. Stay in Coin's room but expose no inconsistent EYE table.
                if self.context_active:
                    await self.leave_table_context(reason="money-unrepresentable")
                else:
                    self.context_hand=None; self.current_hand=None
                    self.context_table_id=0; self.context_hook_room=None
                    self.lifecycle_phase="lobby"
                return

    async def _ensure_observer_context_unlocked(self):
        h=self._build_context_hand()
        if not h:return
        if self.context_active and self.context_table_id==h.table_id:
            self._activate_money_profile(h,"context-refresh")
            self.context_hand=h
            return
        # If switching tables, terminate the old context first.
        if self.context_active and self.context_table_id and self.context_table_id!=h.table_id:
            await self.leave_table_context(reason="switch-table")
        self._activate_money_profile(h,"admission")
        self.context_hand=h; b=core.PPPBuilder(h,self.scale)
        await self._ensure_login_identity()
        await self._send_enter_room_req(h.table_id)
        await self.eye_send_outer(b._outer("pb.EnterRoomRSP",b._enter_room_rsp(),"OTHERS"),"pb.EnterRoomRSP [observer]")
        self.context_active=True; self.context_table_id=h.table_id; self.context_hook_room=self.active_hook_room
        self.enter_request_table_id=0; self.leave_request_sent=False; self.stand_request_sent=False; self.lifecycle_phase="table"
        # EnterRoomRSP and the following broadcasts are generated from the same room.
        self.seat_map={int(r.get("seatId") or 0):dict(r) for r in h.roster if int(r.get("seatId") or 0)}
        self.announced_seats={
            seat:int(row.get("userId") or 0) for seat,row in self.seat_map.items()
            if int(row.get("userId") or 0)
            and int(row.get("userId") or 0)!=h.hero_id
            and (float(row.get("userChips") or 0)>0 or row.get("isPlaying") is True)
        }
        for seat,row in sorted(self.seat_map.items()):
            uid=int(row.get("userId") or 0)
            if not uid: continue
            # A hero reservation with zero chips is BookSeat-like, not a completed SitDown.
            if uid==h.hero_id and float(row.get("userChips") or 0)<=0: continue
            if uid==h.hero_id:
                # A player who was already seated before the bridge attached must still
                # complete the same PPP admission sequence.  Merely broadcasting the
                # seat left EYE at seated=false for every later hand.
                await self._send_hero_sit(row,ensure_context=False)
        log("TABLE",f"observer context coinTable={h.table_id} pppTable={core.ppp_table_id(h.table_id)} room={h.room.room_name!r} seats={sorted(self.announced_seats)}")

    async def _send_hero_sit(self,row:dict,*,ensure_context=True):
        if self.hero_sitting:return
        if self.hero_departing:
            log("SEAT",f"stale hero SitDown suppressed seat={row.get('seatId')}")
            return
        if ensure_context:
            await self.ensure_observer_context()
            if not self.context_active:return
        h=self.context_hand or self._build_context_hand()
        if not h:return
        self._activate_money_profile(h,"sit")
        seat=int(row.get("seatId") or 0); uid=int(row.get("userId") or h.hero_id)
        if not seat:return
        # Refresh context hero seat immediately; no hard-coded seat identity.
        h.hero_seat=seat; h.hero_id=uid; h.hero_name=str(row.get("userName") or h.hero_name)
        self.state.update(hero_seat=seat,user_id=uid,user_name=h.hero_name)
        b=core.PPPBuilder(h,self.scale); chips=core.money(row.get("userChips",0),self.scale)
        sit_req=b"".join([core.p_int(1,seat-1),core.p_int(2,chips),core.p_str(3,core.PPP_CLIENT_IP),
                           core.p_int(4,0),core.p_int(5,0),core.p_int(6,0),
                           core.p_str(7,core.PPP_SIT_EMAIL),core.p_int(8,1)])
        await self.eye_send_cmd("pb.SitDownREQ",sit_req)
        if self.announced_seats.get(seat)!=uid:
            await self.eye_send_cmd("pb.SitDownBRC",b._sitdown_brc(row,wait_blind=True,in_game=False)); self.announced_seats[seat]=uid
        await self.eye_send_cmd("pb.SitDownRSP",core.p_int(1,0)+core.p_int(3,seat-1)+core.p_int(4,chips)+core.p_int(5,10))
        pending_room=self.context_hook_room or self.table_to_room.get(h.table_id) or self.active_hook_room
        pending=self.pending_buyin_by_room.pop(pending_room,None)
        total=core.money(pending if pending is not None else row.get("userChips",0),self.scale)
        await self.eye_send_cmd("pb.TotalBuyinBRC",core.p_int(1,uid)+core.p_int(2,total)+core.p_int(3,0))
        self.hero_sitting=True; self.hero_total_buyin=total; self.wait_blind_cancelled=False; self.stand_request_sent=False
        log("TABLE",f"hero sat uid={uid} coinSeat={seat}->ppp={seat-1} chips={chips} buyin={total}")

    async def _send_stand_req(self):
        if self.stand_request_sent:return
        await self.eye_send_cmd("pb.StandUpREQ",b"")
        self.stand_request_sent=True

    async def _send_hero_stand(self,seat:int,*,emit_request=True):
        self.hero_departing=True; self.deferred_seat_snapshot=None
        if not self.hero_sitting:return
        if emit_request:await self._send_stand_req()
        await self.eye_send_cmd("pb.StandUpRSP",core.p_int(1,0))
        await self.eye_send_cmd("pb.StandUpBRC",core.p_int(1,max(0,seat-1)))
        self.announced_seats.pop(seat,None); self.hero_sitting=False; self.hero_total_buyin=0; self.current_hand=None; self.wait_blind_cancelled=False
        pending_room=self.context_hook_room or self.active_hook_room
        if pending_room is not None:self.pending_buyin_by_room.pop(pending_room,None)
        self.rabbit_cards=[]; self.rabbit_second_board=[]; self.rabbit_finish_stage=None
        self.state["hero_seat"]=0; self.state["hand_id"]=""; self.state["_pending_finish_hint"]=None
        try:self.autoplay.pending=None
        except Exception:pass
        log("TABLE",f"hero stand coinSeat={seat}; observer context remains active")

    async def apply_seat_snapshot(self,data:dict):
        rows=data.get("seatResponseDataList") or []
        new={int(r.get("seatId") or 0):dict(r) for r in rows if isinstance(r,dict) and int(r.get("seatId") or 0)}
        if self.current_hand:
            # Freeze roster for the duration of a hand. PPP DealerInfo/turn state refers
            # to those seats; applying StandUp/SitDown mid-hand can make EYE inconsistent.
            self.deferred_seat_snapshot={"seatResponseDataList":[dict(r) for r in rows if isinstance(r,dict)]}
            return
        # A live snapshot supersedes any older hand-frozen copy.
        self.deferred_seat_snapshot=None
        old=dict(self.seat_map); self.seat_map=new
        if not self.context_active:return
        h=self.context_hand or self._build_context_hand()
        if not h:return
        self._activate_money_profile(h,"seat-snapshot")
        b=core.PPPBuilder(h,self.scale); hero_id=int(self.state.get("user_id") or h.hero_id)
        for seat in sorted(set(old)|set(new)):
            a=old.get(seat); z=new.get(seat); au=int((a or {}).get("userId") or 0); zu=int((z or {}).get("userId") or 0)
            if au==zu:
                # Reservation becomes a real seat once chips arrive.
                if z and zu==hero_id and not self.hero_sitting and float(z.get("userChips") or 0)>0:
                    await self._send_hero_sit(z)
                elif (z and zu and zu!=hero_id
                      and self.announced_seats.get(seat)!=zu
                      and (float(z.get("userChips") or 0)>0 or z.get("isPlaying") is True)):
                    await self.eye_send_cmd("pb.SitDownBRC",b._sitdown_brc(z,wait_blind=False,in_game=bool(z.get("isPlaying"))))
                    self.announced_seats[seat]=zu; log("SEAT",f"seat={seat} uid={zu} {z.get('userName','')} joined after reservation")
                continue
            # UserCurrentProfit belongs to the player, not to the physical chair. A
            # replacement must never inherit the previous occupant's session result.
            self.session_profit.pop(seat-1,None)
            if au:
                if au==hero_id:
                    await self._send_hero_stand(seat)
                elif self.announced_seats.get(seat)==au:
                    await self.eye_send_cmd("pb.StandUpBRC",core.p_int(1,seat-1)); self.announced_seats.pop(seat,None)
                    log("SEAT",f"seat={seat} old uid={au} left/replaced")
            if zu:
                if zu==hero_id and float(z.get("userChips") or 0)<=0:
                    log("SEAT",f"hero reserved seat={seat}; waiting for buyin")
                    continue
                if zu==hero_id:
                    await self._send_hero_sit(z)
                else:
                    if float(z.get("userChips") or 0)<=0 and z.get("isPlaying") is not True:
                        log("SEAT",f"opponent reserved seat={seat} uid={zu}; waiting for buyin")
                        continue
                    await self.eye_send_cmd("pb.SitDownBRC",b._sitdown_brc(z,wait_blind=False,in_game=bool(z.get("isPlaying"))))
                    self.announced_seats[seat]=zu; log("SEAT",f"seat={seat} uid={zu} {z.get('userName','')} joined")
        hero_row=next((row for row in new.values() if int(row.get("userId") or 0)==hero_id),None)
        sitout_flag=False
        if hero_row:
            flag=hero_row.get("isSittingOut") or hero_row.get("sittingOut") or hero_row.get("sitOut")
            sitout_flag=flag is True or str(flag).lower() in {"true","1"}
            playing=hero_row.get("isPlaying") is True
            if sitout_flag or (self.hero_sitting and self.wait_blind_cancelled and not playing and not self.current_hand and self.cc_miss_streak>0):
                if not self.hero_sitting_out:
                    self._diagnostic("sitout_detected","hero is sitting out of hands",{
                        "isPlaying":playing,"flag":sitout_flag,"streak":self.cc_miss_streak,
                    })
                self.hero_sitting_out=True
            else:
                self.hero_sitting_out=False
        else:
            self.hero_sitting_out=False

    def _cancel_leave_timeout(self):
        task=self.leave_timeout_task; self.leave_timeout_task=None
        if task and task is not asyncio.current_task() and not task.done():task.cancel()

    async def _leave_ack_timeout(self,room:Optional[int]):
        try:
            await asyncio.sleep(max(0,float(self.quit_ack_timeout)))
            async with self.context_lock:
                if (self.context_active and self.lifecycle_phase=="leaving"
                        and self.leaving_hook_room==room):
                    log("TABLE",f"Coin quit ACK timeout room={room}; closing PPP context")
                    await self.leave_table_context("quit_table-timeout",send_request=False,emit_stand_request=False)
                    if room is not None:self.closing_rooms.discard(room)
                    if room==self.active_hook_room:self.active_hook_room=None
        except asyncio.CancelledError:
            pass

    async def _begin_leave_table_context(self,room:Optional[int]):
        if not self.context_active:return False
        if self.lifecycle_phase=="leaving" and self.leaving_hook_room==room:return True
        self.hero_departing=True; self.deferred_seat_snapshot=None
        self.leaving_hook_room=room if room is not None else self.context_hook_room
        # Proven stock PPP exit order is StandUpREQ -> LeaveRoomREQ, followed by
        # their server responses.  Sending Leave first can leave the backend's
        # table latch alive even though Coin has already moved to Lobby.
        if self.hero_sitting and self.state.get("hero_seat"):
            await self._send_stand_req()
        await self._send_leave_room_req()
        self._cancel_leave_timeout()
        self.leave_timeout_task=asyncio.create_task(self._leave_ack_timeout(self.leaving_hook_room))
        return True

    def _abort_leave_table_context(self,room:Optional[int],reason="rejected"):
        self._cancel_leave_timeout()
        if room is not None:self.closing_rooms.discard(room)
        self.leaving_hook_room=None; self.leave_request_sent=False
        self.hero_departing=not self.hero_sitting
        self.lifecycle_phase="table" if self.context_active else "lobby"
        log("TABLE",f"Coin quit {reason} room={room}; PPP context retained")

    async def leave_table_context(self,reason="quit",*,send_request=True,emit_stand_request=False):
        if not self.context_active:return
        old_hook_room=self.context_hook_room
        self.hero_departing=True; self.deferred_seat_snapshot=None
        if send_request:await self._send_leave_room_req()
        if self.hero_sitting and self.state.get("hero_seat"):
            await self._send_hero_stand(int(self.state["hero_seat"]),emit_request=emit_stand_request)
        tid=int(self.context_table_id or self.state.get("table_id") or 0)
        await self.eye_send_cmd("pb.LeaveRoomRSP",b"".join([core.p_int(1,0),core.p_int(3,0),core.p_int(4,core.PPP_CLUB_ID),core.p_int(6,0)]))
        await self.eye_send_cmd("pb.OtherLeaveRoomBRC",core.p_int(1,int(self.state.get("user_id") or 0)),"OTHERS")
        log("TABLE",f"leave table={tid} reason={reason}")
        self._cancel_leave_timeout()
        self.context_active=False; self.context_table_id=0; self.context_hook_room=None; self.context_hand=None; self.current_hand=None
        self.enter_request_table_id=0; self.requested_table_id=0; self.requested_config_id=0
        self.leaving_hook_room=None; self.leave_request_sent=False; self.stand_request_sent=False; self.lifecycle_phase="lobby"
        self.seat_map.clear(); self.announced_seats.clear(); self.state.update(table_id=0,hero_seat=0,hand_id="",_hook_room=None,_pending_finish_hint=None)
        self.session_profit.clear(); self.hand_contrib.clear(); self.forced_adjustment_by_seat.clear(); self.forced_raw_remaining_by_seat.clear(); self.hand_participants.clear(); self.game_id_prefix_by_table.pop(tid,None); self.room_session_timestamp_by_table.pop(tid,None)
        self.emitted_primary_stages.clear(); self.emitted_second_boards.clear(); self.pending_actor_seat=None
        if old_hook_room is not None:self.pending_buyin_by_room.pop(old_hook_room,None)
        if old_hook_room is not None:self.closing_rooms.discard(old_hook_room)
        self.deferred_seat_snapshot=None; self.inject_claim_id=None; self.pending_action_ack=None; self.wait_blind_cancelled=False; self.hero_total_buyin=0
        self.pending_round_over=None
        self.rabbit_cards=[]; self.rabbit_second_board=[]; self.rabbit_finish_stage=None
        self.autoplay.clear_room(old_hook_room)
        self.money_profiles_by_table.pop(tid,None)
        self.active_money_profile=None; self.money_profile_table_id=0
        self.scale=self.base_scale; self.autoplay.chip_scale=self.base_scale
        self.state.pop("_money_scale",None); self.state.pop("_money_factor",None)

    def _clear_cc_queue(self):
        n=0
        while True:
            try:self.cc_queue.get_nowait(); n+=1
            except asyncio.QueueEmpty:break
        if n:log("CC",f"discarded {n} stale cc frame(s) before new hint")

    def _claim_wait_blind_cancel(self) -> bool:
        if not self.hero_sitting or self.wait_blind_cancelled:return False
        self.wait_blind_cancelled=True
        return True

    async def heartbeat_loop(self):
        while True:
            if int(self.state.get("user_id") or 0) and self.lifecycle_phase!="offline":
                try:
                    # Heartbeats own idle reconnects too; a quiet Coin lobby must not
                    # wait for the next game packet to rebuild its EYE session.
                    await self.ensure_eye()
                    if not self.login_sent:raise ConnectionError("EYE session not logged in")
                    location="TABLE" if self.context_active else "OTHERS"
                    await self.eye_send_cmd("pb.HeartBeatREQ",b"",location)
                    # Fresh PPP reference answers roughly 0.4 s after the request and
                    # repeats the pair on a ~3 s cadence.
                    await asyncio.sleep(0.4)
                    location="TABLE" if self.context_active else "OTHERS"
                    await self.eye_send_cmd("pb.HeartBeatRSP",b"",location)
                    self.heartbeat_count+=1
                except Exception as e: log("EYE",f"heartbeat failed: {e}")
            await asyncio.sleep(2.6)

    def _sync_identity(self,h):
        h=self._stamp_game_id_context(h)
        self._activate_money_profile(h,"hand-start")
        hook_room=self.table_to_room.get(h.table_id,self.active_hook_room)
        self.state.update(user_name=h.hero_name,user_id=h.hero_id,hero_seat=h.hero_seat,hand_id=str(h.hand_id),table_id=h.table_id,_hook_room=hook_room)
        self.current_hand=h
        self.settlement_round_over_sent=False; self.last_pools=[]; self.last_winner_info=[]; self.chipsback_refresh_seats.clear()
        self.street_generation+=1; self.action_seen.clear()
        self.round_over_seen.clear(); self.round_zoom_seen.clear(); self.pending_round_over=None; self.round_boundary_open=False; self.runout_closed=False; self.last_round_name=""; self.last_gross_pools=[]
        self.winner_fragments_seen.clear(); self.winner_pot_gross={}; self.winner_rsp_sent=False; self.show_hole_cards={}; self.show_hand_sent=False; self.is_doing_evchop=False
        self.rabbit_cards=[]; self.rabbit_second_board=[]; self.rabbit_finish_stage=None
        self.active_seats={int(h.pre.get("sbSeatId") or 0),int(h.pre.get("bbSeatId") or 0)}
        self.active_seats.update(int(r.get("seatId")) for r in h.roster if r.get("isPlaying") is True)
        self.active_seats.discard(0); self.all_in_seats.clear()
        self.last_full_raise=max(1,self._wire_big_blind(h))
        self._bootstrap_hand_accounting(h)
        self.notify_seen.clear()
        self.emitted_primary_stages.clear(); self.emitted_second_boards.clear()
        self.pending_actor_seat=h.ppp_hero_seat

    def _bootstrap_hand_accounting(self,h):
        """Reconstruct committed chips at the first hero turn from Coin facts.

        Profit records at settlement are net per seat in PPP.  Keeping both the
        current-street total and the whole-hand contribution prevents folded players
        from disappearing from WinnerRSP and removes any dependency on fixed blinds.
        """
        self.street_contrib.clear(); self.hand_contrib.clear(); self.remaining_stack.clear()
        self.forced_adjustment_by_seat.clear(); self.forced_raw_remaining_by_seat.clear()
        self.hand_participants=set(self.active_seats)
        active={s for s in self.active_seats if s>0}
        for row in h.roster:
            seat1=int(row.get("seatId") or 0)
            if seat1:self.remaining_stack[seat1-1]=core.money(row.get("userChips",0),self.scale)
        is_bomb=bool(h.room.props.get("_isBombpotHand"))
        if is_bomb:
            ante=core.money(h.room.props.get("_bombpotAnte",h.pre.get("anteAmount",0)),self.scale)
            for seat1 in active:
                seat=seat1-1; available=max(0,int(self.remaining_stack.get(seat,0)))
                paid=min(available,ante); self.hand_contrib[seat]=paid
                self.remaining_stack[seat]=available-paid
                if self.remaining_stack[seat]<=0:self.all_in_seats.add(seat1)
        else:
            ante=core.money(h.pre.get("anteAmount",0),self.scale)
            if ante:
                for seat1 in active:
                    seat=seat1-1; available=max(0,int(self.remaining_stack.get(seat,0)))
                    paid=min(available,ante); self.hand_contrib[seat]+=paid
                    self.remaining_stack[seat]=available-paid
                    if self.remaining_stack[seat]<=0:self.all_in_seats.add(seat1)
            sb_seat=int(h.pre.get("sbSeatId") or 0)-1
            for key,amount_key in (("sbSeatId","sbAmount"),("bbSeatId","bbAmount")):
                seat1=int(h.pre.get(key) or 0)
                amount=(self._wire_small_blind(h) if key=="sbSeatId"
                        else self._wire_big_blind(h))
                if seat1:
                    seat=seat1-1; available=max(0,int(self.remaining_stack.get(seat,0)))
                    paid=min(available,amount)
                    if key=="sbSeatId":
                        raw_value=(h.pre.get(amount_key)
                                   or h.room.props.get("smallBlind",0))
                        raw_paid=min(available,core.money(raw_value,self.scale))
                        self.forced_adjustment_by_seat[seat]=paid-raw_paid
                        self.forced_raw_remaining_by_seat[seat]=raw_paid
                    self.street_contrib[seat]=max(self.street_contrib[seat],paid)
                    self.hand_contrib[seat]+=paid; self.remaining_stack[seat]=available-paid
                    if self.remaining_stack[seat]<=0:self.all_in_seats.add(seat1)
        for e in h.events_before_turn:
            d=e.data if isinstance(e.data,dict) else {}
            if e.name=="game.seat":
                seat1=int(d.get("seatId") or 0); action=normalized_coin_seat_action(d)
                if not seat1:continue
                seat=seat1-1; self.hand_participants.add(seat1)
                available_before=max(0,int(self.remaining_stack.get(seat,0)))
                has_snapshot=d.get("userChips") is not None
                if action=="FOLD":
                    if has_snapshot:self.remaining_stack[seat]=self._wire_remaining_from_coin(seat,d.get("userChips"))
                    self.active_seats.discard(seat1)
                    continue
                if not action:
                    if has_snapshot:self.remaining_stack[seat]=self._wire_remaining_from_coin(seat,d.get("userChips"))
                    continue
                if action not in ("CALL","RAISE","BET","ALLIN","SB","BB","FORCE_BB","STRADDLE"):
                    if has_snapshot:self.remaining_stack[seat]=self._wire_remaining_from_coin(seat,d.get("userChips"))
                    continue
                old=self.street_contrib.get(seat,0); current_before=max(self.street_contrib.values(),default=0)
                total=core.money(d.get("betAmout",d.get("betAmount",0)),self.scale)
                total=min(max(old,total),old+available_before)
                delta=max(0,total-old)
                will_absorb=action in ("CALL","RAISE","BET","ALLIN") and total>=old
                expected_remaining=max(0,available_before-delta)
                if has_snapshot:
                    adjustment_after=0 if will_absorb else int(self.forced_adjustment_by_seat.get(seat,0))
                    snapshot=max(0,core.money(d.get("userChips"),self.scale)-adjustment_after)
                    if snapshot!=expected_remaining:
                        raise RuntimeError(
                            f"Coin stack/action mismatch during bootstrap seat={seat} "
                            f"before={available_before} delta={delta} expected={expected_remaining} snapshot={snapshot}")
                self.street_contrib[seat]=max(old,total); self.hand_contrib[seat]+=delta
                if will_absorb:
                    self._absorb_forced_adjustment(seat)
                self.remaining_stack[seat]=expected_remaining
                if action in ("RAISE","BET","ALLIN") and total>current_before:
                    increment=total-current_before
                    if increment>=self.last_full_raise:self.last_full_raise=increment
                if self.remaining_stack[seat]<=0:self.all_in_seats.add(seat1)
            elif e.name=="game.dealer_chat_action":
                # Some Coin builds race/omit the seat mutation but retain the full
                # dealer history used by PPPBuilder. Fold removal is idempotent, so
                # consuming both sources cannot double-count chips.
                by_name={str(r.get("userName") or ""):int(r.get("seatId") or 0) for r in h.roster}
                for action_row in d.get("gameActionMessagesHistory") or []:
                    if not isinstance(action_row,dict) or action_row.get("type")!="playerAction":continue
                    seat1=by_name.get(str(action_row.get("username") or ""),0)
                    if seat1 and str(action_row.get("newPlayerAction") or action_row.get("action") or "").upper()=="FOLD":
                        self.hand_participants.add(seat1); self.active_seats.discard(seat1)
            elif e.name=="game.return_chips":
                seat=int(d.get("seatId") or 1)-1
                returned,_released=self._wire_chipsback_amount(seat,d.get("chipsToReturn",0))
                self.street_contrib[seat]=max(0,self.street_contrib.get(seat,0)-returned)
                self.hand_contrib[seat]=max(0,self.hand_contrib.get(seat,0)-returned)
                self.remaining_stack[seat]+=returned

    def _dynamic_total_buyin(self,h) -> int:
        # Prefer actual Coin game.take_Seat.buyinAmount; current stack is not total buy-in.
        try:
            hook_room=self.table_to_room.get(h.table_id,self.context_hook_room or self.active_hook_room)
            pending=self.pending_buyin_by_room.get(hook_room)
            if pending is not None:return core.money(pending,self.scale)
            best=None
            for e in core.decode_coin_events(list(self.events)):
                if (e.name=="game.take_Seat" and isinstance(e.data,dict)
                        and (hook_room is None or e.hook_room==hook_room)
                        and int(e.data.get("seatId") or 0)==int(h.hero_seat)):
                    if e.data.get("buyinAmount") is not None: best=float(e.data["buyinAmount"])
            if best is not None:return core.money(best,self.scale)
        except Exception: pass
        return core.money(next((r.get("userChips",0) for r in h.roster if int(r.get("userId") or 0)==h.hero_id),0),self.scale)

    def report_action_exhausted_if_due(self, now:Optional[float]=None) -> bool:
        ack=self.pending_action_ack
        if not ack:return False
        current=time.monotonic() if now is None else float(now)
        if current<float(ack.get("retry_at") or float("inf")):return False
        if int(ack.get("retries") or 0)<max(0,self.action_max_attempts-1):return False
        if ack.get("_exhausted_reported"):return False
        ack["_exhausted_reported"]=True
        ack["retry_at"]=float("inf")
        snapshot=self._error_snapshot(reason="ACTION_NOT_CONFIRMED")
        self._diagnostic(
            "action_exhausted",
            f"Coin did not confirm action after {self.action_max_attempts}/{self.action_max_attempts} attempts",
            {
                "action":str(ack.get("action") or ""),
                "amount":ack.get("display_amount"),
                "attempt":self.action_max_attempts,
                "max_attempts":self.action_max_attempts,
                "telemetry":snapshot,
            },
        )
        return True

    async def _maybe_inject_async(self,event,payload,raw):
        """Claim one dummy and ask hook v3 to send on the same RealWebSocket.

        The hook response is always immediate.  v3 schedules ``send(ByteString)`` in
        the app process for the remaining cc.delay; old hooks simply use the first
        matching dummy after the due time.  A single ACK-based retry is available on
        the exact same websocket and hand.
        """
        cmd,_,_=cmd_room_data(payload)
        if str(event.get("direction") or "").lower()!="out" or cmd!="lobby.dummy":
            return core_hook_empty()
        schedule_supported=bool(event.get("schedule_send")) or int(event.get("v") or 0)>=3
        now=time.monotonic(); ack=self.pending_action_ack
        # Compatibility path: native v6 routing normally reports exhaustion
        # from LiveTableSession.action_offers(), because a fully exhausted action
        # no longer creates another arbitration offer.
        self.report_action_exhausted_if_due(now)
        if ack and now>=float(ack.get("retry_at") or float("inf")) and int(ack.get("retries") or 0)<max(0,self.action_max_attempts-1):
            same_hand=str(ack.get("hand_id") or "")==str(self.state.get("hand_id") or "")
            native_push=int(event.get("v") or 0)>=6
            same_ws=native_push or (not ack.get("ws_id") or ack.get("ws_id")==event.get("ws_id"))
            same_url=native_push or (not ack.get("url") or ack.get("url")==event.get("url"))
            if same_hand and same_ws and same_url:
                ack["retries"]=int(ack.get("retries") or 0)+1
                attempt=1+int(ack["retries"])
                ack["retry_at"]=float("inf")
                from .coin_autoplay import HookResult
                log("ACTION_RETRY",f"no Coin ACK; retry attempt={attempt}/{self.action_max_attempts} action={ack.get('action')} hand={ack.get('hand_id')}")
                self._diagnostic("action_retry","Coin did not confirm send; retrying",{
                    "action":str(ack.get("action") or ""),"amount":ack.get("display_amount"),
                    "attempt":attempt,"max_attempts":self.action_max_attempts,
                })
                return HookResult(inject_raw=ack["raw"],schedule_delay_ms=(0 if schedule_supported else None),
                                  schedule_token=f"{ack.get('token')}:retry{attempt}",log=f"retry {ack.get('action')}",
                                  action_name=str(ack.get("action") or ""),display_amount=ack.get("display_amount"),
                                  target_ws_id=str(ack.get("ws_id") or ""),
                                  target_channel_id=str(ack.get("channel_id") or ""),
                                  attempt=attempt)
        p=self.autoplay.pending
        if not p:return core_hook_empty()
        return self.autoplay.maybe_inject(event,payload,raw,self.state,schedule_supported=schedule_supported)

    async def _finish_hint_after(self,delay_ms:int,table_id:Optional[int],token:str):
        try:
            if delay_ms>0:await asyncio.sleep(delay_ms/1000.0)
            await self.finish_hint(table_id)
        finally:
            if self.schedule_finish_tasks.get(token) is asyncio.current_task():self.schedule_finish_tasks.pop(token,None)

    def _cold_candidate_snapshot(self):
        """Build a room-isolated cold model without guessing across open tables."""
        model=self._capture_model()
        active_room=self.active_hook_room
        active_tid=int(self.room_to_table.get(active_room,0) or 0)
        if not active_tid and self.context_hook_room==active_room:
            active_tid=int(self.context_table_id or self.state.get("table_id") or 0)
        if not active_tid and not self.context_active:
            active_tid=int(self.requested_table_id or 0)
        cands=[x for x in model.candidate_hands() if core.table_id_from_hand(x[0])==active_tid]
        return model,active_tid,cands

    def _current_hero_turn_id(self) -> str:
        room=self.active_hook_room
        if room is None:return ""
        turn=self.autoplay.turn_by_room.get(int(room)) or {}
        return str(turn.get("_turn_id") or "")

    async def _wait_for_cold_seed(self):
        """Allow late seed frames to enter the raw cache before taking a fallback.

        ``handle_event`` appends raw Coin frames before the ordered protocol worker,
        so sleeping here does not stop capture.  The turn token is checked on every
        pass: a new/manual/advanced turn cancels this recovery instead of creating a
        stale action for a different decision.
        """
        model,active_tid,cands=self._cold_candidate_snapshot()
        if cands or self.mid_hand_recovery_grace<=0:
            return model,active_tid,cands,"ready",0
        room=self.active_hook_room
        turn_id=self._current_hero_turn_id()
        attempts=max(1,int(self.mid_hand_recovery_attempts))
        slice_s=float(self.mid_hand_recovery_grace)/float(attempts)
        started=time.monotonic()
        for attempt in range(1,attempts+1):
            await asyncio.sleep(slice_s)
            if room!=self.active_hook_room or (turn_id and self._current_hero_turn_id()!=turn_id):
                return model,active_tid,[],"turn-changed",attempt
            model,active_tid,cands=self._cold_candidate_snapshot()
            if cands:
                self._diagnostic(
                    "mid_hand_seed_recovered",
                    "late Coin seed arrived inside bounded recovery window",
                    {"table_id":active_tid,"attempt":attempt,
                     "wait_ms":int(round((time.monotonic()-started)*1000.0))},
                )
                return model,active_tid,cands,"recovered",attempt
        return model,active_tid,[],"timeout",attempts

    def _nlh_prefold_config(self):
        if self._prefold_config_loaded:
            return self._prefold_config
        self._prefold_config_loaded=True
        try:
            from .prefold import load_prefold_config
            self._prefold_config=load_prefold_config()
        except Exception as exc:
            log("PREFOLD",f"chart load failed: {type(exc).__name__}: {exc}")
            self._prefold_config=None
        return self._prefold_config

    def _nlh_prefold_context(self,h):
        from .prefold import PrefoldContext, facing_from_street, position_from_seats
        props=getattr(getattr(h,"room",None),"props",{}) or {}
        try:mini=int(props.get("miniGameTypeId") or 0)
        except (TypeError,ValueError):mini=0
        label=str(props.get("_gameTypeLabel") or {1:"NLH"}.get(mini,"") or props.get("gameType") or "")
        occupied=[int(r.get("seatId") or 0) for r in (h.roster or []) if int(r.get("seatId") or 0)]
        if h.hero_seat: occupied.append(int(h.hero_seat))
        occupied=sorted({seat for seat in occupied if seat})
        dealer=int((h.pre or {}).get("dealerSeatId") or 0)
        position=position_from_seats(int(h.hero_seat or 0), dealer, occupied)
        bb=int(self._wire_big_blind(h) or 0)
        facing=facing_from_street(self.street_contrib, hero_ppp_seat=int(h.ppp_hero_seat), bb_chips=bb)
        put=self.street_contrib.get(int(h.ppp_hero_seat),0)
        current=max(self.street_contrib.values(),default=0)
        can_check=max(0,current-put)<=0
        cards=tuple(h.cards or ())
        street="PREFLOP"
        if any(name in self.emitted_primary_stages for name in ("FLOP","TURN","RIVER")):
            street="FLOP"
        bp=self._bombpot_for(self.active_hook_room).state
        return PrefoldContext(
            dealt_in_players=len(occupied),
            position=position,
            facing=facing,
            hole_cards=cards,
            game_family=label or "NLH",
            street=street,
            can_check=can_check,
            state_complete=len(cards)==2,
            bombpot=bool(bp.is_bombpot_hand),
            straddle=bool(props.get("straddle") or props.get("isStraddle")),
        )

    async def _try_nlh_prefold(self,h) -> bool:
        """Fold from an NLH chart without asking Eye. Never send a RoundHint on success."""
        config=self._nlh_prefold_config()
        if config is None or not config.enabled:
            return False
        try:
            from .prefold import evaluate_prefold
            context=self._nlh_prefold_context(h)
            decision=evaluate_prefold(config,context)
        except Exception as exc:
            self._diagnostic("prefold_skipped",f"chart context failed: {type(exc).__name__}: {exc}")
            return False
        if not decision.matched:
            if decision.canonical_hand:
                self._diagnostic("prefold_miss",f"{decision.canonical_hand} {decision.reason_code}",{
                    "hand":decision.canonical_hand,"reason":decision.reason_code,
                    "position":getattr(context,"position",""),"facing":getattr(context,"facing",""),
                    "players":getattr(context,"dealt_in_players",0),
                })
            return False
        if decision.audit_only or not decision.bypass_ai:
            self._diagnostic("prefold_audit",f"{decision.canonical_hand} would FOLD",{
                "action":"FOLD","hand":decision.canonical_hand,"rule_id":decision.rule_id,
                "reason":decision.reason_code,
            })
            return False
        try:
            scheduled=self.autoplay.schedule_chart_fold(self.state,reason=str(decision.reason_code or "PREFOLD"))
        except Exception as exc:
            self._diagnostic("prefold_skipped",f"Coin FOLD not available: {exc}",{
                "hand":decision.canonical_hand,"rule_id":decision.rule_id,
            })
            return False
        self._diagnostic("prefold_ready","NLH chart fold queued; Eye hint skipped",{
            "action":"FOLD","amount":None,
            "hand":decision.canonical_hand,"rule_id":decision.rule_id,
            "reason":decision.reason_code,
            "position":context.position,"facing":context.facing,
            "players":context.dealt_in_players,
            "coin_action":scheduled.get("action"),
        })
        return True

    async def start_cold_hint(self,event:dict,payload:Any,raw:bytes):
        async with self.hint_lock:
            try:
                model,active_tid,cands,recovery_state,recovery_attempts=await self._wait_for_cold_seed()
                if recovery_state=="turn-changed":
                    self._diagnostic(
                        "mid_hand_recovery_cancelled",
                        "Coin turn changed while waiting for a complete seed; stale hint suppressed",
                        {"table_id":active_tid,"attempts":recovery_attempts},
                    )
                    return
                if not cands:
                    if active_tid and active_tid not in self.mid_hand_wait_notified:
                        self.mid_hand_wait_notified.add(active_tid)
                        self._diagnostic(
                            "joined_mid_hand",
                            "current hand has no trustworthy complete seed after bounded recovery; using CHECK/FOLD safety action",
                            {"table_id":active_tid,
                             "wait_ms":int(round(self.mid_hand_recovery_grace*1000.0)),
                             "attempts":recovery_attempts},
                        )
                    try:
                        fb=self.autoplay.schedule_failsafe(self.state,reason="MID_HAND_NO_SEED")
                        self._diagnostic("fallback_ready","mid-hand snapshot incomplete; safety action queued",{
                            "action":fb.get("action"),"attempt":1,
                            "max_attempts":self.action_max_attempts,
                            "reason":"MID_HAND_NO_SEED",
                        })
                    except Exception as fallback_error:
                        snapshot=self._error_snapshot(reason="MID_HAND_NO_SEED")
                        self._diagnostic("failsafe_unavailable",f"mid-hand recovery unavailable and no CHECK/FOLD is legal: {fallback_error}",{"telemetry":snapshot})
                    return
                self.mid_hand_wait_notified.discard(active_tid)
                hid=cands[-1][0]
                if hid in self.cold_hands:return
                h=self._stamp_game_id_context(model.build_hand(hid))
                if h.pre.get("_hmuriy_snapshot_recovery"):
                    self._diagnostic(
                        "mid_hand_recovered",
                        "current hand recovered from pristine game_alldata snapshot",
                        {"table_id":active_tid,"hand_id":hid},
                    )
                # Only wire-level bombpot fact we can prove from supplied captures: IsInBombpot.
                bp=self._bombpot_for(self.active_hook_room).state
                h.room.props["_isBombpotHand"]=bool(bp.is_bombpot_hand)
                h.room.props["_isDoubleBoard"]=bool(bp.is_double_board)
                h.room.props["_bombpotAnte"]=bp.hand_ante
                h.room.props["_handsToBombpot"]=bp.current_hand_number if bp.current_hand_number is not None else 0
                self._sync_identity(h)
                if self.context_active and not self.hero_sitting:
                    hero_row=next((r for r in h.roster if int(r.get("userId") or 0)==h.hero_id or str(r.get("userName") or "")==h.hero_name),None)
                    if hero_row and float(hero_row.get("userChips") or 0)>0:
                        await self._send_hero_sit(hero_row)
                elif not self.context_active:
                    # The synthesized cold stream contains the full SitDown sequence.
                    # Mark it locally before reaching CancelWaitBlind so late attach
                    # follows the same one-shot lifecycle as an observed admission.
                    hero_row=next((r for r in h.roster if int(r.get("userId") or 0)==h.hero_id or str(r.get("userName") or "")==h.hero_name),None)
                    if hero_row and float(hero_row.get("userChips") or 0)>0:
                        self.hero_sitting=True; self.hero_total_buyin=self._dynamic_total_buyin(h); self.wait_blind_cancelled=False
                        self.announced_seats[int(hero_row.get("seatId") or h.hero_seat)]=h.hero_id
                # Re-observe now that hero identity is known, so autoplay captures room/ws.
                self.autoplay.observe(event,payload,raw,self.state)
                if await self._try_nlh_prefold(h):
                    self.context_active=True; self.context_table_id=h.table_id
                    self.context_hook_room=self.table_to_room.get(h.table_id,self.active_hook_room)
                    self.cold_hands.add(hid); self.state["_pending_finish_hint"]=None
                    log("HAND",f"prefold hand={hid} hero={h.hero_name} cards={h.cards}")
                    self._diagnostic("cards","hero hole cards captured",{"cards":h.cards,"hand_id":str(hid)})
                    return
                frames=core.PPPBuilder(h,self.scale).synthesize_until_first_hero_turn()
                finish=None
                skip={"pb.UserLoginRSP","pb.EnterRoomRSP","pb.SitDownBRC","pb.SitDownRSP","pb.TotalBuyinBRC"} if self.context_active else set()
                for f in frames:
                    cmd=json.loads(f["msg"])["cmd"]
                    if cmd=="pb.FinishRoundHintRSP": finish=f; continue
                    if cmd in skip: continue
                    if cmd=="pb.CancelWaitBlindBRC":
                        # PPP cancels the hero's wait-for-blind state once after SitDown,
                        # not at the beginning of every subsequent hand.
                        if not self._claim_wait_blind_cancel():continue
                    if cmd=="pb.RoundHintMultipleTableRSP":
                        self._clear_cc_queue(); self.manual_action_event.clear()
                    if cmd=="pb.TotalBuyinBRC":
                        total=self._dynamic_total_buyin(h)
                        f=core.PPPBuilder(h,self.scale)._outer(cmd,core.p_int(1,h.hero_id)+core.p_int(2,total)+core.p_int(3,0))
                        pending_room=self.table_to_room.get(h.table_id,self.active_hook_room)
                        if pending_room is not None:self.pending_buyin_by_room.pop(pending_room,None)
                        log("STATE",f"dynamic TotalBuyin={total}")
                    await self.eye_send_outer(f,cmd)
                    if self.frame_delay: await asyncio.sleep(self.frame_delay)
                self.context_active=True; self.context_table_id=h.table_id; self.context_hook_room=self.table_to_room.get(h.table_id,self.active_hook_room)
                self.cold_hands.add(hid); self.finish_frame_by_table[h.table_id]=finish
                self.state["_pending_finish_hint"]=h.table_id
                log("HAND",f"hint hand={hid} hero={h.hero_name} coinSeat={h.hero_seat}->ppp={h.ppp_hero_seat} cards={h.cards}")
                self._diagnostic("cards","hero hole cards captured",{"cards":h.cards,"hand_id":str(hid)})
                self._diagnostic("hint_sent",f"cold hint sent table={h.table_id} room={self.active_hook_room}")
                await self._wait_cc_and_schedule()
            except Exception as e:
                log("HAND",f"cold hint error: {type(e).__name__}: {e}")
                snapshot=self._error_snapshot(reason="COLD_HINT_ERROR")
                self._diagnostic("hint_error",f"cold hint failed: {type(e).__name__}: {e}",{"telemetry":snapshot})
                try:
                    fb=self.autoplay.schedule_failsafe(self.state,reason="COLD_HINT_ERROR")
                    self._diagnostic("fallback_ready","hint failed; safety action queued",{
                        "action":fb.get("action"),"attempt":1,
                        "max_attempts":self.action_max_attempts,
                        "reason":"COLD_HINT_ERROR",
                    })
                except Exception as fallback_error:
                    self._diagnostic("failsafe_unavailable",f"hint failed and no CHECK/FOLD is legal: {fallback_error}",{"telemetry":snapshot})

    async def start_incremental_hint(self,event:dict,payload:Any,raw:bytes,data:dict):
        async with self.hint_lock:
            if not self.current_hand:return
            # Refresh autoplay turn info and build notify from live Coin options.
            self.autoplay.observe(event,payload,raw,self.state)
            h=self.current_hand; hero=h.ppp_hero_seat
            put=self.street_contrib.get(hero,0); current=max(self.street_contrib.values(),default=0)
            call=max(0,current-put)
            covers=[self.street_contrib.get(other-1,0)+self.remaining_stack.get(other-1,0)
                    for other in self.active_seats if other and other-1!=hero]
            mx=max(0,(max(covers) if covers else put)-put)
            can=max(1,len(self.active_seats-self.all_in_seats))
            mn=0 if can<=1 else max(0,current+max(1,self.last_full_raise)-put)
            notify=core.p_int(1,hero)+core.p_int(2,call)+core.p_int(3,mn)+core.p_int(4,mx)+core.p_int(5,can)
            await self.eye_send_cmd("pb.ActionNotifyBRC",notify)
            self._clear_cc_queue(); self.manual_action_event.clear()
            derived_seats=max([int(r.get("seatId") or 0) for r in h.roster] or [0])
            p=h.room.props; profile=core.protocol_profile(p)
            hint=b"".join([
                (core.p_int(1,profile.hint_roomtype) if profile.hint_roomtype is not None else b""),
                core.p_int(2,self._wire_small_blind(h)),
                core.p_int(3,core.money(h.pre.get("anteAmount") or p.get("ante",0),self.scale)), core.p_int(4,core.ppp_table_id(h.table_id)),
                core.p_int(5,core.PPP_CLUB_ID),
                core.p_int(7,int(data.get("turnTime") or p.get("playerHandTime") or 15)),
                core.p_str(8,h.room.room_name),
                (core.p_str(9,core.PPP_CLUB_NAME) if core.PPP_CLUB_NAME else b""),
                core.p_int(10,int(p.get("maxSize") or derived_seats)), core.p_int(11,core.PPP_ROOM_MODE),
                core.p_int(12,profile.room_type), core.p_int(13,profile.game_mode),
                (core.p_int(14,profile.hint_ppsrid) if profile.hint_ppsrid is not None else b""),
                core.p_int(15,0),
            ])
            await self.eye_send_cmd("pb.RoundHintMultipleTableRSP",hint)
            self.state["_pending_finish_hint"]=h.table_id
            log("HAND",f"incremental hint hand={self.state.get('hand_id')} call={call} min={mn} max={mx}")
            self._diagnostic("hint_sent",f"incremental hint sent table={h.table_id} room={self.context_hook_room or self.active_hook_room}")
            await self._wait_cc_and_schedule()

    async def _wait_cc_and_schedule(self):
        # One table bridge has one hint at a time. Flush pre-existing CC so a late
        # answer from a timed-out hint can never become the next turn's action.
        while True:
            try:self.cc_queue.get_nowait()
            except asyncio.QueueEmpty:break
        cc_task=asyncio.create_task(self.cc_queue.get())
        manual_task=asyncio.create_task(self.manual_action_event.wait())
        self.awaiting_cc=True
        # Never let the backend consume the whole Coin turn.  Seven seconds is
        # only the ceiling; short/already-running turns reserve a fallback window.
        timeout_s=self.cc_timeout_seconds
        try:
            room=self.context_hook_room or self.active_hook_room
            turn=self.autoplay.turn_by_room.get(int(room),{}) if room is not None else {}
            observed=float(turn.get("_observed_monotonic") or 0.0)
            total=float(turn.get("turnTime") or 0.0)
            if observed>0 and total>0:
                remaining=max(0.0,observed+total-time.monotonic())
                timeout_s=min(timeout_s,max(0.75,remaining-self.cc_fallback_margin_seconds))
        except (TypeError,ValueError):
            pass
        try:
            done,_=await asyncio.wait(
                {cc_task,manual_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                message=f"timeout {timeout_s:.1f}s; PokerEYE produced no CC"
                log("CC",message)
                snapshot=self._error_snapshot(reason="CC_TIMEOUT")
                self._hand_cc_failed=True
                self._diagnostic("cc_timeout",message,{
                    "timeout_s":timeout_s,
                    "telemetry":snapshot,
                })
                # Close the stale hint before creating any safety action. This keeps
                # a late backend CC from surviving into the next hero turn.
                try:await self.finish_hint(self.state.get("_pending_finish_hint"))
                except Exception as e:
                    self._diagnostic("finish_hint_error",f"finish after CC timeout failed: {e}")
                try:
                    fb=self.autoplay.schedule_failsafe(self.state,reason="CC_TIMEOUT")
                    self._diagnostic(
                        "fallback_ready",
                        "PokerEYE did not answer; timeout safety action queued",
                        {"action":fb.get("action"),"attempt":1,
                         "max_attempts":self.action_max_attempts,
                         "reason":"CC_TIMEOUT"},
                    )
                except Exception as e:
                    self._diagnostic(
                        "failsafe_unavailable",
                        f"PokerEYE did not answer and no CHECK/FOLD failsafe is legal: {e}",
                        {"telemetry":snapshot},
                    )
                return
            if manual_task in done and self.manual_action_event.is_set():
                log("CC","manual hero action observed before cc; releasing hint lock")
                return
            z=cc_task.result()
            try:d=json.loads(z.get("data","{}")) if isinstance(z.get("data"),str) else (z.get("data") or {})
            except Exception:d={}
            try:
                profile=self._restore_active_money_profile()
                msg=self.autoplay.schedule_cc(d,self.state)
                mapping=self._quantize_pending_cc_amount(d,profile)
                log("CC",msg+(f"; {mapping}" if mapping else ""))
                pending=dict(self.autoplay.pending or {})
                self._hand_cc_failed=False
                self.cc_miss_streak=0
                self._diagnostic("action_ready","PokerEYE action mapped and queued for device arbiter",{
                    "action":str(pending.get("action") or ""),
                    "amount":pending.get("display_amount"),
                    "delay_ms":int(pending.get("delay_ms") or 0),
                    "attempt":1,
                    "max_attempts":self.action_max_attempts,
                })
                if self.broadcast:
                    b={"data":"","msg":json.dumps({"action":"show_message","message":str(d.get("message") or ""),"time":int(d.get("lifetime") or 4000)},separators=(",",":")),"packageName":core.PPP_PACKAGE,"tag":"broadcast"}
                    await self.eye_send_outer(b,"broadcast")
            except Exception as e:
                with self.autoplay.lock:self.autoplay.pending=None
                message=f"cannot map action; pending cancelled: {e}"
                snapshot=self._error_snapshot(reason="CC_MAPPING_ERROR")
                log("CC",message)
                self._hand_cc_failed=True
                self._diagnostic("cc_mapping_error",message,{"telemetry":snapshot})
                try:
                    fb=self.autoplay.schedule_failsafe(self.state,reason="CC_MAPPING_ERROR")
                    self._diagnostic("fallback_ready","PokerEYE action could not be mapped; timeout safety action queued",{
                        "action":fb.get("action"),"attempt":1,
                        "max_attempts":self.action_max_attempts,
                        "reason":"CC_MAPPING_ERROR",
                    })
                except Exception as fallback_error:
                    self._diagnostic("failsafe_unavailable",f"mapping failed and no CHECK/FOLD failsafe is legal: {fallback_error}",{"telemetry":snapshot})
        finally:
            self.awaiting_cc=False
            for t in (cc_task,manual_task):
                if not t.done():t.cancel()

    async def finish_hint(self,table_id:Optional[int]):
        if not table_id:return False
        table_id=int(table_id)
        pending=self.state.get("_pending_finish_hint")
        if pending is None or int(pending)!=table_id:
            return False
        # Claim before awaiting socket I/O so scheduled, manual and ACK paths can
        # race safely without producing duplicate FinishRoundHintRSP frames.
        self.state["_pending_finish_hint"]=None
        f=self.finish_frame_by_table.get(table_id)
        if f: await self.eye_send_outer(f,"pb.FinishRoundHintRSP")
        else: await self.eye_send_cmd("pb.FinishRoundHintRSP",core.p_int(1,core.ppp_table_id(table_id)))
        return True

    async def send_opponent_turn_notify(self,data:dict):
        """Mirror PPP turn progression: ActionNotifyBRC is emitted BEFORE every actor.

        Cold-start replay synthesizes historical notifies in PPPBuilder. This method
        handles later streets/turns live after current_hand already exists.
        """
        if not self.current_hand or not isinstance(data,dict): return
        name=str(data.get("whoseTurn") or "")
        if not name or name==str(self.state.get("user_name") or ""): return
        row=next((r for r in self.current_hand.roster if str(r.get("userName") or "")==name),None)
        if not row:return
        seat1=int(row.get("seatId") or 0); seat=seat1-1
        if self.pending_actor_seat==seat:return
        fp=(str(self.state.get("hand_id")),seat1,str(data.get("initTimeStamp") or ""))
        if fp in self.notify_seen:return
        self.notify_seen.add(fp)
        put=self.street_contrib.get(seat,0); current=max(self.street_contrib.values(),default=0)
        call=max(0,current-put)
        covers=[self.street_contrib.get(other-1,0)+self.remaining_stack.get(other-1,0)
                for other in self.active_seats if other and other!=seat1]
        max_put=max(0,(max(covers) if covers else put)-put)
        can=max(1,len(self.active_seats-self.all_in_seats))
        min_put=0 if can<=1 else max(0,current+max(1,self.last_full_raise)-put)
        body=core.p_int(1,seat)+core.p_int(2,call)+core.p_int(3,min_put)+core.p_int(4,max_put)+core.p_int(5,can)
        await self.eye_send_cmd("pb.ActionNotifyBRC",body)
        self.pending_actor_seat=seat
        log("TURN",f"notify opponent seat={seat} name={name} call={call} min={min_put} max={max_put} can={can}")

    def _row_for_seat(self,seat1:int) -> dict:
        if self.current_hand:
            row=next((r for r in self.current_hand.roster if int(r.get("seatId") or 0)==seat1),None)
            if row:return row
        return self.seat_map.get(seat1,{})

    def _uid_for_seat(self,seat1:int) -> int:
        return int(self._row_for_seat(seat1).get("userId") or 0)

    @staticmethod
    def _hand_type(*values:Any) -> int:
        for value in values:
            key=re.sub(r"[^A-Z]+"," ",str(value or "").upper()).strip()
            if key in HAND_TYPE:return HAND_TYPE[key]
        return 0

    def _cache_show_hole_cards(self,data:dict) -> bool:
        """Freeze Coin's exact showdown cards, deduping show/reveal twins."""
        card_map=data.get("userCardListMap") if isinstance(data,dict) else None
        if not isinstance(card_map,dict):return False
        changed=False
        for key,cards in card_map.items():
            try:seat1=int(key)
            except (TypeError,ValueError):continue
            if seat1<=0 or not isinstance(cards,list) or not cards:continue
            clean=[card for card in cards if isinstance(card,dict) and card.get("suit") and card.get("value")]
            if not clean:continue
            encoded=tuple(core.ppp_card(card) for card in clean[:6])
            if tuple(self.show_hole_cards.get(seat1) or ())!=encoded:
                self.show_hole_cards[seat1]=encoded; changed=True
        return changed

    async def _send_show_hand(self,winner_seats:Optional[set[int]]=None,*,win_rate=False) -> bool:
        """Emit the aggregate PPP showdown packet once per hand."""
        if self.show_hand_sent or not self.show_hole_cards:return False
        body=bytearray()
        for seat1,cards in sorted(self.show_hole_cards.items()):
            info=bytearray(core.p_int(1,seat1-1))
            for field,card in enumerate(cards,start=2):
                if field>7:break
                info+=core.p_int(field,card)
            body+=core.p_msg(1,bytes(info))
        for seat1 in sorted(winner_seats or set()):
            if seat1>0:body+=core.p_int(2,seat1-1)
        body+=core.p_int(3,1 if win_rate else 0)
        # New-wire field 4 is IsDoingEvchop, not a game-family/runout flag.
        # Win-rate ShowHand carries the explicit boolean; normal showdown omits it.
        if win_rate:body+=core.p_int(4,1 if self.is_doing_evchop else 0)
        await self.eye_send_cmd("pb.ShowHandRSP",bytes(body))
        self.show_hand_sent=True
        self.runout_closed=True
        log("RESULT",f"show-hand seats={sorted(self.show_hole_cards)} winRate={bool(win_rate)}")
        return True

    @staticmethod
    def _winner_board_index(data:dict) -> int:
        """Map Coin's primary/RIT/double-board winner fragment to PPP BoardIndex."""
        for key in ("boardIndex","runIndex","runId"):
            value=data.get(key)
            if isinstance(value,(int,float)) and int(value)>=0:return int(value)
        if data.get("rit") is True or data.get("isRit") is True:return 1
        if data.get("rit2") is True or data.get("isRit2") is True:return 2
        if data.get("doubleBoard") is True or data.get("isDoubleBoard") is True:return 1
        return 0

    def _winner_pool_groups(self,pools:list[int]) -> list[tuple[int,list[int]]]:
        """Build WinnerRSP field 8 from gross contribution tiers, not live seats."""
        contributions={int(seat):int(value) for seat,value in self.hand_contrib.items() if int(value)>0}
        if not contributions or not pools:return []
        # Folded short contributions belong to the main pot but do not create a side
        # pot. Logical caps come only from players still showdown-eligible (all-ins stay
        # active); every contributor reaching a cap is nevertheless listed in its group.
        eligible_levels=sorted(set(contributions[seat1-1] for seat1 in self.active_seats
                                   if seat1>0 and contributions.get(seat1-1,0)>0))
        if len(pools)>1 and len(eligible_levels)!=len(pools):return []
        groups=[]
        for pool_id in range(len(pools)):
            threshold=1 if pool_id==0 else eligible_levels[pool_id]
            uids=[]
            for seat,value in sorted(contributions.items()):
                if value<threshold:continue
                uid=self._uid_for_seat(seat+1)
                if uid and uid not in uids:uids.append(uid)
            if uids:groups.append((pool_id,uids))
        return groups

    def _winner_payout_adjustments(self) -> dict[int,int]:
        """Allocate the outstanding normalized-SB delta across main-pot winners."""
        delta=self._outstanding_forced_adjustment()
        indexes=[index for index,item in enumerate(self.last_winner_info)
                 if int(item.get("potId") or 0)==0]
        if not delta or not indexes:return {}
        weights={index:max(0,core.money(self.last_winner_info[index].get("winAmountFromPot",0),self.scale))
                 for index in indexes}
        total=sum(weights.values()); sign=1 if delta>0 else -1; magnitude=abs(delta)
        if total<=0:
            base,remainder=divmod(magnitude,len(indexes))
            return {index:sign*(base+(1 if offset<remainder else 0))
                    for offset,index in enumerate(indexes)}
        allocated={index:(magnitude*weights[index])//total for index in indexes}
        remainder=magnitude-sum(allocated.values())
        order=sorted(indexes,key=lambda index:((magnitude*weights[index])%total,-index),reverse=True)
        for index in order[:remainder]:allocated[index]+=1
        return {index:sign*value for index,value in allocated.items()}

    def _pot_info_pools(self,data:dict) -> list[int]:
        """Aggregate Coin board shares into PPP logical pot layers (gross, pre-rake)."""
        boards=[]
        for key in ("potAmountList","potAmountListRit","potAmountListRit2","potAmountListDoubleBoard"):
            values=data.get(key)
            if isinstance(values,list) and values:boards.append([core.money(x,self.scale) for x in values])
        if boards:
            pools=[]
            for index in range(max(map(len,boards))):
                pools.append(sum(values[index] for values in boards if index<len(values)))
            if any(pools):return self._normalize_gross_pools(pools)
        total=core.money(data.get("totalPotAmount",0),self.scale)
        return self._normalize_gross_pools([total]) if total>0 else []

    def _contribution_pools(self) -> list[int]:
        """Reconstruct gross main/side-pot layers from final Coin contributions."""
        contributions={int(seat):int(value) for seat,value in self.hand_contrib.items()
                       if int(value)>0}
        if not contributions:return []
        # Folded short money belongs to the main pot but must not create a side-pot
        # boundary. Only showdown-eligible (including all-in) contribution caps do.
        levels=sorted(set(contributions[seat1-1] for seat1 in self.active_seats
                          if seat1>0 and contributions.get(seat1-1,0)>0))
        if not levels:return []
        previous=0; pools=[]
        for level in levels:
            layer=sum(max(0,min(value,level)-previous)
                      for value in contributions.values())
            if layer>0:pools.append(layer)
            previous=level
        return pools

    async def _send_round_over(self,pools:list[int],round_name:str) -> bool:
        pools=[max(0,int(value)) for value in pools]
        if not pools:return False
        name=str(round_name or "UNKNOWN").upper()
        key=(str(self.state.get("hand_id") or ""),name,tuple(pools))
        if key in self.round_over_seen:return False
        await self.eye_send_cmd("pb.RoundOverBRC",b"".join(core.p_int(1,value) for value in pools))
        self.round_over_seen.add(key); self.last_gross_pools=list(pools); self.last_round_name=name
        log("RESULT",f"round-over street={name} gross={pools}")
        return True

    async def _queue_round_over(self,pools:list[int],round_name:str):
        """Hold a Coin boundary until an optional ChipsBack has been forwarded.

        Coin reports ``isRoundEnd`` before ``return_chips``. PPP reports the same
        facts in the opposite order: Action -> ChipsBack -> RoundOver. The next
        board/showdown/winner event is the unambiguous flush point when no chips
        are returned.
        """
        pools=[max(0,int(value)) for value in pools]
        if not pools:return
        name=str(round_name or "UNKNOWN").upper()
        key=(str(self.state.get("hand_id") or ""),name,tuple(pools))
        if key not in self.round_zoom_seen:
            await self.eye_send_cmd("pb.ZoomFoldBRC",b"")
            self.round_zoom_seen.add(key)
        self.pending_round_over=(pools,name)
        self.last_pools=list(pools)

    async def _flush_pending_round_over(self) -> bool:
        pending=self.pending_round_over
        if pending is None:return False
        self.pending_round_over=None
        pools,name=pending
        contribution_pools=self._contribution_pools()
        if (pools and len(contribution_pools)==len(pools)
                and sum(contribution_pools)==sum(pools)):
            # This is stronger than adding a normalized-SB delta to pool0: if a
            # genuinely short all-in exists, the corrected amount may change the
            # main/side boundary. Final per-seat contributions recover that layering.
            pools=list(contribution_pools); self.last_pools=list(pools)
        # Integer PPP chips cannot preserve addition for every non-integral scale
        # (for example .01*6250 rounds independently while .02*6250 is exact).
        # Actions/stacks are the already-published ledger, so at a terminal boundary
        # absorb only the mathematically bounded rounding residual into the main pot.
        # A larger gap means Coin facts are incomplete (often an unreported return)
        # and must not be papered over.
        contribution_total=sum(max(0,int(value)) for value in self.hand_contrib.values())
        pool_total=sum(max(0,int(value)) for value in pools)
        residual=contribution_total-pool_total
        tolerance=max(1,len([value for value in self.hand_contrib.values() if int(value)>0]))
        if pools and contribution_total>0 and residual and abs(residual)<=tolerance:
            candidate=int(pools[0])+residual
            if candidate>=0:
                pools=list(pools); pools[0]=candidate; self.last_pools=list(pools)
                log("MONEY",f"pot rounding residual={residual:+d} applied hand={self.state.get('hand_id')} gross={pools}")
        sent=await self._send_round_over(pools,name)
        # A duplicate source event still denotes the same already-open boundary.
        self.round_boundary_open=True
        return sent

    async def _send_settlement_round_over(self,pools:list[int]):
        if self.settlement_round_over_sent or not pools:return
        stage=next((name for name in ("RIVER","TURN","FLOP") if name in self.emitted_primary_stages),"PREFLOP")
        await self._queue_round_over(pools,stage)
        await self._flush_pending_round_over()
        self.settlement_round_over_sent=True

    async def incremental_event(self,cmd:str,data:dict):
        if not self.context_active:return
        try:
            # reset_data is a table/hand boundary and remains meaningful even if hero
            # stood up before the server emitted reset_data. Keep observer state coherent.
            if cmd=="game.reset_data":
                self.autoplay.reset_street(self.context_hook_room or self.active_hook_room)
                self.street_contrib.clear(); self.hand_contrib.clear(); self.forced_adjustment_by_seat.clear(); self.forced_raw_remaining_by_seat.clear(); self.remaining_stack.clear(); self.hand_participants.clear(); self.action_seen.clear(); self.notify_seen.clear(); self.last_full_raise=0; self.street_generation+=1
                self.current_hand=None; self.active_seats.clear(); self.all_in_seats.clear(); self.last_winner_info=[]; self.state["hand_id"]=""; self.state["_pending_finish_hint"]=None
                self.emitted_primary_stages.clear(); self.emitted_second_boards.clear(); self.pending_actor_seat=None
                self.chipsback_refresh_seats.clear(); self.pending_action_ack=None; self.last_pools=[]; self.settlement_round_over_sent=False
                self.round_over_seen.clear(); self.round_zoom_seen.clear(); self.pending_round_over=None; self.round_boundary_open=False; self.runout_closed=False; self.last_round_name=""; self.last_gross_pools=[]
                self.winner_fragments_seen.clear(); self.winner_pot_gross={}; self.winner_rsp_sent=False; self.show_hole_cards={}; self.show_hand_sent=False; self.is_doing_evchop=False
                self.rabbit_cards=[]; self.rabbit_second_board=[]; self.rabbit_finish_stage=None
                log("RESULT","hand reset; next hero hand will rebuild DealerInfo/positions/cards")
                if self._hero_turn_this_hand and self._hand_cc_failed:
                    self.cc_miss_streak=int(self.cc_miss_streak or 0)+1
                    self._diagnostic("cc_miss_hand",f"PokerEYE silent hand streak={self.cc_miss_streak}",{
                        "streak":self.cc_miss_streak,
                    })
                    if self.cc_miss_streak>=3:
                        self._diagnostic(
                            "cc_streak_standup",
                            "PokerEYE silent 3 hands; standup and leave",
                            {"streak":self.cc_miss_streak},
                        )
                        self.hero_departing=True
                elif self._hero_turn_this_hand:
                    self.cc_miss_streak=0
                self._hero_turn_this_hand=False
                self._hand_cc_failed=False
                if self.deferred_seat_snapshot is not None:
                    snap=self.deferred_seat_snapshot; self.deferred_seat_snapshot=None
                    await self.apply_seat_snapshot(snap)
                return
            if not self.current_hand:return
            if cmd=="game.ev_chop_opted_action":
                value=data.get("optedForEVChop",data.get("isDoingEvchop"))
                if isinstance(value,bool):self.is_doing_evchop=value
                elif isinstance(value,(int,float)):self.is_doing_evchop=bool(value)
                elif isinstance(value,str) and value.lower() in ("true","false","1","0"):
                    self.is_doing_evchop=value.lower() in ("true","1")
                if self.is_doing_evchop:
                    log("RESULT","EV-chop: принят/активирован сервером Coin")
                else:
                    log("RESULT","EV-chop: автоматически отклонён сервером Coin")
                return
            if cmd=="game.potInfo":
                if data.get("isRoundEnd") is True:
                    pools=self._pot_info_pools(data)
                    if pools:
                        await self._queue_round_over(pools,str(data.get("roundName") or "UNKNOWN"))
                        if self.show_hole_cards and not self.show_hand_sent and "RIVER" not in self.emitted_primary_stages:
                            await self._flush_pending_round_over()
                            await self._send_show_hand(win_rate=True)
                return
            if cmd=="game.seat":
                seat1=int(data.get("seatId") or 0); coin_action=normalized_coin_seat_action(data)
                if not seat1 or not coin_action:return
                action=coin_action
                seat=seat1-1; old=self.street_contrib.get(seat,0)
                if coin_action=="SB":total=self._wire_small_blind()
                elif coin_action=="BB":total=self._wire_big_blind()
                else:total=core.money(data.get("betAmout",data.get("betAmount",0)),self.scale)
                paid_action=coin_action in ("CALL","RAISE","BET","ALLIN","SB","BB","ANTE","FORCE_BB","STRADDLE")
                if paid_action and total<old:
                    # A same-street contribution cannot move backwards.  Coin uses
                    # zero-bet INUSE refreshes after actions/chips-back; those are
                    # state snapshots, not new villain actions.
                    log("STATE",f"stale seat action suppressed seat={seat} action={coin_action} total={total} old={old}")
                    return
                if (seat in self.remaining_stack and paid_action):
                    total=min(max(old,total),old+max(0,int(self.remaining_stack.get(seat,0))))
                current_before=max(self.street_contrib.values(),default=0)
                action=core.ppp_action_for_state(action,current_before,total,self.last_full_raise)
                if action not in ACTION:return
                # Coin emits a seat refresh immediately after game.return_chips, carrying
                # the player's PREVIOUS lastAction but no new bet amount. It is not a new
                # poker action and must not become a duplicate PPP ActionBRC.
                if seat1 in self.chipsback_refresh_seats:
                    self.chipsback_refresh_seats.discard(seat1)
                    log("STATE",f"seat={seat1-1} post-ChipsBack refresh suppressed action={action}")
                    return
                if seat1==int(self.state.get("hero_seat") or 0) and self.pending_action_ack:
                    expected=str(self.pending_action_ack.get("action") or "").upper()
                    if action==expected or {action,expected}=={"BET","RAISE"}:
                        ack=dict(self.pending_action_ack)
                        log("ACTION_ACK",f"Coin server confirmed hero {action} hand={self.state.get('hand_id')}")
                        self._diagnostic("action_confirmed","Coin seat state confirmed action",{
                            "action":str(ack.get("action") or action),
                            "amount":ack.get("display_amount"),
                            "attempt":1+int(ack.get("retries") or 0),
                            "token":str(ack.get("token") or ""),
                        })
                        self.pending_action_ack=None
                fp=(str(self.state.get("hand_id")),int(self.street_generation),seat1,action,int(total),str(data.get("userChips")))
                if fp in self.action_seen:return
                delta=max(0,total-old) if action in ("CALL","RAISE","BET","SB","BB","ANTE","FORCE_BB","STRADDLE") else 0
                tracked_remaining=seat in self.remaining_stack
                available_before=max(0,int(self.remaining_stack.get(seat,0)))
                expected_remaining=max(0,available_before-delta)
                will_absorb=(coin_action in ("CALL","RAISE","BET","ALLIN","ALL-IN","ALL_IN")
                             and total>=old)
                snapshot=None
                if data.get("userChips") is not None:
                    adjustment_after=0 if will_absorb else int(self.forced_adjustment_by_seat.get(seat,0))
                    snapshot=max(0,core.money(data.get("userChips"),self.scale)-adjustment_after)
                    if tracked_remaining and snapshot!=expected_remaining:
                        raise RuntimeError(
                            f"Coin stack/action mismatch seat={seat} before={available_before} "
                            f"delta={delta} expected={expected_remaining} snapshot={snapshot}")
                if action not in ("SB","BB","ANTE","FORCE_BB","STRADDLE") and self.pending_actor_seat!=seat1-1:
                    # Some Coin streams omit or race the preceding opponent user_turn.
                    # PPP's state machine nevertheless requires notify -> action.
                    row=next((r for r in self.current_hand.roster if int(r.get("seatId") or 0)==seat1),None)
                    if row and str(row.get("userName") or "")!=str(self.state.get("user_name") or ""):
                        await self.send_opponent_turn_notify({"whoseTurn":row.get("userName"),"initTimeStamp":f"fallback-{fp}"})
                self.action_seen.add(fp)
                if action in ("RAISE","BET"):
                    inc=max(0,total-current_before)
                    if inc>=max(1,self.last_full_raise): self.last_full_raise=inc
                if action in ("CALL","RAISE","BET","SB","BB","ANTE","FORCE_BB","STRADDLE"):
                    self.street_contrib[seat]=max(old,total); self.hand_contrib[seat]+=delta; self.hand_participants.add(seat1)
                if will_absorb:
                    self._absorb_forced_adjustment(seat)
                self.remaining_stack[seat]=(expected_remaining if tracked_remaining
                                            else (snapshot if snapshot is not None else expected_remaining))
                if action=="FOLD": self.active_seats.discard(seat1); self.all_in_seats.discard(seat1)
                elif self.remaining_stack[seat]<=0:self.all_in_seats.add(seat1)
                remain=self.remaining_stack[seat]
                body=core.p_int(1,seat)+core.p_int(2,ACTION[action])+core.p_int(3,delta)+core.p_int(4,remain)
                # A hint is a request/response lifecycle, not merely a bot-action
                # timer.  If the backend stays silent and Coin times the hero out
                # (or the user acts manually), PPP still closes the hint before the
                # resulting hero ActionBRC.  Leaving it open poisons the next street
                # and produces GAME_IS_BROKEN at RoundStart.
                if seat1==int(self.state.get("hero_seat") or 0):
                    pending_hint=self.state.get("_pending_finish_hint")
                    if pending_hint is not None:
                        await self.finish_hint(pending_hint)
                await self.eye_send_cmd("pb.ActionBRC",body)
                self.pending_actor_seat=None
                log("STATE",f"seat={seat} {action} +{delta} remain={remain}")
            elif cmd=="game.dealer_cards":
                # Normal streets flush here. In an all-in/RIT runout, cached
                # ShowHand + potInfo has already flushed the single terminal pair.
                await self._flush_pending_round_over()
                blind=self._wire_small_blind()
                dc=data.get("dealerCards") or {}
                hand_card=core.PPPBuilder(self.current_hand,self.scale)._hand_card_rsp()
                # A single Coin update may contain every board street. Emit all missing
                # PPP stages in order. Every official postflop stage is preceded by a
                # gross RoundOver and embeds the current HandCard payload in field 6.
                for stage in ("FLOP","TURN","RIVER"):
                    cards=dc.get(stage) if isinstance(dc,dict) else None
                    if not isinstance(cards,list) or not cards or stage in self.emitted_primary_stages:continue
                    previous={"FLOP":"PREFLOP","TURN":"FLOP","RIVER":"TURN"}[stage]
                    if not self.round_boundary_open and not self.runout_closed:
                        # Do not invent a boundary: Coin's isRoundEnd potInfo is the
                        # authoritative signal. All-in runouts intentionally have one
                        # RoundOver + ShowHand followed by several RoundStart packets.
                        log("STATE",f"street={stage} arrived without isRoundEnd potInfo ({previous})")
                    self.round_boundary_open=False
                    self.autoplay.reset_street(self.context_hook_room or self.active_hook_room)
                    self.street_contrib.clear(); self.notify_seen.clear(); self.pending_actor_seat=None
                    self.action_seen.clear(); self.street_generation+=1
                    self.last_full_raise=max(1,self._wire_big_blind())
                    body=core.p_int(1,STAGE[stage])+b"".join(core.p_int(2,core.ppp_card(c)) for c in cards)+core.p_int(3,blind)+core.p_msg(6,hand_card)
                    await self.eye_send_cmd("pb.RoundStartBRC",body)
                    if self.runout_closed:await self.eye_send_cmd("pb.ZoomFoldBRC",b"")
                    self.emitted_primary_stages.add(stage); log("STATE",f"street={stage} cards={cards}")
                # RoundStartBRC field 5 is SecondBoard.  Coin repeats the primary river
                # when RIT resolves; dedupe it and emit only the newly revealed board.
                for key in ("dealerCardsRit","dealerCardsDoubleBoard","dealerCardsRit2"):
                    extra=data.get(key) or {}
                    if not isinstance(extra,dict):continue
                    for stage in ("FLOP","TURN","RIVER"):
                        cards=extra.get(stage)
                        if not isinstance(cards,list) or not cards:continue
                        fp=(key,stage,tuple(core.ppp_card(c) for c in cards))
                        if fp in self.emitted_second_boards:continue
                        body=core.p_int(1,STAGE[stage])+b"".join(core.p_int(5,core.ppp_card(c)) for c in cards)+core.p_int(3,blind)+core.p_msg(6,hand_card)
                        await self.eye_send_cmd("pb.RoundStartBRC",body)
                        if self.runout_closed:await self.eye_send_cmd("pb.ZoomFoldBRC",b"")
                        self.emitted_second_boards.add(fp)
                        log("STATE",f"second-board source={key} street={stage} cards={cards}")
            elif cmd=="game.return_chips":
                seat1=int(data.get("seatId") or 1); seat=seat1-1
                chips,released=self._wire_chipsback_amount(seat,data.get("chipsToReturn",0))
                if released and self.pending_round_over is not None:
                    pools,name=self.pending_round_over
                    pools=list(pools)
                    if pools:pools[0]=max(0,int(pools[0])-released)
                    self.pending_round_over=(pools,name); self.last_pools=list(pools)
                # Working PPP capture sends only Seatid + Chips here.
                await self.eye_send_cmd("pb.ChipsBackBRC",core.p_int(1,seat)+core.p_int(2,chips))
                self.street_contrib[seat]=max(0,self.street_contrib.get(seat,0)-chips)
                self.hand_contrib[seat]=max(0,self.hand_contrib.get(seat,0)-chips)
                self.remaining_stack[seat]+=chips
                if self.remaining_stack[seat]>0:self.all_in_seats.discard(seat1)
                self.chipsback_refresh_seats.add(seat1)
                log("RESULT",f"chips-back seat={seat} chips={chips}")
                # Exact PPP order: ChipsBackBRC, then the terminal RoundOverBRC.
                if not self.forced_raw_remaining_by_seat.get(seat):
                    await self._flush_pending_round_over()
            elif cmd in ("game.show_hole_cards","game.reveal_cards"):
                changed=self._cache_show_hole_cards(data)
                await self._flush_pending_round_over()
                # Coin repeats the same map as reveal_cards. Send only after the
                # street's gross RoundOver so PPP observes the same showdown order.
                if (self.show_hole_cards and self.round_boundary_open and not self.show_hand_sent
                        and "RIVER" not in self.emitted_primary_stages):
                    await self._send_show_hand(win_rate=True)
                elif changed:
                    log("RESULT",f"show-hand cached seats={sorted(self.show_hole_cards)}")
            elif cmd=="game.rabbit_run":
                def collect(board:Any):
                    cards=[]; first=None
                    if isinstance(board,dict):
                        for stage in ("FLOP","TURN","RIVER"):
                            values=board.get(stage)
                            if not isinstance(values,list) or not values:continue
                            if first is None:first=stage
                            cards.extend(core.ppp_card(card) for card in values)
                    return cards,first
                primary,first_primary=collect(data.get("rabbitRunCards"))
                second,first_second=collect(data.get("rabbitRunCardsDoubleBoard"))
                if primary:self.rabbit_cards=primary
                if second:self.rabbit_second_board=second
                first=first_primary or first_second
                if first:self.rabbit_finish_stage=max(STAGE["PREFLOP"],STAGE[first]-1)
                log("RESULT",f"rabbit cards={len(self.rabbit_cards)} second={len(self.rabbit_second_board)} finish={self.rabbit_finish_stage}")
            elif cmd=="game.winnerInfo":
                await self._flush_pending_round_over()
                wl=[]
                for pot in data.get("winnerDataList") or []:
                    pot_id=int(pot.get("potId") or 0)
                    board_index=self._winner_board_index(dict(data,**pot))
                    gross=core.money(pot.get("potAmount",pot.get("potAmountAfterRake",0)),self.scale)
                    payout=core.money(pot.get("potAmountAfterRake",pot.get("potAmount",0)),self.scale)
                    wd=(pot.get("winnerDetails") or {}).get("winnerList") or []
                    pot_fp=(board_index,pot_id,gross,payout,tuple(
                        (int(x.get("seatId") or 0),str(x.get("actualWinAmount")),str(x.get("winAmountFromPot")))
                        for x in wd if isinstance(x,dict)))
                    if pot_fp in self.winner_fragments_seen:continue
                    self.winner_fragments_seen.add(pot_fp)
                    self.winner_pot_gross[(board_index,pot_id)]=gross
                    for x in wd:
                        best=x.get("userBestHand") if isinstance(x.get("userBestHand"),dict) else {}
                        item={"seatId":int(x.get("seatId") or 1),"playerName":x.get("playerName"),
                              "actualWinAmount":x.get("actualWinAmount",0),"winAmountFromPot":x.get("winAmountFromPot",0),
                              "potId":pot_id,"boardIndex":board_index,"handRank":best.get("handRank"),
                              "winnersHandInfo":(pot.get("winnerDetails") or {}).get("winnersHandInfo")}
                        self.last_winner_info.append(item)
                        wl.append((board_index,item["seatId"],item["playerName"],item["actualWinAmount"],item["winAmountFromPot"]))
                # winnerInfo can arrive once per RIT board. Do not close or settle on a
                # partial fragment; cumulativeWinnerInfo is Coin's atomic completion.
                if self.winner_pot_gross and (not self.last_pools or not (self.round_boundary_open or self.runout_closed)):
                    max_pool=max(pool_id for _,pool_id in self.winner_pot_gross)
                    raw_pools=[sum(value for (board,pid),value in self.winner_pot_gross.items() if pid==pool_id)
                               for pool_id in range(max_pool+1)]
                    self.last_pools=self._normalize_gross_pools(raw_pools)
                log("RESULT",f"winnerInfo={wl}")
            elif cmd=="game.transaction_winnings":
                log("RESULT",f"transaction_winnings currentChips={data.get('currentChips')} active={data.get('isTransactionWinnings')}")
            elif cmd=="game.cumulativeWinnerInfo":
                if self.winner_rsp_sent:
                    log("RESULT","duplicate cumulativeWinnerInfo suppressed")
                    return
                await self._flush_pending_round_over()
                winners=data.get("winnersData") or []; body=b""
                if not self.last_pools:
                    if self.winner_pot_gross:
                        max_pool=max(pool_id for _,pool_id in self.winner_pot_gross)
                        raw_pools=[sum(value for (board,pid),value in self.winner_pot_gross.items() if pid==pool_id)
                                   for pool_id in range(max_pool+1)]
                        self.last_pools=self._normalize_gross_pools(raw_pools)
                    if not self.last_pools:
                        fallback=core.money(data.get("cumulativePotAmount",data.get("cumulativePotAmountWithoutRake",0)),self.scale)
                        if fallback:self.last_pools=self._normalize_gross_pools([fallback])
                if self.round_boundary_open or self.runout_closed:
                    # terminal potInfo already emitted the exact gross RoundOver;
                    # an all-in runout stays closed across all remaining board cards.
                    self.settlement_round_over_sent=True
                else:
                    await self._send_settlement_round_over(self.last_pools)
                raw_winner_net={int(w.get("seatId") or 1)-1:core.money(w.get("cumulativeProfitLoss",0),self.scale) for w in winners}
                winner_type={int(w.get("seatId") or 1)-1:self._hand_type(w.get("winType"),w.get("winTypeRit"),w.get("winTypeDoubleBoard")) for w in winners}
                winner_seats={int(x.get("seatId") or 0) for x in self.last_winner_info if int(x.get("seatId") or 0)>0}
                await self._send_show_hand(winner_seats,win_rate=False)
                payout_adjustments=self._winner_payout_adjustments()
                payout_by_seat=collections.defaultdict(int); actual_adjusted_seats=set()
                for index,x in enumerate(self.last_winner_info):
                    seat=int(x.get("seatId") or 1)-1
                    adjustment=int(payout_adjustments.get(index,0))
                    payout=core.money(x.get("winAmountFromPot",0),self.scale)+adjustment
                    actual=core.money(x.get("actualWinAmount",0),self.scale)+adjustment
                    if seat not in actual_adjusted_seats:
                        actual-=int(self.forced_adjustment_by_seat.get(seat,0)); actual_adjusted_seats.add(seat)
                    payout_by_seat[seat]+=payout
                    hand_type=self._hand_type(x.get("handRank"),x.get("winnersHandInfo")) or winner_type.get(seat,0)
                    # Fresh PPP wire (newer than dump.cs): ActualWinAmount is field 16;
                    # obsolete HandPoint field 14 is not serialized.
                    win=b"".join([core.p_int(1,seat),core.p_int(2,int(x.get("potId") or 0)),core.p_int(3,payout),
                                  core.p_int(4,hand_type),core.p_int(5,0),core.p_int(6,self._uid_for_seat(seat+1)),
                                  core.p_int(8,0),core.p_int(9,0),core.p_int(10,0),core.p_int(11,0),
                                  core.p_str(12,""),core.p_int(13,int(x.get("boardIndex") or 0)),core.p_int(16,actual)])
                    body+=core.p_msg(1,win)
                profits=compute_net_profits(
                    hand_participants=self.hand_participants,
                    hand_contrib=self.hand_contrib,
                    raw_winner_net=raw_winner_net,
                    payout_by_seat=payout_by_seat,
                    forced_adjustment_by_seat=self.forced_adjustment_by_seat,
                )
                next_session={seat:self.session_profit[seat]+profit for seat,profit in profits.items()}
                for seat in sorted(profits):
                    profit=profits[seat]
                    body+=core.p_msg(2,core.p_int(1,seat)+core.p_int(2,profit)+core.p_int(3,0)+core.p_int(6,0)+core.p_int(7,0)+core.p_int(8,0))
                finish=self.rabbit_finish_stage
                if finish is None:
                    finish=next((STAGE[s] for s in ("RIVER","TURN","FLOP") if s in self.emitted_primary_stages),STAGE["PREFLOP"])
                expected={STAGE["PREFLOP"]:5,STAGE["FLOP"]:2,STAGE["TURN"]:1,STAGE["RIVER"]:0}.get(finish,0)
                primary=self.rabbit_cards if len(self.rabbit_cards)==expected else []
                second=self.rabbit_second_board if len(self.rabbit_second_board)==expected else []
                rabbit=b"".join(core.p_int(1,card) for card in primary)+core.p_int(2,finish)
                rabbit+=b"".join(core.p_int(3,card) for card in second)
                body+=core.p_msg(3,rabbit)
                for seat in sorted(profits):
                    current=core.p_int(1,seat)+core.p_int(2,self._uid_for_seat(seat+1))+core.p_int(3,next_session[seat])
                    body+=core.p_msg(5,current)
                for pool_id,uids in self._winner_pool_groups(self.last_pools):
                    body+=core.p_msg(8,core.p_int(1,pool_id)+b"".join(core.p_int(2,uid) for uid in uids))
                body+=core.p_int(9,4)
                if body:
                    await self.eye_send_cmd("pb.WinnerRSP",body)
                    for seat,value in next_session.items():self.session_profit[seat]=value
                    self.winner_rsp_sent=True
                    log("RESULT",f"winner-rsp payout={[(x.get('boardIndex'),x.get('seatId'),x.get('winAmountFromPot')) for x in self.last_winner_info]} net={profits}")
        except Exception as e:
            log("STATE",f"incremental {cmd} error: {e}")
            self._record_bridge_error("state_error",cmd,e,room=self.context_hook_room or self.active_hook_room)

    def _ensure_protocol_worker(self):
        if self.protocol_task is None or self.protocol_task.done():
            self.protocol_task=asyncio.create_task(self._protocol_worker())

    async def _protocol_worker(self):
        while True:
            item=await self.protocol_queue.get()
            try:
                await self._process_protocol_event(*item)
            except Exception as e:
                event,payload,raw,cmd,room,data=item
                log("PROTO",f"ordered event error: {type(e).__name__}: {e}")
                self._record_bridge_error("protocol_event_error",cmd,e,room=room)
            finally:
                self.protocol_queue.task_done()

    async def _process_protocol_event(self,event:dict,payload:Any,raw:bytes,cmd:str,room:Optional[int],data:dict):
        """Serialize lifecycle, turn notification and action mutation across hook sockets."""
        direction=str(event.get("direction") or "").lower()
        await self._ensure_login_identity()

        if direction=="out" and cmd in ("lobby.join_game_table","lobby.join_game"):
            tid=self._table_from_data(data)
            if tid:self.requested_table_id=tid
            try:self.requested_config_id=int(data.get("configId") or 0)
            except Exception:pass
            self.hero_departing=False; self.deferred_seat_snapshot=None
            if tid:await self._send_enter_room_req(tid)

        if direction=="in" and cmd=="lobby.join_game_table":
            tid=self._remember_join_allocation(data)
            if tid:await self._send_enter_room_req(tid)

        if direction=="in" and cmd=="lobby.join_game":
            try:failed=(data.get("isSuccess") is False or int(data.get("errorCode") or 0)!=0)
            except Exception:failed=data.get("isSuccess") is False
            if failed:
                self.enter_request_table_id=0; self.requested_table_id=0; self.requested_config_id=0
                self.lifecycle_phase="lobby"; log("TABLE","Coin join rejected; PPP pending cleared")

        active_room=self.context_hook_room if self.context_active else self.active_hook_room
        if (direction=="out" and cmd in ("game.reserve_Seat","game.reserve_seat")
                and (room is None or room==active_room)):
            # An explicit reservation is the only safe signal that a seat appearing
            # after StandUp is intentional rather than a delayed pre-stand snapshot.
            if data.get("reserve") is not False:
                self.hero_departing=False; self.deferred_seat_snapshot=None
            else:
                self.hero_departing=True; self.deferred_seat_snapshot=None
        elif (direction=="in" and cmd in ("game.reserve_Seat","game.reserve_seat")
              and (room is None or room==active_room)
              and (data.get("isReserved") is False or int(data.get("errorCode") or 0)!=0)):
            self.hero_departing=not self.hero_sitting; self.deferred_seat_snapshot=None

        if direction=="out" and cmd in ("game.leave_Seat","game.leave_seat"):
            self.hero_departing=True
            if self._hand_in_progress():
                self._diagnostic(
                    "standup_queued",
                    "standup requested; hero stays seated until the current hand ends",
                    {"seat":self.state.get("hero_seat"),"hand_id":self.state.get("hand_id")},
                )
            else:
                self.deferred_seat_snapshot=None
                if self._should_announce_standup_to_eye():
                    await self._send_stand_req()

        if cmd=="game.quit_table":
            target=self.leaving_hook_room if self.lifecycle_phase=="leaving" else (self.context_hook_room if self.context_active else self.active_hook_room)
            if room is None or room==target:
                if direction=="out":
                    if room is not None:self.closing_rooms.add(room)
                    if self.context_active:
                        async with self.context_lock:await self._begin_leave_table_context(room)
                    elif self.lifecycle_phase=="pending":
                        # The Coin user backed out before game_alldata completed the
                        # synthetic EnterRoom transaction.  No PPP room was entered.
                        self.enter_request_table_id=0; self.requested_table_id=0; self.requested_config_id=0
                        self.lifecycle_phase="lobby"
                        if room is not None:self.closing_rooms.discard(room)
                        if room==self.active_hook_room:self.active_hook_room=None
                else:
                    try:success=(data.get("isSuccess") is not False and int(data.get("code") or data.get("errorCode") or 0)==0)
                    except Exception:success=data.get("isSuccess") is not False
                    if success:
                        async with self.context_lock:
                            if self.context_active:
                                await self.leave_table_context("quit_table-ack",send_request=False,emit_stand_request=False)
                        if room is not None:self.closing_rooms.discard(room)
                        if room==self.active_hook_room:self.active_hook_room=None
                    else:
                        async with self.context_lock:self._abort_leave_table_context(room)
            return

        if direction=="in" and cmd in ("game.wait_list_data","game.game_alldata"):
            if room==self.active_hook_room:await self.ensure_observer_context()
            return

        if room==self.active_hook_room and self.context_active and self.context_hook_room!=room and cmd in ("game.seatInfo","game.pre_hand_start_info","game.user_turn"):
            await self.ensure_observer_context()

        if not self._is_active_room(room):
            return

        if direction=="in" and cmd=="game.seatInfo" and isinstance(data,dict):
            await self.apply_seat_snapshot(data)
        elif direction=="in" and cmd=="game.take_Seat" and isinstance(data,dict):
            # seatInfo usually arrives first; this is a fallback if it did not.
            self.hero_departing=False; self.deferred_seat_snapshot=None
            uid=int(data.get("userId") or self.state.get("user_id") or 0)
            if uid==int(self.state.get("user_id") or 0) and data.get("seatId"):
                row=dict(data); row.setdefault("userName",self.state.get("user_name")); row.setdefault("userChips",data.get("buyinAmount",0))
                await self._send_hero_sit(row)
        elif direction=="in" and cmd=="game.leave_Seat" and isinstance(data,dict):
            self.hero_departing=True
            if self._hand_in_progress():
                self._diagnostic(
                    "standup_queued",
                    "Coin standup ACK while hand is live; Eye stay-seated until reset_data",
                    {"seat":data.get("seatId"),"hand_id":self.state.get("hand_id")},
                )
            else:
                self.deferred_seat_snapshot=None
                seat=int(data.get("seatId") or self.state.get("hero_seat") or 0)
                if seat and seat==int(self.state.get("hero_seat") or 0):
                    await self._send_hero_stand(seat)

        if cmd=="game.user_turn" and isinstance(data,dict):
            whose=str(data.get("whoseTurn") or "")
            if self._is_hero_turn(data):
                self._hero_turn_this_hand=True
                if event.get("_hmuriy_duplicate_turn"):
                    self._diagnostic(
                        "turn_refresh",
                        "Coin extraTimer/time-bank refresh; existing hint/action retained",
                        {"timer":str(event.get("_hmuriy_turn_refresh") or data.get("timerName") or "")},
                    )
                else:
                    mode="incremental" if (self.current_hand and self.current_hand.hand_id in self.cold_hands) else "cold"
                    if event.get("_hmuriy_options_from_advance"):
                        options=data.get("userTurnOptions") or {}
                        self._diagnostic(
                            "turn_options_recovered",
                            "legal hero options recovered from preceding game.advance_player_action",
                            {"room":room,"option_codes":sorted(str(k) for k in options)},
                        )
                    self._diagnostic(
                        "hero_turn",
                        f"matched Coin hero turn room={room} mode={mode} hand={self.state.get('hand_id') or '-'}",
                    )
                    if mode=="incremental":
                        await self.start_incremental_hint(event,payload,raw,data)
                    else:
                        await self.start_cold_hint(event,payload,raw)
            elif whose and self.current_hand:
                await self.send_opponent_turn_notify(data)

        if direction=="in" and cmd:
            await self.incremental_event(cmd,data)

    async def health_loop(self):
        while True:
            await asyncio.sleep(5.0)
            bp=self._bombpot_for(self.context_hook_room or self.active_hook_room).state
            log("HEALTH",f"events={self.event_count} phase={self.lifecycle_phase} table={self.context_table_id or '-'} observer={self.context_active} seated={self.hero_sitting} hand={self.state.get('hand_id') or '-'} cc={self.cc_count} inject={self.inject_count} hb={self.heartbeat_count} stateErr={self.state_error_count} protoErr={self.protocol_error_count} eye={'up' if self.eye_w and not self.eye_w.is_closing() else 'down'} actionPending={bool(self.autoplay.pending)} hookAge={time.monotonic()-self.last_hook_rx:.1f}s eyeAge={time.monotonic()-self.last_eye_rx:.1f}s bombpot={bp.is_bombpot_hand}")

    async def handle_event(self,event:dict) -> tuple[dict,Optional[int]]:
        self.event_count+=1; self.last_hook_rx=time.monotonic(); self.events.append(event)
        payload,raw=decode_hook_payload(event); cmd,room,data=cmd_room_data(payload)
        direction=str(event.get("direction") or "").lower()
        if direction=="out" and cmd in ("lobby.join_game_table","lobby.join_game"):
            tid=self._table_from_data(data)
            if tid:self.requested_table_id=tid
            try:self.requested_config_id=int(data.get("configId") or 0)
            except Exception:pass
            self.hero_departing=False; self.deferred_seat_snapshot=None
        active_room=self.context_hook_room if self.context_active else self.active_hook_room
        if (direction=="out" and cmd in ("game.reserve_Seat","game.reserve_seat")
                and (room is None or room==active_room)):
            self.hero_departing=data.get("reserve") is False; self.deferred_seat_snapshot=None
        self._remember_event(event,cmd,room,data)
        try:self._bombpot_for(room).observe(payload)
        except Exception:pass
        obs=self.autoplay.observe(event,payload,raw,self.state)
        decision={"id":event.get("id",""),"action":"forward"}; finish_after=None

        # A scheduled app-side send is still cancellable until its due timestamp.
        # Manual action, hand reset or table leave must never allow a stale bot action
        # to leak into the next turn/hand.
        ack=self.pending_action_ack
        manual_out=direction=="out" and cmd=="game.user_action" and not self.autoplay.is_recent_injected_echo(event,raw)
        if manual_out:
            pending_hint=self.state.get("_pending_finish_hint")
            if pending_hint is not None:
                # The hook is still holding this outgoing Coin action, so awaiting
                # the tiny local PPP frame here guarantees Finish -> hero action
                # ordering even when no SCAction was ever returned.
                await self.finish_hint(pending_hint)
        cancel_reason=None
        if ack and (manual_out or obs.manual_cancel):cancel_reason="manual-action"
        elif ack and obs.cancel_reason:cancel_reason=str(obs.cancel_reason).lower().replace("_","-")
        elif ack and cmd in ("game.reset_data","game.quit_table","game.pre_hand_start_info","lobby.join_game_table","lobby.join_game"):cancel_reason=cmd
        elif ack and direction=="in" and cmd=="game.user_turn":
            whose=str(data.get("whoseTurn") or "")
            if whose and whose!=str(self.state.get("user_name") or ""):
                if time.monotonic()<float(ack.get("due") or 0):cancel_reason="turn-advanced-before-due"
                else:
                    log("ACTION_ACK",f"turn advanced after {ack.get('action')} hand={ack.get('hand_id')}")
                    self._diagnostic("action_confirmed","Coin turn advanced after action",{
                        "action":str(ack.get("action") or ""),"amount":ack.get("display_amount"),
                        "attempt":1+int(ack.get("retries") or 0),"token":str(ack.get("token") or ""),
                    })
                    self.pending_action_ack=None; ack=None
        if ack and cancel_reason:
            token=str(ack.get("token") or "")
            if token:
                decision["cancel_schedule"]=token
                task=self.schedule_finish_tasks.pop(token,None)
                if task:task.cancel()
            finish_after=ack.get("finish_room_id")
            self.pending_action_ack=None
            log("ACTION_CANCEL",f"cancel token={token or '-'} reason={cancel_reason}")
            self._diagnostic("action_cancelled","pending action cancelled",{
                "action":str(ack.get("action") or ""),"amount":ack.get("display_amount"),
                "attempt":1+int(ack.get("retries") or 0),"reason":str(cancel_reason or ""),"token":token,
            })

        # Due EYE action: hijack exactly one outgoing lobby.dummy and replace its
        # payload with the binary SmartFox game.user_action. This follows the same
        # synchronous wsBinary return path already used by the APK, avoiding flaky
        # out-of-band injection and multi-request backpressure.
        inj=await self._maybe_inject_async(event,payload,raw)
        if inj.inject_raw is not None:
            scheduled=inj.schedule_delay_ms is not None
            decision["action"]="schedule_send" if scheduled else "replace"
            decision["text"]=False
            decision["payload_b64"]=base64.b64encode(inj.inject_raw).decode()
            if scheduled:
                decision["delay_ms"]=max(0,int(inj.schedule_delay_ms or 0)); decision["token"]=str(inj.schedule_token or "")
            else:finish_after=inj.finish_room_id
            if inj.target_ws_id and int(event.get("v") or 0)>=6:
                decision["ws_id"]=str(inj.target_ws_id)
                try: decision["_ws_u32"]=int(str(inj.target_ws_id),16)
                except (TypeError,ValueError): pass
            if inj.target_channel_id and int(event.get("v") or 0)>=6:
                decision["_target_channel_id"]=str(inj.target_channel_id)
            decision["_operator_action"]={
                "action":str(inj.action_name or ""),"amount":inj.display_amount,
                "attempt":int(inj.attempt or 1),"max_attempts":self.action_max_attempts,
                "token":str(inj.schedule_token or ""),
            }
            self.inject_count+=1
            try:
                packet=decode_packet(inj.inject_raw); a=json.loads(packet["p"]["p"]["data"]); code=int(a.get("userAction") or 0)
                name={3:"CHECK",4:"CALL",5:"RAISE",7:"FOLD"}.get(code,str(code))
            except Exception:name=""
            if not (self.pending_action_ack and str(inj.log).startswith("retry")):
                now=time.monotonic(); delay=max(0,int(inj.schedule_delay_ms or 0)) if scheduled else 0
                token=str(inj.schedule_token or f"{self.state.get('hand_id')}:{event.get('id')}")
                self.pending_action_ack={"action":str(inj.action_name or name),"raw":inj.inject_raw,"at":now,"due":now+delay/1000.0,
                    "retry_at":now+delay/1000.0+self.action_retry_delay,"retries":0,"hand_id":str(self.state.get("hand_id") or ""),
                    "ws_id":str(inj.target_ws_id or event.get("ws_id") or ""),
                    "channel_id":str(inj.target_channel_id or event.get("_channel_id") or ""),
                    "url":event.get("url"),"token":token,
                    "display_amount":inj.display_amount,"finish_room_id":inj.finish_room_id}
                if scheduled:
                    task=asyncio.create_task(self._finish_hint_after(delay,inj.finish_room_id,token)); self.schedule_finish_tasks[token]=task
            elif self.pending_action_ack:
                # Attempt 2/3 or 3/3 has just been handed to HMN1.  Arm another
                # one-second Coin confirmation deadline.  3/3 expires into one
                # action_exhausted incident, never a fourth send.
                now=time.monotonic(); delay=max(0,int(inj.schedule_delay_ms or 0)) if scheduled else 0
                self.pending_action_ack["due"]=now+delay/1000.0
                self.pending_action_ack["retry_at"]=now+delay/1000.0+self.action_retry_delay
                self.pending_action_ack["_exhausted_reported"]=False
            log("INJECT",f"{'SCHEDULE' if scheduled else 'REPLACE'} lobby.dummy -> {inj.log}; awaiting Coin server ACK")
        elif obs.manual_cancel or obs.cancel_reason:
            finish_after=finish_after or obs.finish_room_id
            log("INJECT",obs.log or f"Coin action cancelled reason={obs.cancel_reason or 'manual-action'}")

        # Fast-path flags are safe here; all stateful PPP emission is handled by one
        # ordered worker so simultaneous table sockets cannot reorder notify/action.
        if direction=="out" and cmd=="game.take_Seat" and isinstance(data,dict) and data.get("buyinAmount") is not None:
            self.hero_departing=False; self.deferred_seat_snapshot=None
            try:self.pending_buyin_by_room[room if room is not None else self.active_hook_room]=float(data.get("buyinAmount"))
            except Exception:pass
        if direction=="out" and cmd=="game.user_action":
            self.manual_action_event.set()
        elif self._lifecycle_aborts_cc(cmd):
            # Release a protocol worker waiting for CC so real table-leave / hand
            # reset cannot sit behind a dead hint. Mid-hand standup is queued by
            # Coin and must not abort the current decision.
            self.manual_action_event.set()
        self._ensure_protocol_worker()
        self.protocol_queue.put_nowait((event,payload,raw,cmd,room,data))

        if self.event_count%250==0:
            bp=self._bombpot_for(self.context_hook_room or self.active_hook_room).state
            log("HEALTH",f"events={self.event_count} phase={self.lifecycle_phase} cc={self.cc_count} inject={self.inject_count} eye={'up' if self.eye_w and not self.eye_w.is_closing() else 'down'} actionPending={bool(self.autoplay.pending)} bombpot={bp.is_bombpot_hand}")
        # Extra health metadata is ignored by existing hook but useful to a ping-capable smali module.
        if event.get("kind") in ("ping","health"):
            decision["bridge_health"]={"ok":self.state_error_count==0 and self.protocol_error_count==0,"events":self.event_count,"eye":bool(self.eye_w and not self.eye_w.is_closing()),"phase":self.lifecycle_phase,"table":self.context_table_id,"observer":self.context_active,"hero_sitting":self.hero_sitting,"hand":self.state.get("hand_id"),"pending_action":bool(self.autoplay.pending),"state_errors":self.state_error_count,"protocol_errors":self.protocol_error_count}
        return decision,finish_after


async def serve(args):
    lh,lp=args.listen.rsplit(":",1)
    direct_proxy=None
    if bool(getattr(args,"direct_backend",False)):
        from pathlib import Path
        from .eye_direct_proxy import DirectBackendProxy, DirectBackendSlot
        slot=DirectBackendSlot(
            account_id=str(args.backend_account),
            credential_file=Path(args.backend_credential_file),
            host=str(args.backend_host),
            port=int(args.backend_port),
        )
        direct_proxy=DirectBackendProxy(slot,logger=log)
        eh,ep=await direct_proxy.start()
        transport_label=f"direct:{args.backend_host}:{args.backend_port} account={args.backend_account}"
    else:
        eh,ep=args.eye.rsplit(":",1); ep=int(ep)
        transport_label=f"eye:{args.eye}"
    bridge=LiveCoinBridge(eh,int(ep),args.chip_scale,args.frame_delay,args.broadcast)
    try:
        await bridge.ensure_eye()
        if direct_proxy is not None:direct_proxy.bind_bridge(bridge)
        asyncio.create_task(bridge.health_loop())
        heartbeat_enabled=not bool(getattr(args,"no_eye_heartbeat",False))
        if heartbeat_enabled:
            asyncio.create_task(bridge.heartbeat_loop())
        async def client(reader,writer):
            peer=writer.get_extra_info("peername")
            try:
                while True:
                    raw=await lp_read(reader)
                    if raw is None:break
                    t0=time.monotonic()
                    try:event=json.loads(raw)
                    except Exception:
                        resp={"id":"","action":"forward","error":"bad_json"}; writer.write(lp_pack(resp)); await writer.drain(); continue
                    decision,finish=await bridge.handle_event(event)
                    writer.write(lp_pack(decision)); await writer.drain()
                    dt=(time.monotonic()-t0)*1000
                    if dt>25: log("HOOK",f"slow response {dt:.1f}ms peer={peer}")
                    if finish: asyncio.create_task(bridge.finish_hint(finish))
            except Exception as e: log("HOOK",f"client {peer} error: {e}")
            finally:
                writer.close()
                try:await writer.wait_closed()
                except:pass
        srv=await asyncio.start_server(client,lh,int(lp),backlog=256,reuse_address=True)
        log("BRIDGE",f"READY hook={args.listen} transport={transport_label} frameDelay={args.frame_delay}s broadcast={args.broadcast}")
        log("BRIDGE",f"observer admission + frozen in-hand roster + per-hand rebuild; EYE heartbeat={'ON' if heartbeat_enabled else 'OFF'}; WinnerRSP=SAFE")
        log("BRIDGE","cc transport: ONE outgoing lobby.dummy -> app-side same-WebSocket schedule -> Coin ACK + one retry")
        async with srv: await srv.serve_forever()
    finally:
        if direct_proxy is not None:
            await direct_proxy.close()


def main():
    ap=argparse.ArgumentParser(description="Coin test-room -> PPP EYE live bridge with automatic cc action injection")
    ap.add_argument("--listen",default="127.0.0.1:18010"); ap.add_argument("--eye",default="127.0.0.1:17770")
    ap.add_argument("--direct-backend",action="store_true",help="connect directly to PokerEYE backend; do not require the EYE app")
    ap.add_argument("--backend-account",default="",help="one PokerEYE account slot (one concurrent table)")
    ap.add_argument("--backend-credential-file",default=r"secrets\eye.agent",help="one-line agent code file")
    ap.add_argument("--backend-host",default="gs.eye-panel.com")
    ap.add_argument("--backend-port",type=int,default=443)
    ap.add_argument("--chip-scale",type=int,default=100); ap.add_argument("--frame-delay",type=float,default=0.01)
    ap.add_argument("--broadcast",action="store_true",help="send show_message broadcast after cc (OFF by default)")
    ap.add_argument("--no-eye-heartbeat",action="store_true",help="disable the PPP-like 3 second HeartBeatREQ/RSP cadence")
    ap.add_argument("--eye-heartbeat",action="store_true",help=argparse.SUPPRESS)
    ap.add_argument("--emit-winner-rsp",action="store_true",help=argparse.SUPPRESS)
    args=ap.parse_args(); asyncio.run(serve(args))
if __name__=="__main__": main()
