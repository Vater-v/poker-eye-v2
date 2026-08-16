"""Bounded length-prefixed JSON protocol and nonce-bound HMAC hello."""
import hashlib,hmac,json,struct
MAX_FRAME=1024*1024
def send_frame(sock,obj):
    data=json.dumps(obj,separators=(',',':')).encode();
    if not data or len(data)>MAX_FRAME: raise ValueError('frame too large')
    sock.sendall(struct.pack('!I',len(data))+data)
def _read(sock,n):
    out=bytearray()
    while len(out)<n:
        chunk=sock.recv(n-len(out))
        if not chunk: raise ConnectionError('peer closed')
        out.extend(chunk)
    return bytes(out)
def recv_frame(sock):
    size=struct.unpack('!I',_read(sock,4))[0]
    if size<1 or size>MAX_FRAME: raise ValueError('invalid frame size')
    return json.loads(_read(sock,size).decode())
def verify_hello(msg,secret,nonce,session_id):
    if msg.get('type')!='hello' or msg.get('session_id')!=session_id or msg.get('nonce')!=nonce: raise ValueError('invalid hello')
    table=str(msg.get('table_id','')); provided=str(msg.get('auth',''))
    expected=hmac.new(secret,(session_id+'|'+nonce+'|'+table).encode(),hashlib.sha256).hexdigest()
    if not table or not hmac.compare_digest(provided,expected): raise ValueError('invalid auth')
    return table
