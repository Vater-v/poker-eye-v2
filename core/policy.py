"""Hint→action policy: CC resolution with the call=0 state-gap guard.

This is the single translation layer every CC decision must pass through.
It combines the verified wire projection (``coin_wire.resolve_eye_cc_action``)
with the anomaly guard (``anomalies.assess_call_zero``) so that a suspicious
``call == 0`` while CHECK is legal is never silently sent as ``CALL 0``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .anomalies import AnomalyVerdict, CallZeroAssessment, assess_call_zero
from .coin_wire import ResolvedAction, resolve_eye_cc_action
from .state import StreetState, TableState


@dataclass(frozen=True)
class ActionDecision:
    action: Optional[ResolvedAction]  # None => nothing to send
    status: str  # decided | state_gap_check | needs_operator | error
    reason: str
    anomaly: Optional[CallZeroAssessment] = None


def _cc_type(cc: Dict[str, Any]) -> str:
    return str(cc.get("type") or cc.get("action") or cc.get("message") or "").strip().upper()


def decide_cc_action(
    cc: Dict[str, Any],
    *,
    table_state: TableState,
    hero_seat: int,
    user_turn_options: Optional[Dict[str, Any]] = None,
    chip_scale: int = 100,
    source: Optional[str] = None,
    seq: Optional[int] = None,
    raw_frame: Optional[bytes] = None,
    body: Optional[bytes] = None,
) -> ActionDecision:
    """Translate an EYE CC into the exact Coin action to send, or a refusal.

    Ordering of checks (policy, not convenience):
    1. If the computed call amount is zero AND CHECK is legal, the state says
       "no money needed". Never emit ``CALL 0`` here; prefer CHECK and mark
       the gap (``state_gap_check``). If CHECK is not even legal while the
       call amount is zero, the state is contradictory (``needs_operator``).
    2. Otherwise project the CC onto Coin's paid legal actions.
    """
    street: StreetState = table_state.street_state
    call_need = street.call_need(hero_seat)
    typ = _cc_type(cc)
    paid_types = {"CALL", "RAISE", "BET", "ALLIN", "ALL-IN", "ALL_IN"}

    # The guard applies only when the CC asks for a paid action while the
    # state says no chips are needed: that is the call=0 -> CALL 0 trap.
    # CHECK/FOLD with call_need == 0 is the normal free-action path.
    if call_need == 0 and typ in paid_types:
        assessment = assess_call_zero(
            call_need=0,
            street_state=street,
            user_turn_options=user_turn_options,
            raw_frame=raw_frame,
            body=body,
            seq=seq,
            source=source,
            table_state=table_state,
        )
        if assessment.verdict == AnomalyVerdict.STATE_GAP:
            return ActionDecision(
                action=ResolvedAction("CHECK", 3, 0.0),
                status="state_gap_check",
                reason=assessment.reason,
                anomaly=assessment,
            )
        return ActionDecision(
            action=None,
            status="needs_operator",
            reason=assessment.reason,
            anomaly=assessment,
        )

    try:
        hero = street.seats.get(int(hero_seat))
        current_street_bet = float(hero.street_contribution) if hero else 0.0
        resolved = resolve_eye_cc_action(
            cc,
            user_turn_options=user_turn_options,
            current_street_bet=current_street_bet,
            chip_scale=chip_scale,
        )
    except ValueError as exc:
        return ActionDecision(action=None, status="error", reason=str(exc))

    # Sanity: a paid action against an existing bet requires the matching option.
    if resolved.coin_code == 4 and call_need > 0 and user_turn_options is not None:
        if not any(int(k) == 4 for k in user_turn_options if isinstance(k, (int, str)) and str(k).lstrip("-").isdigit()):
            return ActionDecision(
                action=None,
                status="needs_operator",
                reason="CALL decided but Coin exposes no call option",
            )
    return ActionDecision(action=resolved, status="decided", reason=f"cc {_cc_type(cc)} -> {resolved.name}")
