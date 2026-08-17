"""Anomaly guards: suspicious ``call == 0`` after a raise must not become CALL 0.

This implements the regression documented in ``docs/LOGGING_AND_CASES.md``:

    call=0 -> backend CHECK amount=0 -> Coin CALL betAmount=0

is a policy/state-gap inconsistency, not proof of internet loss. When the computed
call amount is zero AND CHECK is legal, the action policy must prefer CHECK and emit
``state.gap``; it must never silently normalize the selected action to ``CALL 0``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from .state import StreetState, TableState


class AnomalyVerdict(str, Enum):
    OK = "OK"
    STATE_GAP = "STATE_GAP"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"


@dataclass(frozen=True)
class CallZeroAssessment:
    verdict: AnomalyVerdict
    call_need: int
    current_max: int
    last_full_raise: int
    check_legal: bool
    body_sha256: str
    reason: str
    diagnostics: Dict[str, Any]


def _legal_option_codes(user_turn_options: Optional[Dict[str, Any]]) -> set:
    opts = user_turn_options or {}
    codes = set()
    for k in opts.keys():
        try:
            codes.add(int(k))
        except (TypeError, ValueError):
            pass
    return codes


def body_hash(raw_frame: Optional[bytes], body: Optional[bytes]) -> str:
    return hashlib.sha256(body or raw_frame or b"").hexdigest()


def assess_call_zero(
    *,
    call_need: int,
    street_state: StreetState,
    user_turn_options: Optional[Dict[str, Any]] = None,
    raw_frame: Optional[bytes] = None,
    body: Optional[bytes] = None,
    seq: Optional[int] = None,
    source: Optional[str] = None,
    table_state: Optional[TableState] = None,
) -> CallZeroAssessment:
    """Decide how to handle ``call_need == 0``.

    - ``CHECK`` legal (option 3 present) => ``STATE_GAP``: prefer CHECK, never CALL 0.
    - ``CHECK`` not legal but call is 0 => ``NEEDS_OPERATOR`` (contradictory state).
    - otherwise => ``OK``.
    """
    codes = _legal_option_codes(user_turn_options)
    check_legal = 3 in codes  # Coin CHECK option code (verified ACTION_CHECK == 3)
    diagnostics: Dict[str, Any] = {
        "street_state": street_state.snapshot(),
        "legal_options": sorted(codes),
        "seq": seq,
        "source": source,
        "body_sha256": body_hash(raw_frame, body),
    }
    if table_state is not None:
        diagnostics["hand_id"] = table_state.hand_id
        diagnostics["table_id"] = table_state.table_id
        diagnostics["generation"] = table_state.generation

    reason: str
    verdict: AnomalyVerdict
    if call_need != 0:
        verdict = AnomalyVerdict.OK
        reason = "OK: non-zero call; normal path"
    elif check_legal:
        verdict = AnomalyVerdict.STATE_GAP
        reason = "STATE_GAP: call_need==0 with CHECK legal; must prefer CHECK, never CALL 0"
    else:
        verdict = AnomalyVerdict.NEEDS_OPERATOR
        reason = "NEEDS_OPERATOR: call_need==0 but CHECK is not a legal action; contradictory state"

    return CallZeroAssessment(
        verdict=verdict,
        call_need=int(call_need),
        current_max=int(street_state.current_max),
        last_full_raise=int(street_state.last_full_raise),
        check_legal=check_legal,
        body_sha256=diagnostics["body_sha256"],
        reason=reason,
        diagnostics=diagnostics,
    )


def select_action_for_call_zero(assessment: CallZeroAssessment) -> Optional[Dict[str, Any]]:
    """Return the corrected action for a zero-call situation, or None if operator action.

    STATE_GAP -> CHECK. NEEDS_OPERATOR -> None (reconcile, do not auto-send).
    """
    if assessment.verdict == AnomalyVerdict.STATE_GAP:
        return {"type": "CHECK"}
    if assessment.verdict == AnomalyVerdict.NEEDS_OPERATOR:
        return None
    return None
