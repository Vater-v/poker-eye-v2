"""Lobby / felt uidump: parse one dump, emit one plan. No device state."""
from __future__ import annotations

import re
from typing import Any, Optional, TypedDict

MIN_PLAYERS = 3
MIN_TABLE_SIZE = 6

_UI_SKIP = {
    "empty", "fold", "check", "call", "raise", "bet", "allin", "nlh", "plo",
    "plo4", "plo5", "plo6", "joinsimilar", "splash", "prize", "rank",
    "tableclosed", "gotomytables", "yes", "no", "ok", "lobby", "menu",
    "understood", "cancel", "info", "понятно",
}


class UiNodeRow(TypedDict, total=False):
    t: str
    x: int
    y: int
    w: int
    h: int
    id: str


class UiDump(TypedDict, total=False):
    closed: bool
    loading: bool
    waitlist: bool
    players: int
    empty_seats: int
    names: list[str]
    shortest_timer: Optional[float]
    janitor: bool
    tap: str
    tap_age_ms: int
    nodes: int
    height: int
    live: bool
    rows: list[UiNodeRow]


class LobbyPlan(TypedDict, total=False):
    scene: str
    step: str
    tap: Optional[dict[str, Any]]
    leave: bool
    hu: bool
    reason: str


def _ui_attr(tag: str, name: str) -> str:
    key = f'{name}="'
    at = tag.find(key)
    if at < 0:
        return ""
    at += len(key)
    stop = tag.find('"', at)
    return tag[at:stop] if stop > at else ""


def _ui_int(tag: str, name: str) -> int:
    try:
        return int(_ui_attr(tag, name) or 0)
    except ValueError:
        return 0


def _parse_tab_timer(label: str) -> float:
    raw = str(label or "").strip().lower().replace(" ", "")
    if raw.endswith("s") and 2 <= len(raw) <= 6:
        raw = raw[:-1].replace(",", ".")
    elif not (raw.isdigit() and 1 <= len(raw) <= 3):
        return 0.0
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return 0.0
    return value if 0 < value <= 120 else 0.0


def parse_ui_dump(xml: str) -> UiDump:
    """Parse the APK uidump. Immediate table facts, no seat_map timeouts."""
    text = str(xml or "")
    compact = "".join(ch.lower() for ch in text if ch > " ")
    head_end = text.find(">")
    head = text[:head_end] if head_end > 0 else ""
    janitor = _ui_attr(head, "janitor") == "1"
    tap = _ui_attr(head, "tap").strip()
    try:
        tap_age_ms = int(_ui_attr(head, "tapage") or -1)
    except ValueError:
        tap_age_ms = -1
    try:
        node_count = int(_ui_attr(head, "nodes") or 0)
    except ValueError:
        node_count = 0
    closed = "tableclosed" in compact or "столзакрыт" in compact
    rows = []
    token = 0
    while True:
        start = text.find('t="', token)
        if start < 0:
            break
        start += 3
        end = text.find('"', start)
        if end < 0:
            break
        label = text[start:end].strip()
        close = text.find("/>", end)
        attrs = text[end:close] if close > end else text[end:end + 160]
        blob = "<n" + attrs + ">"
        rows.append((label, _ui_int(blob, "x"), _ui_int(blob, "y"), _ui_int(blob, "w"), _ui_int(blob, "h"), _ui_attr(blob, "id")))
        token = end + 1
    height = 1920
    for _label, _x, y, _w, h, _i in rows:
        if y + h > height:
            height = y + h
    names = []
    empty = 0
    timers: list[float] = []
    for label, x, y, w, h, _i in rows:
        low = "".join(ch.lower() for ch in label if ch > " ")
        if low == "empty":
            empty += 1
            continue
        if y <= height * 24 // 100:
            value = _parse_tab_timer(label)
            if value > 0:
                timers.append(value)
        if low in _UI_SKIP or not label or label.isdigit():
            continue
        if y < height * 22 // 100 or y > height * 80 // 100:
            continue
        names.append(label)
    players = len({name.casefold() for name in names})
    if closed:
        players = 0
    elif empty and not names:
        players = 0
    elif empty and names:
        players = len({name.casefold() for name in names})
    waitlist = "joinwaitlist" in compact or "findmeaseat" in compact or "playersinline" in compact
    loading = (not closed) and (not names) and empty > 0
    node_rows: list[UiNodeRow] = [
        {"t": label, "x": x, "y": y, "w": w, "h": h, "id": role}
        for label, x, y, w, h, role in rows
    ]
    live = any(
        "".join(ch.lower() for ch in label if ch > " ") in {
            "fold", "check", "call", "raise", "bet", "allin",
        }
        and y > height * 65 // 100
        for label, x, y, w, h, _role in rows
    )
    return {
        "closed": closed,
        "loading": loading,
        "waitlist": waitlist,
        "players": players,
        "empty_seats": empty,
        "names": names,
        "shortest_timer": min(timers) if timers else None,
        "janitor": janitor,
        "tap": tap,
        "tap_age_ms": tap_age_ms,
        "nodes": node_count,
        "height": height,
        "live": live,
        "rows": node_rows,
    }


