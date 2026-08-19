from __future__ import annotations
import json, struct, zlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

# SmartFox SFS2X types used by CoinPoker's ExtensionRequest.
SFS_BYTE=2; SFS_SHORT=3; SFS_INT=4; SFS_UTF=8; SFS_OBJECT=18

@dataclass(frozen=True)
class _Byte: value:int
@dataclass(frozen=True)
class _Short: value:int
@dataclass(frozen=True)
class _Int: value:int
@dataclass(frozen=True)
class _Str: value:str
@dataclass(frozen=True)
class _Obj: value:dict

def _u16(n:int)->bytes: return int(n).to_bytes(2,'big',signed=False)
def _i16(n:int)->bytes: return int(n).to_bytes(2,'big',signed=True)
def _i32(n:int)->bytes: return int(n).to_bytes(4,'big',signed=True)

def _enc_value(v:Any)->bytes:
    if isinstance(v,_Byte): return bytes([SFS_BYTE, v.value & 0xff])
    if isinstance(v,_Short): return bytes([SFS_SHORT])+_i16(v.value)
    if isinstance(v,_Int): return bytes([SFS_INT])+_i32(v.value)
    if isinstance(v,_Str):
        b=v.value.encode('utf-8'); return bytes([SFS_UTF])+_u16(len(b))+b
    if isinstance(v,_Obj):
        out=bytearray([SFS_OBJECT]); out+=_u16(len(v.value))
        for k,x in v.value.items():
            kb=str(k).encode('utf-8'); out+=_u16(len(kb))+kb+_enc_value(x)
        return bytes(out)
    raise TypeError(type(v))

def encode_packet(value:dict)->bytes:
    payload=_enc_value(_Obj(value))
    # 0x80 + 2-byte length: exact form observed in CoinPoker captures.
    if len(payload)>0xffff: raise ValueError('SFS packet too large')
    return bytes([0x80])+_u16(len(payload))+payload

def build_extension_packet(command:str, room_id:int, data:Optional[Dict[str,Any]]=None)->bytes:
    """Build the exact Coin SmartFox ExtensionRequest shape used by captured packets."""
    inner:dict[str,Any]={}
    if data is not None:
        inner["data"]=_Str(json.dumps(data,separators=(",",":")))
    root={
        'c':_Byte(1),
        'a':_Short(13),
        'p':_Obj({
            'c':_Str(str(command)),
            'r':_Int(int(room_id)),
            'p':_Obj(inner),
        }),
    }
    return encode_packet(root)

def build_game_leave_seat_packet(room_id:int)->bytes:
    return build_extension_packet('game.leave_Seat',room_id)

def build_game_quit_table_packet(room_id:int)->bytes:
    return build_extension_packet('game.quit_table',room_id)

def build_game_user_action_packet(room_id:int, user_action:int, bet_amount:float=0.0)->bytes:
    data=json.dumps({'userAction':int(user_action),'betAmount':float(bet_amount)},separators=(',',':'))
    # Keep integer 0 exactly like the captured wire.
    if float(bet_amount)==0.0:
        data=data.replace('0.0}', '0}')
    root={
        'c':_Byte(1),
        'a':_Short(13),
        'p':_Obj({
            'c':_Str('game.user_action'),
            'r':_Int(int(room_id)),
            'p':_Obj({'data':_Str(data)}),
        }),
    }
    return encode_packet(root)

def _rv(b:bytes,p:int):
    return None

