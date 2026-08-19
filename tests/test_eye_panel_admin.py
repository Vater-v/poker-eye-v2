from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.verified_v1.eye_panel_admin import EyePanelClient, EyePanelError, CreatedAccount
from core.v6router.accounts import AccountPool, AccountState


class FakeResp:
    def __init__(self, obj, status=200):
        self._raw = json.dumps(obj).encode("utf-8")
        self.status = status
        self.headers = {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FakeOpener:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def open(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        android = req.headers.get("x-android-id") or req.headers.get("X-android-id")
        self.calls.append((req.get_method(), req.full_url, body, android))
        return self.handler(req.full_url, body, android)


class EyePanelAdminTests(unittest.TestCase):
    def test_create_account_returns_device_id(self):
        def handler(url, body, android):
            if url.endswith("/api/authenticate"):
                self.assertIn("partnerAuthKey", body)
                return FakeResp({"status": "SUCCESS", "partId": 13238})
            self.assertTrue(url.endswith("/api/common/loginAccountReq"))
            self.assertTrue(android)
            return FakeResp({
                "status": "SUCCESS",
                "data": {
                    "deviceId": "709393393-20",
                    "accId": 76001,
                    "enabled": True,
                    "credentialsV2": "709393393-20:secret",
                },
            })

        client = EyePanelClient("agent-secret", opener=FakeOpener(handler), max_suffix=150)
        created = client.create_account()
        self.assertEqual(created.account_id, "709393393-20")
        self.assertEqual(created.acc_id, 76001)

    def test_limit_reached(self):
        def handler(url, body, android):
            if url.endswith("/api/authenticate"):
                return FakeResp({"status": "SUCCESS", "partId": 1})
            return FakeResp({"status": "ERROR", "code": "LIMIT_ACCOUNT_REACHED"})

        client = EyePanelClient("agent-secret", opener=FakeOpener(handler))
        with self.assertRaises(EyePanelError) as ctx:
            client.create_account()
        self.assertEqual(ctx.exception.code, "LIMIT_ACCOUNT_REACHED")

    def test_suffix_cap(self):
        def handler(url, body, android):
            if url.endswith("/api/authenticate"):
                return FakeResp({"status": "SUCCESS", "partId": 1})
            return FakeResp({"status": "SUCCESS", "data": {"deviceId": "709393393-151"}})

        client = EyePanelClient("agent-secret", opener=FakeOpener(handler), max_suffix=150)
        with self.assertRaises(EyePanelError):
            client.create_account()

    def test_pool_accepts_provisioned_id(self):
        pool = AccountPool(["base-4"], dynamic_base="base")
        pool.acquire("held")
        with self.assertRaises(Exception):
            pool.acquire("need")
        pool.register_validated("base-20", source="panel-loginAccountReq")
        self.assertEqual(pool.acquire("need").account_id, "base-20")
        self.assertEqual(pool.state_for("base-20"), AccountState.LEASED)


if __name__ == "__main__":
    unittest.main()
