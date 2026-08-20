from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

from .models import AccountLease


REGISTRY_SCHEMA = 1
DEFAULT_PPP_SUFFIXES = (9, 10, 11, 12, 17, 18, 19)


def parse_suffixes(value: str | Iterable[int]) -> tuple[int, ...]:
    """Parse ``9-12,17,18-19`` into a stable positive allowlist."""

    if not isinstance(value, str):
        answer = tuple(dict.fromkeys(int(item) for item in value))
    else:
        rows: list[int] = []
        for token in re.split(r"[,;\s]+", value.strip()):
            if not token:
                continue
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
            if not match:
                raise ValueError(f"invalid account suffix/range: {token!r}")
            first = int(match.group(1))
            last = int(match.group(2) or first)
            if last < first:
                raise ValueError(f"descending account suffix range: {token!r}")
            rows.extend(range(first, last + 1))
        answer = tuple(dict.fromkeys(rows))
    if any(item < 1 for item in answer):
        raise ValueError("account suffixes must be positive")
    return answer


class AccountPoolExhausted(RuntimeError):
    pass


class AccountProbeDeferred(AccountPoolExhausted):
    """All validation slots are busy; this is not a finite-pool NO_SLOT."""

    def __init__(self, retry_after: float = 0.1):
        self.retry_after = max(0.01, float(retry_after))
        super().__init__(
            f"backend account validation capacity is busy; retry in {self.retry_after:.2f}s"
        )


class AccountState(str, Enum):
    AVAILABLE = "AVAILABLE"
    LEASED = "LEASED"
    PROBING = "PROBING"
    INVALID = "INVALID"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class AccountStatus:
    account_id: str
    state: AccountState
    owner: str = ""
    validated: bool = False
    suffix: Optional[int] = None
    attempts: int = 0
    retry_in: float = 0.0
    last_error: str = ""


@dataclass
class _AccountRecord:
    account_id: str
    state: AccountState
    validated: bool
    suffix: Optional[int]
    owner: str = ""
    token: str = ""
    attempts: int = 0
    retry_at: float = 0.0
    last_error: str = ""


