#!/usr/bin/env python3
"""COIN -> PPPoker traffic converter / live 18010 bridge.

Offline:
  python coin_ppp_bridge.py convert --pcap port_18010.pcap --out coin_converted_v5.txt
  python coin_ppp_bridge.py inspect --pcap port_18010.pcap

Live (source writes length-prefixed JSON events to 18010, EYE listens on 17770):
  python coin_ppp_bridge.py bridge --listen 127.0.0.1:18010 --eye 127.0.0.1:17770

The bridge intentionally does NOT copy COIN auth/JWT tokens into PPP traffic. Identity,
table and hand state are synthesized from COIN events, while PPP-only admission metadata
stays in the small hardcoded compatibility block below.
"""
from __future__ import annotations

import argparse, asyncio, base64, collections, json, re, socket, struct, sys, time, zlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Minimal PPP-only compatibility constants. These do not exist in COIN.
# Keep this block intentionally small.
# ---------------------------------------------------------------------------
PPP_PACKAGE = "com.lein.pppoker.android"
PPP_CLUB_ID = 3663333
# Only Clubid is authoritative for the requested target. Its human name was not
# supplied, so optional name fields are omitted rather than copying a stale club.
PPP_CLUB_NAME = ""
# Fresh PPP v4.2 wire uses explicit zero in RoomInfo/MultipleTableRoomInfo and
# omits the default-valued field from RoundHintMultipleTableRSP.
PPP_PPSRID = 0
PPP_EXCHANGE_RATE = 100
PPP_ROOM_MODE = 3      # Club
PPP_ROOM_TYPE = 5      # ClubRoom (NLH / legacy fallback)
PPP_SILENT_VERSION = '{"ver":["3.1.18-4"], "isWhiteList":0, "whiteListVer":[]}'
PPP_HINT_ROOMTYPE = 3   # NLH RoundHintMultipleTableRSP.Roomtype from the live PPP wire
PPP_OMAHA_ROOM_TYPE = 8
PPP_PLO5_HINT_ROOMTYPE = 24
PPP_GAME_ID_UTC_OFFSET_SECONDS = 8 * 60 * 60
PPP_CLIENT_VERSION = "4.2.155"
PPP_CLIENT_IP = "127.0.0.1"
PPP_CLIENT_PLATFORM = "android"
PPP_SERVER_ENDPOINT = "127.0.0.1:4000"
PPP_CLIENT_COUNTRY = "Brazil"
PPP_SIT_EMAIL = "email"

# Target club UI limit/BB choices. HoldemRegularPanel halves the selected BB
# before CreateClubRoomREQ field 2; that field, RoomInfo/RoundHint.Blind and
# forced SB therefore carry one half of the displayed target limit.
PPP_TARGET_BIG_BLINDS = tuple(Decimal(value) for value in (
    "0.1", "0.4", "0.6", "0.8", "1", "2", "4", "6", "8", "10", "20",
    "30", "40", "50", "60", "80", "100", "200", "300", "400", "500", "1000",
))
PPP_WIRE_CURRENCY_SCALE = 100
COIN_CHIP_USDT = Decimal("1")
USD_RUB = Decimal("83.99")
TARGET_PPP_CHIP_RUB = Decimal("0.9")

# Coin currently uses seven-digit table ids, while every accepted PPP room
# context captured for the direct backend uses a real eight-digit numeric id.
# A leading zero is not a normalization: protobuf encodes these fields as
# varints, so ``01119392`` and ``1119392`` are identical on the wire.  Keep the
# Coin id unchanged for SmartFox routing and map it only at the PPP boundary.
PPP_SYNTHETIC_ID_BASE = 10_000_000


def ppp_table_id(coin_table_id: int) -> int:
    """Return the stable PPP-facing numeric id for a Coin table.

    The mapping was accepted by the direct backend in isolated, cleanly closed
    oracle sessions (1119392 -> 11119392).  Already-eight-digit ids are retained
    so a future native-compatible Coin id is not rewritten unnecessarily.
    """
    value = int(coin_table_id or 0)
    if 0 < value < PPP_SYNTHETIC_ID_BASE:
        return PPP_SYNTHETIC_ID_BASE + value
    return value

# Legacy reference blobs retained only for byte-level comparison while developing.
# v5 never emits them: they contain stale users/table state. Admission is synthesized
# dynamically by PPPBuilder._enter_room_rsp().
_V4_TABLE_STATUS = base64.b64decode("CAEQ////////////ARgFIAEoAjI6CAAQBhoCMAAgACgAMAA4AFAAkgEKCAAQABgAIAAoAJgBAaABAKgBPPABAJACAJgCAKACAKgCALACADK9AQgBEAcaTAj2j7YGEgZBSkxPU08aOWh0dHBzOi8vYWxpY2RuLnBwcG9rZXIuY2x1Yi9wb2tlci9yZXNvdXJjZS9oZWFkX3RpZ2VyLnBuZzAAOgAg6AcoADAAOABCAlRISABQAFgAaKvVjgJwiO/5L3iFu8oGggEUQm9zc3Bva2Vy4oCLIC3igIsgVjGSAQCYAQCgAQCoATzgAQzqAQ5oZWFkX2ZyYW1lX2RlZvABAJACAJgCAKACAKgCALACADLdAQgCEAEacwiky6IEEgxWLiBPLiBMLiBLLiAaWmh0dHBzOi8vYWxpY2RuLnBwcG9rZXIuY2x1Yi9wb2tlci9oZWFkaWNvbi9oZWFkLzExY2M5NzJkOGU0MGQ0NDVhODdhNmJiM2I2OTViYmU3Xzc2NjMwLmpwZzAAOgAgywEoADAAOABCAlJVSABQAFgAaNXM0gFwjMW9HHjoqakaggENTUFSU+KtkENPU01PU5IBAJgBAKABAKgBPOABE+oBDmhlYWRfZnJhbWVfZGVm8AEAkAIAmAIAoAIAqAIAsAIAMjoIAxAGGgIwACAAKAAwADgAUACSAQoIABAAGAAgACgAmAEBoAEAqAE88AEAkAIAmAIAoAIAqAIAsAIAMrgBCAQQARpMCM+m2wQSCGthaXNlcl8xGjdodHRwczovL2FsaWNkbi5wcHBva2VyLmNsdWIvcG9rZXIvcmVzb3VyY2UvaGVhZF9mb3gucG5nMAA6ACD7CCgAMAA4AEICUlVIA1AAWABol725AXDBtYkXeMuK9BmCAQlQUkVEQVTQnlKSAQCYAQCgAQCoATzgARbqARRoZWFkX2ZyYW1lX3ZpcF9ibGFja/ABAJACAJgCAKACAKgCALACADK8AQgFEAEaUgig/sgGEgpwcDEzNzc4NzIwGjtodHRwczovL2FsaWNkbi5wcHBva2VyLmNsdWIvcG9rZXIvcmVzb3VyY2UvaGVhZF92YW1waXJlLnBuZzAAOgAguAEoADAAOABCAlJVSABQAFgAaNi7jAFwoKHPNnjM+q4YggEN0J/QmiBBS9GD0LvQsJIBAJgBAKABAKgBPOABA+oBDmhlYWRfZnJhbWVfZGVm8AEAkAIAmAIAoAIAqAIAsAIAQABQAVgAggEfMjYwODE0MDEwMDUxLTEwODQ3MTEwLTAwMDAwMDgtMYgBAJABAJgBAKABBcgBANABAeABAPABAPgBALgCAw==")
_V4_ROOM_STATUS = base64.b64decode("CMwLEtkBCgxWLiBPLiBMLiBLLiAQuQMYkv7/////////ASCky6IEKlpodHRwczovL2FsaWNkbi5wcHBva2VyLmNsdWIvcG9rZXIvaGVhZGljb24vaGVhZC8xMWNjOTcyZDhlNDBkNDQ1YTg3YTZiYjNiNjk1YmJlN183NjYzMC5qcGcwCDgIQABIAFAAWABgAGgAcgoIABgAIAAqAFgAeACAAQCYAQCgAQOoAQCwAQC4AQDAAQDQAQDqAQ5oZWFkX2ZyYW1lX2RlZoACAIgCAJoCALACALgCAMACANgCABKwAQoIa2Fpc2VyXzEQ8gcYiQEgz6bbBCo3aHR0cHM6Ly9hbGljZG4ucHBwb2tlci5jbHViL3Bva2VyL3Jlc291cmNlL2hlYWRfZm94LnBuZzAEOAZAAEgAUABYAGAAaAByCggAGAAgACoAWAB4AIABAJgBAKABAagBALABALgBAMABANABAOoBFGhlYWRfZnJhbWVfdmlwX2JsYWNrgAIAiAIAmgIAsAIAuAIAwAIA2AIAEtoBCg7Qn9Cw0LvQsNGC0LAjNhD0AxjgASDzvZsGKlpodHRwczovL2FsaWNkbi5wcHBva2VyLmNsdWIvcG9rZXIvaGVhZGljb24vaGVhZC9lZGU5NDk3MDkzYWIwNzE5ZmJhYmNkM2Y1OTk2MGMzZF8xMTk2Mi5qcGcwBzgSQABIAFAAWABgAGgAcgoIABgAIAAqAFgAeACAAQCYAQCgAQKoAQCwAQC4AQDAAQDQAQDqARVoZWFkX2ZyYW1lX3ZpcF9zaWx2ZXKAAgCIAgCaAgCwAgC4AgDAAgDYAgASsgEKBkFKTE9TTxCFCRjj/v////////8BIPaPtgYqOWh0dHBzOi8vYWxpY2RuLnBwcG9rZXIuY2x1Yi9wb2tlci9yZXNvdXJjZS9oZWFkX3RpZ2VyLnBuZzAHOAlAAEgAUABYAGAAaAByCggAGAAgACoAWAB4AIABAJgBAKABAqgBALABALgBAMABANABAOoBDmhlYWRfZnJhbWVfZGVmgAIAiAIAmgIAsAIAuAIAwAIA2AIAErgBCgpwcDEzNzc4NzIwEMgBGPD//////////wEgoP7IBio7aHR0cHM6Ly9hbGljZG4ucHBwb2tlci5jbHViL3Bva2VyL3Jlc291cmNlL2hlYWRfdmFtcGlyZS5wbmcwAzgAQABIAFAAWABgAGgAcgoIABgAIAAqAFgAeACAAQCYAQCgAQGoAQCwAQC4AQDAAQDQAQDqAQ5oZWFkX2ZyYW1lX2RlZoACAIgCAJoCALACALgCAMACANgCABqDAQjzvZsGEg7Qn9Cw0LvQsNGC0LAjNhpaaHR0cHM6Ly9hbGljZG4ucHBwb2tlci5jbHViL3Bva2VyL2hlYWRpY29uL2hlYWQvZWRlOTQ5NzA5M2FiMDcxOWZiYWJjZDNmNTk5NjBjM2RfMTE5NjIuanBnIgtBdmVudGFkdXIgOSiGoOwBGl4Iu4THBhIIYm9iYTQzMjEaOWh0dHBzOi8vYWxpY2RuLnBwcG9rZXIuY2x1Yi9wb2tlci9yZXNvdXJjZS9oZWFkX3RpZ2VyLnBuZyINUGFyYWRpc2UgTWFwcyjkkskBGlcI45jIBhIJS2FsYXl0YW5vGjVodHRwczovL2FsaWNkbi5wcHBva2VyLmNsdWIvcG9rZXIvcmVzb3VyY2UvaGVhZF9xLnBuZyIL0KAuINCgLiDQoS4o4CIgACgBOAJIA1AAWgBwAA==")
_V4_PLAYING_STATUS = base64.b64decode("GAIgDSgaMNQHYApoAIABAIgBAJgBAKABAKgBALABALoBDxD///////////8BGAAgAJgCAKACAKgCAg==")

SUIT_CODE = {"CLUBS": 1, "DIAMONDS": 2, "HEARTS": 3, "SPADES": 4}
RANK_CODE = {
    "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SEVEN": 7,
    "EIGHT": 8, "NINE": 9, "TEN": 10, "JACK": 11, "QUEEN": 12,
    "KING": 13, "ACE": 14,
}


