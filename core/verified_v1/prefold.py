"""NLH prefold charts: explicit OpenHoldem-like cells, never ranges."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import os
import re
import shlex
from typing import Any, Iterable, Mapping, Sequence


_RANKS = "23456789TJQKA"
_RANK_WORDS = {
    "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
    "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9",
    "TEN": "T", "JACK": "J", "QUEEN": "Q", "KING": "K", "ACE": "A",
}
_SUIT_WORDS = {
    "CLUBS": "C", "DIAMONDS": "D", "HEARTS": "H", "SPADES": "S",
    "CLUB": "C", "DIAMOND": "D", "HEART": "H", "SPADE": "S",
}
_POSITIONS = frozenset({"UTG", "UTG1", "UTG2", "LJ", "HJ", "CO", "BTN", "SB", "BB", "MP", "EP"})
_FACING = frozenset({"UNOPENED", "LIMPED", "RAISE", "RERAISE", "ALLIN"})
_FACING_ALIASES = {"LIMP": "LIMPED", "3BET": "RERAISE", "4BET": "RERAISE", "ALL-IN": "ALLIN"}
_MAX_CONFIG_LINES = 20_000
_MAX_RULE_CARDS = 169
_POSITION_BY_COUNT = {
    2: ("SB", "BB"),
    3: ("BTN", "SB", "BB"),
    4: ("BTN", "SB", "BB", "UTG"),
    5: ("BTN", "SB", "BB", "UTG", "CO"),
    6: ("BTN", "SB", "BB", "UTG", "HJ", "CO"),
    7: ("BTN", "SB", "BB", "UTG", "UTG1", "HJ", "CO"),
    8: ("BTN", "SB", "BB", "UTG", "UTG1", "UTG2", "HJ", "CO"),
    9: ("BTN", "SB", "BB", "UTG", "UTG1", "UTG2", "LJ", "HJ", "CO"),
}
_EP_BY_COUNT = {
    4: ("UTG",),
    5: ("UTG",),
    6: ("UTG", "HJ"),
    7: ("UTG", "UTG1", "HJ"),
    8: ("UTG", "UTG1", "UTG2", "HJ"),
    9: ("UTG", "UTG1", "UTG2", "LJ", "HJ"),
}
_TRASH = frozenset({
    "32o", "42o", "43o", "52o", "53o", "54o", "62o", "63o", "64o", "65o",
    "72o", "73o", "74o", "75o", "76o", "82o", "83o", "84o", "85o", "86o",
    "92o", "93o", "94o", "95o", "96o", "T2o", "T3o", "T4o", "T5o", "T6o",
    "J2o", "J3o", "J4o", "J5o", "J6o", "Q2o", "Q3o", "Q4o", "Q5o",
    "K2o", "K3o", "K4o",
    "32s", "42s", "52s", "62s", "72s", "82s", "92s",
    "43s", "53s", "63s", "73s", "83s",
})


class PrefoldConfigError(ValueError):
    pass


class PrefoldMode(str, Enum):
    AUDIT = "audit"
    LIVE = "live"


@dataclass(frozen=True)
class PrefoldRule:
    rule_id: str
    dealt_in_players: int
    position: str
    facing: str
    cards: frozenset[str]
    source_line: int
    action: str = "FOLD"


@dataclass(frozen=True)
class PrefoldConfig:
    enabled: bool = False
    mode: PrefoldMode = PrefoldMode.AUDIT
    rules: tuple[PrefoldRule, ...] = ()

    @classmethod
    def safe_default(cls) -> "PrefoldConfig":
        return cls(enabled=False, mode=PrefoldMode.AUDIT, rules=())


@dataclass(frozen=True)
class PrefoldContext:
    dealt_in_players: int
    position: str
    facing: str
    hole_cards: tuple[Any, Any]
    game_family: str = "NLH"
    street: str = "PREFLOP"
    can_check: bool = False
    state_complete: bool = True
    bombpot: bool = False
    straddle: bool = False


@dataclass(frozen=True)
class PrefoldDecision:
    matched: bool
    recommended_action: str | None
    reason_code: str
    canonical_hand: str | None = None
    rule_id: str | None = None
    audit_only: bool = True
    emit_protocol: bool = False
    bypass_ai: bool = False


def _bool_word(value: str, *, line: int) -> bool:
    word = value.strip().lower()
    if word in {"1", "true", "yes", "on"}:
        return True
    if word in {"0", "false", "no", "off"}:
        return False
    raise PrefoldConfigError(f"line {line}: enabled must be true or false")


def _position(value: str, *, line: int = 0) -> str:
    word = value.strip().upper().replace("+", "")
    if word == "BUTTON":
        word = "BTN"
    if word not in _POSITIONS:
        prefix = f"line {line}: " if line else ""
        raise PrefoldConfigError(f"{prefix}unsupported position {value!r}")
    return word


def _facing(value: str, *, line: int = 0) -> str:
    word = value.strip().upper().replace("_", "")
    word = _FACING_ALIASES.get(word, word)
    if word not in _FACING:
        prefix = f"line {line}: " if line else ""
        raise PrefoldConfigError(f"{prefix}unsupported facing state {value!r}")
    return word


def normalize_hand_class(value: str, *, line: int = 0) -> str:
    word = value.strip().upper().replace("10", "T")
    prefix = f"line {line}: " if line else ""
    if any(marker in word for marker in ("*", "+", "%", "-")):
        raise PrefoldConfigError(f"{prefix}hand ranges/wildcards are not allowed: {value!r}")
    if len(word) == 2 and word[0] == word[1] and word[0] in _RANKS:
        return word
    if len(word) != 3 or word[0] not in _RANKS or word[1] not in _RANKS or word[2] not in {"S", "O"}:
        raise PrefoldConfigError(f"{prefix}invalid explicit NLH hand class {value!r}")
    if word[0] == word[1]:
        raise PrefoldConfigError(f"{prefix}pairs must not have suitedness suffix: {value!r}")
    first, second = sorted(word[:2], key=_RANKS.index, reverse=True)
    return first + second + word[2].lower()


def _card(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        raw_rank = str(value.get("value") or value.get("rank") or "").upper()
        raw_suit = str(value.get("suit") or "").upper()
        rank = _RANK_WORDS.get(raw_rank, raw_rank.replace("10", "T"))
        suit = _SUIT_WORDS.get(raw_suit, raw_suit[:1])
    else:
        word = str(value or "").strip().upper().replace("10", "T")
        if len(word) != 2:
            raise ValueError(f"invalid card {value!r}")
        rank, suit = word
    if rank not in _RANKS or suit not in {"C", "D", "H", "S"}:
        raise ValueError(f"invalid card {value!r}")
    return rank, suit


def canonical_nlh_hand(cards: Sequence[Any]) -> str:
    if len(cards) != 2:
        raise ValueError("NLH prefold requires exactly two hole cards")
    first = _card(cards[0])
    second = _card(cards[1])
    if first == second:
        raise ValueError("duplicate physical hole card")
    if first[0] == second[0]:
        return first[0] * 2
    high, low = sorted((first[0], second[0]), key=_RANKS.index, reverse=True)
    suffix = "s" if first[1] == second[1] else "o"
    return high + low + suffix


def position_from_seats(hero_seat: int, dealer_seat: int, occupied: Iterable[int]) -> str:
    seats = sorted({int(seat) for seat in occupied if int(seat or 0) > 0})
    labels = _POSITION_BY_COUNT.get(len(seats))
    if not labels:
        raise ValueError(f"unsupported dealt-in count {len(seats)}")
    if int(hero_seat) not in seats or int(dealer_seat) not in seats:
        raise ValueError("hero or dealer missing from occupied seats")
    start = seats.index(int(dealer_seat))
    ordered = seats[start:] + seats[:start]
    return labels[ordered.index(int(hero_seat))]


def facing_from_street(street_contrib: Mapping[int, int], *, hero_ppp_seat: int, bb_chips: int) -> str:
    others = [int(value) for seat, value in street_contrib.items() if int(seat) != int(hero_ppp_seat)]
    mx = max(others or [0])
    bb = max(1, int(bb_chips or 1))
    if mx > bb * 2:
        return "RERAISE"
    if mx > bb:
        return "RAISE"
    if any(0 < int(value) < bb for value in others):
        return "LIMPED"
    return "UNOPENED"


def parse_prefold_config(text: str) -> PrefoldConfig:
    """Parse a bounded OpenHoldem-like explicit rule file."""

    lines = str(text).splitlines()
    if len(lines) > _MAX_CONFIG_LINES:
        raise PrefoldConfigError("prefold config is too large")
    enabled = False
    mode = PrefoldMode.AUDIT
    rules: list[PrefoldRule] = []
    occupied: dict[tuple[int, str, str, str], int] = {}
    seen_ids: set[str] = set()
    for line_number, raw in enumerate(lines, 1):
        if len(raw) > 4096:
            raise PrefoldConfigError(f"line {line_number}: line is too long")
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        parts = list(lexer)
        if not parts:
            continue
        if len(parts) == 1 and "=" in parts[0] and not parts[0].upper().startswith("WHEN"):
            key, value = parts[0].split("=", 1)
            key = key.strip().lower()
            if key == "enabled":
                enabled = _bool_word(value, line=line_number)
            elif key == "mode":
                word = value.strip().lower()
                if word == PrefoldMode.LIVE.value:
                    mode = PrefoldMode.LIVE
                elif word == PrefoldMode.AUDIT.value:
                    mode = PrefoldMode.AUDIT
                else:
                    raise PrefoldConfigError(f"line {line_number}: mode must be audit or live")
            else:
                raise PrefoldConfigError(f"line {line_number}: unknown directive {key!r}")
            continue
        if parts[0].upper() != "WHEN":
            raise PrefoldConfigError(f"line {line_number}: expected WHEN rule")
        values: dict[str, str] = {}
        for token in parts[1:]:
            if "=" not in token:
                raise PrefoldConfigError(f"line {line_number}: expected key=value, got {token!r}")
            key, value = token.split("=", 1)
            key = key.strip().lower()
            if key in values:
                raise PrefoldConfigError(f"line {line_number}: duplicate key {key!r}")
            values[key] = value.strip()
        allowed = {"players", "position", "facing", "cards", "action", "id"}
        unknown = set(values) - allowed
        required = {"players", "position", "facing", "cards", "action"}
        if unknown or required - set(values):
            raise PrefoldConfigError(
                f"line {line_number}: invalid keys; missing={sorted(required-set(values))} unknown={sorted(unknown)}"
            )
        try:
            players = int(values["players"])
        except ValueError as exc:
            raise PrefoldConfigError(f"line {line_number}: players must be an integer") from exc
        if not 2 <= players <= 9:
            raise PrefoldConfigError(f"line {line_number}: players must be between 2 and 9")
        position = _position(values["position"], line=line_number)
        facing = _facing(values["facing"], line=line_number)
        if values["action"].upper() != "FOLD":
            raise PrefoldConfigError(f"line {line_number}: only action=FOLD is supported")
        raw_cards = [value for value in values["cards"].split(",") if value]
        if not raw_cards or len(raw_cards) > _MAX_RULE_CARDS:
            raise PrefoldConfigError(f"line {line_number}: cards must contain 1..169 explicit cells")
        cards = frozenset(normalize_hand_class(value, line=line_number) for value in raw_cards)
        rule_id = values.get("id") or f"L{line_number}"
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", rule_id) or rule_id in seen_ids:
            raise PrefoldConfigError(f"line {line_number}: invalid or duplicate rule id {rule_id!r}")
        seen_ids.add(rule_id)
        for hand in cards:
            key = (players, position, facing, hand)
            if key in occupied:
                raise PrefoldConfigError(
                    f"line {line_number}: duplicate matrix cell {key}; first defined on line {occupied[key]}"
                )
            occupied[key] = line_number
        rules.append(PrefoldRule(rule_id, players, position, facing, cards, line_number))
    return PrefoldConfig(enabled=enabled, mode=mode, rules=tuple(rules))


def street_from_board(
    *,
    hand_id: Any = "",
    board_count: int = 0,
    emitted_stages: Iterable[str] = (),
    street_hand_id: Any = "",
) -> str:
    """Street for the NLH chart. A leaked FLOP mark from the previous hand
    is not a flop when this deal has no board and a different/empty hand id.
    """
    try:
        count = int(board_count or 0)
    except (TypeError, ValueError):
        count = 0
    if count >= 5:
        return "RIVER"
    if count >= 4:
        return "TURN"
    if count >= 3:
        return "FLOP"
    hid = str(hand_id or "").strip()
    previous = str(street_hand_id or "").strip()
    stages = {str(stage or "").strip().upper() for stage in emitted_stages}
    if hid and hid == previous and stages.intersection({"FLOP", "TURN", "RIVER"}):
        return "FLOP"
    return "PREFLOP"


def _mode_name(mode: Any) -> str:
    return str(getattr(mode, "value", mode) or "").strip().lower()


def evaluate_prefold(config: PrefoldConfig, context: PrefoldContext) -> PrefoldDecision:
    live = _mode_name(config.mode) == PrefoldMode.LIVE.value
    flags = {"audit_only": not live, "emit_protocol": live, "bypass_ai": live}
    if not config.enabled:
        return PrefoldDecision(False, None, "PREFOLD_DISABLED", **flags)
    if _mode_name(config.mode) not in {PrefoldMode.AUDIT.value, PrefoldMode.LIVE.value}:
        return PrefoldDecision(False, None, "PREFOLD_MODE_BLOCKED", audit_only=True, emit_protocol=False, bypass_ai=False)
    if not context.state_complete:
        return PrefoldDecision(False, None, "PREFOLD_STATE_INCOMPLETE", **flags)
    family = context.game_family.strip().upper().replace(" ", "").replace("_", "")
    if family in {"RING", "CASH", "CASHGAME", "NL", "NLHE", "HOLDEM", "HOLD'EM"}:
        family = "NLH"
    if family != "NLH":
        return PrefoldDecision(False, None, "PREFOLD_NOT_NLH", **flags)
    if context.street.strip().upper() != "PREFLOP":
        return PrefoldDecision(False, None, "PREFOLD_NOT_PREFLOP", **flags)
    if context.bombpot or context.straddle:
        return PrefoldDecision(False, None, "PREFOLD_SPECIAL_GAME_BLOCKED", **flags)
    if context.can_check:
        return PrefoldDecision(False, None, "PREFOLD_FREE_CHECK_BLOCKED", **flags)
    try:
        hand = canonical_nlh_hand(context.hole_cards)
        position = _position(context.position)
        facing = _facing(context.facing)
    except (PrefoldConfigError, ValueError):
        return PrefoldDecision(False, None, "PREFOLD_CONTEXT_INVALID", **flags)
    if not 2 <= int(context.dealt_in_players) <= 9:
        return PrefoldDecision(False, None, "PREFOLD_CONTEXT_INVALID", canonical_hand=hand, **flags)
    for rule in config.rules:
        if (
            rule.dealt_in_players == int(context.dealt_in_players)
            and rule.position == position
            and rule.facing == facing
            and hand in rule.cards
        ):
            reason = "LOCAL_PREFOLD_LIVE_MATCH" if live else "LOCAL_PREFOLD_AUDIT_MATCH"
            return PrefoldDecision(
                True, "FOLD", reason,
                canonical_hand=hand, rule_id=rule.rule_id, **flags,
            )
    return PrefoldDecision(False, None, "PREFOLD_NO_EXPLICIT_RULE", canonical_hand=hand, **flags)


def default_nlh_prefold_config() -> PrefoldConfig:
    """Conservative explicit trash-fold matrix for 2-9 NLH players."""

    cards = frozenset(normalize_hand_class(item) for item in _TRASH)
    rules: list[PrefoldRule] = []
    line = 1
    for players in range(2, 10):
        early = _EP_BY_COUNT.get(players, ())
        late = ("CO",) if players >= 5 else ()
        steal = ("BTN", "SB")
        for position in early:
            for facing in ("UNOPENED", "RAISE", "RERAISE"):
                line += 1
                rules.append(PrefoldRule(
                    f"{players}-{position}-{facing}", players, position, facing, cards, line,
                ))
        for position in late:
            for facing in ("RAISE", "RERAISE"):
                line += 1
                rules.append(PrefoldRule(
                    f"{players}-{position}-{facing}", players, position, facing, cards, line,
                ))
        for position in steal:
            facings = ("RAISE", "RERAISE")
            for facing in facings:
                line += 1
                rules.append(PrefoldRule(
                    f"{players}-{position}-{facing}", players, position, facing, cards, line,
                ))
        if players >= 3:
            for facing in ("RAISE", "RERAISE"):
                line += 1
                rules.append(PrefoldRule(
                    f"{players}-BB-{facing}", players, "BB", facing, cards, line,
                ))
    return PrefoldConfig(enabled=True, mode=PrefoldMode.LIVE, rules=tuple(rules))


def load_prefold_config(path: str | Path | None = None) -> PrefoldConfig:
    env_enabled = str(os.getenv("POKEREYE_PREFOLD", "1")).strip().lower()
    if env_enabled in {"0", "false", "no", "off"}:
        return PrefoldConfig.safe_default()
    candidate = path or os.getenv("POKEREYE_PREFOLD_FILE")
    if candidate:
        text = Path(candidate).read_text(encoding="utf-8")
        return parse_prefold_config(text)
    return default_nlh_prefold_config()
