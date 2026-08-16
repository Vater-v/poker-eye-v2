"""Threaded authenticated TCP acceptor with bounded table slots."""
import socket,threading
from protocol import recv_frame,send_frame,verify_hello
class TrainerServer:
    def __init__(self,secret,session_id,advertised_nonce,slot_pool,host='0.0.0.0',port=0,on_connect=None):
        self.secret,self.session_id,self.advertised_nonce=secret,session_id,advertised_nonce; self.slot_pool,self.host,self.port,self.on_connect=slot_pool,host,port,on_connect; self._server=None; self._stop=threading.Event(); self._active=set(); self._lock=threading.Lock(); self._threads=[]
    def start(self):
        self._server=socket.socket(socket.AF_INET,socket.SOCK_STREAM); self._server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); self._server.bind((self.host,self.port)); self._server.listen(); self.port=self._server.getsockname()[1]; threading.Thread(target=self._accept_loop,daemon=True).start(); return self.port
    def _accept_loop(self):
        self._server.settimeout(.2)
        while not self._stop.is_set():
            try: conn,address=self._server.accept()
            except socket.timeout: continue
            except OSError: break
            t=threading.Thread(target=self._handle,args=(conn,address),daemon=True); self._threads.append(t); t.start()
    def _handle(self,conn,address):
        table_id=None
        try:
            table_id=verify_hello(recv_frame(conn),self.secret,self.advertised_nonce,self.session_id)
            with self._lock:
                if table_id in self._active: send_frame(conn,{'type':'error','error':'duplicate_connection'}); return
                slot=self.slot_pool.reserve(table_id)
                if slot is None: send_frame(conn,{'type':'error','error':'no_slots'}); return
                self._active.add(table_id)
            send_frame(conn,{'type':'welcome','table_id':table_id,'slot':slot})
            if self.on_connect: self.on_connect(table_id,slot,conn,address)
            else:
                while not self._stop.is_set() and recv_frame(conn): pass
        except (ConnectionError,OSError,ValueError): pass
        finally:
            if table_id:
                with self._lock: self._active.discard(table_id)
                self.slot_pool.release(table_id)
            try: conn.close()
            except OSError: pass
    def stop(self):
        self._stop.set()
        if self._server:
            try:self._server.close()
            except OSError:pass
