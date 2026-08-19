#!/usr/bin/env python3
from __future__ import annotations
import json,socket,struct,sys,time
from pathlib import Path
ROOT=Path('/opt/pokereye'); sys.path.insert(0,str(ROOT))
from core.production_runtime import PROTOCOL_VERSION,direct_proof
secret=(ROOT/'secrets'/'trainer.secret').read_text(encoding='utf-8-sig').strip().encode()
device='dev-vps-selftest'; transport='dev-vps-selftest-native-probe'
msg={'type':'direct_hello','version':PROTOCOL_VERSION,'device_id':device,'table_id':transport,
     'proof':direct_proof(secret,device,transport),'native_mux':1,
     'build_id':(ROOT/'BUILD_ID').read_text(encoding='utf-8-sig').strip(),
     'device_label':'VPS HMN1 selftest'}
raw=json.dumps(msg,separators=(',',':')).encode(); t=time.monotonic()
with socket.create_connection(('127.0.0.1',19037),timeout=3) as s:
    s.settimeout(3); s.sendall(struct.pack('!I',len(raw))+raw)
    def rx(n):
        b=b''
        while len(b)<n:
            x=s.recv(n-len(b))
            if not x: raise RuntimeError('socket closed')
            b+=x
        return b
    n=struct.unpack('!I',rx(4))[0]; reply=json.loads(rx(n))
    if reply.get('type')!='welcome': raise RuntimeError(f'unexpected {reply!r}')
print(f"[PASS] HMN1 welcome {(time.monotonic()-t)*1000:.1f}ms build={reply.get('build_id')}")