def _ui_compact(text: str) -> str:
    return "".join(ch.lower() for ch in str(text or "") if ch > " ")


_CURRENCY_RE = re.compile(r"[₮$€£₹₽¥₩¤]")
_CHIP_OFF_MARKS = ("×", "✕", "✖")


def _money_text(text: str) -> str:
    raw = _CURRENCY_RE.sub("", str(text or ""))
    return raw.replace(" ", "").replace(",", ".").lower()


def _limit_pairs(bb: float) -> tuple[str, ...]:
    target = float(bb)
    sb = target / 2.0
    return (
        f"{sb:g}/{target:g}",
        f"{sb:.2f}".rstrip("0").rstrip(".") + "/" + f"{target:.2f}".rstrip("0").rstrip("."),
        f"{sb:.2f}/{target:.2f}",
    )


def _bb_needles(bb: float) -> tuple[str, ...]:
    return _limit_pairs(bb)


def _is_limit_pair(text: str, bb: float) -> bool:
    blob = _money_text(text)
    return any(pair in blob for pair in _limit_pairs(bb))


def _chip_enabled(compact: str) -> bool:
    c = str(compact or "")
    return any(mark in c for mark in _CHIP_OFF_MARKS)


def _strip_chip_mark(compact: str) -> str:
    c = str(compact or "")
    for mark in _CHIP_OFF_MARKS:
        c = c.replace(mark, "")
    if c.endswith("x") and len(c) > 1:
        c = c[:-1]
    return c


def _is_hu_label(compact: str) -> bool:
    c = str(compact or "")
    if c in {"nlh", "holdem", "nlhe"}:
        return False
    if "multiway" in c:
        return False
    if c in {"hu", "heads-up", "headsup", "headsupnlh", "nlhhu"}:
        return True
    if "headsup" in c or "heads-up" in c:
        return True
    if c.endswith("hu") and "6max" not in c:
        return True
    return False


def _is_six_max_label(compact: str) -> bool:
    c = str(compact or "").replace("-", "")
    return "6max" in c or c in {"sixmax", "6handed", "fullring", "nlh6max"}


def _is_multiway_label(compact: str) -> bool:
    c = str(compact or "").replace("-", "")
    return "multiway" in c or c in {"mw", "мультивей"}


def _is_holdem_label(compact: str) -> bool:
    c = _strip_chip_mark(compact)
    if _is_hu_label(c) or _is_multiway_label(c):
        return False
    return c in {"nlh", "holdem", "nlhe", "texas", "hold'em"} or "холдем" in c


def _is_plo_label(compact: str) -> bool:
    c = _strip_chip_mark(compact)
    return c in {"plo", "omaha", "plo4"} or c.startswith("plo") or "омаха" in c


def _is_cash_label(compact: str) -> bool:
    c = str(compact or "")
    if c in {"freegames", "freegame"}:
        return False
    return (
        c in {"cash", "cashgames", "ring", "ringgames", "playcash", "playcashgames"}
        or "cashgame" in c
        or "кэш" in c
    )


def _is_join_similar(compact: str) -> bool:
    c = str(compact or "")
    return "joinsimilar" in c or c == "similar"


def _is_home_marker(compact: str) -> bool:
    c = str(compact or "").replace("'", "").replace("\u2019", "")
    return c in {"letsexplore", "pokerformats", "popular", "freegames"} or "letsexplore" in c


def _is_loading_label(compact: str) -> bool:
    c = str(compact or "")
    if not c:
        return False
    return (
        "initializ" in c
        or "loading" in c
        or "connecting" in c
        or "pleasewait" in c
        or "загрузк" in c
        or "подождит" in c
        or c in {"tableloading", "loadingtable"}
    )


def _is_stack_label(compact: str) -> bool:
    c = str(compact or "").replace(",", ".")
    return c.endswith("bb") and any(ch.isdigit() for ch in c)


def _is_felt_stakes(compact: str, text: str, bb: float) -> bool:
    """Center-felt 'NLH ₮0.01/₮0.02' — table chrome, never a lobby limit card."""
    if not _is_limit_pair(text, bb):
        return False
    c = str(compact or "").replace("-", "")
    return c.startswith("nlh") or c.startswith("plo") or c.startswith("omaha")


def _is_exit_confirm(compact: str) -> bool:
    c = str(compact or "")
    if "areyousureyouwanttoexittable" in c:
        return True
    if c in {"understood", "понятно", "leaveandexit", "leaveandexittable"}:
        return True
    return "уверен" in c and "покинутьстол" in c