@dataclass(frozen=True)
class ProtocolProfile:
    """PPP wire identity for a Coin game family.

    ``RoundHintMultipleTableRSP.Roomtype`` (field 1) is a legacy uint and is
    not the same value as the current ``RoomType`` enum in field 12. We only
    emit legacy fields for families for which a live PPP packet proves the
    value; inventing an NLH value for PLO4/PLO6 is worse than omitting it.
    """
    room_type: int
    game_mode: int
    hint_roomtype: Optional[int]
    ppsrid: int = PPP_PPSRID
    hint_ppsrid: Optional[int] = None

def ppp_game_mode(props: dict[str, Any]) -> int:
    """Translate COIN mini-game identity; fail closed instead of pretending an unknown game is NLH."""
    try:
        mini = int(props.get("miniGameTypeId"))
    except (TypeError, ValueError):
        mini = None
    game_type = str(props.get("gameType") or "").lower()
    if game_type == "allinfold":
        return 13
    # PPP's GameMode.Regular is shared by NLH and four-card PLO; the number of
    # hole cards in HandCardRSP disambiguates them for the hand parser.  The
    # other values are the enum values recovered from the shipped PPP model.
    mapping = {1: 0, 2: 0, 17: 10, 20: 18}
    if mini in mapping:
        return mapping[mini]
    raise ValueError(f"unsupported COIN game mode: miniGameTypeId={mini!r} gameType={props.get('gameType')!r}")


def protocol_profile(props: dict[str, Any]) -> ProtocolProfile:
    """Return only wire values established by the PPP model/live captures."""
    game_mode = ppp_game_mode(props)
    try:
        mini = int(props.get("miniGameTypeId"))
    except (TypeError, ValueError):
        mini = None
    game_type = str(props.get("gameType") or "").lower()

    # PLO5 uses Omaha/Plo5 plus its established legacy hint room type. PPSRID
    # 227 belonged to a different PPSR table and is not a PLO5 family constant;
    # this target is an ordinary club-only room.
    if mini == 17 and game_type != "allinfold":
        return ProtocolProfile(PPP_OMAHA_ROOM_TYPE, game_mode,
                               PPP_PLO5_HINT_ROOMTYPE)
    # The shipped model proves the current RoomType/GameMode values for PLO4
    # and PLO6, but no capture proves their legacy RoundHint field 1. Omit it.
    if mini in (2, 20) and game_type != "allinfold":
        return ProtocolProfile(PPP_OMAHA_ROOM_TYPE, game_mode, None)
    # NLH live references use 5/0 and legacy hint value 3. Preserve the known
    # legacy envelope for All-in-or-Fold until a dedicated reference exists.
    return ProtocolProfile(PPP_ROOM_TYPE, game_mode, PPP_HINT_ROOMTYPE)

ACTION_CODE = {
    "FOLD": 1, "CHECK": 2, "CALL": 3, "RAISE": 4, "WAIT": 5,
    "BET": 7, "SB": 8, "BB": 9, "ANTE": 10,
    "AUTOBB": 11, "POST_BB": 11, "FORCE_BB": 11, "STRADDLE": 14,
}


def ppp_action_for_state(action: str, current_before: int, target: int,
                         last_full_raise: int) -> str:
    """Translate Coin's caption into the stateful PPP action enum.

    Coin calls an opening post-flop wager ``Raise`` in its seat snapshots. PPP
    distinguishes that from a raise over an existing wager and rejects RAISE on
    an unopened street. Conversely, an occasional Coin ``Bet`` over an existing
    wager is a PPP RAISE. ALLIN has no PPP enum and is classified from the wager
    state; a short all-in raise remains CALL and does not reopen betting.
    """
    normalized=re.sub(r"[^A-Z0-9]+","_",str(action or "").upper()).strip("_")
    current=max(0,int(current_before)); target=max(0,int(target)); full=max(1,int(last_full_raise or 0))
    if normalized in ("ALLIN","ALL_IN"):
        increase=max(0,target-current)
        if target<=current or (current>0 and increase<full):return "CALL"
        return "BET" if current==0 else "RAISE"
    if normalized=="RAISE" and current==0:return "BET"
    if normalized=="BET" and current>0:return "RAISE"
    return normalized
ROUND_STAGE = {"PREFLOP": 1, "FLOP": 2, "TURN": 3, "RIVER": 4}

# ---------------------------------------------------------------------------
# Protobuf wire helpers (only wire types needed by the reference stream).
# ---------------------------------------------------------------------------
def _varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def p_int(field_no: int, value: int, *, emit_zero: bool = True) -> bytes:
    if value == 0 and not emit_zero:
        return b""
    return _varint((field_no << 3) | 0) + _varint(int(value))


def p_bytes(field_no: int, value: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(value)) + value


def p_str(field_no: int, value: str) -> bytes:
    return p_bytes(field_no, value.encode("utf-8"))


def p_msg(field_no: int, value: bytes) -> bytes:
    return p_bytes(field_no, value)


def money(x: Any, scale: int = 100) -> int:
    if x is None:
        return 0
    return int(round(float(x) * scale))


def _finite_decimal(value: Any) -> Optional[Decimal]:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def coin_money_quantum(*values: Any, fallback_scale: int = 100) -> Decimal:
    """Smallest decimal currency unit exposed by Coin's blinds/options."""
    decimals = [value for value in (_finite_decimal(x) for x in values)
                if value is not None and value != 0]
    if not decimals:
        return Decimal(1) / Decimal(max(1, int(fallback_scale)))
    exponent = min([0, *(value.as_tuple().exponent for value in decimals)])
    return Decimal(1).scaleb(exponent)


@dataclass(frozen=True)
class MoneyProfile:
    """One table's reversible Coin-currency <-> PPP-chip normalization.

    The target club accepts a discrete set of table limits (big blinds).  Coin's
    dynamic BB is mapped to the nearest allowed target, while every non-SB amount
    uses the exact target-wire-BB/raw-Coin-BB factor.  The forced SB and PPP Blind
    fields are the explicit target BB/2, because a Coin .02/.05 SB is not half its
    BB.  Raw Coin hand/turn state is deliberately not mutated.
    """

    base_scale: int
    chip_scale: float
    coin_small_blind: Decimal
    coin_big_blind: Decimal
    target_big_blind: Decimal
    ppp_small_blind: int
    ppp_big_blind: int
    coin_quantum: Decimal
    valid_blinds: bool

    @property
    def normalized(self) -> bool:
        return self.valid_blinds and self.chip_scale != self.base_scale

    @property
    def factor(self) -> float:
        return self.chip_scale / self.base_scale

    def to_ppp(self, coin_amount: Any) -> int:
        return money(coin_amount, self.chip_scale)

    def from_ppp(self, ppp_amount: Any) -> float:
        value = _finite_decimal(ppp_amount)
        if value is None:
            raise ValueError(f"invalid PPP amount {ppp_amount!r}")
        return float(value / Decimal(str(self.chip_scale)))

    def quantize_coin(self, coin_amount: Any, *, minimum: Any = None,
                      maximum: Any = None, quantum: Any = None) -> float:
        """Round to Coin's dynamically derived currency quantum and legal range."""
        value = _finite_decimal(coin_amount)
        if value is None:
            raise ValueError(f"invalid Coin amount {coin_amount!r}")
        unit = _finite_decimal(quantum) if quantum is not None else self.coin_quantum
        if unit is None or unit <= 0:
            unit = Decimal(1) / Decimal(self.base_scale)
        result = (value / unit).to_integral_value(rounding=ROUND_HALF_UP) * unit
        lo = _finite_decimal(minimum) if minimum is not None else None
        hi = _finite_decimal(maximum) if maximum is not None else None
        if lo is not None and result < lo:
            result = (lo / unit).to_integral_value(rounding=ROUND_CEILING) * unit
        if hi is not None and result > hi:
            result = (hi / unit).to_integral_value(rounding=ROUND_FLOOR) * unit
        if lo is not None and result < lo or hi is not None and result > hi:
            raise ValueError(f"no Coin quantum {unit} inside legal range [{lo},{hi}]")
        return float(result)

    def ppp_to_legal_coin(self, ppp_amount: Any, *, minimum: Any = None,
                          maximum: Any = None, quantum: Any = None) -> float:
        """Inverse-map a PPP integer, preferring a legal forward-conserving value."""
        exact = _finite_decimal(self.from_ppp(ppp_amount))
        unit = _finite_decimal(quantum) if quantum is not None else self.coin_quantum
        if exact is None or unit is None or unit <= 0:
            raise ValueError(f"invalid inverse amount={ppp_amount!r} quantum={quantum!r}")
        primary = _finite_decimal(self.quantize_coin(
            exact, minimum=minimum, maximum=maximum, quantum=unit))
        lo = _finite_decimal(minimum) if minimum is not None else None
        hi = _finite_decimal(maximum) if maximum is not None else None
        requested = int(round(float(ppp_amount)))
        candidates = []
        # Adjacent currency ticks are enough: the source interval is one PPP chip
        # wide and primary is already the nearest tick to its exact center.
        for offset in (0, -1, 1, -2, 2):
            value = primary + unit * offset
            if lo is not None and value < lo or hi is not None and value > hi:
                continue
            candidates.append(value)
        conserving = [value for value in candidates if self.to_ppp(value) == requested]
        chosen = min(conserving or candidates, key=lambda value: abs(value - exact))
        return float(chosen)


class UnrepresentableMoneyProfile(ValueError):
    """Coin's legal currency tick cannot be conserved in integer PPP chips."""


def derive_money_profile(small_blind: Any, big_blind: Any,
                         base_scale: int = 100) -> MoneyProfile:
    """Map the dynamic Coin BB cash value to the nearest target-club UI limit.

    The allowed values are target-club protocol data, not a Coin stake lookup.
    No player count, stack or buy-in participates in the selection. With the
    supplied 1 Coin chip = 1 USDT, USD/RUB=83.99 and target chip=0.9 RUB,
    Coin .02/.05 selects target BB 4: wire SB/BB=200/400 and
    scale=400/.05=8000.
    """
    base = int(base_scale)
    if base <= 0:
        raise ValueError(f"chip scale must be positive, got {base_scale!r}")
    sb = _finite_decimal(small_blind)
    bb = _finite_decimal(big_blind)
    positive = sb is not None and bb is not None and sb > 0 and bb > 0
    if not positive:
        zero = Decimal(0)
        return MoneyProfile(base, float(base), sb or zero, bb or zero, zero,
                            money(sb or zero, base), money(bb or zero, base),
                            Decimal(1) / Decimal(base), False)

    quantum = coin_money_quantum(sb, bb, fallback_scale=base)
    # Match cash value, not the unrelated numeric chip denomination.  Ties choose
    # the lower target limit, matching the supplied .60 Coin BB -> 60 target case.
    desired_target_bb = bb * COIN_CHIP_USDT * USD_RUB / TARGET_PPP_CHIP_RUB
    target_bb = min(PPP_TARGET_BIG_BLINDS,
                    key=lambda value: (abs(value - desired_target_bb), value))
    ppp_bb = int((target_bb * PPP_WIRE_CURRENCY_SCALE).to_integral_value(
        rounding=ROUND_HALF_UP))
    if ppp_bb <= 0 or ppp_bb % 2:
        raise ValueError(f"target PPP big blind is not splittable: {target_bb} -> {ppp_bb}")
    ppp_sb = ppp_bb // 2
    scale_decimal = Decimal(ppp_bb) / bb
    tick_wire = quantum * scale_decimal
    if tick_wire != tick_wire.to_integral_value():
        # Without this gate, two legal Coin ticks can serialize differently from
        # their summed amount (for example .01+.01 at scale 6250 => 124 vs 125),
        # making Action/stack/pot conservation impossible in integer PPP chips.
        # All supplied/live stake grids are tick-compatible; fail closed on an
        # unseen incompatible grid instead of feeding EYE a broken game.
        raise UnrepresentableMoneyProfile(
            f"Coin money quantum {quantum} is incompatible with PPP scale "
            f"{scale_decimal} for BB {bb} -> {target_bb}")
    scale = float(scale_decimal)
    if money(bb, scale) != ppp_bb:
        raise ValueError(f"money scale cannot preserve Coin BB {bb} -> PPP {ppp_bb}")
    return MoneyProfile(base, scale, sb, bb, target_bb,
                        ppp_sb, ppp_bb, quantum, True)