class AccountPool:
    """Deterministic leases over a verified, growable backend-account registry.

    Normal dynamic mode grows only through explicit PPPoker candidates. Production
    v2 can additionally enable monotonic ``base-N`` growth: verified/free accounts
    are always leased first (lowest suffix wins); only when none are free is the
    next suffix after the highest known suffix probed. Growth is capped by
    ``POKEREYE_ACCOUNT_MAX_SUFFIX`` (150 by default). A candidate is committed
    only after SCLogin succeeds via ``confirm``.
    """

    def __init__(
        self,
        accounts: Iterable[str],
        *,
        clock=time.monotonic,
        wall_clock=time.time,
        dynamic_base: Optional[str] = None,
        probe_candidates: Iterable[str | int] = (),
        max_probe_concurrency: int = 2,
        probe_retry_seconds: float = 2.0,
        invalid_retry_seconds: float = 900.0,
        registry_path: Optional[str | Path] = None,
        profile: str = "PPPoker",
        auto_expand_unbounded: bool = False,
        blocked_accounts: Iterable[str] = (),
    ):
        ordered = tuple(dict.fromkeys(str(x).strip() for x in accounts if str(x).strip()))
        if not ordered:
            raise ValueError("account pool cannot be empty")
        if int(max_probe_concurrency) < 1:
            raise ValueError("max_probe_concurrency must be positive")
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._records: dict[str, _AccountRecord] = {}
        self._by_owner: dict[str, AccountLease] = {}
        self._owner_by_account: dict[str, str] = {}
        self._approved_candidates: dict[str, str] = {}
        self._dynamic_base = str(dynamic_base or "").strip()
        self._profile = str(profile or "").strip()
        self._max_probe_concurrency = int(max_probe_concurrency)
        self._probe_retry_seconds = max(0.0, float(probe_retry_seconds))
        self._invalid_retry_seconds = max(0.0, float(invalid_retry_seconds))
        self._registry_path = Path(registry_path).expanduser() if registry_path else None
        self._auto_expand_unbounded = bool(auto_expand_unbounded)
        self._blocked = {
            str(item).strip()
            for item in blocked_accounts
            if str(item).strip()
        }
        if self._auto_expand_unbounded and not self._dynamic_base:
            raise ValueError("auto_expand_unbounded requires dynamic_base")
        raw_limit = str(os.getenv("POKEREYE_ACCOUNT_MAX_SUFFIX", "150")).strip()
        try:
            self._dynamic_suffix_limit = max(1, int(raw_limit))
        except ValueError as exc:
            raise ValueError(
                f"invalid POKEREYE_ACCOUNT_MAX_SUFFIX={raw_limit!r}"
            ) from exc

        for account_id in ordered:
            if account_id in self._blocked:
                continue
            suffix = self._suffix(account_id)
            self._records[account_id] = _AccountRecord(
                account_id=account_id,
                state=AccountState.AVAILABLE,
                validated=True,
                suffix=suffix,
            )
        if self._registry_path and self._registry_path.is_file():
            self._load_registry_locked()
        for candidate in probe_candidates:
            self._register_candidate_locked(candidate, source="configured")
        self._persist_locked()

    @classmethod
    def numbered(
        cls, base: str = "709393393", first: int = 1, last: int = 19, **kwargs
    ) -> "AccountPool":
        """Historical finite range for legacy EYE/singleton compatibility."""

        if first < 1 or last < first:
            raise ValueError("invalid numbered account range")
        return cls((f"{base}-{index}" for index in range(first, last + 1)), **kwargs)

    @classmethod
    def dynamic_registered(
        cls,
        base: str = "709393393",
        *,
        known_suffixes: Iterable[int] = DEFAULT_PPP_SUFFIXES,
        probe_suffixes: Iterable[int] = (),
        max_probe_concurrency: int = 2,
        probe_retry_seconds: float = 2.0,
        invalid_retry_seconds: float = 900.0,
        registry_path: Optional[str | Path] = None,
        profile: str = "PPPoker",
        clock=time.monotonic,
        wall_clock=time.time,
        auto_expand_unbounded: bool = False,
    ) -> "AccountPool":
        known = tuple(dict.fromkeys(int(value) for value in known_suffixes))
        if not known or any(value < 1 for value in known):
            raise ValueError("known_suffixes must contain positive provisioned suffixes")
        probes = tuple(dict.fromkeys(int(value) for value in probe_suffixes))
        if any(value < 1 for value in probes):
            raise ValueError("probe_suffixes must contain positive provisioned suffixes")
        return cls(
            (f"{base}-{suffix}" for suffix in known),
            clock=clock,
            wall_clock=wall_clock,
            dynamic_base=base,
            probe_candidates=(f"{base}-{suffix}" for suffix in probes if suffix not in known),
            max_probe_concurrency=max_probe_concurrency,
            probe_retry_seconds=probe_retry_seconds,
            invalid_retry_seconds=invalid_retry_seconds,
            registry_path=registry_path,
            profile=profile,
            auto_expand_unbounded=auto_expand_unbounded,
        )

    @classmethod
    def dynamic_numbered(
        cls,
        base: str = "709393393",
        first: int = 1,
        known_last: int = 19,
        **kwargs,
    ) -> "AccountPool":
        """Explicit contiguous allowlist compatibility; it never scans past last."""

        if first < 1 or known_last < first:
            raise ValueError("invalid known account range")
        return cls.dynamic_registered(
            base,
            known_suffixes=range(first, known_last + 1),
            **kwargs,
        )

    @property
    def dynamic(self) -> bool:
        return bool(self._dynamic_base)

    @property
    def auto_expand_unbounded(self) -> bool:
        """Compatibility flag: automatic dynamic suffix growth is enabled."""

        return bool(self._auto_expand_unbounded)

    @property
    def dynamic_suffix_limit(self) -> int:
        """Highest suffix that automatic growth may create/probe (default: 150)."""

        return int(self._dynamic_suffix_limit)

    def _suffix(self, account_id: str) -> Optional[int]:
        if self._dynamic_base:
            match = re.fullmatch(re.escape(self._dynamic_base) + r"-(\d+)", account_id)
        else:
            match = re.search(r"-(\d+)$", account_id)
        return int(match.group(1)) if match else None

    def _candidate_id(self, value: str | int) -> str:
        if isinstance(value, int) or str(value).strip().isdigit():
            if not self._dynamic_base:
                raise ValueError("numeric candidate requires dynamic_base")
            account_id = f"{self._dynamic_base}-{int(value)}"
        else:
            account_id = str(value).strip()
        suffix = self._suffix(account_id)
        if not account_id or (self._dynamic_base and suffix is None):
            raise ValueError(f"candidate does not belong to {self._dynamic_base!r}: {value!r}")
        return account_id

    @staticmethod
    def _sort_key(record: _AccountRecord) -> tuple[int, object]:
        return (0, record.suffix) if record.suffix is not None else (1, record.account_id)

    def _candidate_sort_key(self, account_id: str) -> tuple[int, object]:
        suffix = self._suffix(account_id)
        return (0, suffix) if suffix is not None else (1, account_id)

    def _register_candidate_locked(self, value: str | int, *, source: str) -> str:
        account_id = self._candidate_id(value)
        if account_id in self._blocked:
            return account_id
        if account_id not in self._records:
            self._approved_candidates.setdefault(account_id, str(source or "explicit"))
        return account_id

    def register_candidate(
        self,
        value: str | int,
        *,
        profile: str = "PPPoker",
        source: str = "admin",
    ) -> str:
        """Add one externally provisioned full ID; no login success is assumed."""

        if str(profile).strip().lower() != self._profile.lower():
            raise ValueError(
                f"refusing {profile!r} candidate in {self._profile!r} backend pool"
            )
        with self._lock:
            account_id = self._register_candidate_locked(value, source=source)
            self._persist_locked()
            return account_id

    def register_validated(
        self,
        value: str | int,
        *,
        profile: str = "PPPoker",
        source: str = "imported-login-proof",
    ) -> str:
        """Import a prior login proof as AVAILABLE, without imposing a pool cap."""

        if str(profile).strip().lower() != self._profile.lower():
            raise ValueError(
                f"refusing {profile!r} account in {self._profile!r} backend pool"
            )
        with self._lock:
            account_id = self._candidate_id(value)
            record = self._records.get(account_id)
            if record and record.owner:
                raise RuntimeError(f"cannot replace active account {account_id}")
            self._approved_candidates.pop(account_id, None)
            self._records[account_id] = _AccountRecord(
                account_id=account_id,
                state=AccountState.AVAILABLE,
                validated=True,
                suffix=self._suffix(account_id),
                last_error=f"validated source={str(source)[:96]}",
            )
            self._persist_locked()
            return account_id

    def _lease_locked(self, record: _AccountRecord, owner: str, state: AccountState) -> AccountLease:
        token = uuid.uuid4().hex
        lease = AccountLease(
            owner=owner,
            account_id=record.account_id,
            token=token,
            leased_at=time.time(),
        )
        record.state = state
        record.owner = owner
        record.token = token
        record.retry_at = 0.0
        record.last_error = ""
        record.attempts += 1
        self._by_owner[owner] = lease
        self._owner_by_account[record.account_id] = owner
        self._persist_locked()
        return lease

    def _retry_candidate_locked(self, now: float) -> Optional[_AccountRecord]:
        candidates = [
            record
            for record in self._records.values()
            if not record.validated
            and record.state in {AccountState.INVALID, AccountState.QUARANTINED}
            and record.retry_at <= now
        ]
        return min(candidates, key=self._sort_key) if candidates else None

    def _approved_candidate_locked(self) -> Optional[_AccountRecord]:
        if not self._approved_candidates:
            return None
        account_id = min(self._approved_candidates, key=self._candidate_sort_key)
        self._approved_candidates.pop(account_id, None)
        record = _AccountRecord(
            account_id=account_id,
            state=AccountState.PROBING,
            validated=False,
            suffix=self._suffix(account_id),
        )
        self._records[account_id] = record
        return record

    def _next_unbounded_candidate_locked(self) -> Optional[_AccountRecord]:
        """Reuse a hole between known suffixes, then grow by +1 if AUTOGROW is on."""

        if not self._dynamic_base:
            return None
        now = self._clock()
        busy = {
            record.suffix
            for record in self._records.values()
            if record.suffix is not None
            and record.state in {AccountState.LEASED, AccountState.PROBING}
        }
        known = {
            record.suffix
            for record in self._records.values()
            if record.suffix is not None
        }
        known.update(
            suffix
            for account_id in self._approved_candidates
            if (suffix := self._suffix(account_id)) is not None
        )
        if not known:
            if not self._auto_expand_unbounded:
                return None
            known = {0}
        low, high = min(known), max(known)
        # Prefer an existing cooled-down ID. Inventing missing suffixes inside
        # [low, high] is hole-fill; AUTOGROW=0 must stay on the explicit pool.
        for suffix in range(low, high + 1):
            if suffix in busy:
                continue
            account_id = f"{self._dynamic_base}-{suffix}"
            if account_id in self._blocked:
                continue
            record = self._records.get(account_id)
            if record is None:
                if not self._auto_expand_unbounded:
                    continue
                record = _AccountRecord(
                    account_id=account_id,
                    state=AccountState.PROBING,
                    validated=False,
                    suffix=suffix,
                    last_error="reuse hole between known Eye suffixes",
                )
                self._records[account_id] = record
                return record
            if record.state in {AccountState.INVALID, AccountState.QUARANTINED} and record.retry_at <= now:
                return record
        if not self._auto_expand_unbounded:
            return None
        suffix = high + 1
        if suffix > self._dynamic_suffix_limit:
            return None
        account_id = f"{self._dynamic_base}-{suffix}"
        record = _AccountRecord(
            account_id=account_id,
            state=AccountState.PROBING,
            validated=False,
            suffix=suffix,
            last_error=(
                "auto-generated monotonic candidate "
                f"(limit={self._dynamic_suffix_limit})"
            ),
        )
        self._records[account_id] = record
        return record

    def _expire_quarantine_locked(self) -> None:
        now = self._clock()
        for record in self._records.values():
            if (
                record.state == AccountState.QUARANTINED
                and record.validated
                and record.retry_at <= now
            ):
                record.state = AccountState.AVAILABLE
                record.retry_at = 0.0
                record.last_error = ""

    def acquire(self, owner: str) -> AccountLease:
        owner = str(owner)
        if not owner:
            raise ValueError("lease owner cannot be empty")
        with self._lock:
            self._expire_quarantine_locked()
            existing = self._by_owner.get(owner)
            if existing is not None:
                return existing
            available = [
                record
                for record in self._records.values()
                if record.state == AccountState.AVAILABLE and record.validated
            ]
            if available:
                return self._lease_locked(
                    min(available, key=self._sort_key), owner, AccountState.LEASED
                )
            if not self._dynamic_base:
                raise AccountPoolExhausted(f"no free backend account for {owner}")

            probing = sum(
                record.state == AccountState.PROBING for record in self._records.values()
            )
            if probing >= self._max_probe_concurrency:
                raise AccountProbeDeferred()
            # Consume never-tried provisioned IDs before revisiting a previously
            # rejected one whose cooldown elapsed.  This also prevents a zero
            # retry setting from looping on one gap while good candidates wait.
            record = (
                self._approved_candidate_locked()
                or self._retry_candidate_locked(self._clock())
                or self._next_unbounded_candidate_locked()
            )
            if record is None:
                raise AccountPoolExhausted(
                    "no verified/approved PPPoker backend account; provisioning/import required"
                )
            return self._lease_locked(record, owner, AccountState.PROBING)

    def confirm(self, owner: str, token: Optional[str] = None) -> Optional[AccountLease]:
        """Commit a successful SCLogin; an approved candidate becomes validated."""

        owner = str(owner)
        with self._lock:
            lease = self._by_owner.get(owner)
            if lease is None or (token is not None and lease.token != token):
                return None
            record = self._records[lease.account_id]
            record.validated = True
            record.state = AccountState.LEASED
            record.retry_at = 0.0
            record.last_error = ""
            self._persist_locked()
            return lease

    def invalidate(
        self,
        owner: str,
        token: Optional[str] = None,
        *,
        reason: str = "backend login rejected",
        retry_seconds: Optional[float] = None,
    ) -> Optional[AccountLease]:
        """Reject exactly one candidate; other registered accounts remain routable."""

        owner = str(owner)
        with self._lock:
            lease = self._by_owner.get(owner)
            if lease is None or (token is not None and lease.token != token):
                return None
            self._by_owner.pop(owner, None)
            self._owner_by_account.pop(lease.account_id, None)
            record = self._records[lease.account_id]
            was_validated = bool(record.validated)
            record.owner = ""
            record.token = ""
            record.last_error = str(reason)[:256]
            delay = self._invalid_retry_seconds if retry_seconds is None else max(
                0.0, float(retry_seconds)
            )
            if was_validated:
                # A previously proven account is not permanently destroyed by one
                # SCLogin rejection. Move on immediately and retry it only after
                # cooldown. Permanent invalidation is reserved for never-proven
                # candidates (or an explicit administrative removal).
                record.state = AccountState.QUARANTINED
                record.validated = True
            else:
                record.state = AccountState.INVALID
                record.validated = False
            record.retry_at = self._clock() + delay
            self._persist_locked()
            return lease

    def release(
        self,
        owner: str,
        token: Optional[str] = None,
        *,
        quarantine_seconds: float = 0.0,
        reason: str = "",
    ) -> Optional[AccountLease]:
        owner = str(owner)
        with self._lock:
            lease = self._by_owner.get(owner)
            if lease is None or (token is not None and token != lease.token):
                return None
            self._by_owner.pop(owner, None)
            self._owner_by_account.pop(lease.account_id, None)
            record = self._records[lease.account_id]
            record.owner = ""
            record.token = ""
            record.last_error = str(reason)[:256]
            delay = max(0.0, float(quarantine_seconds))
            if record.state == AccountState.PROBING and delay <= 0:
                delay = self._probe_retry_seconds
            if delay > 0:
                record.state = AccountState.QUARANTINED
                record.retry_at = self._clock() + delay
            elif record.validated:
                record.state = AccountState.AVAILABLE
                record.retry_at = 0.0
            else:
                record.state = AccountState.QUARANTINED
                record.retry_at = self._clock()
            self._persist_locked()
            return lease

    def release_prefix(self, owner_prefix: str) -> tuple[AccountLease, ...]:
        with self._lock:
            owners = [owner for owner in self._by_owner if owner.startswith(owner_prefix)]
        released = [self.release(owner) for owner in owners]
        return tuple(lease for lease in released if lease is not None)

    def lease_for(self, owner: str) -> Optional[AccountLease]:
        with self._lock:
            return self._by_owner.get(str(owner))

    def state_for(self, account_id: str) -> Optional[AccountState]:
        with self._lock:
            self._expire_quarantine_locked()
            record = self._records.get(str(account_id))
            return record.state if record else None

    def snapshot(self) -> tuple[AccountLease, ...]:
        with self._lock:
            return tuple(self._by_owner[owner] for owner in sorted(self._by_owner))

    def candidate_snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._approved_candidates, key=self._candidate_sort_key))

    def state_snapshot(self) -> tuple[AccountStatus, ...]:
        with self._lock:
            self._expire_quarantine_locked()
            now = self._clock()
            return tuple(
                AccountStatus(
                    account_id=record.account_id,
                    state=record.state,
                    owner=record.owner,
                    validated=record.validated,
                    suffix=record.suffix,
                    attempts=record.attempts,
                    retry_in=max(0.0, record.retry_at - now),
                    last_error=record.last_error,
                )
                for record in sorted(self._records.values(), key=self._sort_key)
            )

    def quarantine_snapshot(self) -> dict[str, float]:
        with self._lock:
            self._expire_quarantine_locked()
            now = self._clock()
            return {
                record.account_id: max(0.0, record.retry_at - now)
                for record in self._records.values()
                if record.state == AccountState.QUARANTINED
            }

    @property
    def free_count(self) -> int:
        """Validated immediate slots only; dynamic growth is capped separately."""

        with self._lock:
            self._expire_quarantine_locked()
            return sum(
                record.state == AccountState.AVAILABLE and record.validated
                for record in self._records.values()
            )

    def _persist_locked(self) -> None:
        path = self._registry_path
        if path is None:
            return
        now = self._clock()
        wall = self._wall_clock()
        accounts = []
        for record in sorted(self._records.values(), key=self._sort_key):
            state = record.state
            if state == AccountState.LEASED:
                state = AccountState.AVAILABLE if record.validated else AccountState.QUARANTINED
            elif state == AccountState.PROBING:
                state = AccountState.QUARANTINED
            accounts.append(
                {
                    "account_id": record.account_id,
                    "state": state.value,
                    "validated": bool(record.validated),
                    "attempts": int(record.attempts),
                    "retry_after": wall + max(0.0, record.retry_at - now),
                    "last_error": record.last_error,
                }
            )
        payload = {
            "schema": REGISTRY_SCHEMA,
            "base": self._dynamic_base,
            "profile": self._profile,
            "max_suffix": self._dynamic_suffix_limit if self._dynamic_base else None,
            "accounts": accounts,
            "blocked_accounts": sorted(self._blocked),
            "approved_candidates": [
                {"account_id": account_id, "source": self._approved_candidates[account_id]}
                for account_id in sorted(self._approved_candidates, key=self._candidate_sort_key)
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _load_registry_locked(self) -> None:
        assert self._registry_path is not None
        payload = json.loads(self._registry_path.read_text(encoding="utf-8-sig"))
        if int(payload.get("schema") or 0) != REGISTRY_SCHEMA:
            raise ValueError(f"unsupported account registry schema: {payload.get('schema')!r}")
        stored_base = str(payload.get("base") or "")
        stored_profile = str(payload.get("profile") or "")
        if stored_base != self._dynamic_base or stored_profile.lower() != self._profile.lower():
            raise ValueError(
                f"account registry identity mismatch: {stored_base}/{stored_profile}"
            )
        now = self._clock()
        wall = self._wall_clock()
        for raw_block in payload.get("blocked_accounts") or ():
            blocked_id = str(raw_block or "").strip()
            if blocked_id:
                self._blocked.add(blocked_id)
        for row in payload.get("accounts") or ():
            if not isinstance(row, dict):
                continue
            try:
                account_id = self._candidate_id(str(row.get("account_id") or ""))
                state = AccountState(str(row.get("state") or ""))
            except (TypeError, ValueError):
                continue
            if account_id in self._blocked:
                self._records.pop(account_id, None)
                continue
            validated = bool(row.get("validated"))
            retry_at = now + max(0.0, float(row.get("retry_after") or 0.0) - wall)
            if validated:
                # A validated account can still be cooling down after a transient
                # SCLogin rejection (agent slot full, temporary backend deny).
                # Preserve that quarantine across a service restart instead of
                # silently resetting the whole pool to AVAILABLE.
                if state == AccountState.QUARANTINED and retry_at > now:
                    state = AccountState.QUARANTINED
                else:
                    state = AccountState.AVAILABLE
                    retry_at = 0.0
            elif state not in {AccountState.INVALID, AccountState.QUARANTINED}:
                self._approved_candidates.setdefault(account_id, "interrupted-registry")
                continue
            self._records[account_id] = _AccountRecord(
                account_id=account_id,
                state=state,
                validated=validated,
                suffix=self._suffix(account_id),
                attempts=max(0, int(row.get("attempts") or 0)),
                retry_at=retry_at,
                last_error=str(row.get("last_error") or "")[:256],
            )
        for row in payload.get("approved_candidates") or ():
            if not isinstance(row, dict):
                continue
            try:
                account_id = self._candidate_id(str(row.get("account_id") or ""))
            except ValueError:
                continue
            if account_id not in self._records:
                self._approved_candidates.setdefault(
                    account_id, str(row.get("source") or "registry")[:96]
                )
