import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlmodel import select

import app.db as db
import app.main as main
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import DouyinAccount, FollowEdge


def _user(uid="fan-1", nickname="粉丝一"):
    return {
        "uid": uid,
        "sec_uid": f"sec-{uid}",
        "nickname": nickname,
        "avatar": "",
        "signature": "",
        "is_following": False,
        "is_mutual": False,
    }


class _Browser:
    def __init__(self):
        self.identity_calls = 0

    def identity_for(self, _account):
        self.identity_calls += 1
        return SimpleNamespace(key="fixture")


class _Engine:
    def __init__(self, *, guarded_result=None):
        self.public_direct = AsyncMock(return_value=([], "empty_body"))
        self.locked_direct = AsyncMock(return_value=([], "empty_body"))
        self.fetch_douyin_follows_direct = self.public_direct
        self._fetch_douyin_follows_direct_locked = self.locked_direct
        self.guarded_result = guarded_result
        self.guard_calls = 0

    async def guarded_read_pair(self, _account_id, _kind, _key, operation,
                                *, empty_result):
        self.guard_calls += 1
        if self.guarded_result is not None:
            return self.guarded_result
        return await operation()


class FollowSyncTests(unittest.TestCase):
    def setUp(self):
        self.previous_db_engine = db._engine
        self.previous_main_engine = main.engine
        self.previous_browser = main.browser
        self.previous_read_mode = main.cfg.engine.douyin_read_mode
        self.previous_followers_mode = main.cfg.engine.douyin_followers_mode
        main.cfg.engine.douyin_read_mode = "hybrid"
        main.cfg.engine.douyin_followers_mode = "hybrid"
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "follow-sync.db"))
        main.browser = _Browser()
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin",
                nickname="fixture",
                status="active",
                storage_state='{"cookies":[]}',
                sec_uid="sec-self",
                follower_count=12,
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    def tearDown(self):
        main.engine = self.previous_main_engine
        main.browser = self.previous_browser
        main.cfg.engine.douyin_read_mode = self.previous_read_mode
        main.cfg.engine.douyin_followers_mode = self.previous_followers_mode
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_db_engine
        self.tmp.cleanup()

    def test_fan_skips_known_empty_direct_endpoint_and_uses_one_guard(self):
        engine = _Engine()
        main.engine = engine
        browser_fetch = AsyncMock(return_value=([_user()], ""))

        with patch("app.main.fetch_follows", browser_fetch):
            result = asyncio.run(main.sync_follows(self.account_id, "fan"))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["source"], "browser_fallback")
        self.assertEqual(engine.guard_calls, 1)
        engine.public_direct.assert_not_awaited()
        engine.locked_direct.assert_awaited_once_with(self.account_id, "fan")
        browser_fetch.assert_awaited_once()

    def test_browser_mode_following_never_attempts_direct_api(self):
        main.cfg.engine.douyin_read_mode = "browser"
        engine = _Engine()
        main.engine = engine
        browser_fetch = AsyncMock(return_value=([_user()], ""))

        with patch("app.main.fetch_follows", browser_fetch):
            result = asyncio.run(main.sync_follows(self.account_id, "following"))

        self.assertEqual(result["source"], "browser")
        engine.locked_direct.assert_not_awaited()
        browser_fetch.assert_awaited_once()

    def test_api_mode_following_failure_never_falls_back_to_browser(self):
        main.cfg.engine.douyin_read_mode = "api"
        engine = _Engine()
        main.engine = engine
        browser_fetch = AsyncMock(side_effect=AssertionError(
            "API mode must not open a browser"))

        with patch("app.main.fetch_follows", browser_fetch):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.sync_follows(self.account_id, "following"))

        self.assertEqual(caught.exception.status_code, 502)
        engine.locked_direct.assert_awaited_once()
        browser_fetch.assert_not_awaited()
        self.assertEqual(main.browser.identity_calls, 0)

    def test_api_mode_fan_reports_matrix_incompatibility_without_browser(self):
        main.cfg.engine.douyin_followers_mode = "api"
        engine = _Engine()
        main.engine = engine
        browser_fetch = AsyncMock(side_effect=AssertionError(
            "unsupported API-only operation must not open a browser"))

        with patch("app.main.fetch_follows", browser_fetch):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.sync_follows(self.account_id, "fan"))

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("已保留原数据", str(caught.exception.detail))
        browser_fetch.assert_not_awaited()
        engine.locked_direct.assert_awaited_once_with(self.account_id, "fan")

    def test_following_direct_and_browser_fallback_share_one_guard(self):
        engine = _Engine()
        main.engine = engine
        browser_fetch = AsyncMock(return_value=([_user("follow-1", "关注一")], ""))

        with patch("app.main.fetch_follows", browser_fetch):
            result = asyncio.run(main.sync_follows(self.account_id, "following"))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["source"], "browser_fallback")
        self.assertEqual(engine.guard_calls, 1)
        engine.public_direct.assert_not_awaited()
        engine.locked_direct.assert_awaited_once_with(self.account_id, "following")
        browser_fetch.assert_awaited_once()

    def test_ambiguous_empty_preserves_existing_snapshot_and_reports_failure(self):
        engine = _Engine()
        main.engine = engine
        with db.get_session() as session:
            session.add(FollowEdge(
                platform="douyin", account_id=self.account_id,
                direction="fan", uid="old-fan", nickname="旧粉丝"))
            session.commit()

        browser_fetch = AsyncMock(return_value=(
            [], "未拦截到粉丝列表(没等到该方向专属接口)"))
        with patch("app.main.fetch_follows", browser_fetch):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.sync_follows(self.account_id, "fan"))

        self.assertEqual(caught.exception.status_code, 502)
        with db.get_session() as session:
            rows = session.exec(select(FollowEdge).where(
                FollowEdge.account_id == self.account_id,
                FollowEdge.direction == "fan")).all()
        self.assertEqual([row.uid for row in rows], ["old-fan"])

    def test_risk_deferred_is_not_reported_as_completed_sync(self):
        engine = _Engine(guarded_result=(
            [], "risk_deferred:尚未达到该操作最小间隔"))
        main.engine = engine

        result = asyncio.run(main.sync_follows(self.account_id, "fan"))

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "尚未达到该操作最小间隔")
        engine.public_direct.assert_not_awaited()