def money_profile_for_hand(hand: Any, base_scale: int = 100) -> MoneyProfile:
    """Use actual per-hand blinds first, falling back to dynamic room metadata."""
    props = getattr(getattr(hand, "room", None), "props", {}) or {}
    pre = getattr(hand, "pre", {}) or {}

    def first_positive(*values: Any) -> Any:
        for value in values:
            dec = _finite_decimal(value)
            if dec is not None and dec > 0:
                return value
        return 0

    sb = first_positive(pre.get("sbAmount"), props.get("smallBlind"))
    bb = first_positive(pre.get("bbAmount"), props.get("bigBlind"))
    return derive_money_profile(sb, bb, base_scale)


def ppp_card(card: dict[str, Any]) -> int:
    # PPP card wire value is suit-byte + rank-byte (e.g. 0x03_0c in the reference).
    # Suit codes are centralized above because a wrong suit mapping changes poker semantics.
    return (SUIT_CODE[str(card["suit"]).upper()] << 8) | RANK_CODE[str(card["value"]).upper()]


# ---------------------------------------------------------------------------
# PCAP/PCAPNG -> length-prefixed 18010 JSON events.
# Supports DLT_NULL (0), Ethernet (1) and Linux cooked (113).
# ---------------------------------------------------------------------------
def _pcap_records(path: str | Path):
    b = Path(path).read_bytes()
    if len(b) < 12:
        raise ValueError("short capture")
    if b[:4] == b"\x0a\x0d\x0d\x0a":
        off = 0; endian = "<"; links: list[int] = []
        while off + 12 <= len(b):
            block_type = b[off:off+4]
            if block_type == b"\x0a\x0d\x0d\x0a":
                bom = b[off+8:off+12]
                endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
                links.clear()
            block_len = struct.unpack(endian + "I", b[off+4:off+8])[0]
            if block_len < 12 or off + block_len > len(b):
                raise ValueError("invalid pcapng block length")
            body = b[off+8:off+block_len-4]
            block_no = struct.unpack(endian + "I", block_type)[0]
            if block_no == 1 and len(body) >= 8:  # Interface Description Block
                links.append(struct.unpack(endian + "H", body[:2])[0])
            elif block_no == 6 and len(body) >= 20:  # Enhanced Packet Block
                iface, ts_hi, ts_lo, caplen, _origlen = struct.unpack(endian + "IIIII", body[:20])
                if iface < len(links) and 20 + caplen <= len(body):
                    ts = (ts_hi << 32) | ts_lo
                    yield ts / 1e6, links[iface], body[20:20+caplen]
            off += block_len
        return
    if len(b) < 24:
        raise ValueError("short pcap")
    magic = b[:4]
    endian = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1") else ">"
    ns = magic in (b"M<\xb2\xa1", b"\xa1\xb2<M")
    link = struct.unpack(endian + "IHHIIII", b[:24])[-1]
    off = 24
    while off + 16 <= len(b):
        sec, frac, caplen, _ = struct.unpack(endian + "IIII", b[off:off+16])
        off += 16
        pkt = b[off:off+caplen]
        off += caplen
        yield sec + frac / (1e9 if ns else 1e6), link, pkt


def _tcp_payload(link: int, pkt: bytes):
    d = pkt
    if link == 0:  # DLT_NULL
        if len(d) < 4 or struct.unpack("<I", d[:4])[0] != 2:
            return None
        d = d[4:]
    elif link == 113:  # Linux cooked v1
        if len(d) < 16 or struct.unpack("!H", d[14:16])[0] != 0x0800:
            return None
        d = d[16:]
    elif link == 1:  # Ethernet
        if len(d) < 14 or struct.unpack("!H", d[12:14])[0] != 0x0800:
            return None
        d = d[14:]
    else:
        return None
    if len(d) < 20 or d[0] >> 4 != 4 or d[9] != 6:
        return None
    ihl = (d[0] & 15) * 4
    src = socket.inet_ntoa(d[12:16]); dst = socket.inet_ntoa(d[16:20])
    t = d[ihl:]
    if len(t) < 20:
        return None
    sport, dport, seq, _ack, flagsword = struct.unpack("!HHIIH", t[:14])
    doff = (flagsword >> 12) * 4
    return src, sport, dst, dport, seq, t[doff:]


def _reassemble(parts: list[tuple[int, bytes]]) -> bytes:
    parts = sorted(parts, key=lambda x: x[0])
    out = bytearray(); end = None
    for seq, payload in parts:
        if not payload:
            continue
        if end is None:
            end = seq
        if seq > end:  # capture gap; preserve offsets so prefix scan can recover later frames
            out.extend(b"\x00" * (seq - end)); end = seq
        overlap = end - seq
        if overlap < len(payload):
            out.extend(payload[max(0, overlap):])
            end = max(end, seq + len(payload))
    return bytes(out)


def _lp_frames(buf: bytes):
    off = 0
    while off + 4 <= len(buf):
        n = struct.unpack(">I", buf[off:off+4])[0]
        if not (0 < n <= 20_000_000) or off + 4 + n > len(buf):
            found = False
            for j in range(off + 1, min(len(buf) - 4, off + 10000)):
                nn = struct.unpack(">I", buf[j:j+4])[0]
                if 0 < nn <= 20_000_000 and j + 4 + nn <= len(buf) and buf[j+4:j+5] in (b"{", b"["):
                    off = j; found = True; break
            if not found:
                return
            continue
        yield buf[off+4:off+4+n]
        off += 4 + n


def load_18010_events_from_pcap(path: str | Path, port: int = 18010) -> list[dict[str, Any]]:
    flows: dict[tuple[str,int,str,int], list[tuple[int,bytes]]] = collections.defaultdict(list)
    for _ts, link, pkt in _pcap_records(path):
        x = _tcp_payload(link, pkt)
        if not x:
            continue
        src, sp, dst, dp, seq, payload = x
        if dp == port and payload:
            flows[(src,sp,dst,dp)].append((seq,payload))
    objs = []
    for key, parts in flows.items():
        for raw in _lp_frames(_reassemble(parts)):
            try:
                o = json.loads(raw)
                o["_flow"] = key
                objs.append(o)
            except Exception:
                pass
    # IDs end in a monotonic decimal suffix in the supplied capture. Preserve TCP-flow order
    # unless that suffix is available; then sorting restores global event chronology.
    def order(o: dict[str,Any]):
        m = re.search(r"-(\d+)$", str(o.get("id", "")))
        return int(m.group(1)) if m else 10**18
    objs.sort(key=order)
    return objs


# ---------------------------------------------------------------------------
# COIN BlueBox/SFS payload extraction.
# We only need the named JSON values already present in the intercepted WS payload.
# ---------------------------------------------------------------------------
def coin_payload_bytes(o: dict[str, Any]) -> bytes:
    # Native HMN1 ingress carries the exact Coin WebSocket frame as _raw to
    # avoid Base64/JSON work on Android.  Treat it as the canonical payload.
    raw=o.get("_raw")
    if isinstance(raw,(bytes,bytearray,memoryview)):
        return bytes(raw)
    try:
        return base64.b64decode(o.get("payload_b64", ""))
    except Exception:
        return b""


def maybe_decompress(b: bytes) -> bytes:
    # Compressed BlueBox messages in the capture have a tiny envelope followed by zlib.
    for i in range(min(16, max(0, len(b)-2))):
        if b[i:i+2] in (b"x\x9c", b"x\xda", b"x\x01"):
            try:
                return zlib.decompress(b[i:])
            except Exception:
                pass
    return b


def extract_json_after(b: bytes, anchor: bytes = b"data") -> Optional[Any]:
    start = 0
    while True:
        p = b.find(anchor, start)
        if p < 0:
            return None
        for j in range(p + len(anchor), min(len(b), p + len(anchor) + 64)):
            if b[j] in (ord("{"), ord("[")):
                try:
                    s = b[j:].decode("utf-8", "ignore")
                    return json.JSONDecoder().raw_decode(s)[0]
                except Exception:
                    pass
        start = p + len(anchor)


def extract_all_json_after(b: bytes, anchor: bytes) -> list[Any]:
    out = []; start = 0
    while True:
        p = b.find(anchor, start)
        if p < 0:
            break
        found = False
        for j in range(p + len(anchor), min(len(b), p + len(anchor) + 96)):
            if b[j] in (ord("{"), ord("[")):
                try:
                    s = b[j:].decode("utf-8", "ignore")
                    obj, _ = json.JSONDecoder().raw_decode(s)
                    out.append(obj); found = True; break
                except Exception:
                    pass
        start = p + len(anchor)
    return out


def coin_event_name(b: bytes) -> Optional[str]:
    ms = re.findall(rb"game\.[A-Za-z0-9_]+", b)
    return ms[-1].decode() if ms else None


def coin_hand_id(b: bytes, data: Any) -> Optional[int]:
    m = re.search(rb"gameHandId[^0-9]{0,12}([0-9]{8,})", b)
    if m:
        return int(m.group(1))
    if isinstance(data, dict):
        for k in ("gameHandId", "handId", "gameId"):
            v = data.get(k)
            if isinstance(v, int) and v > 10**8:
                return v
    return None


def table_id_from_hand(hand_id: int) -> int:
    # Observed COIN ids are <tableId><5-digit hand sequence>, e.g. 1118286 00004.
    return hand_id // 100000


def printable_strings(b: bytes, minlen: int = 4) -> list[str]:
    return [m.group().decode("utf-8", "replace") for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, b)]


@dataclass
class CoinEvent:
    idx: int
    raw: dict[str, Any]
    body: bytes
    name: Optional[str]
    data: Any
    hand_id: Optional[int]
    table_id: Optional[int]
    hook_room: Optional[int] = None


def decode_coin_events(objs: list[dict[str, Any]]) -> list[CoinEvent]:
    out = []
    for i, o in enumerate(objs):
        b = maybe_decompress(coin_payload_bytes(o))
        data = extract_json_after(b, b"data")
        name = coin_event_name(b)
        hid = coin_hand_id(b, data)
        tid = table_id_from_hand(hid) if hid else None
        hook_room = None
        try:
            from .coin_action_wire import decode_packet
            packet = decode_packet(coin_payload_bytes(o))
            ext = packet.get("p") if isinstance(packet, dict) else None
            room = ext.get("r") if isinstance(ext, dict) else None
            if isinstance(room, (int, float)):
                hook_room = int(room)
        except Exception:
            pass
        out.append(CoinEvent(i, o, b, name, data, hid, tid, hook_room))
    return out


@dataclass
class RoomMeta:
    table_id: int
    config_id: Optional[int] = None
    room_name: str = "COIN TABLE"
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class HandModel:
    hand_id: int
    table_id: int
    hero_id: int
    hero_name: str
    hero_seat: int
    roster: list[dict[str, Any]]
    pre: dict[str, Any]
    cards: list[dict[str, Any]]
    turn: dict[str, Any]
    room: RoomMeta
    events_before_turn: list[CoinEvent]
    timestamp_ms: int

    @property
    def ppp_hero_seat(self) -> int:
        return self.hero_seat - 1


