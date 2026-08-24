import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
from sqlmodel import select

from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import (AccountRiskState, CommentRule, CommentTask, CommentWatch,
                        ContentRecord, DouyinAccount, MonitorTarget, RiskEvent)
from app.platforms.xhs.client import XhsApiClient, XhsApiError
from app.risk import OperationKind


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

    def anon_identity(self):
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

    def _account_with_xhs_state(self, cookie_value="fixture"):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="xhs", nickname="fixture", status="active",
                storage_state=(
                    '{"cookies": [{"name": "a1", "value": "'
                    + cookie_value + '"}]}'),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

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

    def test_direct_client_aligns_ua_client_hints_and_tls_target(self):
        client = XhsApiClient(
            "a1=fixture",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36",
        )
        target_major = client.impersonate.removeprefix("chrome")

        self.assertIn(
            f"Chrome/{target_major}.0.0.0",
            client.base_headers["user-agent"],
        )
        self.assertIn(
            f'"Chromium";v="{target_major}"',
            client.base_headers["sec-ch-ua"],
        )
        self.assertEqual(
            client.base_headers["sec-ch-ua-platform"], '"Windows"')

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

    def test_xhs_comment_discovery_returns_risk_instead_of_empty_success(self):
        calls = 0

        class RiskClient:
            async def note_comments(self, *_args, **_kwargs):
                nonlocal calls
                calls += 1
                raise XhsApiError(
                    "challenge", category="risk", status_code=429,
                    signal="http_429")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: RiskClient()
        rule = {
            "platform": "xhs", "mode": "auto_reply", "target_kind": "work",
            "aweme_id": "note-1", "xsec_token": "token", "keyword": "",
            "sec_uid": "", "has_creator": False, "account_uid": "",
        }
        candidates, error = asyncio.run(engine._discover_targets(
            rule, "state", "", "", "", _Identity()))
        self.assertEqual(candidates, [])
        self.assertIsInstance(error, XhsApiError)
        self.assertEqual(calls, 1)

    def test_guarded_read_records_structured_nested_risk_not_success(self):
        account_id = self._account_with_xhs_state()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        error = XhsApiError(
            "challenge", category="risk", status_code=429,
            signal="http_429")

        async def operation():
            return {"ok": False, "error": error}

        result = asyncio.run(engine._guarded_read_dict(
            account_id, OperationKind.READ_HEAVY, "nested-risk", operation))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "challenge")
        self.assertIsInstance(result["error"], str)
        with db.get_session() as session:
            state = session.get(AccountRiskState, account_id)
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual(state.risk_level, 1)
        self.assertEqual([event.outcome for event in events], ["risk"])

    def test_sync_work_comments_returns_structured_risk_as_string_once(self):
        account_id = self._account_with_xhs_state()

        class RiskClient:
            async def note_comments(self, *_args, **_kwargs):
                raise XhsApiError(
                    "challenge", category="risk", status_code=429,
                    signal="http_429")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: RiskClient()
        result = asyncio.run(engine.sync_work_comments(
            account_id, "xhs", "note-1", "token"))

        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "challenge")
        self.assertIsInstance(result["error"], str)
        with db.get_session() as session:
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual([event.outcome for event in events], ["risk"])

    def test_comment_watch_risk_persists_state_and_scheduler_continues(self):
        failed_account_id = self._account_with_xhs_state("failed")
        next_account_id = self._account_with_xhs_state("next")
        with db.get_session() as session:
            failed_watch = CommentWatch(
                platform="xhs", kind="video", aweme_id="note-risk",
                xsec_token="token", account_id=failed_account_id, enabled=True)
            next_watch = CommentWatch(
                platform="xhs", kind="video", aweme_id="note-ok",
                xsec_token="token", account_id=next_account_id, enabled=True)
            session.add(failed_watch)
            session.add(next_watch)
            session.commit()
            session.refresh(failed_watch)
            session.refresh(next_watch)
            failed_watch_id = failed_watch.id
            next_watch_id = next_watch.id

        class WatchClient:
            async def note_comments(self, note_id, **_kwargs):
                if note_id == "note-risk":
                    raise XhsApiError(
                        "challenge", category="risk", status_code=429,
                        signal="http_429")
                return {"comments": []}

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: WatchClient()
        original_scan = engine.scan_comment_watch
        calls = []
        results = []

        async def tracked_scan(watch_id):
            calls.append(watch_id)
            result = await original_scan(watch_id)
            results.append(result)
            return result

        engine.scan_comment_watch = tracked_scan
        asyncio.run(engine._scan_comment_watches())

        self.assertEqual(calls, [failed_watch_id, next_watch_id])
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], dict)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "challenge")
        self.assertIsInstance(results[0]["error"], str)
        with db.get_session() as session:
            failed_watch = session.get(CommentWatch, failed_watch_id)
            next_watch = session.get(CommentWatch, next_watch_id)
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == failed_account_id)).all()
        self.assertIsNotNone(failed_watch.last_scan_at)
        self.assertEqual(failed_watch.last_error, "challenge")
        self.assertIsNotNone(next_watch.last_scan_at)
        self.assertEqual([event.outcome for event in events], ["risk"])

    def test_xhs_target_detail_risk_stops_and_records_one_risk_event(self):
        account_id = self._account_with_xhs_state()
        with db.get_session() as session:
            target = MonitorTarget(
                platform="xhs", target_kind="creator", sec_uid="creator-1",
                account_id=account_id, download_enabled=False,
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        detail_calls = 0

        class RiskClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def notes_by_creator(self, *_args, **_kwargs):
                return {"notes": [
                    {"id": "note-1", "display_title": "one"},
                    {"id": "note-2", "display_title": "two"},
                ]}

            async def user_info(self, *_args, **_kwargs):
                return {}

            async def note_detail(self, *_args, **_kwargs):
                nonlocal detail_calls
                detail_calls += 1
                raise XhsApiError(
                    "challenge", category="risk", status_code=429,
                    signal="http_429")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        with patch("app.engine.monitor.XhsApiClient", RiskClient):
            result = asyncio.run(engine.scan_target(target_id))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "challenge")
        self.assertIsInstance(result["error"], str)
        self.assertEqual(detail_calls, 1)
        with db.get_session() as session:
            target = session.get(MonitorTarget, target_id)
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual(target.last_error, "challenge")
        self.assertEqual([event.outcome for event in events], ["risk"])

    def test_xhs_target_author_timeout_stops_before_detail_and_records_network_once(self):
        account_id = self._account_with_xhs_state()
        with db.get_session() as session:
            target = MonitorTarget(
                platform="xhs", target_kind="creator", sec_uid="creator-1",
                account_id=account_id, download_enabled=False,
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        detail_calls = 0

        class TimeoutClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def notes_by_creator(self, *_args, **_kwargs):
                return {"notes": [{"id": "note-1", "display_title": "one"}]}

            async def user_info(self, *_args, **_kwargs):
                raise TimeoutError("connection timeout")

            async def note_detail(self, *_args, **_kwargs):
                nonlocal detail_calls
                detail_calls += 1
                return {}

        engine = MonitorEngine(self.cfg, _BrowserStub())
        with patch("app.engine.monitor.XhsApiClient", TimeoutClient):
            result = asyncio.run(engine.scan_target(target_id))

        self.assertFalse(result["ok"])
        self.assertEqual(detail_calls, 0)
        with db.get_session() as session:
            records = session.exec(select(ContentRecord).where(
                ContentRecord.target_id == target_id)).all()
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual(records, [])
        self.assertEqual([event.outcome for event in events], ["network"])

    def test_xhs_target_detail_connection_error_does_not_create_partial_record(self):
        account_id = self._account_with_xhs_state()
        with db.get_session() as session:
            target = MonitorTarget(
                platform="xhs", target_kind="creator", sec_uid="creator-1",
                account_id=account_id, download_enabled=False,
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        detail_calls = 0

        class ConnectionClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def notes_by_creator(self, *_args, **_kwargs):
                return {"notes": [
                    {"id": "note-1", "display_title": "one"},
                    {"id": "note-2", "display_title": "two"},
                ]}

            async def user_info(self, *_args, **_kwargs):
                return {}

            async def note_detail(self, *_args, **_kwargs):
                nonlocal detail_calls
                detail_calls += 1
                raise ConnectionError("connection reset")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        with patch("app.engine.monitor.XhsApiClient", ConnectionClient):
            result = asyncio.run(engine.scan_target(target_id))

        self.assertFalse(result["ok"])
        self.assertEqual(detail_calls, 1)
        with db.get_session() as session:
            records = session.exec(select(ContentRecord).where(
                ContentRecord.target_id == target_id)).all()
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual(records, [])
        self.assertEqual([event.outcome for event in events], ["network"])

    def test_xhs_comment_watch_timeout_records_network_instead_of_success(self):
        account_id = self._account_with_xhs_state()
        with db.get_session() as session:
            watch = CommentWatch(
                platform="xhs", kind="video", aweme_id="note-timeout",
                xsec_token="token", account_id=account_id, enabled=True)
            session.add(watch)
            session.commit()
            session.refresh(watch)
            watch_id = watch.id

        class TimeoutClient:
            async def note_comments(self, *_args, **_kwargs):
                raise TimeoutError("connection timeout")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: TimeoutClient()
        recorded_errors = []
        original_record_failure = engine.risk.record_failure

        def record_failure(*args, **kwargs):
            recorded_errors.append(args[2])
            return original_record_failure(*args, **kwargs)

        engine.risk.record_failure = record_failure
        result = asyncio.run(engine.scan_comment_watch(watch_id))

        self.assertFalse(result["ok"])
        self.assertEqual(len(recorded_errors), 1)
        self.assertIsInstance(recorded_errors[0], TimeoutError)
        with db.get_session() as session:
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual([event.outcome for event in events], ["network"])

    def test_sync_work_comments_preserves_connection_exception_for_risk_accounting(self):
        account_id = self._account_with_xhs_state()

        class ConnectionClient:
            async def note_comments(self, *_args, **_kwargs):
                raise ConnectionError("connection reset")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: ConnectionClient()
        recorded_errors = []
        original_record_failure = engine.risk.record_failure

        def record_failure(*args, **kwargs):
            recorded_errors.append(args[2])
            return original_record_failure(*args, **kwargs)

        engine.risk.record_failure = record_failure
        result = asyncio.run(engine.sync_work_comments(
            account_id, "xhs", "note-1", "token"))

        self.assertFalse(result["ok"])
        self.assertEqual(len(recorded_errors), 1)
        self.assertIsInstance(recorded_errors[0], ConnectionError)
        with db.get_session() as session:
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual([event.outcome for event in events], ["network"])

    def test_xhs_creator_watch_connection_error_stops_before_comment_requests(self):
        account_id = self._account_with_xhs_state()
        with db.get_session() as session:
            watch = CommentWatch(
                platform="xhs", kind="user", sec_uid="creator-1",
                xsec_token="token", account_id=account_id, enabled=True)
            session.add(watch)
            session.commit()
            session.refresh(watch)
            watch_id = watch.id

        comment_calls = 0

        class ConnectionClient:
            async def notes_by_creator(self, *_args, **_kwargs):
                return {"notes": [{"id": "note-1", "display_title": "one"}]}

            async def user_info(self, *_args, **_kwargs):
                raise ConnectionError("connection reset")

            async def note_comments(self, *_args, **_kwargs):
                nonlocal comment_calls
                comment_calls += 1
                return {"comments": []}

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: ConnectionClient()
        result = asyncio.run(engine.scan_comment_watch(watch_id))

        self.assertFalse(result["ok"])
        self.assertEqual(comment_calls, 0)
        with db.get_session() as session:
            events = session.exec(select(RiskEvent).where(
                RiskEvent.account_id == account_id)).all()
        self.assertEqual([event.outcome for event in events], ["network"])

    def test_xhs_discovery_connection_error_discards_existing_partial_candidates(self):
        calls = 0

        class PartialThenConnectionErrorClient:
            async def notes_by_creator(self, *_args, **_kwargs):
                return {"notes": [
                    {"id": "note-1", "xsec_token": "token-1"},
                    {"id": "note-2", "xsec_token": "token-2"},
                ]}

            async def note_comments(self, note_id, **_kwargs):
                nonlocal calls
                calls += 1
                if note_id == "note-2":
                    raise ConnectionError("connection reset")
                return {"comments": [{"fixture": "first"}]}

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._xhs_client = lambda *_args, **_kwargs: PartialThenConnectionErrorClient()
        rule = {
            "platform": "xhs", "mode": "auto_reply", "target_kind": "creator",
            "aweme_id": "", "xsec_token": "token", "keyword": "",
            "sec_uid": "", "has_creator": False, "account_uid": "",
        }
        parsed = {
            "comment_id": "comment-1", "user_nickname": "visitor", "text": "hello"
        }
        with patch("app.engine.monitor.flatten_xhs_comments", side_effect=lambda rows: rows), \
                patch("app.engine.monitor.parse_xhs_comment", return_value=parsed):
            candidates, error = asyncio.run(engine._discover_targets(
                rule, "state", "", "creator-1", "owner", _Identity()))

        self.assertEqual(candidates, [])
        self.assertIsInstance(error, ConnectionError)
        self.assertEqual(calls, 2)

    def test_comment_rule_controlled_discovery_failure_never_creates_tasks(self):
        for partial in ([], [{
                "aweme_id": "partial-work", "xsec_token": "token",
                "target_comment_id": "partial-comment", "target_nick": "visitor",
                "ctx": {"nick": "visitor"}, "source_text": "hello",
        }]):
            with self.subTest(partial=bool(partial)):
                account_id = self._account_with_xhs_state(
                    "partial" if partial else "empty")
                with db.get_session() as session:
                    rule = CommentRule(
                        platform="xhs", mode="auto_reply", target_kind="self",
                        account_id=account_id, templates='["收到"]',
                        max_per_run=1, daily_cap=1, min_gap_seconds=60,
                    )
                    session.add(rule)
                    session.commit()
                    session.refresh(rule)
                    rule_id = rule.id

                engine = MonitorEngine(self.cfg, _BrowserStub())
                controlled = TimeoutError("connection timeout")

                async def discover(*_args):
                    return list(partial), controlled

                engine._discover_targets = discover
                result = asyncio.run(engine.run_comment_rule(rule_id))

                self.assertFalse(result["ok"])
                self.assertEqual(result.get("created", 0), 0)
                with db.get_session() as session:
                    tasks = session.exec(select(CommentTask).where(
                        CommentTask.rule_id == rule_id)).all()
                    events = session.exec(select(RiskEvent).where(
                        RiskEvent.account_id == account_id)).all()
                self.assertEqual(tasks, [])
                self.assertEqual([event.outcome for event in events], ["network"])

    def test_non_xhs_nested_controlled_errors_stop_discovery(self):
        engine = MonitorEngine(self.cfg, _BrowserStub())
        rule = {
            "platform": "douyin", "mode": "auto_reply", "target_kind": "creator",
            "aweme_id": "", "xsec_token": "", "keyword": "",
            "sec_uid": "creator-1", "has_creator": False, "account_uid": "",
        }

        async def videos(*_args, **_kwargs):
            return ([
                {"aweme_id": "work-1", "desc": "one", "create_time": 0},
                {"aweme_id": "work-2", "desc": "two", "create_time": 0},
            ], None, "")

        for platform_error in ("risk captcha", "login expired", "connection timeout"):
            calls = 0

            async def comments(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                return [], platform_error

            with self.subTest(platform_error=platform_error), \
                    patch("app.engine.monitor.fetch_videos", videos), \
                    patch("app.engine.monitor.fetch_comments", comments):
                candidates, error = asyncio.run(engine._discover_targets(
                    rule, "", "", "creator-1", "fixture", _Identity()))
            self.assertEqual(candidates, [])
            self.assertEqual(error, platform_error)
            self.assertEqual(calls, 1)

    def test_non_xhs_nested_business_errors_keep_existing_discovery_behavior(self):
        calls = 0

        async def videos(*_args, **_kwargs):
            return ([
                {"aweme_id": "work-1", "desc": "one", "create_time": 0},
                {"aweme_id": "work-2", "desc": "two", "create_time": 0},
            ], None, "")

        async def comments(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return [], "comment unavailable"

        engine = MonitorEngine(self.cfg, _BrowserStub())
        rule = {
            "platform": "douyin", "mode": "auto_reply", "target_kind": "creator",
            "aweme_id": "", "xsec_token": "", "keyword": "",
            "sec_uid": "creator-1", "has_creator": False, "account_uid": "",
        }
        with patch("app.engine.monitor.fetch_videos", videos), \
                patch("app.engine.monitor.fetch_comments", comments):
            candidates, error = asyncio.run(engine._discover_targets(
                rule, "", "", "creator-1", "fixture", _Identity()))
        self.assertEqual(candidates, [])
        self.assertEqual(error, "")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
