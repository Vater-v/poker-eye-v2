"""Root CLI entry point (stdlib only)."""
import argparse, secrets, threading, time
from core.discovery import Broadcaster, DEFAULT_INTERVAL
from core.transport import TrainerServer
from core.logging import SessionLogger

def run(args):
    logger = SessionLogger(args.log_dir)
    broadcaster = Broadcaster(args.host, 0, args.secret.encode(), args.slots, args.interval, args.broadcast_port, args.session_id)
    broadcaster.advertised_nonce = secrets.token_hex(16)
    server = TrainerServer(broadcaster.secret, broadcaster.session_id, broadcaster.advertised_nonce, broadcaster.slot_pool, args.host, args.port,
                           on_connect=lambda table, _slot, _conn, address: logger.emit("transport.connected", message=f"[+] Новое подключение от {table}!", table_id=table, peer=str(address), flush=True))
    server.start(); broadcaster.tcp_port = server.port
    threading.Thread(target=broadcaster.run, daemon=True).start()
    logger.emit("trainer.ready", message="[+] Trainer корректно запущен, ожидаю подключений.", flush=True, tcp_port=server.port, broadcast_port=args.broadcast_port, slots=args.slots)
    print("[+] Trainer корректно запущен, ожидаю подключений.", flush=True)
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: pass
    finally: broadcaster.stop(); server.stop(); logger.close()

def main():
    p = argparse.ArgumentParser(description="minimal trainer discovery/TCP prototype")
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=0); p.add_argument("--broadcast-port", type=int, default=37020)
    p.add_argument("--slots", type=int, default=1); p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL); p.add_argument("--session-id", default="trainer")
    p.add_argument("--secret", required=True); p.add_argument("--log-dir", default="logs")
    run(p.parse_args())
if __name__ == "__main__": main()