class CoinCaptureModel:
    def __init__(self, events: list[CoinEvent]):
        self.events = events
        self.hero_id = 0
        self.hero_name = "HERO"
        self.rooms_by_table: dict[int, RoomMeta] = {}
        self.room_props_by_config: dict[int, dict[str, Any]] = {}
        self.hook_rooms_by_table: dict[int, set[int]] = collections.defaultdict(set)
        self._discover_identity_and_rooms()

    def _discover_identity_and_rooms(self):
        # Login response: first JSON with userId/userName/sessionId. Ignore auth/JWT contents.
        for e in self.events:
            d = e.data
            if isinstance(d, dict) and isinstance(d.get("userId"), int) and d.get("userId",0) > 0 and d.get("userName") and "sessionId" in d:
                self.hero_id = d["userId"]; self.hero_name = str(d["userName"]); break
        # Discover every GameRoomProperties JSON, indexed by configId.
        for e in self.events:
            for p in extract_all_json_after(e.body, b"GameRoomProperties"):
                if isinstance(p, dict) and isinstance(p.get("configId"), int):
                    self.room_props_by_config[p["configId"]] = p
        # wait_list_data binds tableId -> configId.
        for e in self.events:
            d = e.data
            if e.name == "game.wait_list_data" and isinstance(d, dict) and isinstance(d.get("tableId"), int):
                tid = d["tableId"]; cid = d.get("configId")
                self.rooms_by_table.setdefault(tid, RoomMeta(tid, cid))
                self.rooms_by_table[tid].config_id = cid
                if e.hook_room is not None:
                    self.hook_rooms_by_table[tid].add(e.hook_room)
            elif e.name in ("game.game_init", "game.game_alldata") and isinstance(d, dict) and isinstance(d.get("tableId"), int):
                if e.hook_room is not None:
                    self.hook_rooms_by_table[int(d["tableId"])].add(e.hook_room)
        # Room names are printable strings ending in the table id. Search globally.
        all_strings = []
        for e in self.events:
            all_strings.extend(printable_strings(e.body))
        for tid, room in self.rooms_by_table.items():
            candidates = [s for s in all_strings if re.search(rf"\b{tid}$", s)]
            if candidates:
                room.room_name = min(candidates, key=len).lstrip("!\"#'()")
            if room.config_id in self.room_props_by_config:
                room.props = self.room_props_by_config[room.config_id]

    def candidate_hands(self) -> list[tuple[int,int,int]]:
        """(hand_id, seed_idx, first_hero_turn_idx).

        Preferred seed is Coin's normal ``pre_hand_start_info``.  On a Trainer
        attach/restart during an already-open HU table Coin can instead send a
        pristine ``game_alldata`` snapshot at the start of the current hand.
        Accept that snapshot only when its action history is still empty and the
        round is PREFLOP; later mid-hand snapshots are intentionally not invented.
        """
        pre_by_hand: dict[int,int] = {}
        for e in self.events:
            if e.name == "game.pre_hand_start_info" and isinstance(e.data, dict):
                hid = int(e.data.get("gameId") or e.hand_id or 0)
                if hid:
                    pre_by_hand[hid] = e.idx

        # Safe late-attach seed observed in the supplied HU capture.
        for e in self.events:
            if e.name != "game.game_alldata" or not isinstance(e.data, dict):
                continue
            init = e.data.get("gameInitResponseData")
            seats = e.data.get("seatInfoRsponseData") or e.data.get("seatInfoResponseData")
            pot = e.data.get("potInfoResponseData") or {}
            history = e.data.get("gameActionHistoryResponseData") or {}
            if not isinstance(init, dict) or not isinstance(seats, dict):
                continue
            hid = int(init.get("gameId") or init.get("handId") or 0)
            if not hid or hid in pre_by_hand:
                continue
            rows = seats.get("seatResponseDataList") or []
            actions = history.get("gameActionMessagesHistory") if isinstance(history, dict) else None
            round_name = str(pot.get("roundName") or "PREFLOP").upper() if isinstance(pot, dict) else "PREFLOP"
            dealer_cards = init.get("dealerCards") or {}
            if not rows or (actions not in (None, [])) or round_name not in {"", "ANTE", "PREFLOP"} or dealer_cards:
                continue
            pre_by_hand[hid] = e.idx

        out = []
        for hid, pi in sorted(pre_by_hand.items(), key=lambda kv: kv[1]):
            tid = table_id_from_hand(hid)
            hook_rooms = self.hook_rooms_by_table.get(tid, set())
            for e in self.events:
                if e.idx <= pi:
                    continue
                same_scope = e.table_id == tid or (
                    e.hook_room is not None and e.hook_room in hook_rooms
                )
                if not same_scope:
                    continue
                # Never let an interrupted/missing hand borrow the first hero turn
                # from the following hand on the same SmartFox room.
                if e.name in {"game.pre_hand_start_info", "game.game_alldata"}:
                    next_hid = 0
                    if isinstance(e.data, dict):
                        source = (
                            e.data.get("gameInitResponseData")
                            if isinstance(e.data.get("gameInitResponseData"), dict)
                            else e.data
                        )
                        try:
                            next_hid = int(source.get("gameId") or source.get("handId") or e.hand_id or 0)
                        except (TypeError, ValueError):
                            next_hid = 0
                    if next_hid and next_hid != hid:
                        break
                belongs = e.hand_id == hid or (
                    e.hook_room is not None and e.hook_room in hook_rooms
                )
                if not belongs:
                    continue
                if e.name == "game.user_turn" and isinstance(e.data, dict) and e.data.get("whoseTurn") == self.hero_name:
                    out.append((hid, pi, e.idx)); break
        return out

    def build_hand(self, hand_id: Optional[int] = None) -> HandModel:
        cands = self.candidate_hands()
        if not cands:
            raise ValueError("no COIN hand with a hero turn found")
        if hand_id is None:
            hand_id, pre_idx, turn_idx = cands[-1]  # latest captured hero hand
        else:
            matches = [x for x in cands if x[0] == hand_id]
            if not matches:
                raise ValueError(f"hand {hand_id} not found among hero-turn hands: {[x[0] for x in cands]}")
            hand_id, pre_idx, turn_idx = matches[0]
        tid = table_id_from_hand(hand_id)
        hook_rooms = self.hook_rooms_by_table.get(tid, set())
        def belongs(e: CoinEvent) -> bool:
            return e.table_id == tid or (e.hook_room is not None and e.hook_room in hook_rooms)
        def in_hand_window(e: CoinEvent) -> bool:
            if not (pre_idx <= e.idx <= turn_idx) or not belongs(e):
                return False
            # An explicit different hand id always wins over room affinity.  Most
            # Coin action packets omit handId entirely, so hand_id=None is accepted
            # only inside this bounded pre-hand -> first hero-turn window.
            return e.hand_id in (None, hand_id)
        pre_e = next(e for e in self.events if e.idx == pre_idx)
        turn_e = next(e for e in self.events if e.idx == turn_idx)

        snapshot_seed = pre_e.name == "game.game_alldata" and isinstance(pre_e.data, dict)
        pre_data = pre_e.data
        snapshot_roster = None
        if snapshot_seed:
            init = pre_e.data.get("gameInitResponseData") or {}
            seats = pre_e.data.get("seatInfoRsponseData") or pre_e.data.get("seatInfoResponseData") or {}
            if not isinstance(init, dict) or not isinstance(seats, dict):
                raise ValueError("invalid game_alldata recovery seed")
            pre_data = dict(init)
            pre_data["sbSeatId"] = int(init.get("smallBlindSeatId") or init.get("sbSeatId") or 0)
            pre_data["bbSeatId"] = int(init.get("bigBlindSeatId") or init.get("bbSeatId") or 0)
            pre_data["sbAmount"] = float(init.get("smallBlind") or init.get("sbAmount") or 0)
            pre_data["bbAmount"] = float(init.get("bigBlind") or init.get("bbAmount") or 0)
            pre_data["anteAmount"] = float(init.get("ante") or init.get("anteAmount") or 0)
            pre_data["_hmuriy_snapshot_recovery"] = True
            rows = seats.get("seatResponseDataList") or []
            snapshot_roster = []
            ante = float(pre_data.get("anteAmount") or 0)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                restored = dict(row)
                # game_alldata stacks are post-forced-post. Restore the pre-hand
                # stack only for this verified empty-history PREFLOP snapshot.
                try:
                    paid = float(restored.get("betAmout") or restored.get("betAmount") or 0)
                    if restored.get("isPlaying") is True:
                        paid += ante
                    restored["userChips"] = float(restored.get("userChips") or 0) + paid
                except (TypeError, ValueError):
                    pass
                snapshot_roster.append(restored)

        # COIN normally emits a fresh seatInfo immediately after pre_hand_start_info and
        # before forced posts. Prefer that snapshot: it has the authoritative UID->seat
        # mapping and isPlaying flags for THIS hand while stacks are still pre-blind.
        roster = snapshot_roster
        if roster is None:
            for e in self.events:
                if pre_idx <= e.idx <= turn_idx and belongs(e) and e.name == "game.seatInfo" and isinstance(e.data, dict):
                    rows = e.data.get("seatResponseDataList")
                    if rows:
                        roster = [dict(r) for r in rows if isinstance(r, dict)]
                        break
        if not roster:
            # Fallback to the latest prior snapshot only when the hand did not carry one.
            for e in self.events:
                if e.idx >= pre_idx:
                    break
                if e.name == "game.seatInfo" and isinstance(e.data, dict) and belongs(e):
                    rows = e.data.get("seatResponseDataList")
                    if rows: roster = [dict(r) for r in rows if isinstance(r, dict)]
        if not roster:
            raise ValueError("could not recover table roster")
        hero_rows = [r for r in roster if int(r.get("userId") or 0) == self.hero_id or r.get("userName") == self.hero_name]
        if not hero_rows:
            # Reservation/take-seat can race the seatInfo snapshot. Replace any stale
            # occupant at the same seat instead of keeping two UIDs on one PPP seat.
            for e in self.events:
                if pre_idx <= e.idx <= turn_idx and belongs(e) and e.name == "game.seat" and isinstance(e.data, dict) and (
                    int(e.data.get("userId") or 0) == self.hero_id or e.data.get("userName") == self.hero_name):
                    fresh = dict(e.data); seat = int(fresh.get("seatId") or 0)
                    roster = [r for r in roster if int(r.get("seatId") or 0) != seat and int(r.get("userId") or 0) != self.hero_id]
                    roster.append(fresh); hero_rows = [fresh]; break
        if not hero_rows:
            raise ValueError("hero seat not found")
        hero_seat = int(hero_rows[0]["seatId"])

        cards = []
        for e in self.events:
            if in_hand_window(e) and e.name == "game.player_info" and isinstance(e.data, dict):
                cards = e.data.get("playerCards") or []
                if cards: break
        if len(cards) < 2:
            raise ValueError("hero hole cards not found before hero turn")

        room = self.rooms_by_table.get(tid, RoomMeta(tid))
        # If no wait_list binding was seen, infer common cash settings from pre-hand.
        if not room.props:
            room.props = {
                "smallBlind": pre_data.get("sbAmount", 0), "bigBlind": pre_data.get("bbAmount", 0),
                "ante": pre_data.get("anteAmount", 0), "minBuyIn": 0, "maxBuyIn": 0,
                "maxSize": max([int(r.get("seatId") or 0) for r in roster] or [0]), "playerHandTime": turn_e.data.get("turnTime", 0),
                # Regular NLH/PLO share PPP GameMode.Regular; card count is the only
                # honest fallback when Coin room properties were not captured.
                "miniGameTypeId": 1 if len(cards)==2 else 2,
            }
        ts = int(str(pre_data.get("initTimeStamp") or turn_e.data.get("initTimeStamp") or int(time.time()*1000)))
        return HandModel(hand_id, tid, self.hero_id, self.hero_name, hero_seat, roster, pre_data,
                         cards, turn_e.data, room,
                         [e for e in self.events if in_hand_window(e)], ts)


