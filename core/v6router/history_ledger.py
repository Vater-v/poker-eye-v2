"""History A:N ledger keyed by Coin nickname + type + limit.

Last-row fill only for an in-progress nick+type+limit session. Finished History
lines are never rewritten. Same type+limit may sum; NL vs PLO and distinct
limits never share a row. Google I/O is queued only while the device ``учет``
tumbler is on. Tests drive FakeSheetsTransport; missing credentials stay dry.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


HISTORY_HEADERS = (
    "Date",
    "Nickname",
    "Club",
    "Time St",
    "Time End",
    "Time",
    "Balance St",
    "Balance End",
    "Profit",
    "Type",
    "Limit",
    "Hands S",
    "Hands E",
    "Hands Sum",
)
SPREADSHEET_ID = "12qr4j6kAhnCVOAgB4xsy40PhVmR-FSM8La250FQnpyo"
HISTORY_SHEET = "History"
HISTORY_GID = 490361232
HISTORY_RANGE = "History!A:N"
CLUB = "coin"
GOOGLE_DATE_EPOCH = date(1899, 12, 30)
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
COLUMNS = 14

# Google ru_RU uses `;` as the formula argument separator. Comma formulas
# write as `#ERROR!` on the live History sheet.
DURATION_FORMULA = (
    '=IF(OR(INDEX(A:A;ROW())="";INDEX(D:D;ROW())="";'
    'INDEX(E:E;ROW())="");"";'
    '(INDEX(A:A;ROW())+INDEX(E:E;ROW())+'
    'IF(INDEX(E:E;ROW())<INDEX(D:D;ROW());1;0))'
    '-(INDEX(A:A;ROW())+INDEX(D:D;ROW())))'
)
PROFIT_FORMULA = (
    '=IF(OR(INDEX(G:G;ROW())="";INDEX(H:H;ROW())="");"";'
    'INDEX(H:H;ROW())-INDEX(G:G;ROW()))'
)

_TYPE_ALIASES = {
    "NLH": "NL",
    "NLHE": "NL",
    "HOLDEM": "NL",
    "HOLD'EM": "NL",
    "NL": "NL",
    "RING": "NL",
    "CASH": "NL",
    "CASHGAME": "NL",
    "NLB": "NLB",
    "BOMBPOT": "NLB",
    "PLO": "PLO4",
    "OMAHA": "PLO4",
    "PLO4": "PLO4",
    "PLO5": "PLO5",
    "PLO6": "PLO6",
}
_SHEET_GAME_TYPES = frozenset({"NL", "PLO4", "PLO5", "PLO6", "NLB"})
_JUNK_TYPE_CELLS = frozenset({"ring", "cash", "cashgame"})


def novosibirsk_now() -> datetime:
    """History Date / Time St / Time End are Novosibirsk wall clock (UTC+7)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Novosibirsk"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=7)))


moscow_now = novosibirsk_now


def ledger_type(game_type: Any) -> str:
    """History Type cell: NL / PLO4 / PLO5 / PLO6 / NLB.

    Coin ``gameType=Ring`` is the cash format, not the variant. It must never
    land in the Type column.
    """
    text = str(game_type or "").upper().replace(" ", "").replace("_", "").replace("-", "")
    if "BOMB" in text:
        return "NLB"
    mapped = _TYPE_ALIASES.get(text, "")
    if mapped in _SHEET_GAME_TYPES:
        return mapped
    return "NL"


# CGM History Limit column (shared strings): 0,01/0,02 0,02/0,05 0,05/0,1
# 0,1/0,2 0,2/0,4 0,25/0,5 0,5/1 — trailing zeros are stripped.
_KNOWN_SB_FOR_BB = {
    0.02: 0.01,
    0.05: 0.02,
    0.1: 0.05,
    0.2: 0.1,
    0.4: 0.2,
    0.5: 0.25,
    1.0: 0.5,
}


def euro_amount(value: float) -> str:
    """European stake fragment matching CGM History (0,1 not 0,10; 1 not 1,00)."""
    number = round(float(value), 2)
    text = f"{number:.2f}".replace(".", ",")
    if "," in text:
        text = text.rstrip("0").rstrip(",")
    return text or "0"


def normalize_limit(value: Any) -> str:
    text = _cell_text(value).replace(".", ",")
    if "/" not in text:
        return text
    parts = []
    for part in text.split("/"):
        chunk = part.strip()
        if "," in chunk:
            chunk = chunk.rstrip("0").rstrip(",")
        parts.append(chunk)
    return "/".join(parts)


def ledger_limit(coin_bb: Any) -> str:
    try:
        bb = float(coin_bb)
    except (TypeError, ValueError):
        return ""
    if bb <= 0 or bb != bb or bb in {float("inf"), float("-inf")}:
        return ""
    known = min(_KNOWN_SB_FOR_BB, key=lambda item: abs(item - bb))
    if abs(known - bb) <= 0.011:
        return f"{euro_amount(_KNOWN_SB_FOR_BB[known])}/{euro_amount(known)}"
    return f"{euro_amount(bb / 2.0)}/{euro_amount(bb)}"


def session_key(nickname: str, game_type: str, limit: str) -> str:
    nick = " ".join(str(nickname or "").split()).casefold()
    return f"{nick}|{ledger_type(game_type)}|{normalize_limit(limit)}"


def google_date_serial(when: datetime) -> int:
    return (when.date() - GOOGLE_DATE_EPOCH).days


def google_time_fraction(when: datetime) -> float:
    return (
        when.hour * 3600
        + when.minute * 60
        + when.second
        + when.microsecond / 1_000_000
    ) / 86400.0


def google_time_formula(when: datetime) -> str:
    """Locale-safe clock cell. USER_ENTERED `=TIME(h;m;s)` displays as time."""
    return f"=TIME({int(when.hour)};{int(when.minute)};{int(when.second)})"


