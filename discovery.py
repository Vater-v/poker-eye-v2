"""UDP IPv4 broadcast advertisement and atomic table-slot reservation."""
import json, secrets, socket, threading
class SlotPool:
    def __init__(self, slots=1):
        if not 1 <= slots <= 3: raise ValueError('slots must be between 1 and 3')
        self._available=set(range(1, slots+1)); self._claims={}; self._lock=threading.Lock()
    def reserve(self, table_id):
        with self._lock:
            if table_id in self._claims: return self._claims[table_id]
            if not self._available: return None
            slot=min(self._available); self._available.remove(slot); self._claims[table_id]=slot; return slot
    def release(self, table_id):
        with self._lock:
            slot=self._claims.pop(table_id,None)
            if slot is not None: self._available.add(slot)
            return slot
    def claimed(self):
        with self._lock: return dict(self._claims)
    def metadata(self):
        with self._lock: return {'slots':len(self._available)+len(self._claims),'available':len(self._available),'claims':dict(self._claims)}
class Broadcaster:
    def __init__(self,host,tcp_port,secret,slots=1,interval=.5,broadcast_port=37020,session_id='session'):
        self.host,self.tcp_port,self.secret=host,tcp_port,secret; self.interval,self.broadcast_port,self.session_id=interval,broadcast_port,session_id; self.slot_pool=SlotPool(slots); self.advertised_nonce=None; self._stop=threading.Event()
    def advertisement(self): return {'type':'trainer','version':1,'session_id':self.session_id,'host':self.host,'tcp_port':self.tcp_port,'nonce':self.advertised_nonce,'metadata':self.slot_pool.metadata()}
    def run(self):
        if self.advertised_nonce is None: self.advertised_nonce=secrets.token_hex(16)
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); sock.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
        try:
            payload=lambda:json.dumps(self.advertisement(),separators=(',',':')).encode()
            while not self._stop.is_set(): sock.sendto(payload(),('255.255.255.255',self.broadcast_port)); self._stop.wait(self.interval)
        finally: sock.close()
    def stop(self): self._stop.set()