class FollowDirectErrorTests(unittest.TestCase):
    def setUp(self):
        self.previous_db_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "follow-direct.db"))
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin", nickname="fixture", status="active",
                storage_state='{"cookies":[{"name":"sid_tt","value":"x"}]}',
                sec_uid="sec-self")
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_db_engine
        self.tmp.cleanup()

    def test_direct_empty_body_keeps_transport_error(self):
        @asynccontextmanager
        async def session_scope():
            yield client

        client = SimpleNamespace(last_error="empty_body",
                                 fetch_all_follows=AsyncMock(return_value=[]))
        client.session_scope = session_scope
        engine = MonitorEngine(Config(), _Browser())
        with patch("app.engine.monitor.DouyinClient", return_value=client):
            users, error = asyncio.run(
                engine._fetch_douyin_follows_direct_locked(
                    self.account_id, "following"))

        self.assertEqual(users, [])
        self.assertEqual(error, "empty_body")


class FollowSyncUiTests(unittest.TestCase):
    def test_skipped_sync_uses_deferred_message_instead_of_success(self):
        source = (Path(__file__).parents[1] / "app" / "web" / "app.js").read_text(
            encoding="utf-8")
        start = source.index("async function syncFollows(direction)")
        body = source[start:source.index("async function actFollow", start)]
        self.assertIn("if (r.skipped)", body)
        self.assertIn("同步暂缓", body)


if __name__ == "__main__":
    unittest.main()