def google_duration_formula(started: datetime, ended: datetime) -> str:
    seconds = int((ended - started).total_seconds())
    if seconds < 0:
        seconds += 86400
    seconds = max(0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"=TIME({hours};{minutes};{secs})"


def time_fraction_from_cell(value: Any) -> float:
    text = _cell_text(value)
    if not text:
        return 0.0
    upper = text.upper().replace(" ", "")
    if upper.startswith("=TIME("):
        inner = upper[6:-1] if upper.endswith(")") else upper[6:]
        parts = inner.replace(";", ",").split(",")
        try:
            hour = int(float(parts[0])) if parts else 0
            minute = int(float(parts[1])) if len(parts) > 1 else 0
            second = int(float(parts[2])) if len(parts) > 2 else 0
        except (TypeError, ValueError):
            return 0.0
        return (hour * 3600 + minute * 60 + second) / 86400.0
    try:
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_open_history_row(row: list[Any] | tuple[Any, ...]) -> bool:
    cells = list(row) + [""] * COLUMNS
    return not _cell_text(cells[4])


def row_matches(
    row: list[Any] | tuple[Any, ...],
    *,
    nickname: str,
    game_type: str,
    limit: str,
) -> bool:
    cells = list(row) + [""] * COLUMNS
    nick = " ".join(_cell_text(cells[1]).split()).casefold()
    want = " ".join(str(nickname or "").split()).casefold()
    if not nick or nick != want:
        return False
    raw_type = _cell_text(cells[9])
    if raw_type.casefold() in _JUNK_TYPE_CELLS:
        return False
    if ledger_type(raw_type) != ledger_type(game_type):
        return False
    return normalize_limit(cells[10]) == normalize_limit(limit)


def datetime_from_history_row(row: list[Any] | tuple[Any, ...], fallback: datetime) -> datetime:
    cells = list(row) + [""] * COLUMNS
    try:
        serial = int(float(str(cells[0]).replace(",", ".")))
        frac = time_fraction_from_cell(cells[3])
    except (TypeError, ValueError):
        return fallback
    tz = fallback.tzinfo
    base = datetime.combine(GOOGLE_DATE_EPOCH + timedelta(days=serial), datetime.min.time(), tzinfo=tz)
    return base + timedelta(seconds=frac * 86400.0)


def last_filled_row_number(rows: list[list[Any]]) -> int:
    last = 1
    for index, row in enumerate(rows):
        if index == 0:
            continue
        cells = list(row) + [""] * COLUMNS
        if any(_cell_text(cell) for cell in cells[:COLUMNS]):
            last = index + 1
    return last


def find_last_matching_row(
    rows: list[list[Any]],
    *,
    nickname: str,
    game_type: str,
    limit: str,
) -> tuple[Optional[int], bool]:
    found: Optional[int] = None
    open_row = False
    for index, row in enumerate(rows):
        if index == 0:
            continue
        if not row_matches(row, nickname=nickname, game_type=game_type, limit=limit):
            continue
        found = index + 1
        open_row = is_open_history_row(row)
    return found, open_row


def _int_cell(value: Any) -> Optional[int]:
    text = _cell_text(value)
    if not text:
        return None
    try:
        return int(float(str(text).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _cash_cell(value: float) -> float:
    return round(float(value), 2)


def _cash_parse(value: Any) -> Optional[float]:
    text = _cell_text(value)
    if not text:
        return None
    try:
        number = float(str(text).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return round(number, 2)


def last_nickname_row(
    rows: list[list[Any]],
    nickname: str,
) -> Optional[list[Any]]:
    want = " ".join(str(nickname or "").split()).casefold()
    if not want:
        return None
    found: Optional[list[Any]] = None
    for index, row in enumerate(rows):
        if index == 0:
            continue
        cells = list(row) + [""] * COLUMNS
        nick = " ".join(_cell_text(cells[1]).split()).casefold()
        if nick == want:
            found = cells
    return found


def _usable_wallet(value: Any) -> Optional[float]:
    """Lobby cash. Zero/blank is unknown — never a table stack."""
    parsed = _cash_parse(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def last_game_hands_e(rows: list[list[Any]], nickname: str) -> Optional[int]:
    """Career Hands E from the last real game row, skipping rakeback/deposit."""
    prior = None
    want = " ".join(str(nickname or "").split()).casefold()
    if not want:
        return None
    for index, row in enumerate(rows):
        if index == 0:
            continue
        cells = list(row) + [""] * COLUMNS
        nick = " ".join(_cell_text(cells[1]).split()).casefold()
        if nick != want:
            continue
        kind = str(cells[9] or "").strip().casefold()
        if kind in LEDGER_MONEY_TYPES or kind in _JUNK_TYPE_CELLS:
            continue
        prior = cells
    if prior is None:
        return None
    ended = _int_cell(prior[12])
    if ended is not None:
        return ended
    return _int_cell(prior[11])


def seed_source(
    rows: list[list[Any]],
    nickname: str,
    *,
    wallet_cash: Any = None,
) -> str:
    """Where St would come from. ``lobby`` is fact; ``fallback`` is last End."""
    if _usable_wallet(wallet_cash) is not None:
        return "lobby"
    if last_nickname_row(rows, nickname) is not None:
        return "fallback"
    return "unknown"


def seed_balance_st(
    rows: list[list[Any]],
    nickname: str,
    *,
    wallet_cash: Any = None,
) -> Optional[float]:
    """New session St: lobby wallet if known, else last nick End, else last St.

    Never a table buy-in. wallet_cash is Coin lobby cash at sit. Last History
    End is the fallback when that lobby snapshot is missing. CGM 402 St=19.47
    is the previous End after rakeback, not a sum of table stacks.
    """
    wallet = _usable_wallet(wallet_cash)
    if wallet is not None:
        return wallet
    prior = last_nickname_row(rows, nickname)
    if prior is not None:
        ended = _cash_parse(prior[7])
        if ended is not None and ended > 0:
            return ended
        started = _cash_parse(prior[6])
        if started is not None and started > 0:
            return started
    return None


MIN_HANDS_TO_CLOSE = 5


def session_ready_to_close(hands: Any, *, force: bool = False) -> bool:
    """Short sits stay an open History row unless the emulator went offline."""
    if force:
        return True
    try:
        count = int(hands or 0)
    except (TypeError, ValueError):
        count = 0
    return count >= MIN_HANDS_TO_CLOSE


def close_balance_end(
    *,
    balance_st: Any,
    hand_profit: Any = 0.0,
    live_lobby: Any = None,
    reconstructed_wallet: Any = None,
) -> tuple[Optional[float], float]:
    """End cash after the last table. Lobby traffic wins.

    Reconstructed wallet (start leftover + sitting stacks) is the 4-5$ lie
    from run_7e1d. If Coin has not sent a lobby packet yet, St + hand P/L
    is the honest close — never the reconstructed number.
    """
    del reconstructed_wallet
    lobby = _usable_wallet(live_lobby)
    try:
        started = float(balance_st or 0.0)
    except (TypeError, ValueError):
        started = 0.0
    try:
        profit = float(hand_profit or 0.0)
    except (TypeError, ValueError):
        profit = 0.0
    if profit != profit or profit in {float("inf"), float("-inf")}:
        profit = 0.0
    if lobby is not None:
        if started > 0:
            profit = round(float(lobby) - started, 4)
        return float(lobby), profit
    if started > 0:
        return round(started + profit, 4), profit
    return None, profit


LEDGER_MONEY_TYPES = frozenset({"deposit", "withdrawal", "rakeback"})


def last_rakeback_baseline(
    rows: list[list[Any]],
    nickname: str,
) -> Optional[list[Any]]:
    """Last nick row that can seed rakeback: a real game/rakeback End, not RING junk."""
    want = " ".join(str(nickname or "").split()).casefold()
    if not want:
        return None
    found: Optional[list[Any]] = None
    for index, row in enumerate(rows):
        if index == 0:
            continue
        cells = list(row) + [""] * COLUMNS
        nick = " ".join(_cell_text(cells[1]).split()).casefold()
        if nick != want:
            continue
        raw = str(cells[9] or "").strip()
        kind = raw.casefold()
        if kind in _JUNK_TYPE_CELLS:
            continue
        if _cash_parse(cells[7]) is None:
            continue
        if kind in LEDGER_MONEY_TYPES or ledger_type(raw) in _SHEET_GAME_TYPES:
            found = cells
    return found


def rakeback_delta(
    rows: list[list[Any]],
    nickname: str,
    new_st: Any,
) -> Optional[float]:
    """Positive last-End → new-St gap with no deposit/withdrawal in between.

    CGM: End 18.86 → rakeback +0.61 → next St 19.47. Negative gaps and
    deposit/withdrawal rows are not rakeback.
    """
    prior = last_rakeback_baseline(rows, nickname)
    if prior is None:
        return None
    kind = str(prior[9] or "").strip().casefold()
    if kind in {"deposit", "withdrawal"}:
        return None
    ended = _cash_parse(prior[7])
    nxt = _usable_wallet(new_st)
    if ended is None or nxt is None:
        return None
    delta = round(float(nxt) - float(ended), 2)
    if delta <= 0:
        return None
    return delta


def assemble_rakeback_row(
    *,
    nickname: str,
    started_at: datetime,
    balance_st: float,
    profit: float,
) -> list[Any]:
    """Closed rakeback A:N matching CGM (times 0, empty limit, hands 0)."""
    st = _cash_cell(balance_st)
    profit = _cash_cell(profit)
    return [
        google_date_serial(started_at),
        str(nickname or "").strip(),
        CLUB,
        0.0,
        0.0,
        0,
        st,
        _cash_cell(st + profit),
        profit,
        "rakeback",
        "",
        0,
        0,
        0,
    ]


def _st_cell(value: Any) -> Any:
    parsed = _cash_parse(value)
    if parsed is None or parsed <= 0:
        return ""
    return _cash_cell(parsed)


def assemble_open_row(
    *,
    nickname: str,
    game_type: str,
    limit: str,
    started_at: datetime,
    balance_st: float,
    hands_s: int,
) -> list[Any]:
    return [
        google_date_serial(started_at),
        str(nickname or "").strip(),
        CLUB,
        google_time_formula(started_at),
        "",
        "",
        _st_cell(balance_st),
        "",
        "",
        ledger_type(game_type),
        str(limit or "").strip(),
        int(hands_s),
        "",
        "",
    ]


def assemble_close_row(
    *,
    nickname: str,
    game_type: str,
    limit: str,
    started_at: datetime,
    ended_at: datetime,
    balance_st: float,
    profit: float,
    hands_s: int,
    hands: int,
    balance_end: Any = None,
    fill_end_from_profit: bool = True,
) -> list[Any]:
    hands_e = int(hands_s) + int(hands)
    st_cell = _st_cell(balance_st)
    ended = _usable_wallet(balance_end)
    if ended is not None:
        end_cell: Any = _cash_cell(ended)
    elif fill_end_from_profit and st_cell != "":
        end_cell = _cash_cell(float(balance_st) + float(profit))
    else:
        end_cell = ""
    return [
        google_date_serial(started_at),
        str(nickname or "").strip(),
        CLUB,
        google_time_formula(started_at),
        google_time_formula(ended_at),
        google_duration_formula(started_at, ended_at),
        st_cell,
        end_cell,
        PROFIT_FORMULA if end_cell != "" and st_cell != "" else "",
        ledger_type(game_type),
        str(limit or "").strip(),
        int(hands_s),
        int(hands_e),
        int(hands),
    ]


def attach_session_profit(
    table_row: dict[str, Any],
    profit: Optional[float],
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Compact snapshot helper: current table profit only while ``учет`` is on."""
    row = dict(table_row)
    if enabled and profit is not None:
        row["session_profit"] = round(float(profit), 2)
    else:
        row.pop("session_profit", None)
    return row


def attach_session_docs(
    table_row: dict[str, Any],
    docs: Optional[dict[str, Any]],
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Sitting badge: written/missing, hidden by the console after 50 hands."""
    row = dict(table_row)
    if not enabled:
        row.pop("docs_status", None)
        row.pop("docs_hands", None)
        return row
    if not docs:
        row["docs_status"] = "missing"
        row["docs_hands"] = 0
        return row
    status = str(docs.get("status") or "")
    if status:
        row["docs_status"] = status
    else:
        row.pop("docs_status", None)
    try:
        row["docs_hands"] = int(docs.get("hands") or 0)
    except (TypeError, ValueError):
        row["docs_hands"] = 0
    return row


def default_credential_path() -> Path:
    raw = os.getenv("POKEREYE_GOOGLE_SHEETS_JSON", "").strip()
    if raw:
        return Path(raw)
    root = Path(os.getenv("POKEREYE_ROOT", "")).expanduser() if os.getenv("POKEREYE_ROOT") else None
    candidates = []
    if root:
        candidates.append(root / "secrets" / "google-sheets.json")
    here = Path(__file__).resolve().parents[2]
    candidates.append(here / "secrets" / "google-sheets.json")
    candidates.append(Path("/opt/pokereye/secrets/google-sheets.json"))
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


class SheetsTransport(Protocol):
    def get_history(self) -> list[list[Any]]:
        ...

    def write_row(self, row_number: int, values: list[Any]) -> int:
        ...

    def append_row(self, values: list[Any]) -> int:
        ...


class FakeSheetsTransport:
    """In-memory History grid. Gating tests never touch Google."""

    def __init__(self, rows: Optional[list[list[Any]]] = None) -> None:
        header = list(HISTORY_HEADERS)
        self.rows: list[list[Any]] = [header]
        if rows:
            for row in rows:
                if list(row[:3]) == list(HISTORY_HEADERS[:3]):
                    self.rows = [list(row) + [""] * (COLUMNS - len(row))]
                    continue
                padded = list(row) + [""] * (COLUMNS - len(row))
                self.rows.append(padded[:COLUMNS])
        self.ops: list[tuple[str, int, list[Any]]] = []

    def get_history(self) -> list[list[Any]]:
        return [list(row) for row in self.rows]

    def write_row(self, row_number: int, values: list[Any]) -> int:
        row_number = int(row_number)
        if row_number < 2:
            raise ValueError("History data row must be below the header")
        padded = list(values) + [""] * (COLUMNS - len(values))
        padded = padded[:COLUMNS]
        while len(self.rows) < row_number:
            self.rows.append([""] * COLUMNS)
        self.rows[row_number - 1] = padded
        self.ops.append(("write", row_number, list(padded)))
        return row_number

    def append_row(self, values: list[Any]) -> int:
        row_number = last_filled_row_number(self.rows) + 1
        return self.write_row(row_number, values)


def sheets_is_live(transport: SheetsTransport) -> bool:
    return type(transport).__name__ != "DrySheetsTransport"


class DrySheetsTransport:
    """No network. Same A:N cells as a live write, recorded locally."""

    def __init__(self, rows: Optional[list[list[Any]]] = None, *, reason: str = "dry") -> None:
        self._fake = FakeSheetsTransport(rows)
        self.reason = str(reason or "dry")

    @property
    def ops(self) -> list[tuple[str, int, list[Any]]]:
        return self._fake.ops

    @property
    def rows(self) -> list[list[Any]]:
        return self._fake.rows

    def get_history(self) -> list[list[Any]]:
        return self._fake.get_history()

    def write_row(self, row_number: int, values: list[Any]) -> int:
        return self._fake.write_row(row_number, values)

    def append_row(self, values: list[Any]) -> int:
        return self._fake.append_row(values)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class GoogleSheetsTransport:
    def __init__(
        self,
        credential_file: str | Path,
        *,
        spreadsheet_id: str = SPREADSHEET_ID,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.credential_file = Path(credential_file)
        self.spreadsheet_id = str(spreadsheet_id)
        self.timeout_seconds = float(timeout_seconds)
        self._token: Optional[tuple[str, float]] = None
        raw = json.loads(self.credential_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("type") != "service_account":
            raise ValueError("Google credential is not a service account")
        self._email = str(raw.get("client_email") or "").strip()
        self._key = str(raw.get("private_key") or "").replace("\\n", "\n").strip()
        self._token_uri = str(
            raw.get("token_uri") or "https://oauth2.googleapis.com/token"
        ).strip()
        if not self._email or "BEGIN PRIVATE KEY" not in self._key:
            raise ValueError("Google credential is missing service-account fields")

    def _assertion(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        issued = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64url(
            json.dumps(
                {
                    "iss": self._email,
                    "scope": SHEETS_SCOPE,
                    "aud": self._token_uri,
                    "iat": issued,
                    "exp": issued + 3600,
                },
                separators=(",", ":"),
            ).encode()
        )
        unsigned = f"{header}.{claims}".encode("ascii")
        key = serialization.load_pem_private_key(self._key.encode("utf-8"), password=None)
        signature = key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64url(signature)}"

    def _access_token(self) -> str:
        now = time.time()
        cached = self._token
        if cached is not None and cached[1] - now >= 60:
            return cached[0]
        body = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(),
            }
        ).encode()
        req = urllib.request.Request(
            self._token_uri,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        token = str(payload["access_token"])
        expires = now + max(60, int(payload.get("expires_in", 3600)))
        self._token = (token, expires)
        return token

    def _request(self, method: str, url: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        data = None
        headers = {
            "Authorization": "Bearer " + self._access_token(),
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Google Sheets HTTP {exc.code}: {detail}") from exc
        return json.loads(raw) if raw else {}

    def get_history(self) -> list[list[Any]]:
        quoted = urllib.parse.quote(HISTORY_RANGE, safe="")
        url = (
            f"{SHEETS_API}/{self.spreadsheet_id}/values/{quoted}"
            "?majorDimension=ROWS&valueRenderOption=UNFORMATTED_VALUE"
        )
        payload = self._request("GET", url)
        values = payload.get("values") or []
        rows = []
        for row in values:
            padded = list(row) + [""] * (COLUMNS - len(row))
            rows.append(padded[:COLUMNS])
        if not rows:
            rows = [list(HISTORY_HEADERS)]
        return rows

    def write_row(self, row_number: int, values: list[Any]) -> int:
        row_number = int(row_number)
        target = f"{HISTORY_SHEET}!A{row_number}:N{row_number}"
        quoted = urllib.parse.quote(target, safe="")
        url = (
            f"{SHEETS_API}/{self.spreadsheet_id}/values/{quoted}"
            "?valueInputOption=USER_ENTERED"
        )
        padded = list(values) + [""] * (COLUMNS - len(values))
        self._request("PUT", url, {"values": [padded[:COLUMNS]]})
        return row_number

    def append_row(self, values: list[Any]) -> int:
        quoted = urllib.parse.quote(HISTORY_RANGE, safe="")
        url = (
            f"{SHEETS_API}/{self.spreadsheet_id}/values/{quoted}:append"
            "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
        )
        padded = list(values) + [""] * (COLUMNS - len(values))
        payload = self._request("POST", url, {"values": [padded[:COLUMNS]]})
        updated = str(payload.get("updates", {}).get("updatedRange") or "")
        # History!A405:N405
        marker = "!A"
        if marker in updated:
            tail = updated.split(marker, 1)[1]
            digits = ""
            for char in tail:
                if char.isdigit():
                    digits += char
                else:
                    break
            if digits:
                return int(digits)
        return last_filled_row_number(self.get_history())


def open_transport(credential_file: Optional[Path] = None) -> SheetsTransport:
    path = Path(credential_file) if credential_file is not None else default_credential_path()
    if path.is_file():
        try:
            return GoogleSheetsTransport(path)
        except Exception as exc:
            return DrySheetsTransport(reason=f"credential_init:{type(exc).__name__}: {exc}")
    return DrySheetsTransport(reason="missing_credential")


def table_token(device_id: str, table_id: int) -> str:
    return f"{device_id}:{int(table_id)}"


@dataclass
class OpenSession:
    nickname: str
    game_type: str
    limit: str
    started_at: datetime
    balance_st: float = 0.0
    profit: float = 0.0
    hands: int = 0
    hands_s: int = 0
    row_number: Optional[int] = None
    tables: set[str] = field(default_factory=set)
    table_stacks: dict[str, float] = field(default_factory=dict)
    table_profit: dict[str, float] = field(default_factory=dict)
    last_table_profit: dict[str, float] = field(default_factory=dict)
    closed: bool = False
    balance_end: Optional[float] = None
    write_error: str = ""
    sheet_confirmed: bool = False
    rakeback_written: bool = False
    st_from_lobby: bool = False
    pending_close: bool = False
    pending_device: str = ""

    @property
    def key(self) -> str:
        return session_key(self.nickname, self.game_type, self.limit)


class HistoryLedger:
    def __init__(
        self,
        transport: SheetsTransport,
        *,
        now_factory: Callable[[], datetime] = novosibirsk_now,
        persist_path: Optional[Path] = None,
    ) -> None:
        self.transport = transport
        self.now_factory = now_factory
        self.persist_path = Path(persist_path) if persist_path else None
        self.wallet_lookup: Optional[Callable[[str], Any]] = None
        self.lobby_wallet_lookup: Optional[Callable[[str], Any]] = None
        self.event_sink: Optional[Callable[[str, str, dict[str, Any]], None]] = None
        self._lock = threading.Lock()
        self._enabled: dict[str, bool] = {}
        self._sessions: dict[str, OpenSession] = {}
        self._table_key: dict[str, str] = {}
        self._nick_state: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        self._announced_transport = False
        self._load_persist()

    @classmethod
    def open_default(cls) -> "HistoryLedger":
        root = Path(os.getenv("POKEREYE_ROOT") or "/opt/pokereye")
        persist = root / "data" / "history_ledger.json"
        try:
            persist.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            persist = None
        return cls(open_transport(), persist_path=persist)

    def set_device_enabled(self, device_id: str, enabled: bool) -> None:
        with self._lock:
            self._enabled[str(device_id)] = bool(enabled)

    def device_enabled(self, device_id: str) -> bool:
        return bool(self._enabled.get(str(device_id)))

    def table_profit(self, device_id: str, table_id: int) -> Optional[float]:
        if not self.device_enabled(device_id):
            return None
        token = table_token(device_id, table_id)
        with self._lock:
            key = self._table_key.get(token)
            session = self._sessions.get(key or "")
            if session is None or session.closed:
                return None
            if token not in session.tables:
                return None
            return round(float(session.table_profit.get(token, 0.0)), 2)

    def session_docs(self, device_id: str, table_id: int) -> Optional[dict[str, Any]]:
        if not self.device_enabled(device_id):
            return None
        token = table_token(device_id, table_id)
        with self._lock:
            key = self._table_key.get(token)
            session = self._sessions.get(key or "")
            if session is None or session.closed or token not in session.tables:
                return None
            live = sheets_is_live(self.transport)
            if live and session.sheet_confirmed and session.row_number is not None and not session.write_error:
                status = "written"
            elif not live:
                status = "dry"
            elif session.write_error or self.errors:
                status = "error"
            else:
                status = "pending"
            return {
                "status": status,
                "hands": int(session.hands),
                "row_number": session.row_number,
            }

    def on_table_sat(
        self,
        *,
        device_id: str,
        table_id: int,
        nickname: str,
        game_type: Any,
        coin_bb: Any,
        stack: Any = None,
        session_hands: Any = None,
        wallet_cash: Any = None,
    ) -> Optional[OpenSession]:
        """Open the unclosed History row as soon as hero sits, not on first hand."""
        return self.on_hand_started(
            device_id=device_id,
            table_id=table_id,
            nickname=nickname,
            game_type=game_type,
            coin_bb=coin_bb,
            stack=stack,
            session_hands=session_hands,
            wallet_cash=wallet_cash,
        )

    def observe(self, observation: Any) -> None:
        kind = str(getattr(observation, "kind", "") or "")
        device_id = str(getattr(observation, "device_id", "") or "")
        table_id = int(getattr(observation, "table_id", 0) or 0)
        detail = dict(getattr(observation, "detail", None) or {})
        tag = str(detail.get("tag") or "")
        game_type = str(getattr(observation, "game_type", None) or detail.get("game_type") or "")
        coin_bb = (
            getattr(observation, "coin_bb", None)
            if getattr(observation, "coin_bb", None) is not None
            else detail.get("coin_bb")
        )
        sat = kind in {"table_sat", "seated"} or (kind == "bridge_diag" and tag == "seated")
        if sat and table_id > 0:
            self.on_table_sat(
                device_id=device_id,
                table_id=table_id,
                nickname=str(detail.get("nickname") or ""),
                game_type=game_type,
                coin_bb=coin_bb,
                stack=detail.get("stack"),
                session_hands=detail.get("session_hands"),
                wallet_cash=detail.get("wallet_cash") if detail.get("wallet_cash") is not None else detail.get("lobby_wallet"),
            )
        elif kind == "hand_started" and table_id > 0:
            self.on_hand_started(
                device_id=device_id,
                table_id=table_id,
                nickname=str(detail.get("nickname") or ""),
                game_type=game_type,
                coin_bb=coin_bb,
                stack=detail.get("stack"),
                session_hands=detail.get("session_hands"),
                wallet_cash=detail.get("wallet_cash"),
            )
        elif kind == "hand_completed" and table_id > 0:
            self.on_hand_completed(
                device_id=device_id,
                table_id=table_id,
                table_profit=detail.get("table_profit"),
            )
        elif kind == "table_close" and table_id > 0:
            self.on_table_left(
                device_id=device_id,
                table_id=table_id,
                wallet_cash=detail.get("wallet_cash") if detail.get("wallet_cash") is not None else detail.get("lobby_wallet"),
            )
        elif kind in {"automation.lobby_wallet", "automation.live_wallet"}:
            cash = detail.get("wallet_cash")
            if cash is None:
                cash = detail.get("lobby_wallet")
            self.apply_lobby_cash(device_id, cash)

    def on_hand_started(
        self,
        *,
        device_id: str,
        table_id: int,
        nickname: str,
        game_type: Any,
        coin_bb: Any,
        stack: Any = None,
        session_hands: Any = None,
        wallet_cash: Any = None,
    ) -> Optional[OpenSession]:
        if not self.device_enabled(device_id):
            return None
        nick = str(nickname or "").strip()
        limit = ledger_limit(coin_bb)
        kind = ledger_type(game_type)
        if not nick or not limit:
            return None
        token = table_token(device_id, table_id)
        try:
            stack_cash = float(stack or 0.0)
        except (TypeError, ValueError):
            stack_cash = 0.0
        if stack_cash != stack_cash or stack_cash in {float("inf"), float("-inf")}:
            stack_cash = 0.0
        try:
            seed_hands = int(session_hands or 0)
        except (TypeError, ValueError):
            seed_hands = 0
        if wallet_cash is None and callable(self.lobby_wallet_lookup):
            try:
                wallet_cash = self.lobby_wallet_lookup(device_id)
            except Exception:
                wallet_cash = None
        with self._lock:
            key = session_key(nick, kind, limit)
            session = self._sessions.get(key)
            if session is not None and session.closed:
                session = None
            if session is not None and not session.sheet_confirmed:
                rows = self._read_history()
                row_number, open_row = find_last_matching_row(
                    rows, nickname=nick, game_type=kind, limit=limit
                )
                if open_row and row_number is not None:
                    session.row_number = row_number
                    session.sheet_confirmed = True
                else:
                    session.closed = True
                    session = None
            if session is None:
                rows = self._read_history()
                row_number, open_row = find_last_matching_row(
                    rows, nickname=nick, game_type=kind, limit=limit
                )
                started = self.now_factory()
                cached = self._nick_state.get(" ".join(nick.split()).casefold()) or {}
                if row_number is not None and open_row:
                    existing = rows[row_number - 1]
                    prior_s = _int_cell(existing[11])
                    hands_s = int(prior_s) if prior_s is not None else 0
                    started = datetime_from_history_row(existing, started)
                    sheet_balance = _cash_parse(existing[6])
                    session = OpenSession(
                        nickname=nick,
                        game_type=kind,
                        limit=limit,
                        started_at=started,
                        balance_st=float(sheet_balance) if sheet_balance and sheet_balance > 0 else 0.0,
                        hands_s=hands_s,
                        row_number=row_number,
                        sheet_confirmed=True,
                    )
                else:
                    hands_s = 0
                    if row_number is not None:
                        prior_e = _int_cell(rows[row_number - 1][12])
                        if prior_e is None:
                            prior_e = _int_cell(rows[row_number - 1][11])
                        if prior_e is not None:
                            hands_s = prior_e
                    if hands_s <= 0:
                        cached_hands = last_game_hands_e(rows, nick)
                        if cached_hands is not None:
                            hands_s = cached_hands
                    if hands_s <= 0:
                        try:
                            hands_s = int(cached.get("hands_e") or 0)
                        except (TypeError, ValueError):
                            hands_s = 0
                    if hands_s <= 0 and seed_hands > 0:
                        hands_s = seed_hands
                    seeded = seed_balance_st(rows, nick, wallet_cash=wallet_cash)
                    source = seed_source(rows, nick, wallet_cash=wallet_cash)
                    if seeded is None:
                        seeded = _usable_wallet(cached.get("balance_end"))
                    if seeded is None:
                        seeded = 0.0
                    wrote_rb = self._write_rakeback_locked(rows, nick, seeded, started)
                    session = OpenSession(
                        nickname=nick,
                        game_type=kind,
                        limit=limit,
                        started_at=started,
                        balance_st=float(seeded),
                        hands_s=hands_s,
                        row_number=None,
                        rakeback_written=bool(wrote_rb),
                        st_from_lobby=source == "lobby",
                    )
                self._sessions[key] = session
                session.tables.add(token)
                session.table_stacks[token] = stack_cash
                session.pending_device = ""
                session.pending_close = False
                self._table_key[token] = key
                self._write_open_locked(session)
                return session
            if token not in session.tables:
                session.tables.add(token)
                session.table_stacks[token] = stack_cash
                session.pending_device = ""
                session.pending_close = False
                self._table_key[token] = key
            self._apply_lobby_start_locked(session, wallet_cash)
            return session

    def on_hand_completed(
        self,
        *,
        device_id: str,
        table_id: int,
        table_profit: Any = None,
        hero_profit: Any = None,
    ) -> Optional[OpenSession]:
        if not self.device_enabled(device_id):
            return None
        token = table_token(device_id, table_id)
        with self._lock:
            key = self._table_key.get(token)
            session = self._sessions.get(key or "")
            if session is None or session.closed or token not in session.tables:
                return None
            try:
                cumulative = float(table_profit) if table_profit is not None else None
            except (TypeError, ValueError):
                cumulative = None
            if cumulative is None:
                try:
                    delta = float(hero_profit or 0.0)
                except (TypeError, ValueError):
                    delta = 0.0
            else:
                previous = float(session.last_table_profit.get(token, 0.0))
                delta = cumulative - previous
                session.last_table_profit[token] = cumulative
                session.table_profit[token] = cumulative
            if delta != delta or delta in {float("inf"), float("-inf")}:
                delta = 0.0
            if cumulative is None:
                session.table_profit[token] = round(
                    float(session.table_profit.get(token, 0.0)) + delta, 4
                )
            session.profit = round(float(session.profit) + delta, 4)
            session.hands += 1
            if session.row_number is None or session.write_error:
                self._write_open_locked(session)
            else:
                self._persist_locked()
            return session

    def on_table_left(
        self,
        *,
        device_id: str,
        table_id: int,
        wallet_cash: Any = None,
    ) -> Optional[OpenSession]:
        token = table_token(device_id, table_id)
        with self._lock:
            key = self._table_key.pop(token, None)
            if not key:
                for candidate, item in self._sessions.items():
                    if token in item.tables:
                        key = candidate
                        break
            session = self._sessions.get(key or "")
            if session is None or session.closed:
                return None
            session.tables.discard(token)
            if session.tables:
                self._persist_locked()
                return session
            if not self.device_enabled(device_id):
                return session
            if not session_ready_to_close(session.hands):
                session.pending_device = str(device_id)
                session.pending_close = False
                self._persist_locked()
                return session
            live = _usable_wallet(wallet_cash)
            ended, profit = close_balance_end(
                balance_st=session.balance_st,
                hand_profit=session.profit,
                live_lobby=live,
                reconstructed_wallet=None,
            )
            session.profit = float(profit)
            if ended is not None:
                session.balance_end = float(ended)
            elif session.st_from_lobby and session.balance_st:
                ended2, profit2 = close_balance_end(
                    balance_st=session.balance_st,
                    hand_profit=session.profit,
                    live_lobby=None,
                    reconstructed_wallet=None,
                )
                # St was lobby fact: St+hands is honest when Coin has not
                # sent a post-leave lobby packet yet.
                if ended2 is not None:
                    session.balance_end = float(ended2)
                    session.profit = float(profit2)
                else:
                    session.balance_end = None
            else:
                session.balance_end = None
            self._write_close_locked(session)
            return session

    def apply_lobby_cash(self, device_id: str, wallet_cash: Any) -> None:
        """Late lobby traffic rewrites St, or finishes a pending close."""
        cash = _usable_wallet(wallet_cash)
        if cash is None or not self.device_enabled(device_id):
            return
        prefix = f"{device_id}:"
        with self._lock:
            for session in list(self._sessions.values()):
                if session.closed:
                    continue
                owned = any(token.startswith(prefix) for token in session.tables)
                pending = session.pending_close and session.pending_device == str(device_id)
                if not owned and not pending:
                    continue
                if pending or not session.tables:
                    if not session_ready_to_close(session.hands):
                        session.pending_device = str(device_id)
                        session.pending_close = False
                        continue
                    ended, profit = close_balance_end(
                        balance_st=session.balance_st,
                        hand_profit=session.profit,
                        live_lobby=cash,
                    )
                    if ended is None:
                        continue
                    session.balance_end = float(ended)
                    session.profit = float(profit)
                    session.pending_close = False
                    self._write_close_locked(session)
                    continue
                if not session.st_from_lobby:
                    self._apply_lobby_start_locked(session, cash)
                    session.st_from_lobby = True
                else:
                    self._apply_lobby_start_locked(session, cash)

    def flush_device(self, device_id: str) -> list[OpenSession]:
        prefix = f"{device_id}:"
        closed: list[OpenSession] = []
        with self._lock:
            if not self.device_enabled(device_id):
                return closed
            for session in list(self._sessions.values()):
                if session.closed:
                    continue
                owned = [token for token in list(session.tables) if token.startswith(prefix)]
                parked = (not session.tables and session.pending_device == str(device_id))
                if not owned and not parked:
                    continue
                for token in owned:
                    session.tables.discard(token)
                    self._table_key.pop(token, None)
                if not session.tables:
                    self._write_close_locked(session)
                    closed.append(session)
        return closed

    def _read_history(self) -> list[list[Any]]:
        try:
            return self.transport.get_history()
        except Exception as exc:
            self._record_error_locked(f"{type(exc).__name__}: {exc}")
            return [list(HISTORY_HEADERS)]

    def _announce_transport_locked(self) -> None:
        if self._announced_transport:
            return
        self._announced_transport = True
        if sheets_is_live(self.transport):
            self._note("ledger_transport", "учет: Google Sheets подключён", status="live")
            return
        reason = str(getattr(self.transport, "reason", "") or "dry")
        self._note(
            "ledger_transport",
            f"учет: Google Sheets недоступен · {reason}",
            status="dry",
            reason=reason,
        )

    def _note(self, code: str, text: str, **fields: Any) -> None:
        sink = self.event_sink
        if sink is None:
            return
        try:
            sink(str(code), str(text), dict(fields))
        except Exception:
            pass

    def _record_error_locked(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self.errors.append(text)
        self._note("ledger_error", f"учет: {text}", error=text)

    def _write_rakeback_locked(
        self,
        rows: list[list[Any]],
        nickname: str,
        seeded: float,
        started: datetime,
    ) -> bool:
        gap = rakeback_delta(rows, nickname, seeded) if seeded else None
        prior = last_rakeback_baseline(rows, nickname)
        prior_end = _cash_parse(prior[7]) if prior is not None else None
        if gap is None and prior is None:
            cached = self._nick_state.get(" ".join(nickname.split()).casefold()) or {}
            prior_end = _usable_wallet(cached.get("balance_end"))
            nxt = _usable_wallet(seeded)
            if prior_end is not None and nxt is not None:
                delta = round(float(nxt) - float(prior_end), 2)
                if delta > 0:
                    gap = delta
        if gap is None:
            return False
        if prior_end is None:
            prior_end = round(float(seeded) - float(gap), 2)
        try:
            self.transport.append_row(
                assemble_rakeback_row(
                    nickname=nickname,
                    started_at=started,
                    balance_st=float(prior_end),
                    profit=float(gap),
                )
            )
            return True
        except Exception as exc:
            self._record_error_locked(f"{type(exc).__name__}: {exc}")
            return False

    def _apply_lobby_start_locked(self, session: OpenSession, wallet_cash: Any) -> None:
        """Room-load / late lobby cash becomes St and can still write rakeback."""
        cash = _usable_wallet(wallet_cash)
        if cash is None:
            return
        if not session.rakeback_written:
            rows = self._read_history()
            session.rakeback_written = bool(
                self._write_rakeback_locked(rows, session.nickname, cash, session.started_at)
            )
        if abs(float(session.balance_st or 0.0) - float(cash)) < 0.005:
            return
        session.balance_st = float(cash)
        self._write_open_locked(session)

    def _confirm_sheet_row_locked(self, session: OpenSession) -> bool:
        if session.row_number is None:
            return False
        rows = self._read_history()
        idx = int(session.row_number) - 1
        if idx < 1 or idx >= len(rows):
            return False
        row = rows[idx]
        if not row_matches(
            row,
            nickname=session.nickname,
            game_type=session.game_type,
            limit=session.limit,
        ):
            return False
        return is_open_history_row(row)

    def _write_open_locked(self, session: OpenSession) -> None:
        self._announce_transport_locked()
        values = assemble_open_row(
            nickname=session.nickname,
            game_type=session.game_type,
            limit=session.limit,
            started_at=session.started_at,
            balance_st=session.balance_st,
            hands_s=session.hands_s,
        )
        had_row = session.row_number is not None
        try:
            if session.row_number is None:
                session.row_number = int(self.transport.append_row(values))
            else:
                rows = self._read_history()
                idx = session.row_number - 1
                if 0 <= idx < len(rows) and not is_open_history_row(rows[idx]):
                    session.row_number = int(self.transport.append_row(values))
                else:
                    self.transport.write_row(int(session.row_number), values)
            session.write_error = ""
            session.sheet_confirmed = self._confirm_sheet_row_locked(session)
            if sheets_is_live(self.transport) and session.sheet_confirmed and not had_row:
                self._note(
                    "ledger_written",
                    f"учет: записали незакрытую строку {session.nickname} {session.limit}",
                    nickname=session.nickname,
                    limit=session.limit,
                    row=session.row_number,
                )
            elif sheets_is_live(self.transport) and not session.sheet_confirmed:
                session.write_error = "History row missing after write"
                self._record_error_locked(session.write_error)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if session.write_error != message:
                session.write_error = message
                self._record_error_locked(message)
            else:
                session.write_error = message
        self._persist_locked()

    def _write_close_locked(self, session: OpenSession) -> None:
        if session.closed:
            return
        self._announce_transport_locked()
        values = assemble_close_row(
            nickname=session.nickname,
            game_type=session.game_type,
            limit=session.limit,
            started_at=session.started_at,
            ended_at=self.now_factory(),
            balance_st=session.balance_st,
            profit=session.profit,
            hands_s=session.hands_s,
            hands=session.hands,
            balance_end=session.balance_end,
            fill_end_from_profit=False,
        )
        try:
            if session.row_number is None:
                session.row_number = int(self.transport.append_row(values))
            else:
                rows = self._read_history()
                idx = session.row_number - 1
                if 0 <= idx < len(rows) and not is_open_history_row(rows[idx]):
                    session.row_number = int(self.transport.append_row(values))
                else:
                    self.transport.write_row(int(session.row_number), values)
            session.closed = True
            session.write_error = ""
            self._nick_state[" ".join(session.nickname.split()).casefold()] = {
                "balance_end": session.balance_end,
                "hands_e": int(session.hands_s) + int(session.hands),
                "nickname": session.nickname,
            }
            self._note(
                "ledger_closed",
                f"учет: закрыли строку {session.nickname} {session.limit}",
                nickname=session.nickname,
                limit=session.limit,
                row=session.row_number,
                profit=session.profit,
            )
        except Exception as exc:
            session.write_error = f"{type(exc).__name__}: {exc}"
            self._record_error_locked(session.write_error)
        self._persist_locked()

    def _persist_locked(self) -> None:
        if self.persist_path is None:
            return
        rows = []
        for session in self._sessions.values():
            if session.closed:
                continue
            rows.append(
                {
                    "nickname": session.nickname,
                    "game_type": session.game_type,
                    "limit": session.limit,
                    "started_at": session.started_at.isoformat(),
                    "balance_st": session.balance_st,
                    "balance_end": session.balance_end,
                    "profit": session.profit,
                    "hands": session.hands,
                    "hands_s": session.hands_s,
                    "row_number": session.row_number,
                    "tables": sorted(session.tables),
                    "table_profit": dict(session.table_profit),
                    "last_table_profit": dict(session.last_table_profit),
                    "table_stacks": dict(session.table_stacks),
                    "write_error": session.write_error,
                    "sheet_confirmed": bool(session.sheet_confirmed),
                    "rakeback_written": bool(session.rakeback_written),
                    "pending_device": session.pending_device,
                    "pending_close": bool(session.pending_close),
                }
            )
        payload = {
            "sessions": rows,
            "table_key": dict(self._table_key),
            "nick_state": dict(self._nick_state),
        }
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.persist_path)
        except OSError as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def _load_persist(self) -> None:
        path = self.persist_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for row in raw.get("sessions") or []:
            if not isinstance(row, dict):
                continue
            nick = str(row.get("nickname") or "").strip()
            kind = ledger_type(row.get("game_type") or "")
            limit = str(row.get("limit") or "")
            if not nick or not limit:
                continue
            started = self.now_factory()
            try:
                started = datetime.fromisoformat(str(row.get("started_at") or ""))
            except ValueError:
                pass
            session = OpenSession(
                nickname=nick,
                game_type=kind,
                limit=limit,
                started_at=started,
                balance_st=float(row.get("balance_st") or 0.0),
                profit=float(row.get("profit") or 0.0),
                hands=int(row.get("hands") or 0),
                hands_s=int(row.get("hands_s") or 0),
                row_number=int(row["row_number"]) if row.get("row_number") else None,
                write_error=str(row.get("write_error") or ""),
                sheet_confirmed=False,
                rakeback_written=bool(row.get("rakeback_written")),
                pending_device=str(row.get("pending_device") or ""),
                pending_close=bool(row.get("pending_close")),
            )
            if row.get("balance_end") is not None:
                try:
                    session.balance_end = float(row.get("balance_end"))
                except (TypeError, ValueError):
                    pass
            for token in row.get("tables") or []:
                tok = str(token)
                session.tables.add(tok)
                self._table_key[tok] = session.key
            for key, value in dict(row.get("table_profit") or {}).items():
                try:
                    session.table_profit[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            for key, value in dict(row.get("last_table_profit") or {}).items():
                try:
                    session.last_table_profit[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            for key, value in dict(row.get("table_stacks") or {}).items():
                try:
                    session.table_stacks[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            self._sessions[session.key] = session
        for token, key in dict(raw.get("table_key") or {}).items():
            self._table_key[str(token)] = str(key)
        for nick, value in dict(raw.get("nick_state") or {}).items():
            if isinstance(value, dict) and str(nick).strip():
                self._nick_state[str(nick).casefold()] = dict(value)
