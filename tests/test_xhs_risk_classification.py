import asyncio
import tempfile
import unittest
from pathlib import Path

import app.db as db
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import AccountRiskState, DouyinAccount
from app.platforms.xhs.client import XhsApiClient, XhsApiError


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Identity:
    timezone_id = "Asia/Shanghai"


class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return _Identity()


class XhsRiskClassificationTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "xhs-risk.db"))
        self.cfg = Config()
        self.cfg.engine.account_check_interval_seconds = 1

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_http_challenge_errors_are_structured_as_risk(self):
        for status in (461, 471):
            with self.assertRaises(XhsApiError) as caught:
                XhsApiClient._unwrap(_Response(status, {}))

            error = caught.exception
            self.assertEqual(error.category, "risk")
            self.assertEqual(error.status_code, status)
            self.assertEqual(error.signal, f"http_{status}")

    def test_explicit_login_expiry_is_structured_as_auth(self):
        response = _Response(200, {
            "success": False,
            "code": -100,
            "msg": "登录状态已失效，请重新登录",
        })

        with self.assertRaises(XhsApiError) as caught:
            XhsApiClient._unwrap(response)

        self.assertEqual(caught.exception.category, "auth")
        self.assertEqual(caught.exception.signal, "auth_expired")

    def test_account_health_risk_does_not_mark_account_invalid(self):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="xhs", nickname="fixture", status="active",
                storage_state='{"cookies": [{"name": "a1", "value": "fixture"}]}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        class RiskClient:
            async def self_info(self):
                raise XhsApiError(
                    "触发验证码", category="risk", status_code=461,
                    signal="http_461")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._last_acct_check = 0
        engine._xhs_client = lambda *_args, **_kwargs: RiskClient()

        asyncio.run(engine._check_accounts())

        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            state = session.get(AccountRiskState, account_id)
            self.assertEqual(account.status, "active")
            self.assertIsNotNone(state)
            self.assertGreater(state.risk_level, 0)


if __name__ == "__main__":
    unittest.main()
