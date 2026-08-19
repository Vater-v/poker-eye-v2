"""Partner-panel account provisioning. Secrets never go to logs."""
from __future__ import annotations

import http.cookiejar
import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


DEFAULT_PANEL = "https://mobile.eye-panel.com"
MAX_SUFFIX_DEFAULT = 150


class EyePanelError(RuntimeError):
    def __init__(self, code: str, payload: Any = None):
        self.code = str(code or "PANEL_ERROR")
        self.payload = payload
        super().__init__(self.code)


@dataclass(frozen=True)
class CreatedAccount:
    account_id: str
    acc_id: Optional[int]
    enabled: bool


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"_text": raw.decode("utf-8", "replace")[:240]}


class EyePanelClient:
    """SESSION cookie after partnerAuthKey authenticate; then loginAccountReq."""

    def __init__(
        self,
        partner_auth_key: str,
        *,
        base: str = DEFAULT_PANEL,
        timeout: float = 20.0,
        opener=None,
        max_suffix: int = MAX_SUFFIX_DEFAULT,
    ) -> None:
        key = str(partner_auth_key or "").strip()
        if not key:
            raise ValueError("partner_auth_key is empty")
        self._key = key
        self._base = str(base or DEFAULT_PANEL).rstrip("/")
        self._timeout = float(timeout)
        self._max_suffix = max(1, int(max_suffix))
        if opener is not None:
            self._opener = opener
        else:
            jar = http.cookiejar.CookieJar()
            ctx = ssl.create_default_context()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx),
                urllib.request.HTTPCookieProcessor(jar),
            )
        self._authed = False

    def _request(
        self,
        path: str,
        *,
        method: str = "POST",
        body: Optional[dict[str, Any]] = None,
        android_id: str = "",
    ) -> Any:
        raw = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._base + path, data=raw, method=method)
        req.add_header("User-Agent", "PokerEye-Trainer")
        req.add_header("Accept", "application/json")
        if raw is not None:
            req.add_header("Content-Type", "application/json")
        if android_id:
            req.add_header("x-android-id", android_id)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                payload = _parse_json(resp.read())
        except urllib.error.HTTPError as exc:
            payload = _parse_json(exc.read())
        return payload

    def authenticate(self) -> int:
        payload = self._request(
            "/api/authenticate",
            body={"partnerAuthKey": self._key},
        )
        if not isinstance(payload, dict) or str(payload.get("status") or "") != "SUCCESS":
            raise EyePanelError(
                str((payload or {}).get("code") or "AUTH_FAILED") if isinstance(payload, dict) else "AUTH_FAILED"
            )
        self._authed = True
        try:
            return int(payload.get("partId") or 0)
        except (TypeError, ValueError):
            return 0

    def create_account(self) -> CreatedAccount:
        """Allocate one new game login. Caller must persist the returned id."""

        if not self._authed:
            self.authenticate()
        android_id = secrets.token_hex(8)
        payload = self._request(
            "/api/common/loginAccountReq",
            body={"partnerAuthKey": self._key},
            android_id=android_id,
        )
        if not isinstance(payload, dict):
            raise EyePanelError("BAD_PANEL_RESPONSE")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        status = str(payload.get("status") or data.get("status") or "")
        code = str(payload.get("code") or data.get("code") or "")
        if status != "SUCCESS":
            raise EyePanelError(code or "CREATE_FAILED", payload)
        account_id = str(data.get("deviceId") or "").strip()
        if not account_id:
            cred = str(data.get("credentialsV2") or "")
            account_id = cred.split(":", 1)[0].strip()
        if not account_id or "-" not in account_id:
            raise EyePanelError("NO_DEVICE_ID")
        suffix = account_id.rsplit("-", 1)[-1]
        try:
            if int(suffix) > self._max_suffix:
                raise EyePanelError(f"SUFFIX_CAP_{self._max_suffix}")
        except ValueError:
            pass
        acc_id = data.get("accId")
        try:
            acc_num = int(acc_id) if acc_id is not None else None
        except (TypeError, ValueError):
            acc_num = None
        return CreatedAccount(
            account_id=account_id,
            acc_id=acc_num,
            enabled=bool(data.get("enabled", True)),
        )


def client_from_env(partner_auth_key: str) -> Optional[EyePanelClient]:
    flag = os.getenv("POKEREYE_ACCOUNT_AUTOPROVISION", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    if not str(partner_auth_key or "").strip():
        return None
    raw_limit = str(os.getenv("POKEREYE_ACCOUNT_MAX_SUFFIX", str(MAX_SUFFIX_DEFAULT))).strip()
    try:
        max_suffix = max(1, int(raw_limit))
    except ValueError:
        max_suffix = MAX_SUFFIX_DEFAULT
    base = os.getenv("POKEREYE_EYE_PANEL", DEFAULT_PANEL).strip() or DEFAULT_PANEL
    return EyePanelClient(partner_auth_key, base=base, max_suffix=max_suffix)