# ---------------------------------------------------------------------------
# PPP v4-like synthesis.
# ---------------------------------------------------------------------------
class PPPBuilder:
    def __init__(self, hand: HandModel, chip_scale: int = 100):
        self.h = hand; self.scale = chip_scale
        self.money_profile = money_profile_for_hand(hand, PPP_WIRE_CURRENCY_SCALE)
        self.target_money_active = (
            self.money_profile.valid_blinds
            and abs(float(self.scale) - float(self.money_profile.chip_scale)) <= 1e-9
        )
        self.profile = protocol_profile(hand.room.props)
        self.frames: list[dict[str,Any]] = []
        self.now = hand.timestamp_ms
        self.roster_by_name = {str(r.get("userName")): r for r in hand.roster if r.get("userName")}
        self.roster_by_seat = {int(r.get("seatId")): r for r in hand.roster if r.get("seatId")}

    def _wire_small_blind(self) -> int:
        if self.target_money_active:return self.money_profile.ppp_small_blind
        p=self.h.room.props; pre=self.h.pre
        return money(pre.get("sbAmount") or p.get("smallBlind",0),self.scale)

    def _wire_big_blind(self) -> int:
        if self.target_money_active:return self.money_profile.ppp_big_blind
        p=self.h.room.props; pre=self.h.pre
        return money(pre.get("bbAmount") or p.get("bigBlind",0),self.scale)

    def _brief(self, r: dict[str,Any]) -> bytes:
        uid = int(r.get("userId") or 0); name = str(r.get("userName") or f"P{uid}")
        # UID/name are COIN-derived. Club identity is the only PPP-only roster context retained.
        return b"".join([p_int(1, uid), p_str(2, name),
                         (p_str(4,PPP_CLUB_NAME) if PPP_CLUB_NAME else b""),
                         p_int(5, PPP_CLUB_ID)])

    def _seat_status(self, r: dict[str,Any], *, wait_blind: bool = False, in_game: bool = False) -> bytes:
        """Minimal coherent SeatStatus built from COIN instead of copying stale PPP users."""
        seat = int(r.get("seatId") or 1) - 1
        chips = money(r.get("userChips", 0), self.scale)
        # ActionSited=6 mirrors a freshly seated PPP player. DealerInfo/ActionBRC then
        # transition the actual hand state. WaitBlind is explicitly cancelled before deal.
        return b"".join([
            p_int(1, seat), p_int(2, ACTION_CODE["WAIT"] + 1), p_msg(3, self._brief(r)),
            p_int(4, chips), p_int(5, 0), p_int(6, 0), p_int(7, 0),
            p_int(10, 1 if wait_blind else 0), p_int(11, 0), p_int(13, PPP_CLUB_ID),
            (p_str(16,PPP_CLUB_NAME) if PPP_CLUB_NAME else b""),
            p_int(19, 0), p_int(20, 0), p_int(21, 60),
            p_int(22, 0), p_int(28, 4), p_int(30, 0), p_int(34, 0),
            p_int(35, 0), p_int(36, 0), p_int(37, 0), p_int(38, 1 if in_game else 0),
        ])

    def _sitdown_brc(self, r: dict[str,Any], *, wait_blind: bool = False, in_game: bool = False) -> bytes:
        seat = int(r.get("seatId") or 1) - 1
        chips = money(r.get("userChips", 0), self.scale)
        return b"".join([p_int(1,seat), p_int(2,chips), p_msg(3,self._brief(r)),
                         p_msg(4,self._seat_status(r, wait_blind=wait_blind, in_game=in_game))])

    def _table_status(self) -> bytes:
        """Dynamic EnterRoomRSP.TableStatus without any reference-table users."""
        p = self.h.room.props
        seatnum = int(p.get("maxSize") or max(self.roster_by_seat, default=0))
        # Coin cannot provide a complete PPP mid-hand admission replay (pool/stage/
        # actor/actions). Advertise one coherent idle snapshot; DealerInfo immediately
        # rebuilds the first tracked hand. Hybrid "playing but stage=0" state breaks EYE.
        playing = False
        dealer = sbseat = bbseat = 0
        body = bytearray()
        body += p_int(1, 1 if playing else 0)       # IsPlaying
        body += p_int(2, -1)                        # no actor during admission
        body += p_int(3, dealer) + p_int(4, sbseat) + p_int(5, bbseat)
        for seat1 in range(1, seatnum + 1):
            row = self.roster_by_seat.get(seat1)
            if row and int(row.get("userId") or 0):
                # The PPP admission sequence reserves an empty hero seat here and then
                # announces it through SitDownBRC/RSP + TotalBuyinBRC exactly once.
                if int(row.get("userId") or 0)==self.h.hero_id:continue
                # Coin can expose a BookSeat-style reservation before the buy-in lands.
                # It is not a PPP SitDown yet; advertising a zero-stack, non-playing
                # opponent creates a phantom seated player in EYE.
                if money(row.get("userChips",0),self.scale)<=0 and row.get("isPlaying") is not True:continue
                body += p_msg(6, self._seat_status(
                    row,
                    wait_blind=False,
                    in_game=False,
                ))
        body += p_int(7, 0) + p_int(8, 0) + p_int(10, 1)
        body += p_str(16, self._game_id()) + p_int(19, 0)
        body += p_int(20, self._wire_small_blind())
        # field 27 RiverSqueeze is a message in the current model; omit when empty.
        # Idle admission is not the start of an actionable hand.  The official
        # client keeps InitStart false until DealerInfo/RoundStart establish it.
        body += p_int(28, 0) + p_int(36, 0) + p_int(37, 0)
        return bytes(body)

    def _room_status(self) -> bytes:
        playing = False
        return b"".join([
            # fields 2 Profit and 3 Observer are repeated messages, not varints.
            p_int(1, 0),
            p_int(4, 0), p_int(5, 1 if playing else 0), p_int(6, 0),
            p_int(9, 0), p_int(10, 0), p_str(11, ""), p_int(14, 0),
        ])

    def _playing_status(self) -> bytes:
        # This is the local player's table view. Hole cards and actionable limits are
        # sent later by HandCardRSP/ActionNotifyBRC; admission must not contain stale data.
        return b"".join([
            p_int(3, -1), p_int(4, 0), p_int(5, 0), p_int(6, 0), p_int(7, 0),
            p_int(12, 10), p_int(13, 0), p_int(14, 0), p_int(19, 0),
            p_int(20, 0), p_int(30, 0), p_int(36, 0),
            # No cards and no actor exist at admission.  ActionNotifyBRC later
            # supplies the dynamic count for the actual hand.
            p_int(37, 0),
        ])

    def _enter_room_rsp(self) -> bytes:
        return b"".join([
            p_int(1, 0), p_msg(3, self._table_status()), p_msg(4, self._room_status()),
            p_msg(5, self._playing_status()), p_msg(6, self._room_info()),
            p_int(7, self.profile.room_type), p_int(11, PPP_ROOM_MODE),
            p_msg(14, self._multiple_table_room_info()), p_msg(17, b""),
            p_msg(18, p_int(1, 0)), p_msg(20, b""), p_msg(22, b""), p_msg(29, b""),
        ])

    def _room_info(self) -> bytes:
        p = self.h.room.props; pre = self.h.pre
        sb = self._wire_small_blind()
        ante = money(pre.get("anteAmount") or p.get("ante", 0), self.scale)
        minb = money(p.get("minBuyIn", 0), self.scale); maxb = money(p.get("maxBuyIn", 0), self.scale)
        seatnum = int(p.get("maxSize") or max(self.roster_by_seat, default=0))
        action_time = int(p.get("playerHandTime") or self.h.turn.get("turnTime") or 0)
        game_time = int(p.get("extraTime") or p.get("disconnectTime") or 0)
        profile = self.profile
        # Only PPP identity/policy fields are compatibility constants.  Every fact that
        # COIN actually exposes (limits, seats, timers, restrictions and game options)
        # must remain dynamic; otherwise a table switch silently poisons EYE's model.
        try: fee_point = int(round(float(p.get("rake") or 0)))
        except (TypeError, ValueError): fee_point = 0
        # Coin has used more than one property name for the RIT capability.  Its
        # iRitOptions is authoritative on current ring tables; isRabbitRunAllowed
        # is deliberately excluded because rabbit hunting is a different feature.
        rit_enabled = any(bool(p.get(key)) for key in (
            "isRit", "isRIT", "isRunItTwiceAllowed",
            "runItTwiceAllowed", "isRunItMultipleTimes",
        )) or bool(p.get("iRitOptions"))
        parts = [
            p_int(1, ppp_table_id(self.h.table_id)), p_str(2, self.h.room.room_name),
            p_int(4, sb), p_int(5, ante), p_int(6, minb), p_int(7, action_time), p_int(8, game_time),
            p_int(9, seatnum), p_int(10, 0), p_int(11, profile.room_type),
            p_int(12, 2 if p.get("isPotRake", True) else 0), p_int(13, fee_point), p_int(14, 0), p_int(15, 2),
            p_int(17, PPP_CLUB_ID), p_int(19, minb),
            (p_int(20,int(p.get("cap"))) if p.get("cap") is not None else b""), p_int(21, 0),
            p_int(22, maxb), p_int(23, 1 if rit_enabled else 0), p_int(24, 0),
            (p_str(26,PPP_CLUB_NAME) if PPP_CLUB_NAME else b""), p_str(29, ""),
            p_int(31, 1 if p.get("isGpsRestricted") or p.get("isGpsRestriction") else 0),
            p_int(32, 1 if p.get("isIpRestricted") or p.get("isIpRestriction") else 0),
            p_int(33,int(p.get("gpsDistanceLimit") or p.get("gpsDistance") or 0)),
            p_int(35, PPP_ROOM_MODE), p_int(37, 2), p_int(38, profile.game_mode),
            p_int(43, 100), p_int(44, 1), p_int(46, 1), p_int(47, 24), p_int(48, 1),
            p_int(52, 1 if p.get("evChopAllowed") or p.get("isEVChopAllowed") else 0),
            (p_int(72,1) if p.get("isBombpot") else b""),
            (p_int(73,int(p.get("handsInBombpot"))) if p.get("isBombpot") and p.get("handsInBombpot") is not None else b""),
            (p_int(75,int(p.get("_handsToBombpot"))) if p.get("_handsToBombpot") is not None else b""),
            (p_int(77,1) if p.get("_isBombpotHand") else b""),
            (p_int(78,int(float(p.get("minAnte")))) if p.get("isBombpot") and p.get("minAnte") is not None else b""),
            (p_int(79,int(float(p.get("maxAnte")))) if p.get("isBombpot") and p.get("maxAnte") is not None else b""),
            (p_int(80,1) if p.get("isBombpot") and p.get("bombpotInducesDoubleBoard") else b""),
            (p_int(81,1) if p.get("_isDoubleBoard") else b""),
            p_int(51, 1), p_int(58, 1), p_int(61, 1),
            p_int(68, profile.ppsrid), p_int(69, PPP_EXCHANGE_RATE),
            p_int(70, 1 if p.get("isJackpot") else 0),
            p_int(84, 0), p_int(88, 4), p_int(92, 1 if p.get("isStraddle") else 0),
        ]
        return b"".join(parts)

    def _multiple_table_room_info(self) -> bytes:
        p=self.h.room.props; pre=self.h.pre
        sb=self._wire_small_blind()
        minb=money(p.get("minBuyIn",0),self.scale)
        seatnum=int(p.get("maxSize") or max(self.roster_by_seat,default=0))
        profile=self.profile
        return b"".join([
            p_int(1,ppp_table_id(self.h.table_id)), p_int(2,PPP_CLUB_ID), p_int(3,profile.room_type),
            p_int(4,0), p_int(5,0), p_int(6,0), p_int(7,PPP_ROOM_MODE), p_int(8,profile.game_mode),
            p_int(9,seatnum), p_int(10,sb), p_str(11,self.h.room.room_name), p_int(12,minb),
            p_int(13,-1), p_int(14,0), p_str(15,""),
            p_int(18,0), p_int(19,profile.ppsrid), p_int(23,0),
        ])

    def _game_id(self) -> str:
        # PPP freezes this prefix for the whole room session; changing it every hand
        # makes EYE treat sequential hands as unrelated games. PPP room ids use the
        # server's UTC+8 wall clock rather than the local bridge timezone.
        prefix=str(self.h.room.props.get("_gameIdPrefix") or "")
        if not re.fullmatch(r"\d{12}",prefix):
            sec=max(0,int(self.h.timestamp_ms)//1000)
            prefix=time.strftime("%y%m%d%H%M%S", time.gmtime(sec+PPP_GAME_ID_UTC_OFFSET_SECONDS))
        serial=max(0,int(self.h.hand_id)-int(self.h.table_id)*100000)
        return f"{prefix}-{ppp_table_id(self.h.table_id)}-{serial:07d}-1"

    def _hand_card_rsp(self) -> bytes:
        card_fields = (1, 2, 3, 4, 5, 7)  # field 6 is DefaultCard, not Card6
        return b"".join(p_int(field_no, ppp_card(card)) for field_no, card in zip(card_fields, self.h.cards[:6])) + p_int(8,1) + p_int(9,0)

    def _bombpot_start_brc(self, active_seats: set[int], sb: int, bb: int) -> bytes:
        """Exact BombPotStartBRC model recovered from dump.cs and the fresh PPP pcap."""
        p=self.h.room.props
        amount=money(p.get("_bombpotAnte", self.h.pre.get("anteAmount",0)), self.scale)
        if amount <= 0 and p.get("minAnte") is not None:
            amount=max(0,int(round(float(p["minAnte"])*bb)))
        times=max(1,int(round(amount/bb))) if bb else max(1,int(float(p.get("minAnte") or 1)))
        body=bytearray(p_int(1,ppp_table_id(self.h.table_id)))
        body+=p_msg(2,p_int(1,1)+p_int(3,sb))
        body+=p_msg(3,self._hand_card_rsp())
        pool_total=0
        for seat1 in sorted(x for x in active_seats if x):
            row=self.roster_by_seat.get(seat1,{})
            stack=max(0,money(row.get("userChips",0),self.scale))
            paid=min(stack,amount); remain=stack-paid
            pool_total+=paid
            action=p_int(1,seat1-1)+p_int(2,17)+p_int(3,paid)+p_int(4,remain)
            body+=p_msg(4,action)
        body+=p_msg(5,p_int(1,pool_total))
        body+=p_int(6,times)
        return bytes(body)

    def _outer(self, cmd: str, payload: bytes, location: str = "TABLE", ts: Optional[int] = None) -> dict[str,Any]:
        if ts is None:
            self.now += 1; ts = self.now
        msg = {
            "timestamp": int(ts), "pid": str(self.h.hero_id), "uid": str(self.h.hero_id),
            "cmd": cmd, "direction": "ServerToClient", "data": base64.b64encode(payload).decode(),
            "location": location, "seq": 0,
        }
        return {"data":"", "msg":json.dumps(msg, ensure_ascii=False, indent=2), "packageName":PPP_PACKAGE, "tag":"traffic"}

    def add(self, cmd: str, payload: bytes, location: str = "TABLE"):
        self.frames.append(self._outer(cmd, payload, location))

    def synthesize_until_first_hero_turn(self) -> list[dict[str,Any]]:
        h = self.h; p = h.room.props; pre = h.pre
        sb = self._wire_small_blind()
        bb = self._wire_big_blind()
        ante = money(pre.get("anteAmount") or p.get("ante", 0), self.scale)
        seatnum = int(p.get("maxSize") or max(self.roster_by_seat, default=0))
        game_mode = ppp_game_mode(p)
        # Admission: dynamic uid/table identity, PPP-only compatibility metadata hardcoded above.
        login = p_int(1,0) + p_str(3,PPP_SILENT_VERSION) + p_int(4,0)
        self.add("pb.UserLoginRSP", login, "OTHERS")
        self.add("pb.EnterRoomRSP", self._enter_room_rsp(), "OTHERS")

        # Existing opponents are already present once in TableStatus. The hero seat was
        # intentionally left empty there and is now admitted through the exact one-shot
        # SitDownBRC -> SitDownRSP -> TotalBuyinBRC sequence.
        hero_r = next((r for r in h.roster if int(r.get("userId") or 0)==h.hero_id), {"userChips":0,"seatId":h.hero_seat,"userId":h.hero_id,"userName":h.hero_name})
        hero_chips = money(hero_r.get("userChips",0), self.scale)
        self.add("pb.SitDownBRC", self._sitdown_brc(hero_r, wait_blind=True, in_game=False))
        self.add("pb.SitDownRSP", p_int(1,0)+p_int(3,h.ppp_hero_seat)+p_int(4,hero_chips)+p_int(5,10))
        self.add("pb.TotalBuyinBRC", p_int(1,h.hero_id)+p_int(2,hero_chips)+p_int(3,0))
        # Hand snapshot #1: all occupied stacks.
        d1 = bytearray()
        for r in sorted(h.roster,key=lambda x:int(x.get("seatId") or 99)):
            if not int(r.get("userId") or 0): continue
            si = p_int(1,int(r["seatId"])-1) + p_int(2,money(r.get("userChips",0),self.scale)) + p_int(4,0)+p_int(5,0)
            d1 += p_msg(4,si)
        d1 += p_str(5,self._game_id()) + p_int(7,0)
        self.add("pb.DealerInfoRSP", bytes(d1))
        # The real PPP capture sends this immediately before the first hand in which the
        # newly seated hero receives cards: Seatid=<hero>, Wait=false.
        self.add("pb.CancelWaitBlindBRC", p_int(1,h.ppp_hero_seat)+p_int(2,0))

        # Hand snapshot #2: positions + active stacks. Use active players in pre-hand roster.
        dealer = int(pre.get("dealerSeatId") or 1)-1; sbseat=int(pre.get("sbSeatId") or 1)-1; bbseat=int(pre.get("bbSeatId") or 1)-1
        d2 = bytearray(p_int(1,dealer)+p_int(2,sbseat)+p_int(3,bbseat))
        active_seats = {int(pre.get("sbSeatId") or 0), int(pre.get("bbSeatId") or 0)}
        active_seats.update(int(r.get("seatId")) for r in h.roster if r.get("isPlaying") is True)
        for seat1 in sorted(x for x in active_seats if x):
            r = self.roster_by_seat.get(seat1, {"userChips":0})
            si = p_int(1,seat1-1)+p_int(2,money(r.get("userChips",0),self.scale))+p_int(4,0)+p_int(5,0)+p_int(6,1)
            d2 += p_msg(4,si)
        d2 += p_str(5,self._game_id())
        d2 += p_int(6, int(p.get("_handsToBombpot") or 0))
        d2 += p_int(7, 0)  # IsInSquidRound; unrelated to bombpot
        d2 += p_int(8, int(p.get("_handsToPotFold") or 0))
        self.add("pb.DealerInfoRSP", bytes(d2))
        # Exact PPP start-of-hand pool/UI reset. This is distinct from the final
        # settlement RoundOver, whose body contains one or more real pool values.
        self.add("pb.RoundOverBRC", b"")
        self.add("pb.SquidMarkInfoBRC", p_int(1,0)+p_int(2,0))
        if p.get("_isBombpotHand"):
            self.add("pb.BombPotStartBRC", self._bombpot_start_brc(active_seats,sb,bb))
        else:
            self.add("pb.RoundStartBRC", p_int(1,1)+p_int(3,sb))
            self.add("pb.HandCardRSP", self._hand_card_rsp())

        # Forced money has two different meanings in PPP state. Ante/bombpot money is
        # already in the hand pot, but does not count toward the current street wager.
        # Blinds (and a later live straddle/FORCE_BB), by contrast, do establish a street
        # wager. Track remaining chips explicitly so every ActionBRC reports the stack
        # after *all* prior forced and voluntary money.
        def starting_stack(seat1:int)->int:
            r=self.roster_by_seat.get(seat1,{})
            return money(r.get("userChips",0),self.scale)

        live_active = {s-1 for s in active_seats if s}
        street_contributed = collections.defaultdict(int)
        forced_paid = collections.defaultdict(int)
        forced_adjustment = collections.defaultdict(int)
        remaining_stack = {seat: starting_stack(seat+1) for seat in live_active}
        all_in=set()

        def remaining(seat:int)->int:
            return max(0,remaining_stack.get(seat,starting_stack(seat+1)))

        def mark_all_in(seat:int):
            if seat in live_active and remaining(seat)==0:
                all_in.add(seat)
            else:
                all_in.discard(seat)

        is_bombpot=bool(p.get("_isBombpotHand"))
        if is_bombpot:
            # BombPotStartBRC carries these forced posts; retain them only in whole-hand
            # accounting. The first betting street starts with zero street contribution.
            bomb_amount=money(p.get("_bombpotAnte",pre.get("anteAmount",0)),self.scale)
            for seat in sorted(live_active):
                paid=min(remaining(seat),bomb_amount)
                forced_paid[seat]+=paid
                remaining_stack[seat]=remaining(seat)-paid
                mark_all_in(seat)
        else:
            if ante:
                for seat in sorted(live_active):
                    paid=min(remaining(seat),ante)
                    forced_paid[seat]+=paid
                    remaining_stack[seat]=remaining(seat)-paid
                    self.add("pb.ActionBRC",p_int(1,seat)+p_int(2,ACTION_CODE["ANTE"])+p_int(3,paid)+p_int(4,remaining(seat)))
                    mark_all_in(seat)

            # The posted blind is current-street money; HandChips is after ante + blind.
            for seat,action,amount in ((sbseat,"SB",sb),(bbseat,"BB",bb)):
                available=remaining(seat)
                paid=min(available,amount)
                if seat==sbseat:
                    raw_sb=money(pre.get("sbAmount") or p.get("smallBlind",0),self.scale)
                    # A short stack cannot pay either nominal blind in full.  The
                    # explicit target-SB correction is only the difference between
                    # the two amounts that were actually payable.
                    forced_adjustment[seat]=paid-min(available,raw_sb)
                street_contributed[seat]+=paid
                remaining_stack[seat]=remaining(seat)-paid
                self.add("pb.ActionBRC",p_int(1,seat)+p_int(2,ACTION_CODE[action])+p_int(3,paid)+p_int(4,remaining(seat)))
                mark_all_in(seat)

        # Replay confirmed actions before the first hero turn. Voluntary actions preserve
        # PPP's notify -> action ordering; forced blind/straddle actions have no notify.
        current_max=max(street_contributed.values(),default=0)
        last_full_raise=max(1,bb)
        seen_actions=set()
        saw_player_history=False

        def action_limits(seat:int)->tuple[int,int,int,int]:
            put=street_contributed[seat]
            call=max(0,current_max-put)
            # Exact PPP wire uses the largest opponent street capacity and does not cap
            # MaxChipin by the actor's own stack.
            opponent_caps=[street_contributed[s]+remaining(s) for s in live_active if s != seat]
            max_put=max(0,max(opponent_caps,default=put)-put)
            can=len(live_active-all_in)
            candidate=max(0,current_max+last_full_raise-put)
            # PPP reports the theoretical full-raise increment even when it exceeds
            # MaxChipin (fresh wire has Call=1162/Min=2324/Max=1162). Only the absence
            # of a second actionable player collapses MinChipin to zero.
            min_put=candidate if can>1 else 0
            return call,min_put,max_put,can

        def notify_actor(seat:int):
            call,min_put,max_put,can=action_limits(seat)
            self.add("pb.ActionNotifyBRC",p_int(1,seat)+p_int(2,call)+p_int(3,min_put)+p_int(4,max_put)+p_int(5,can))

        def normalized_action(row:dict[str,Any])->str:
            def key(value:Any)->str:
                return re.sub(r"[^A-Z0-9]+","_",str(value or "").upper()).strip("_")
            new=key(row.get("newPlayerAction")); old=key(row.get("action")); aliases={new,old}
            if aliases & {"ALLIN","ALL_IN"}: return "ALLIN"
            if aliases & {"AUTOBB","AUTO_BB","POSTBB","POST_BB"}: return "AUTOBB"
            # newPlayerAction is often a UI caption, not a poker action.  Pot-size
            # buttons arrive as newPlayerAction=POT while the canonical action is
            # action=RAISE.  Preferring POT silently dropped the raise from the cold
            # replay and made the hero hint disagree with the table state.
            if new in ACTION_CODE:return new
            if old in ACTION_CODE:return old
            return new or old

        for e in h.events_before_turn:
            if e.name != "game.dealer_chat_action" or not isinstance(e.data,dict): continue
            for a in e.data.get("gameActionMessagesHistory") or []:
                if a.get("type") != "playerAction": continue
                saw_player_history=True
                name=str(a.get("username") or ""); r=self.roster_by_name.get(name)
                if not r: continue
                seat=int(r["seatId"])-1
                action=normalized_action(a)
                # These were synthesized above from authoritative pre-hand data. Historical
                # FORCE_BB and STRADDLE are additional live posts and must be retained.
                if action in ("SB","BB","ANTE","BOMBPOT_BB"): continue
                if action not in ACTION_CODE and action != "ALLIN":
                    raise ValueError(
                        f"unsupported Coin player action new={a.get('newPlayerAction')!r} "
                        f"canonical={a.get('action')!r} hand={h.hand_id}")
                fp=(a.get("handId"),a.get("userId"),seat,action,a.get("actionAmount"),a.get("initTimestamp"),a.get("initTimeStamp"))
                if fp in seen_actions: continue
                seen_actions.add(fp)

                forced_action=action in ("AUTOBB","STRADDLE")
                if not forced_action:
                    notify_actor(seat)

                amt=money(a.get("actionAmount",0),self.scale)
                old_put=street_contributed[seat]
                old_max=current_max
                delta=0; wire_action=action

                if action in ("AUTOBB","STRADDLE"):
                    # Both are actual additional posts, not a total-bet snapshot.
                    delta=min(remaining(seat),amt)
                    street_contributed[seat]+=delta
                    current_max=max(current_max,street_contributed[seat])
                    if action=="STRADDLE" and street_contributed[seat]>old_max:
                        # A live straddle becomes the opening blind for minimum-raise math.
                        last_full_raise=max(last_full_raise,street_contributed[seat])
                elif action=="CALL":
                    # Normalize to the shared PPP street target. This intentionally
                    # absorbs a forced-SB override (.02/.05 raw ratio) on the first
                    # voluntary action instead of carrying it into every later bet.
                    target=min(old_put+remaining(seat),current_max)
                    delta=max(0,target-old_put)
                    street_contributed[seat]=target
                    forced_adjustment[seat]=0
                    current_max=max(current_max,street_contributed[seat])
                elif action in ("RAISE","BET"):
                    # RAISE/BET history is a total street target.
                    target=min(old_put+remaining(seat),max(old_put,amt))
                    delta=max(0,target-old_put)
                    wire_action=ppp_action_for_state(action,old_max,target,last_full_raise)
                    street_contributed[seat]=target
                    forced_adjustment[seat]=0
                    current_max=max(current_max,target)
                    raise_inc=max(0,target-old_max)
                    if raise_inc>=last_full_raise:
                        last_full_raise=raise_inc
                elif action=="ALLIN":
                    # ALLIN consumes the tracked remainder. PPP has no ALLIN action code:
                    # a short raise is represented as CALL, while a full opening/raise is
                    # BET/RAISE. A short raise still becomes the amount others must call.
                    target=old_put+remaining(seat)
                    delta=max(0,target-old_put)
                    raise_inc=max(0,target-old_max)
                    wire_action=ppp_action_for_state(action,old_max,target,last_full_raise)
                    street_contributed[seat]=target
                    forced_adjustment[seat]=0
                    current_max=max(current_max,target)
                    if wire_action in ("BET","RAISE") and raise_inc>=last_full_raise:
                        last_full_raise=raise_inc

                remaining_stack[seat]=max(0,remaining(seat)-delta)
                code=ACTION_CODE[wire_action]
                self.add("pb.ActionBRC",p_int(1,seat)+p_int(2,code)+p_int(3,delta)+p_int(4,remaining(seat)))
                if action=="FOLD":
                    live_active.discard(seat); all_in.discard(seat)
                else:
                    mark_all_in(seat)

        # Hero notify uses Coin's call value/minimum total target, plus PPP's dynamic
        # opponent-capacity MaxChipin and active-minus-all-in CanActionNum semantics.
        call_need=max(0,current_max-street_contributed[h.ppp_hero_seat])
        if saw_player_history and h.turn.get("callAmount") is not None:
            expected_call=max(0,money(h.turn.get("callAmount"),self.scale)
                              -int(forced_adjustment.get(h.ppp_hero_seat,0)))
            if expected_call!=call_need:
                raise ValueError(
                    f"cold replay/turn mismatch hand={h.hand_id}: replay call={call_need} "
                    f"Coin call={expected_call}; unsupported or omitted history action")
        opts=h.turn.get("userTurnOptions") or {}
        ranges=[]
        for v in opts.values():
            if isinstance(v,list) and len(v)>=2 and all(isinstance(x,(int,float)) for x in v[:2]): ranges.append(v)
        hero_street_put=street_contributed[h.ppp_hero_seat]
        _state_call,state_min,max_chip,can_actions=action_limits(h.ppp_hero_seat)
        min_chip=state_min
        notify=p_int(1,h.ppp_hero_seat)+p_int(2,call_need)+p_int(3,min_chip)+p_int(4,max_chip)+p_int(5,can_actions)
        self.add("pb.ActionNotifyBRC",notify)

        turn_time=int(h.turn.get("turnTime") or p.get("playerHandTime") or 15)
        profile=self.profile
        hint_parts = [
            (p_int(1,profile.hint_roomtype) if profile.hint_roomtype is not None else b""),
            p_int(2,sb), p_int(3,ante), p_int(4,ppp_table_id(h.table_id)), p_int(5,PPP_CLUB_ID),
            p_int(7,turn_time), p_str(8,h.room.room_name),
            (p_str(9,PPP_CLUB_NAME) if PPP_CLUB_NAME else b""), p_int(10,seatnum),
            p_int(11,PPP_ROOM_MODE), p_int(12,profile.room_type), p_int(13,profile.game_mode),
            (p_int(14,profile.hint_ppsrid) if profile.hint_ppsrid is not None else b""), p_int(15,0),
        ]
        hint = b"".join(hint_parts)
        self.add("pb.RoundHintMultipleTableRSP",hint)
        # In live mode FinishRoundHint is held until cc. Offline v4-style file contains it next.
        self.add("pb.FinishRoundHintRSP",p_int(1,ppp_table_id(h.table_id)))
        return self.frames

    def synthesize_exit(self) -> list[dict[str,Any]]:
        # Minimal clean teardown matching the known-good v4 exit semantics.
        return [
            self._outer("pb.StandUpRSP", p_int(1,0)),
            self._outer("pb.StandUpBRC", p_int(1,self.h.ppp_hero_seat)),
            self._outer("pb.LeaveRoomRSP", b"".join([p_int(1,0),p_int(3,0),p_int(4,PPP_CLUB_ID),p_int(6,0)])),
            self._outer("pb.OtherLeaveRoomBRC", p_int(1,self.h.hero_id), "OTHERS"),
        ]


def write_reference_style(path: str|Path, frames: list[dict[str,Any]], title: str):
    lines=["# " + "="*78, f"# {title}", "# Generated by coin_ppp_bridge.py", "# " + "="*78, ""]
    for i,o in enumerate(frames,1):
        m=json.loads(o["msg"]); cmd=m.get("cmd","")
        lines.append(f"=== FRAME #{i:02d} [COIN->PPP] traffic:{cmd} ===")
        lines.append(json.dumps(o,ensure_ascii=False,separators=(",",":")))
        lines.append("")
    Path(path).write_text("\n".join(lines),encoding="utf8")


def frame_summary(frames:list[dict[str,Any]]):
    out=[]
    for i,o in enumerate(frames,1):
        m=json.loads(o["msg"]); out.append((i,m["cmd"],len(base64.b64decode(m.get("data","")))))
    return out


# ---------------------------------------------------------------------------
# Live bridge. It accepts the same 4-byte-BE length-prefixed JSON records seen in port_18010.pcap.
# On a hero turn it synthesizes a coherent cold-start PPP stream and forwards it to EYE.
# ---------------------------------------------------------------------------
async def read_lp(reader: asyncio.StreamReader) -> Optional[bytes]:
    try: hdr=await reader.readexactly(4)
    except (asyncio.IncompleteReadError,ConnectionError): return None
    n=struct.unpack(">I",hdr)[0]
    if not 0<n<=20_000_000: raise ValueError(f"bad frame size {n}")
    return await reader.readexactly(n)

async def send_lp(writer: asyncio.StreamWriter, raw: bytes):
    writer.write(struct.pack(">I",len(raw))+raw); await writer.drain()

def restamp_outer(o: dict[str,Any]) -> dict[str,Any]:
    """Use current wall-clock envelope timestamps in live mode; protobuf payload is unchanged."""
    x=json.loads(json.dumps(o))
    if x.get("tag")=="traffic":
        try:
            m=json.loads(x.get("msg", "")); m["timestamp"]=int(time.time()*1000)
            x["msg"]=json.dumps(m,ensure_ascii=False,separators=(",",":"))
        except Exception:
            pass
    return x


class LiveBridge:
    def __init__(self, eye_host:str, eye_port:int, scale:int=100, frame_delay:float=0.05, cleanup_after_hand:bool=False):
        self.eye_host=eye_host;self.eye_port=eye_port;self.base_scale=scale;self.scale=scale; self.frame_delay=frame_delay; self.cleanup_after_hand=cleanup_after_hand
        self.raw_events=[]; self.sent_hands=set(); self.eye_r=None;self.eye_w=None
        self.eye_task=None; self.cc_queue=asyncio.Queue()

    async def ensure_eye(self):
        if self.eye_w and not self.eye_w.is_closing(): return
        self.eye_r,self.eye_w=await asyncio.open_connection(self.eye_host,self.eye_port)
        self.eye_task=asyncio.create_task(self._eye_reader())
        print(f"[eye] connected {self.eye_host}:{self.eye_port}",flush=True)

    async def _eye_reader(self):
        try:
            while True:
                raw=await read_lp(self.eye_r)
                if raw is None:return
                try:
                    z=json.loads(raw); print(f"[eye<<] tag={z.get('tag')} data={str(z.get('data',''))[:220]}",flush=True)
                    if z.get("tag")=="cc": await self.cc_queue.put(z)
                except Exception: print(f"[eye<<] {raw[:200]!r}",flush=True)
        except Exception as e: print(f"[eye] reader stopped: {e}",flush=True)

    async def _send_cleanup(self, hand: HandModel):
        if not self.cleanup_after_hand or not self.eye_w or self.eye_w.is_closing(): return
        await asyncio.sleep(0.25)
        scale=money_profile_for_hand(hand,self.base_scale).chip_scale
        for f in PPPBuilder(hand,scale).synthesize_exit():
            cmd=json.loads(f["msg"])["cmd"]
            await send_lp(self.eye_w,json.dumps(restamp_outer(f),ensure_ascii=False,separators=(",",":")).encode())
            print(f"[eye>>] {cmd} [cleanup]",flush=True)
            await asyncio.sleep(self.frame_delay)

    async def ingest(self,o:dict[str,Any]):
        self.raw_events.append(o)
        # Rebuilding the complete table/hand model for every WS packet is quadratic and
        # can make a realtime 18010 feed lag badly. A private hero turn is identifiable
        # by game.user_turn + userTurnOptions, so only rebuild on that boundary.
        try:
            one=decode_coin_events([o])
            if not one or one[0].name != "game.user_turn" or not isinstance(one[0].data, dict):
                return
            if not isinstance(one[0].data.get("userTurnOptions"), dict):
                return
            model=CoinCaptureModel(decode_coin_events(self.raw_events))
            cands=model.candidate_hands()
            if not cands:return
            hid,_,_=cands[-1]
            if hid in self.sent_hands:return
            hand=model.build_hand(hid)
            self.scale=money_profile_for_hand(hand,self.base_scale).chip_scale
            frames=PPPBuilder(hand,self.scale).synthesize_until_first_hero_turn()
        except Exception:
            return
        await self.ensure_eye()
        print(f"[bridge] hero turn hand={hand.hand_id} table={hand.table_id} hero={hand.hero_name} seat={hand.hero_seat} cards={hand.cards}",flush=True)
        # Match test_cold_compare semantics: send everything through RoundHint, then wait briefly for cc;
        # FinishRoundHint is sent after cc delay when available, otherwise after a conservative fallback.
        finish=None
        for f in frames:
            cmd=json.loads(f["msg"])["cmd"]
            if cmd=="pb.FinishRoundHintRSP": finish=f; continue
            await send_lp(self.eye_w,json.dumps(restamp_outer(f),ensure_ascii=False,separators=(",",":")).encode())
            print(f"[eye>>] {cmd}",flush=True)
            await asyncio.sleep(self.frame_delay)
        # Exact v4-full handshake: RoundHint -> wait for cc -> honor cc.delay -> FinishRoundHint.
        try:
            cc=await asyncio.wait_for(self.cc_queue.get(),timeout=10.0)
            try:
                d=json.loads(cc.get("data","{}")) if isinstance(cc.get("data"),str) else (cc.get("data") or {})
            except Exception:
                d={}
            delay_ms=int(d.get("delay",1000))
            message=str(d.get("message") or "fold")
            lifetime=int(d.get("lifetime",4000))
            print(f"[bridge] cc message={message} delay={delay_ms}ms",flush=True)
            # The working PPP capture sends this GUI echo back on the same LP stream
            # immediately after cc, then FinishRoundHint after the requested delay.
            broadcast={"data":"","msg":json.dumps({"action":"show_message","message":message,"time":lifetime},separators=(",",":")),
                       "packageName":PPP_PACKAGE,"tag":"broadcast"}
            await send_lp(self.eye_w,json.dumps(broadcast,ensure_ascii=False,separators=(",",":")).encode())
            print("[eye>>] broadcast",flush=True)
            await asyncio.sleep(delay_ms/1000.0)
        except asyncio.TimeoutError:
            print("[bridge] WARNING: no cc within 10s; FinishRoundHint not sent",flush=True)
            await self._send_cleanup(hand)
            self.sent_hands.add(hid)
            return
        if finish:
            await send_lp(self.eye_w,json.dumps(restamp_outer(finish),ensure_ascii=False,separators=(",",":")).encode())
            print("[eye>>] pb.FinishRoundHintRSP",flush=True)
        await self._send_cleanup(hand)
        self.sent_hands.add(hid)

async def run_bridge(listen_host:str,listen_port:int,eye_host:str,eye_port:int,scale:int,frame_delay:float=0.05,cleanup_after_hand:bool=False):
    bridge=LiveBridge(eye_host,eye_port,scale,frame_delay,cleanup_after_hand)
    async def client(reader,writer):
        try:
            while True:
                raw=await read_lp(reader)
                if raw is None:break
                try:o=json.loads(raw)
                except Exception:continue
                await bridge.ingest(o)
        finally:
            writer.close();
            try:await writer.wait_closed()
            except:pass
    srv=await asyncio.start_server(client,listen_host,listen_port)
    print(f"[listen] COIN feed {listen_host}:{listen_port} -> EYE {eye_host}:{eye_port}",flush=True)
    async with srv: await srv.serve_forever()


async def replay_pcap_to_listener(pcap:str, host:str, port:int, event_delay:float=0.002):
    """Replay the recorded 18010 LP JSON feed into a live bridge listener."""
    objs=load_18010_events_from_pcap(pcap,18010)
    _r,w=await asyncio.open_connection(host,port)
    print(f"[replay] connected {host}:{port}; events={len(objs)}",flush=True)
    try:
        for i,o in enumerate(objs,1):
            clean={k:v for k,v in o.items() if not k.startswith("_flow") and not k.startswith("_pcap")}
            raw=json.dumps(clean,ensure_ascii=False,separators=(",",":")).encode()
            await send_lp(w,raw)
            if event_delay>0: await asyncio.sleep(event_delay)
            if i % 100 == 0: print(f"[replay] sent {i}/{len(objs)}",flush=True)
    finally:
        w.close()
        try: await w.wait_closed()
        except Exception: pass
    print(f"[replay] complete events={len(objs)}",flush=True)


def parse_hp(s:str)->tuple[str,int]:
    host,port=s.rsplit(":",1);return host,int(port)


def make_report(model:CoinCaptureModel, hand:HandModel, frames:list[dict[str,Any]],
                chip_scale: Optional[float] = None)->dict[str,Any]:
    p=hand.room.props; opts=hand.turn.get("userTurnOptions") or {}; profile=protocol_profile(p)
    money_profile=money_profile_for_hand(hand,PPP_WIRE_CURRENCY_SCALE)
    wire_scale=float(money_profile.chip_scale if chip_scale is None else chip_scale)
    target_money_active=(money_profile.valid_blinds
                         and abs(wire_scale-float(money_profile.chip_scale))<=1e-9)
    wire_sb=(money_profile.ppp_small_blind if target_money_active
             else money(p.get("smallBlind",hand.pre.get("sbAmount",0)),wire_scale))
    wire_bb=(money_profile.ppp_big_blind if target_money_active
             else money(p.get("bigBlind",hand.pre.get("bbAmount",0)),wire_scale))
    active_seats={int(hand.pre.get("sbSeatId") or 0), int(hand.pre.get("bbSeatId") or 0)}
    active_seats.update(int(r.get("seatId")) for r in hand.roster if r.get("isPlaying") is True)
    active_seats.discard(0)
    hero_put=0
    if hand.hero_seat==int(hand.pre.get("sbSeatId") or 0): hero_put += wire_sb
    if hand.hero_seat==int(hand.pre.get("bbSeatId") or 0): hero_put += wire_bb
    ranges=[v for v in opts.values() if isinstance(v,list) and len(v)>=2 and all(isinstance(x,(int,float)) for x in v[:2])]
    min_chip=max(0,money(max(ranges,key=lambda x:float(x[1])-float(x[0]))[0],wire_scale)-hero_put) if ranges else money(hand.turn.get("callAmount",0),wire_scale)
    hero_stack=money(next((r.get("userChips",0) for r in hand.roster if int(r.get("seatId") or 0)==hand.hero_seat),0),wire_scale)
    range_max=max(0,money(max(ranges,key=lambda x:float(x[1])-float(x[0]))[1],wire_scale)-hero_put) if ranges else max(0,hero_stack-hero_put)
    opp_stacks=[money(r.get("userChips",0),wire_scale) for r in hand.roster if int(r.get("seatId") or 0) in active_seats and int(r.get("seatId") or 0)!=hand.hero_seat]
    max_chip=min(range_max,max(opp_stacks)) if opp_stacks else range_max
    return {
        "hero":{"uid":hand.hero_id,"name":hand.hero_name,"coin_seat":hand.hero_seat,"ppp_seat":hand.ppp_hero_seat},
        "players":[{"uid":int(r.get("userId") or 0),"name":str(r.get("userName") or ""),"coin_seat":int(r.get("seatId") or 0),
                    "ppp_seat":int(r.get("seatId") or 0)-1,"stack":r.get("userChips"),"stack_ppp":money(r.get("userChips",0),wire_scale),
                    "is_playing_snapshot":bool(r.get("isPlaying")),"active_for_hand":int(r.get("seatId") or 0) in active_seats}
                   for r in sorted(hand.roster,key=lambda x:int(x.get("seatId") or 99)) if int(r.get("userId") or 0)],
        "table":{"table_id":hand.table_id,"room_name":hand.room.room_name,"config_id":hand.room.config_id,
                 "small_blind":p.get("smallBlind",hand.pre.get("sbAmount")),"big_blind":p.get("bigBlind",hand.pre.get("bbAmount")),
                 "ante":hand.pre.get("anteAmount") or p.get("ante"),"min_buyin":p.get("minBuyIn"),"max_buyin":p.get("maxBuyIn")},
        "hand":{"hand_id":hand.hand_id,"dealer_seat":hand.pre.get("dealerSeatId"),"sb_seat":hand.pre.get("sbSeatId"),"bb_seat":hand.pre.get("bbSeatId"),
                 "cards":hand.cards,"ppp_card_values":[ppp_card(c) for c in hand.cards[:6]],
                "hero_turn":{"callAmount":hand.turn.get("callAmount"),"options":opts,"turnTime":hand.turn.get("turnTime")}},
        "translated_ppp":{"game_mode":profile.game_mode,"room_type":profile.room_type,
                          "blind":wire_sb,"big_blind":wire_bb,"chip_scale":wire_scale,
                          "target_displayed_big_blind":float(money_profile.target_big_blind),
                          "cash_ratio":float(COIN_CHIP_USDT*USD_RUB/TARGET_PPP_CHIP_RUB),
                          "room_seats":int(p.get("maxSize") or 0),
                          "positions":{"dealer":int(hand.pre.get("dealerSeatId") or 1)-1,"small_blind":int(hand.pre.get("sbSeatId") or 1)-1,"big_blind":int(hand.pre.get("bbSeatId") or 1)-1},
                          "action_notify":{"seatid":hand.ppp_hero_seat,"call_need":money(hand.turn.get("callAmount",0),wire_scale),
                                           "min_chipin":min_chip,"max_chipin":max_chip,"can_action_num":len(active_seats)}},
        "hardcoded_ppp_only":{"club_id":PPP_CLUB_ID,"ppsrid":profile.ppsrid,"ppsr_exchange_rate":PPP_EXCHANGE_RATE,"room_mode":PPP_ROOM_MODE,"room_type":profile.room_type,"hint_roomtype":profile.hint_roomtype,"hint_ppsrid":profile.hint_ppsrid,"silent_version":PPP_SILENT_VERSION},
        "privacy":{"coin_auth_tokens_forwarded":False},
        "frame_count":len(frames),"commands":[x[1] for x in frame_summary(frames)],
        "candidate_hero_hands":[x[0] for x in model.candidate_hands()],
    }


def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    c=sub.add_parser("convert"); c.add_argument("--pcap",required=True);c.add_argument("--out",default="coin_converted_v5.txt");c.add_argument("--report",default="coin_conversion_report.json");c.add_argument("--hand-id",type=int);c.add_argument("--chip-scale",type=float,default=None,help="explicit legacy override; default derives the target-club MoneyProfile from Coin blinds")
    i=sub.add_parser("inspect");i.add_argument("--pcap",required=True);i.add_argument("--hand-id",type=int)
    b=sub.add_parser("bridge");b.add_argument("--listen",default="127.0.0.1:18010");b.add_argument("--eye",default="127.0.0.1:17770");b.add_argument("--chip-scale",type=int,default=100);b.add_argument("--frame-delay",type=float,default=0.05);b.add_argument("--cleanup-after-hand",action="store_true")
    r=sub.add_parser("replay");r.add_argument("--pcap",required=True);r.add_argument("--target",default="127.0.0.1:18010");r.add_argument("--event-delay",type=float,default=0.002)
    a=ap.parse_args()
    if a.cmd=="bridge":
        lh,lp=parse_hp(a.listen);eh,ep=parse_hp(a.eye);asyncio.run(run_bridge(lh,lp,eh,ep,a.chip_scale,a.frame_delay,a.cleanup_after_hand));return
    if a.cmd=="replay":
        th,tp=parse_hp(a.target);asyncio.run(replay_pcap_to_listener(a.pcap,th,tp,a.event_delay));return
    objs=load_18010_events_from_pcap(a.pcap); ev=decode_coin_events(objs); model=CoinCaptureModel(ev); hand=model.build_hand(a.hand_id)
    requested_scale=getattr(a,"chip_scale",None)
    wire_scale=(money_profile_for_hand(hand,PPP_WIRE_CURRENCY_SCALE).chip_scale
                if requested_scale is None else requested_scale)
    frames=PPPBuilder(hand,wire_scale).synthesize_until_first_hero_turn()
    rep=make_report(model,hand,frames,wire_scale)
    if a.cmd=="inspect":
        print(json.dumps(rep,ensure_ascii=False,indent=2));return
    write_reference_style(a.out,frames,f"COIN -> PPP V5 COLD STREAM, hand {hand.hand_id}")
    Path(a.report).write_text(json.dumps(rep,ensure_ascii=False,indent=2),encoding="utf8")
    print(f"wrote {a.out} ({len(frames)} frames)")
    print(f"wrote {a.report}")
    print(json.dumps(rep,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
