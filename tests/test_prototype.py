import hashlib,hmac,socket,threading,time,unittest
from discovery import SlotPool
from protocol import recv_frame,send_frame
class FakeSocket:
    def __init__(self,data): self.data=data
    def recv(self,n): out=self.data[:n]; self.data=self.data[n:]; return out
class PrototypeTests(unittest.TestCase):
    def test_slots_reserve_duplicate_release(self):
        p=SlotPool(2); self.assertEqual(p.reserve('a'),1); self.assertEqual(p.reserve('a'),1); self.assertEqual(p.reserve('b'),2); self.assertIsNone(p.reserve('c')); p.release('a'); self.assertEqual(p.reserve('c'),1)
    def test_partial_frame(self):
        left,right=socket.socketpair(); send_frame(left,{'x':1}); raw=right.recv(1024); self.assertTrue(raw); right.close(); left.close()
    def test_auth_material_is_deterministic(self):
        secret=b's'; msg='session|nonce|table'; self.assertEqual(len(hmac.new(secret,msg.encode(),hashlib.sha256).hexdigest()),64)
if __name__=='__main__': unittest.main()
