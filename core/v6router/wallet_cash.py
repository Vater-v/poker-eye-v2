"""Lobby cash from Coin traffic. Never reconstruct from table stacks."""
from __future__ import annotations

from typing import Any, Optional


_LOBBY_CASH_KEYS = (
    "balance",
    "coinBalance",
    "userBalance",
    "chipsBalance",
    "cashBalance",
    "availableBalance",
)
_GOLD_KEYS = ("gold", "leftGold", "userGold", "goldBalance")


def _finite(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def wallet_from_payload(data: Any, *, source: str = "lobby") -> Optional[float]:
    """Parse spendable lobby cash.

    Table/game packets often carry leftover gold after a buy-in. That is not
    the lobby wallet and must not seed History St/End.
    """
    if not isinstance(data, dict):
        return None
    origin = str(source or "lobby").strip().lower()
    if origin in {"table", "game", "seat"}:
        return None
    for key in _LOBBY_CASH_KEYS:
        value = _finite(data.get(key))
        if value is not None and value > 0:
            return float(value)
    if origin in {"lobby", "account", "user"}:
        for key in _GOLD_KEYS:
            value = _finite(data.get(key))
            if value is not None and value > 0:
                return float(value)
    return None


def apply_table_close(
    *,
    wallet: Optional[float],
    stack_bb: Any = None,
    bb: Any = None,
) -> Optional[float]:
    """Table close must not invent cash by adding the sitting stack back."""
    value = _finite(wallet)
    return None if value is None else float(value)


def is_lobby_command(command: Any) -> bool:
    word = str(command or "").strip().lower()
    return word.startswith("lobby.") or word in {"account.info", "user.info", "user.balance"}