def classify_lobby_scene(
    nodes: list[dict[str, Any]],
    *,
    bb: float,
    min_players: int = MIN_PLAYERS,
    min_size: int = MIN_TABLE_SIZE,
) -> LobbyPlan:
    """One dump → one validated step. Never open HU or an empty observer felt."""
    height = 1920
    for row in nodes or ():
        try:
            height = max(height, int(row.get("y") or 0) + int(row.get("h") or 0))
        except (TypeError, ValueError):
            continue

    def compact(row: dict[str, Any]) -> str:
        return _ui_compact(row.get("t") or "")

    def center(row: dict[str, Any], why: str) -> dict[str, Any]:
        x = int(row.get("x") or 0) + max(1, int(row.get("w") or 1)) // 2
        y = int(row.get("y") or 0) + max(1, int(row.get("h") or 1)) // 2
        return {"x": x, "y": y, "why": why}

    rows = list(nodes or ())
    live = any(
        compact(row) in {"fold", "check", "call", "raise", "bet", "allin"}
        and int(row.get("y") or 0) > height * 65 // 100
        for row in rows
    )
    closed = any("tableclosed" in compact(row) or "столзакрыт" in compact(row) for row in rows)
    empty = sum(1 for row in rows if compact(row) == "empty")
    hu_chip = next((row for row in rows if _is_hu_label(compact(row))), None)
    six_chip = next((row for row in rows if _is_six_max_label(compact(row))), None)
    multiway = next((row for row in rows if _is_multiway_label(compact(row))), None)
    join_similar = next((row for row in rows if _is_join_similar(compact(row))), None)
    cash = next((row for row in rows if _is_cash_label(compact(row))), None)
    home_screen = any(_is_home_marker(compact(row)) for row in rows)
    has_pot = any(compact(row) == "pot" or compact(row).startswith("pot") for row in rows)
    exit_confirm = any(_is_exit_confirm(compact(row)) for row in rows)

    def by_id(role: str) -> Optional[dict[str, Any]]:
        return next((row for row in rows if str(row.get("id") or "") == role), None)

    join_similar = by_id("peye.join_similar") or join_similar
    cash = by_id("peye.cash_games") or cash
    multiway = by_id("peye.multiway") or multiway
    hu_chip = by_id("peye.heads_up") or hu_chip
    loading_ui = any(_is_loading_label(compact(row)) for row in rows)
    if loading_ui:
        return {
            "scene": "boot",
            "step": "wait",
            "tap": None,
            "leave": False,
            "hu": False,
            "reason": "загрузка",
        }
    if exit_confirm:
        return {
            "scene": "table",
            "step": "confirm",
            "tap": None,
            "leave": False,
            "hu": False,
            "reason": "подтверждение выхода",
        }
    occ: list[tuple[dict[str, Any], int, int]] = []
    for row in rows:
        match = re.search(r"^(\d)\s*/\s*(\d)$", str(row.get("t") or "").strip())
        if not match:
            continue
        taken, size = int(match.group(1)), int(match.group(2))
        if 1 <= size <= 9 and 0 <= taken <= size:
            occ.append((row, taken, size))
    ring = [(row, taken, size) for row, taken, size in occ if size >= int(min_size)]
    hu_only = bool(occ) and all(size <= 2 for _row, _taken, size in occ)
    hu = hu_chip is not None or hu_only
    lobby_list = len(ring) >= 1 or (len(occ) >= 2 and not hu_only)
    felt_stakes = any(
        _is_felt_stakes(compact(row), str(row.get("t") or ""), bb)
        for row in rows
    )
    table_chrome = bool(join_similar) or felt_stakes or any(
        compact(row).startswith("nlh-") or compact(row).startswith("plo-")
        or compact(row).startswith("plo4-") or compact(row).startswith("plo5-")
        or compact(row).startswith("plo6-")
        for row in rows
    )
    at_felt = (
        live or closed or table_chrome or has_pot
        or (empty >= 2 and not lobby_list and multiway is None and cash is None and not home_screen)
    )
    players = len({
        str(row.get("t") or "")
        for row in rows
        if compact(row) not in _UI_SKIP
        and not _is_stack_label(compact(row))
        and str(row.get("t") or "") and not str(row.get("t") or "").isdigit()
        and height * 22 // 100 <= int(row.get("y") or 0) <= height * 80 // 100
    })

    if at_felt:
        chrome_only = (
            join_similar is not None
            and not live
            and not closed
            and empty < 2
            and players < 2
            and not felt_stakes
            and not has_pot
        )
        if chrome_only:
            return {
                "scene": "boot",
                "step": "wait",
                "tap": None,
                "leave": False,
                "hu": False,
                "reason": "загрузка",
            }
        lonely = (not live) and (not has_pot) and players < int(min_players) and empty >= 2
        junk = bool(closed or hu_only or lonely)
        if junk:
            return {
                "scene": "junk",
                "step": "leave",
                "tap": None,
                "leave": True,
                "hu": True if hu or hu_only else False,
                "reason": "HU стол" if (hu or hu_only) else "пустой стол",
            }
        if join_similar is not None:
            return {
                "scene": "table",
                "step": "similar",
                "tap": center(join_similar, "join-similar"),
                "leave": False,
                "hu": False,
                "reason": "Join Similar",
            }
        return {"scene": "table", "step": "wait", "tap": None, "leave": False, "hu": False, "reason": "за столом"}

    need = max(1, int(min_players) - 1)
    ring_open = [
        (row, taken, size) for row, taken, size in ring
        if taken < size and taken >= need
    ]
    if ring_open:
        ring_open.sort(key=lambda item: (abs(item[1] - 4), -item[1]))
        return {
            "scene": "lobby",
            "step": "table",
            "tap": center(ring_open[0][0], "lobby-table"),
            "leave": False,
            "hu": False,
            "reason": f"стол {ring_open[0][1]}/{ring_open[0][2]}",
        }

    if multiway is not None:
        return {
            "scene": "lobby",
            "step": "multiway",
            "tap": center(multiway, "lobby-multiway"),
            "leave": False,
            "hu": False,
            "reason": "Multiway",
        }

    if hu_only or (hu_chip is not None and not ring):
        if six_chip is not None:
            return {"scene": "lobby", "step": "clear-hu", "tap": center(six_chip, "lobby-6max"), "leave": False, "hu": True, "reason": "снять HU"}
        if hu_chip is not None:
            return {"scene": "lobby", "step": "clear-hu", "tap": center(hu_chip, "lobby-clear-hu"), "leave": False, "hu": True, "reason": "снять HU фильтр"}
        return {"scene": "lobby", "step": "wait", "tap": None, "leave": False, "hu": True, "reason": "HU фильтр"}

    nlh_on = any(_chip_enabled(compact(row)) and _is_holdem_label(compact(row)) for row in rows)
    plo_on = any(_chip_enabled(compact(row)) and _is_plo_label(compact(row)) for row in rows)
    plo_off = next(
        (
            row for row in rows
            if _is_plo_label(compact(row))
            and not _chip_enabled(compact(row))
            and compact(row) in {"plo", "omaha"}
            and int(row.get("h") or 0) < height * 10 // 100
        ),
        None,
    )
    nlh_off = next(
        (
            row for row in rows
            if _is_holdem_label(compact(row))
            and not _chip_enabled(compact(row))
            and compact(row) in {"nlh", "holdem", "nlhe"}
            and int(row.get("h") or 0) < height * 10 // 100
        ),
        None,
    )
    if nlh_on and not plo_on and plo_off is not None:
        return {"scene": "lobby", "step": "plo", "tap": center(plo_off, "lobby-plo"), "leave": False, "hu": False, "reason": "включить PLO"}
    if plo_on and not nlh_on and nlh_off is not None:
        return {"scene": "lobby", "step": "nlh", "tap": center(nlh_off, "lobby-nlh"), "leave": False, "hu": False, "reason": "включить NLH"}

    for row in rows:
        if not _is_limit_pair(str(row.get("t") or ""), bb):
            continue
        if _is_hu_label(compact(row)) or _is_felt_stakes(compact(row), str(row.get("t") or ""), bb):
            continue
        if int(row.get("y") or 0) < height * 8 // 100:
            continue
        if int(row.get("h") or 0) >= height * 16 // 100:
            continue
        return {"scene": "lobby", "step": "limit", "tap": center(row, "lobby-limit"), "leave": False, "hu": False, "reason": "лимит"}

    if cash is not None and home_screen:
        return {"scene": "lobby", "step": "cash", "tap": center(cash, "lobby-cash"), "leave": False, "hu": False, "reason": "кэш"}

    for row in rows:
        c = compact(row)
        if c in {"lobby", "лобби", "games"} and int(row.get("y") or 0) > height * 70 // 100:
            return {"scene": "lobby", "step": "home", "tap": center(row, "lobby-home"), "leave": False, "hu": False, "reason": "лобби"}
    return {"scene": "lobby", "step": "wait", "tap": None, "leave": False, "hu": hu, "reason": "ждём экран"}


def plan_lobby_join_tap(
    nodes: list[dict[str, Any]],
    *,
    bb: float,
    game: str = "NLH",
    min_players: int = MIN_PLAYERS,
) -> Optional[dict[str, Any]]:
    plan = classify_lobby_scene(nodes, bb=bb, min_players=min_players)
    if plan.get("leave"):
        return None
    tap = plan.get("tap")
    return dict(tap) if isinstance(tap, dict) else None
