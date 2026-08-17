"""Root CLI entry point for the v2 trainer (stdlib only).

One command::

    RUN.cmd
    python main.py --secret <secret>

Starts UDP IPv4 broadcast discovery (default 1.25 s), authenticated TCP LAN
(one connection per table), the EYE channel client when ``--eye-host`` is set,
per-table state tracking, hint/cc/action/ACK handling, bounded PCAP ring and
hierarchical run logs. Prints short operator status lines.
"""
import argparse

from core.trainer import Trainer


def run(args):
    trainer = Trainer(
        secret=args.secret,
        host=args.host,
        tcp_port=args.tcp_port,
        broadcast_port=args.broadcast_port,
        slots=args.slots,
        interval=args.interval,
        session_id=args.session_id,
        log_dir=args.log_dir,
        eye_host=args.eye_host,
        eye_port=args.eye_port,
        game_port=args.game_port,
        chip_scale=args.chip_scale,
        bootstrap_port=args.bootstrap_port,
        callback_start=args.callback_start,
        callback_end=args.callback_end,
        public_host=args.public_host,
    )
    trainer.start()
    trainer.run_forever()


def main():
    p = argparse.ArgumentParser(description="poker-eye-v2 minimal trainer")
    p.add_argument("--secret", required=True, help="shared HMAC secret (POKEREYE_V2_SECRET)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--tcp-port", type=int, default=None, help="0 = OS-assigned")
    p.add_argument("--broadcast-port", type=int, default=37020)
    p.add_argument("--slots", type=int, default=16)
    p.add_argument("--interval", type=float, default=1.25)
    p.add_argument("--session-id", default="trainer")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--eye-host", default=None, help="PokerEYE app/backend host")
    p.add_argument("--eye-port", type=int, default=None, help="PokerEYE app/backend port")
    p.add_argument("--game-port", type=int, default=17770, help="verified game port for the PCAP BPF")
    p.add_argument("--chip-scale", type=int, default=100)
    p.add_argument("--bootstrap-port", type=int, default=19037)
    p.add_argument("--callback-start", type=int, default=54300)
    p.add_argument("--callback-end", type=int, default=54399)
    p.add_argument("--public-host", default="37.192.228.101")
    run(p.parse_args())


if __name__ == "__main__":
    main()
