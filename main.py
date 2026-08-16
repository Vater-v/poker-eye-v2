"""Minimal trainer-side UDP discovery/TCP prototype."""
import argparse
import threading
import time
from discovery import Broadcaster
from transport import TrainerServer

def run(args):
    broadcaster = Broadcaster(args.host, 0, args.secret.encode(), args.slots, args.interval, args.broadcast_port, args.session_id)
    server = TrainerServer(broadcaster.secret, broadcaster.session_id, "", broadcaster.slot_pool, args.host, args.port)
    server.start()
    broadcaster.tcp_port = server.port
    broadcaster.advertised_nonce = __import__('secrets').token_hex(16)
    server.advertised_nonce = broadcaster.advertised_nonce
    thread = threading.Thread(target=broadcaster.run, daemon=True)
    thread.start()
    print('[+] Trainer корректно запущен, ожидаю подключений.', flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        broadcaster.stop(); server.stop()

def main():
    p=argparse.ArgumentParser(description='minimal trainer discovery/TCP prototype')
    p.add_argument('--host', default='0.0.0.0'); p.add_argument('--port', type=int, default=0)
    p.add_argument('--broadcast-port', type=int, default=37020); p.add_argument('--slots', type=int, default=1)
    p.add_argument('--interval', type=float, default=0.5); p.add_argument('--session-id', default='trainer')
    p.add_argument('--secret', required=True)
    run(p.parse_args())
if __name__ == '__main__': main()
