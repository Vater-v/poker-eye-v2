from __future__ import annotations
import base64, hashlib, json, os, threading, time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from .coin_action_wire import build_game_user_action_packet, resolve_eye_cc_action

@dataclass
class HookResult:
    inject_raw: Optional[bytes]=None
    schedule_delay_ms: Optional[int]=None
    schedule_token: Optional[str]=None
    finish_room_id: Optional[int]=None
    manual_cancel: bool=False
    cancel_reason: str=''
    log: str=''
    action_name: str=''
    display_amount: Optional[float]=None
    target_ws_id: str=''
    target_channel_id: str=''
    attempt: int=1

class CoinAutoplayCoordinator:
    def __init__(self, chip_scale:int=100):
        self.chip_scale=chip_scale
        self.lock=threading.RLock()
        self.pending:Optional[dict]=None
        self.ws_by_room:Dict[int,dict]={}
        self.turn_by_room:Dict[int,dict]={}
        self.hero_bet_by_room:Dict[int,float]={}
        # Coin normally emits legal options in game.advance_player_action one
        # packet before game.user_turn.  Cache that packet by SmartFox room and
        # merge it into the following turn; many live user_turn frames contain no
        # userTurnOptions at all.
        self.pending_options_by_room:Dict[int,dict]={}
        # Coin emits a second game.user_turn when the same decision enters
        # extraTimer/time-bank.  Timer fields change, the poker decision does not.
        # Track semantic turn ownership so that refresh does not create a second hint.
        self.last_turn_owner_by_room:Dict[int,str]={}
        self.last_hero_semantic_by_room:Dict[int,str]={}
        self.turn_generation_by_room:Dict[int,int]={}
        self.recent_injected:Dict[str,float]={}
        self.hold_window=float(os.getenv('POKER_AUTOPLAY_HOLD_WINDOW','0.250'))
        # ``SCAction.lifetime`` is the lifetime of EYE's UI message, not the
        # validity window of the poker action.  Stock EYE captures execute actions
        # whose delay is greater than lifetime.  The actual safety boundary is the
        # Coin turn timer, with enough margin left for the websocket send/server ACK.
        self.turn_deadline_margin_ms=max(250,int(os.getenv('POKER_TURN_DEADLINE_MARGIN_MS','750')))

    @staticmethod
    def _fingerprint(raw:bytes)->str: return hashlib.sha256(raw).hexdigest()
    def _prune_fingerprints(self,now:float):
        for k,t in list(self.recent_injected.items()):
            if now-t>8.0: self.recent_injected.pop(k,None)

    def _cmd_room(self,payload:Any):
        if not isinstance(payload,dict): return '',None,{}
        p=payload.get('p') if isinstance(payload.get('p'),dict) else {}
        cmd=str(p.get('c') or '')
        room=p.get('r')
        body=p.get('p') if isinstance(p.get('p'),dict) else {}
        data=body.get('data',body)
        if isinstance(data,str):
            try: data=json.loads(data)
            except Exception: pass
        return cmd, int(room) if isinstance(room,(int,float)) else None, data if isinstance(data,dict) else {}

    @staticmethod
    def _turn_id(data:Dict[str,Any])->str:
        """Stable identity for one Coin turn, including replay/reconnect copies."""
        for key in ('initTimeStamp','turnId','turnID','actionId'):
            value=data.get(key)
            if value not in (None,''):
                return f'{key}:{value}'
        stable={
            'whoseTurn':data.get('whoseTurn') or data.get('userName'),
            'turnTime':data.get('turnTime'),
            'callAmount':data.get('callAmount'),
            'userTurnOptions':data.get('userTurnOptions') or {},
        }
        return 'shape:'+hashlib.sha256(json.dumps(stable,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()

    @staticmethod
    def _semantic_turn(data:Dict[str,Any])->str:
        # Intentionally ignore timerName/turnTime/initTimeStamp: extraTimer is the
        # same poker decision.  Monetary/legal-action shape defines the turn.
        stable={
            'whoseTurn':data.get('whoseTurn') or data.get('userName'),
            'callAmount':data.get('callAmount'),
            'roundMaxBet':data.get('roundMaxBet'),
            'totalPot':data.get('totalPot'),
            'valueAmount':data.get('valueAmount'),
            'potRaiseValue':data.get('potRaiseValue'),
            'userTurnOptions':data.get('userTurnOptions') or {},
        }
        return hashlib.sha256(
            json.dumps(stable,sort_keys=True,separators=(',',':'),default=str).encode()
        ).hexdigest()

    def _reset_turn_identity(self,room:Optional[int])->None:
        if room is None:return
        room=int(room)
        self.last_turn_owner_by_room.pop(room,None)
        self.last_hero_semantic_by_room.pop(room,None)

    def observe(self,event:Dict[str,Any],payload:Any,raw:bytes,state:Dict[str,Any])->HookResult:
        now=time.monotonic(); direction=str(event.get('direction') or '').lower()
        cmd,room,data=self._cmd_room(payload)
        cancellation=HookResult()
        if room is not None and event.get('ws_id'):
            self.ws_by_room[room]={
                'ws_id':event.get('ws_id'),
                'url':event.get('url'),
                'text':False,
                'channel_id':event.get('_channel_id'),
            }
        if cmd in ('game.game_init','game.reset_data','game.pre_hand_start_info') and room is not None:
            # A Coin betAmount is street-local.  Never carry the hero's previous
            # hand/river contribution into the next preflop action projection.
            self.reset_street(room)
            self._reset_turn_identity(room)
            self.pending_options_by_room.pop(room,None)

        if cmd=='game.advance_player_action' and room is not None:
            options=data.get('userTurnOptions')
            if isinstance(options,dict) and options:
                self.pending_options_by_room[room]={
                    'options':dict(options),
                    'observed':now,
                    'initTimeStamp':data.get('initTimeStamp'),
                }
        if cmd=='game.seat' and room is not None:
            uid=int(data.get('userId') or 0); uname=str(data.get('userName') or '')
            if uid and uid==int(state.get('user_id') or 0) or (uname and uname==str(state.get('user_name') or '')):
                try:self.hero_bet_by_room[room]=float(data.get('betAmout') or data.get('betAmount') or 0.0)
                except Exception:pass
        if cmd=='game.user_turn' and room is not None:
            whose=str(data.get('whoseTurn') or data.get('userName') or '')
            hero=str(state.get('user_name') or '')
            try:hero_id=int(state.get('user_id') or 0)
            except (TypeError,ValueError):hero_id=0
            try:whose_id=int(data.get('userId') or data.get('whoseTurnUserId') or 0)
            except (TypeError,ValueError):whose_id=0
            if (not whose or not hero or whose.casefold()!=hero.casefold()) and hero_id and whose_id==hero_id:
                whose=hero
            if whose and hero and whose.casefold()==hero.casefold():
                whose=hero
            cached=self.pending_options_by_room.pop(room,None)
            if cached and now-float(cached.get('observed') or 0.0)<=3.0:
                options=cached.get('options')
                if isinstance(options,dict) and options and not data.get('userTurnOptions'):
                    # Mutating the decoded event is intentional: bridge hint/fallback
                    # code and the stored turn must see the same legal-action set.
                    data['userTurnOptions']=dict(options)
                    event['_hmuriy_options_from_advance']=True
            semantic=self._semantic_turn(data)
            with self.lock:
                previous_owner=str(self.last_turn_owner_by_room.get(room) or '')
                previous_semantic=str(self.last_hero_semantic_by_room.get(room) or '')
                duplicate=bool(
                    whose and hero and whose==hero
                    and previous_owner==hero
                    and previous_semantic==semantic
                    and room in self.turn_by_room
                )
                if duplicate:
                    # Same legal/monetary decision, only the timer refreshed. Keep
                    # the original turn id/deadline/action alive and skip re-hinting.
                    event['_hmuriy_duplicate_turn']=True
                    event['_hmuriy_turn_refresh']=str(data.get('timerName') or 'timer-refresh')
                    existing=self.turn_by_room.get(room) or {}
                    turn_id=str(existing.get('_turn_id') or self._turn_id(data))
                    # A process/channel reconnect may replay the same semantic turn.
                    # Keep decision identity/deadline but refresh where the actual
                    # RealWebSocket currently lives.
                    existing['_ws_id']=event.get('ws_id') or existing.get('_ws_id')
                    existing['_url']=event.get('url') or existing.get('_url')
                    existing['_channel_id']=event.get('_channel_id') or existing.get('_channel_id')
                    existing['_ws_u32']=event.get('_ws_u32', existing.get('_ws_u32'))
                else:
                    generation=int(self.turn_generation_by_room.get(room,0))+1
                    self.turn_generation_by_room[room]=generation
                    turn_id=f'semantic:{room}:{generation}:{semantic[:16]}'

                pending=self.pending
                if pending and int(pending.get('room') or -1)==room:
                    pending_turn=str(pending.get('turn_id') or '')
                    if (whose and whose!=hero) or (
                            not duplicate and pending_turn and pending_turn!=turn_id):
                        rid=state.get('_pending_finish_hint')
                        self.pending=None
                        cancellation=HookResult(
                            finish_room_id=rid,
                            cancel_reason='TURN_CHANGED',
                            log='Coin turn changed; cancelled pending bot action',
                        )

                self.last_turn_owner_by_room[room]=whose
                hero_turn=bool(whose and hero and whose.casefold()==hero.casefold())
                if not hero_turn and hero_id and whose_id==hero_id:
                    hero_turn=True
                    whose=hero
                if not hero_turn:
                    opts=data.get('userTurnOptions') or {}
                    try:hero_seat=int(state.get('hero_seat') or 0)
                    except (TypeError,ValueError):hero_seat=0
                    try:turn_seat=int(data.get('seatId') or 0)
                    except (TypeError,ValueError):turn_seat=0
                    if opts and hero_seat and turn_seat==hero_seat:
                        hero_turn=True
                        whose=hero or whose
                if hero_turn:
                    if not duplicate:
                        self.last_hero_semantic_by_room[room]=semantic
                        self.turn_by_room[room]=dict(data)
                        self.turn_by_room[room]['_ws_id']=event.get('ws_id')
                        self.turn_by_room[room]['_url']=event.get('url')
                        self.turn_by_room[room]['_channel_id']=event.get('_channel_id')
                        self.turn_by_room[room]['_ws_u32']=event.get('_ws_u32')
                        self.turn_by_room[room]['_observed_monotonic']=now
                        self.turn_by_room[room]['_turn_id']=turn_id
                elif whose:
                    # An opponent turn proves that the next hero occurrence is new.
                    self.last_hero_semantic_by_room.pop(room,None)
        # Manual outgoing action cancels a pending bot action, unless it is our own echo.
        if cmd=='game.user_action' and direction=='out':
            self._reset_turn_identity(room)
            fp=self._fingerprint(raw) if raw else ''
            with self.lock:
                self._prune_fingerprints(now)
                if fp and fp in self.recent_injected:
                    return HookResult(log='injected echo')
                if self.pending:
                    rid=state.get('_pending_finish_hint')
                    self.pending=None
                    return HookResult(finish_room_id=rid,manual_cancel=True,cancel_reason='MANUAL_ACTION',log='manual action cancelled pending bot action')
        return cancellation

    def is_recent_injected_echo(self,event:Dict[str,Any],raw:bytes)->bool:
        if str(event.get('direction') or '').lower()!='out' or not raw:return False
        now=time.monotonic(); fp=self._fingerprint(raw)
        with self.lock:
            self._prune_fingerprints(now)
            return fp in self.recent_injected

    def schedule_cc(self,cc:Dict[str,Any],state:Dict[str,Any])->str:
        lifetime_ms=max(0,int(cc.get('lifetime') or 4000))
        # The live bridge supplies the active SmartFox room. Never select a different
        # table merely because its background websocket produced a newer timestamp.
        if not self.turn_by_room: raise RuntimeError('no Coin hero game.user_turn observed yet')
        active=state.get('_hook_room')
        if active is not None and int(active) in self.turn_by_room:
            room=int(active); turn=self.turn_by_room[room]
        elif active is not None:
            raise RuntimeError(f'no Coin hero game.user_turn for active room={active}')
        else:
            room,turn=max(self.turn_by_room.items(), key=lambda kv: int(kv[1].get('initTimeStamp') or 0))
        requested_delay_ms=max(0,min(15000,int(cc.get('delay') or 0)))
        now=time.monotonic()
        try:turn_seconds=max(0.0,float(turn.get('turnTime') or 0.0))
        except (TypeError,ValueError):turn_seconds=0.0
        try:observed=float(turn.get('_observed_monotonic') or now)
        except (TypeError,ValueError):observed=now
        if turn_seconds>0:
            turn_deadline_at=observed+turn_seconds
            remaining_ms=max(0,int(round((observed+turn_seconds-now)*1000.0))-self.turn_deadline_margin_ms)
            delay_ms=min(requested_delay_ms,remaining_ms)
        else:
            turn_deadline_at=None
            remaining_ms=None
            delay_ms=requested_delay_ms
        ws_id=turn.get('_ws_id'); url=turn.get('_url'); channel_id=turn.get('_channel_id')
        turn_options=turn.get('userTurnOptions') or {}
        action=resolve_eye_cc_action(cc,user_turn_options=turn_options,
                                     current_street_bet=float(self.hero_bet_by_room.get(room,0.0)),
                                     chip_scale=self.chip_scale)
        display_amount=None
        if action.name=='CALL':
            call_opt=turn_options.get('4') if '4' in turn_options else turn_options.get(4)
            if isinstance(call_opt,(list,tuple)) and call_opt:
                try: display_amount=float(call_opt[0])
                except (TypeError,ValueError): display_amount=None
        elif action.name=='RAISE':
            display_amount=float(action.bet_amount)
        raw=build_game_user_action_packet(room,action.coin_code,action.bet_amount)
        with self.lock:
            self.pending={'due':now+delay_ms/1000.0,'raw':raw,'room':room,'ws_id':ws_id,'url':url,
                          'channel_id':channel_id,
                          'action':action.name,'bet_amount':action.bet_amount,'display_amount':display_amount,'delay_ms':delay_ms,
                          'finish_room_id':state.get('_pending_finish_hint'),'hand_id':str(state.get('hand_id') or ''),
                          'lifetime_ms':lifetime_ms,'requested_delay_ms':requested_delay_ms,
                          'created_at':now,'turn_id':str(turn.get('_turn_id') or self._turn_id(turn)),
                          'turn_deadline_at':turn_deadline_at,
                          'turn_deadline_remaining_ms':remaining_ms}
        deadline_note='' if delay_ms==requested_delay_ms else f' cappedFrom={requested_delay_ms}ms by Coin turn deadline'
        return f'scheduled {action.name} room={room} betAmount={action.bet_amount} delay={delay_ms}ms{deadline_note} ws={ws_id}'

    def schedule_failsafe(self,state:Dict[str,Any],*,reason:str="NO_CC")->dict[str,Any]:
        """Queue a non-strategic timeout safety action for the current hero turn.

        Policy is intentionally narrow: CHECK when Coin exposes a free check;
        otherwise FOLD when Coin exposes fold.  No paid CALL/RAISE decision is
        invented without PokerEYE.
        """
        active=state.get('_hook_room')
        turn=None
        room=None
        if active is not None:
            room=int(active)
            turn=self.turn_by_room.get(room) or {}
        elif self.turn_by_room:
            room,turn=max(
                self.turn_by_room.items(),
                key=lambda kv: float(kv[1].get('_observed_monotonic') or 0.0),
            )
        if room is None:
            raise RuntimeError("no Coin hero game.user_turn available for failsafe")
        if not isinstance(turn, dict):
            turn={}
        ws=self.ws_by_room.get(int(room), {})
        turn.setdefault('_ws_id', ws.get('ws_id'))
        turn.setdefault('_url', ws.get('url'))
        turn.setdefault('_channel_id', ws.get('channel_id'))

        opts=turn.get('userTurnOptions') or {}
        def present(code:int)->bool:
            return str(code) in opts or code in opts

        # A live hero turn with missing/odd option keys still must not sit out
        # the clock. CHECK if Coin listed it; otherwise FOLD. Never invent CALL.
        if present(3):
            name,coin_code='CHECK',3
        else:
            name,coin_code='FOLD',7

        now=time.monotonic()
        try:turn_seconds=max(0.0,float(turn.get('turnTime') or 0.0))
        except (TypeError,ValueError):turn_seconds=0.0
        try:observed=float(turn.get('_observed_monotonic') or now)
        except (TypeError,ValueError):observed=now
        turn_deadline_at=(observed+turn_seconds) if turn_seconds>0 else None
        remaining_ms=(
            max(0,int(round((turn_deadline_at-now)*1000.0))-self.turn_deadline_margin_ms)
            if turn_deadline_at is not None else None
        )
        raw=build_game_user_action_packet(room,coin_code,0.0)
        turn_id=str(turn.get('_turn_id') or self._turn_id(turn))
        with self.lock:
            self.pending={
                'due':now,
                'raw':raw,
                'room':room,
                'ws_id':turn.get('_ws_id'),
                'url':turn.get('_url'),
                'channel_id':turn.get('_channel_id'),
                'action':name,
                'bet_amount':0.0,
                'display_amount':None,
                'delay_ms':0,
                'finish_room_id':state.get('_pending_finish_hint'),
                'hand_id':str(state.get('hand_id') or ''),
                'lifetime_ms':0,
                'requested_delay_ms':0,
                'created_at':now,
                'turn_id':turn_id,
                'turn_deadline_at':turn_deadline_at,
                'turn_deadline_remaining_ms':remaining_ms,
                'fallback':True,
                'fallback_reason':str(reason or 'NO_CC'),
            }
        return {
            'action':name,
            'room':room,
            'turn_id':turn_id,
            'turn_deadline_at':turn_deadline_at,
            'reason':str(reason or 'NO_CC'),
        }

    def schedule_chart_fold(self,state:Dict[str,Any],*,reason:str="PREFOLD")->dict[str,Any]:
        """Queue an immediate Coin FOLD for an NLH chart hit. Never invent CHECK."""
        if not self.turn_by_room:
            raise RuntimeError("no Coin hero game.user_turn available for prefold")
        active=state.get('_hook_room')
        if active is not None and int(active) in self.turn_by_room:
            room=int(active); turn=self.turn_by_room[room]
        elif active is not None:
            raise RuntimeError(f"no current hero turn for active room={active}")
        else:
            room,turn=max(
                self.turn_by_room.items(),
                key=lambda kv: float(kv[1].get('_observed_monotonic') or 0.0),
            )
        opts=turn.get('userTurnOptions') or {}
        has_fold=str(7) in opts or 7 in opts
        has_check=str(3) in opts or 3 in opts
        if opts and not has_fold:
            raise RuntimeError(
                f"chart fold unavailable: Coin did not expose FOLD; "
                f"options={sorted(str(k) for k in opts.keys())}"
            )
        if has_check and not has_fold:
            raise RuntimeError("chart fold unavailable: Coin exposes CHECK")
        now=time.monotonic()
        try:turn_seconds=max(0.0,float(turn.get('turnTime') or 0.0))
        except (TypeError,ValueError):turn_seconds=0.0
        try:observed=float(turn.get('_observed_monotonic') or now)
        except (TypeError,ValueError):observed=now
        turn_deadline_at=(observed+turn_seconds) if turn_seconds>0 else None
        remaining_ms=(
            max(0,int(round((turn_deadline_at-now)*1000.0))-self.turn_deadline_margin_ms)
            if turn_deadline_at is not None else None
        )
        raw=build_game_user_action_packet(room,7,0.0)
        turn_id=str(turn.get('_turn_id') or self._turn_id(turn))
        with self.lock:
            self.pending={
                'due':now,
                'raw':raw,
                'room':room,
                'ws_id':turn.get('_ws_id'),
                'url':turn.get('_url'),
                'channel_id':turn.get('_channel_id'),
                'action':'FOLD',
                'bet_amount':0.0,
                'display_amount':None,
                'delay_ms':0,
                'finish_room_id':None,
                'hand_id':str(state.get('hand_id') or ''),
                'lifetime_ms':0,
                'requested_delay_ms':0,
                'created_at':now,
                'turn_id':turn_id,
                'turn_deadline_at':turn_deadline_at,
                'turn_deadline_remaining_ms':remaining_ms,
                'prefold':True,
                'fallback_reason':str(reason or 'PREFOLD'),
            }
        return {
            'action':'FOLD',
            'room':room,
            'turn_id':turn_id,
            'reason':str(reason or 'PREFOLD'),
        }

    def clear_room(self,room:Optional[int]=None):
        """Cancel only the selected table's action/turn state on leave or switch."""
        with self.lock:
            if room is None:
                self.pending=None; self.turn_by_room.clear(); self.hero_bet_by_room.clear()
                self.pending_options_by_room.clear()
                self.last_turn_owner_by_room.clear(); self.last_hero_semantic_by_room.clear(); self.turn_generation_by_room.clear()
                return
            room=int(room)
            if self.pending and int(self.pending.get('room') or -1)==room:self.pending=None
            self.turn_by_room.pop(room,None); self.hero_bet_by_room.pop(room,None)
            self.pending_options_by_room.pop(room,None)
            self.last_turn_owner_by_room.pop(room,None); self.last_hero_semantic_by_room.pop(room,None); self.turn_generation_by_room.pop(room,None)

    def reset_street(self,room:Optional[int]):
        """Reset only Coin's raw hero contribution at a proven street boundary."""
        if room is None:return
        with self.lock:self.hero_bet_by_room[int(room)]=0.0

    def maybe_inject(self,event:Dict[str,Any],payload:Any,raw:bytes,state:Dict[str,Any],*,schedule_supported:bool=False)->HookResult:
        with self.lock:
            p=self.pending
            if not p:return HookResult()
            native_push=int(event.get('v') or 0)>=6
            if not native_push:
                if p.get('ws_id') and event.get('ws_id')!=p.get('ws_id'):return HookResult()
                if p.get('url') and event.get('url')!=p.get('url'):return HookResult()
            now=time.monotonic(); due=float(p['due'])
            wait=max(0.0,due-now)
            # Never sleep in RealWebSocket.send(): the APK hook has a 220 ms read
            # timeout, so the old multi-second reservation logged a replacement that
            # never reached the app.  A v3 hook schedules the exact send locally; an
            # old hook waits for the first dummy at/after due.
            if wait>0 and not schedule_supported:return HookResult()
        with self.lock:
            p=self.pending
            if not p:return HookResult()
            # Hook v3 marks its app-side call SYNTHETIC, and legacy replacement returns
            # from the same wsBinary invocation.  Neither path re-enters observe(), so
            # pre-marking this fingerprint would misclassify a real manual identical
            # action made before cc.delay as an injected echo.
            inj=p['raw']; self.pending=None
            # LiveCoinBridge.finish_hint owns the idempotent state transition.  Do
            # not clear it here: a scheduled action and its Coin ACK can race, and
            # whichever observes the boundary first must still emit exactly one
            # FinishRoundHintRSP.
            rid=state.get('_pending_finish_hint')
            delay_ms=int(round(wait*1000)) if schedule_supported else None
            token=f"{p.get('hand_id','')}:{self._fingerprint(inj)[:20]}"
            verb="schedule" if schedule_supported else "inject"
            return HookResult(inject_raw=inj,schedule_delay_ms=delay_ms,schedule_token=token,
                              finish_room_id=rid,log=f"{verb} {p['action']} room={p['room']} bet={p['bet_amount']}",
                              action_name=str(p.get('action') or ''),display_amount=p.get('display_amount'),
                              target_ws_id=str(p.get('ws_id') or ''),
                              target_channel_id=str(p.get('channel_id') or ''),
                              attempt=1)
