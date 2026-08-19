import base64
import json
import threading
import unittest
from collections import defaultdict, deque

from core import rescue_runtime as rt
from core.verified_v1.coin_action_wire import (
    _Byte, _Int, _Obj, _Short, _Str, encode_packet,
)


def coin_event(cmd, room=None, *, mid="x", direction="in", data=None):
    p = {"c": _Str(cmd)}
    if room is not None:
        p["r"] = _Int(room)
    p["p"] = _Obj({"data": _Str(json.dumps(data or {}, separators=(",", ":")))})
    root = {
        "c": _Byte(1),
        "a": _Short(13),
        "p": _Obj(p),
    }
    raw = encode_packet(root)
    return {
        "type": "ws_message",
        "kind": "ws_message",
        "v": 4,
        "async": True,
        "id": mid,
        "direction": direction,
        "text": False,
        "url": "wss://coin",
        "ws_id": "ws",
        "payload_b64": base64.b64encode(raw).decode(),
    }


class FakeBusiness:
    def __init__(self):
        self.keys = []
        self.events = []

    def handle(self, device, table, event):
        self.keys.append((device, table))
        self.events.append((table, event.get("id")))
        return {"id": event.get("id"), "action": "forward"}


class _Ring:
    def write_packet(self, *args, **kwargs):
        pass


class _Pcap:
    def ring(self, *args, **kwargs):
        return _Ring()


def fake_trainer():
    trainer = rt.RescueTrainer.__new__(rt.RescueTrainer)
    trainer.business = FakeBusiness()
    trainer.pcap = _Pcap()
    trainer._route_lock = threading.RLock()
    trainer._device_prelude = defaultdict(lambda: deque(maxlen=500))
    trainer._room_prelude = defaultdict(lambda: deque(maxlen=2500))
    trainer._channel_room = {}
    trainer._logical_seen = set()
    return trainer


class AsyncLogicalRoutingTests(unittest.TestCase):
    def test_ten_transport_channels_do_not_spend_accounts(self):
        trainer = fake_trainer()
        for i in range(10):
            result = trainer._on_message(
                "dev1", f"channel-{i}",
                coin_event("lobby.dummy", None, mid=f"lobby-{i}"),
            )
            self.assertEqual(result["action"], "forward")
        self.assertEqual(trainer.business.keys, [])

    def test_one_real_room_is_one_business_context_across_channels(self):
        trainer = fake_trainer()

        # Context arrives on one SmartFox transport channel.
        trainer._on_message(
            "dev1", "ch-a",
            coin_event("game.game_alldata", 777, mid="ctx"),
        )
        trainer._on_message(
            "dev1", "ch-a",
            coin_event("game.take_Seat", 777, mid="seat", direction="out"),
        )

        # Coin later creates another RealWebSocket object carrying the SAME room.
        trainer._on_message(
            "dev1", "ch-b",
            coin_event("game.user_turn", 777, mid="turn"),
        )

        self.assertEqual(set(trainer.business.keys), {("dev1", "room:777")})

    def test_two_real_rooms_get_two_contexts(self):
        trainer = fake_trainer()
        trainer._on_message(
            "dev1", "ch-a",
            coin_event("game.take_Seat", 777, mid="seat-777", direction="out"),
        )
        trainer._on_message(
            "dev1", "ch-b",
            coin_event("game.take_Seat", 888, mid="seat-888", direction="out"),
        )
        self.assertEqual(
            set(trainer.business.keys),
            {("dev1", "room:777"), ("dev1", "room:888")},
        )

    def test_passive_game_room_does_not_allocate_until_activation(self):
        trainer = fake_trainer()
        trainer._on_message(
            "dev1", "ch-a",
            coin_event("game.potInfo", 777, mid="pot"),
        )
        self.assertEqual(trainer.business.keys, [])
        trainer._on_message(
            "dev1", "ch-a",
            coin_event("game.user_turn", 777, mid="turn"),
        )
        self.assertEqual(set(trainer.business.keys), {("dev1", "room:777")})


if __name__ == "__main__":
    unittest.main()
