from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Iterator, Optional


@dataclass
class BombpotState:
    """Room capability plus facts that belong only to the current hand.

    ``induces_double_board`` is a room/configuration capability.  It must never be
    confused with ``is_double_board``, which is set only when the current hand has
    strong hand-scoped evidence of a second board.
    """

    enabled: bool = False
    fixed_hand: bool = False
    hands_in_bombpot: Optional[int] = None
    min_ante: Optional[float] = None
    max_ante: Optional[float] = None
    preflop_start: Optional[bool] = None
    induces_double_board: bool = False

    hand_id: str = ""
    is_double_board: bool = False
    is_bombpot_hand: bool = False
    hand_ante: Optional[float] = None
    total_hand_count: Optional[int] = None
    current_hand_number: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def proven_ppp_fields(self) -> dict[str, bool]:
        return {"RoomInfo.IsInBombpot": bool(self.is_bombpot_hand)}


_CONFIG_PATH_MARKERS = (
    "roomproperties",
    "gameroomproperties",
    "tablesToJoin".lower(),
    "room_props",
    "roomprops",
)

_SECOND_BOARD_KEYS = {
    "dealerCardsDoubleBoard",
    "potAmountListDoubleBoard",
    "rabbitRunCardsDoubleBoard",
    "winnerInfoDoubleBoard",
    "doubleBoardCards",
    "secondBoardCards",
    "dealerCardsRit2",
}

def _walk(value: Any, path: str = "root") -> Iterator[tuple[str, Any]]:
    """Walk nested objects and JSON strings without throwing on malformed payloads."""
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except Exception:
                return
            yield from _walk(decoded, f"{path}<json>")


def _command(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    outer = payload.get("p")
    if not isinstance(outer, dict):
        return ""
    return str(outer.get("c") or "")


def _nonempty(value: Any) -> bool:
    if value in (None, "", False, 0):
        return False
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _configuration_path(path: str, node: dict[str, Any]) -> bool:
    lower = path.lower()
    if any(marker in lower for marker in _CONFIG_PATH_MARKERS):
        return True
    # Coin room properties place these capability fields next to each other.
    return (
        ("isBombpot" in node or "isFixedHandBombpot" in node)
        and "isBombpotHand" not in node
    )


def detect_double_board(payload: Any, command: Optional[str] = None) -> tuple[bool, str]:
    """Return true only for an *active current-hand* second board.

    The legacy corpus proves that a room can advertise
    ``bombpotInducesDoubleBoard=true`` while dozens of ordinary hands have
    ``isBombpotHand=false`` and no second-board cards.  Therefore room capability,
    lobby labels and configuration flags alone are deliberately ignored.
    """

    cmd = str(command or _command(payload) or "")
    cmd_lower = cmd.lower()

    # Double-board is a hand-scoped game fact.  Lobby/configuration messages may
    # contain previews, capabilities and even cached board-shaped fields; none of
    # them is evidence that the currently played hand has a second board.
    if not cmd_lower.startswith("game."):
        return False, ""

    # This command exists only for the actual second-board odds flow, not lobby
    # capability discovery.
    if cmd_lower == "game.poker_odds_doubleboard":
        return True, f"{cmd}: active double-board odds"

    for path, value in _walk(payload):
        if not isinstance(value, dict):
            continue

        # Non-empty second-board data is the strongest evidence and is valid even
        # if Coin omitted the boolean flags.
        for key in _SECOND_BOARD_KEYS:
            if key in value and _nonempty(value.get(key)):
                return True, f"{path}.{key}=nonempty"

        is_hand = value.get("isBombpotHand") is True
        if is_hand and value.get("bombpotInducesDoubleBoard") is True:
            return True, f"{path}.isBombpotHand+bombpotInducesDoubleBoard"
        if is_hand and value.get("isDoubleBoard") is True:
            return True, f"{path}.isBombpotHand+isDoubleBoard"

        # Catalog/wait_list often ships isDoubleBoard:1 as a room capability
        # (same class as isStraddle:1). That is not this-hand second board.
        # Only nonempty second-board cards / odds / bombpot-hand flags count.

    return False, ""


class BombpotTracker:
    def __init__(self) -> None:
        self.state = BombpotState()

    def reset_hand(self, hand_id: Any = "") -> BombpotState:
        """Clear hand-scoped facts while preserving room configuration."""
        self.state.hand_id = str(hand_id or "")
        self.state.is_double_board = False
        self.state.is_bombpot_hand = False
        self.state.hand_ante = None
        self.state.total_hand_count = None
        self.state.current_hand_number = None
        return self.state

    def observe(self, payload: Any) -> BombpotState:
        cmd = _command(payload)
        cmd_lower = cmd.lower()

        if cmd_lower == "game.reset_data":
            return self.reset_hand()

        # A distinct new hand cannot inherit a previous bombpot/double-board flag.
        if cmd_lower in {"game.game_init", "game.pre_hand_start_info", "game.game_alldata"}:
            hand_id = ""
            for _path, node in _walk(payload):
                if isinstance(node, dict):
                    candidate = node.get("gameId") or node.get("handId")
                    if candidate not in (None, ""):
                        hand_id = str(candidate)
                        break
            if hand_id and hand_id != self.state.hand_id:
                self.reset_hand(hand_id)

        for path, node in _walk(payload):
            if not isinstance(node, dict):
                continue

            if "isBombpot" in node or "isFixedHandBombpot" in node:
                self.state.enabled = bool(node.get("isBombpot", self.state.enabled))
                self.state.fixed_hand = bool(
                    node.get("isFixedHandBombpot", self.state.fixed_hand)
                )
                for key, attr, cast in (
                    ("handsInBombpot", "hands_in_bombpot", int),
                    ("minAnte", "min_ante", float),
                    ("maxAnte", "max_ante", float),
                ):
                    if node.get(key) is not None:
                        try:
                            setattr(self.state, attr, cast(node[key]))
                        except Exception:
                            pass
                if "isPreflopStart" in node:
                    self.state.preflop_start = bool(node["isPreflopStart"])
                if "bombpotInducesDoubleBoard" in node:
                    self.state.induces_double_board = bool(
                        node["bombpotInducesDoubleBoard"]
                    )

            if "isBombpotHand" in node:
                self.state.is_bombpot_hand = bool(node.get("isBombpotHand"))
                if not self.state.is_bombpot_hand:
                    self.state.is_double_board = False
                    self.state.hand_ante = None
                if node.get("ante") is not None:
                    try:
                        self.state.hand_ante = float(node["ante"])
                    except Exception:
                        pass
                if node.get("totalHandCount") is not None:
                    try:
                        self.state.total_hand_count = int(node["totalHandCount"])
                    except Exception:
                        pass
                if node.get("currentHandNumber") is not None:
                    try:
                        self.state.current_hand_number = int(node["currentHandNumber"])
                    except Exception:
                        pass

        active, _reason = detect_double_board(payload, cmd)
        if active:
            self.state.is_double_board = True
        return self.state