def decode_packet(raw:bytes)->dict:
    if len(raw)<3: raise ValueError('short packet')
    header=raw[0]; wide=bool(header & 0x08); compressed=bool(header & 0x20)
    if wide:
        n=int.from_bytes(raw[1:5],'big'); p=5
    else:
        n=int.from_bytes(raw[1:3],'big'); p=3
    payload=raw[p:p+n]
    if compressed: payload=zlib.decompress(payload)
    def dec(buf:bytes,pos:int=0):
        typ=buf[pos]; pos+=1
        if typ==SFS_BYTE: return buf[pos],pos+1
        if typ==SFS_SHORT: return int.from_bytes(buf[pos:pos+2],'big',signed=True),pos+2
        if typ==SFS_INT: return int.from_bytes(buf[pos:pos+4],'big',signed=True),pos+4
        if typ==SFS_UTF:
            ln=int.from_bytes(buf[pos:pos+2],'big'); pos+=2
            return buf[pos:pos+ln].decode('utf-8'),pos+ln
        if typ==SFS_OBJECT:
            count=int.from_bytes(buf[pos:pos+2],'big'); pos+=2; out={}
            for _ in range(count):
                ln=int.from_bytes(buf[pos:pos+2],'big'); pos+=2
                k=buf[pos:pos+ln].decode('utf-8'); pos+=ln
                out[k],pos=dec(buf,pos)
            return out,pos
        raise ValueError(f'unsupported SFS type {typ}')
    obj,used=dec(payload,0)
    return obj

@dataclass
class ResolvedAction:
    name:str
    coin_code:int
    bet_amount:float


def resolve_eye_cc_action(cc:Dict[str,Any], *, user_turn_options:Optional[dict]=None,
                          current_street_bet:float=0.0, chip_scale:int=100)->ResolvedAction:
    typ=str(cc.get('type') or cc.get('action') or cc.get('message') or '').strip().upper()
    opts=user_turn_options or {}
    if typ in ('FOLD','FAST_FOLD'): return ResolvedAction('FOLD',7,0.0)
    if typ=='CHECK': return ResolvedAction('CHECK',3,0.0)
    if typ=='CALL': return ResolvedAction('CALL',4,0.0)
    try: subtype=int(cc.get('subtype') or 0)
    except (TypeError,ValueError): subtype=0
    message=str(cc.get('message') or '').strip().upper().replace('-','').replace('_','').replace(' ','')
    explicit_allin=(typ in ('ALLIN','ALL-IN','ALL_IN')
                    or (typ in ('RAISE','BET') and (subtype==1 or message=='ALLIN')))
    if typ in ('RAISE','BET','ALLIN','ALL-IN','ALL_IN'):
        # Project every aggressive EYE decision onto Coin's paid legal actions.
        # EYE encodes an all-in CALL as RAISE/subtype=1 as well; in that state
        # Coin correctly omits option 5 and exposes only option 4.  Captures prove
        # CALL uses code 4 with a zero betAmount, while option 4 contains the
        # additional amount being called.  The same projection also handles a
        # normal backend raise that falls closer to a legal call than Coin's
        # minimum raise.  Fold/check are deliberately excluded: equal monetary
        # distance does not make them equivalent to a paid action.
        try: amount=float(cc.get('amount'))
        except (TypeError,ValueError): amount=None
        desired=(current_street_bet+amount/chip_scale) if amount is not None else None
        candidates=[]
        call_opt=opts.get('4') if '4' in opts else opts.get(4)
        if isinstance(call_opt,(list,tuple)) and call_opt:
            try:
                call_target=current_street_bet+float(call_opt[0])
                distance=abs(desired-call_target) if desired is not None else float('inf')
                candidates.append((distance,1,ResolvedAction('CALL',4,0.0)))
            except (TypeError,ValueError): pass
        rr=opts.get('5') if '5' in opts else opts.get(5)
        if isinstance(rr,(list,tuple)) and len(rr)>=2:
            try:
                lo,hi=float(rr[0]),float(rr[1])
                if hi>=lo:
                    target=hi if desired is None else max(lo,min(hi,desired))
                    distance=abs(desired-target) if desired is not None else 0.0
                    # Prefer the aggressive action only on an exact distance tie.
                    candidates.append((distance,0,ResolvedAction('RAISE',5,round(target,8))))
            except (TypeError,ValueError): pass
        if candidates:return min(candidates,key=lambda x:(x[0],x[1]))[2]
        kind='ALLIN' if explicit_allin else typ
        raise ValueError(f'{kind} requested but Coin exposes no paid action')
    raise ValueError(f'unsupported EYE action {typ!r}')
