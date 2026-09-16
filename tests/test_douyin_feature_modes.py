import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.db as db
import app.main as main
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import DanmakuRecord, DmConversation, DouyinAccount


class _Browser:
    def __init__(self):
        self.identity_calls = 0
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        self.identity_calls += 1
        return SimpleNamespace(key="fixture")


class DouyinProfileModeTests(unittest.TestCase):
    def setUp(self):
        self.previous_db_engine = db._engine
        self.previous_browser = main.browser
        self.previous_engine = main.engine
        self.previous_mode = main.cfg.engine.douyin_profile_mode
        self.previous_dm_mode = main.cfg.engine.douyin_dm_sync_mode
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "feature-modes.db"))
        main.browser = _Browser()
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin", nickname="旧名称", status="active",
                storage_state=(
                    '{"cookies":[{"name":"sid_tt","value":"fixture",'
                    '"domain":".douyin.com"}]}'),
                sec_uid="sec-old", ua="Mozilla/5.0 Chrome/152.0.0.0")
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    def tearDown(self):
        main.cfg.engine.douyin_profile_mode = self.previous_mode
        main.cfg.engine.douyin_dm_sync_mode = self.previous_dm_mode
        main.engine = self.previous_engine
        main.browser = self.previous_browser
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_db_engine
        self.tmp.cleanup()

    @staticmethod
    def _client(*, profile=None, error=""):
        client = SimpleNamespace(last_error=error)
        client.fetch_self_profile = AsyncMock(return_value=profile)
        client.fetch_profile = AsyncMock(return_value=None)

        @asynccontextmanager
        async def session_scope():
            yield client

        client.session_scope = session_scope
        return client

    def test_api_only_refresh_updates_profile_without_browser_identity(self):
        main.cfg.engine.douyin_profile_mode = "api"
        client = self._client(profile={
            "nickname": "API 名称", "sec_uid": "sec-new", "unique_id": "api-id",
            "follower_count": 42, "aweme_count": 7,
        })

        with patch("app.main.DouyinClient", return_value=client), \
                patch("app.main.fetch_self_profile", AsyncMock(
                    side_effect=AssertionError("API-only must not use browser"))):
            result = asyncio.run(main._enrich_account_profile(
                self.account_id,
                '{"cookies":[{"name":"sid_tt","value":"fixture"}]}',
                detailed=True))

        self.assertEqual(result, ("ok", ""))
        self.assertEqual(main.browser.identity_calls, 0)
        client.fetch_self_profile.assert_awaited_once()
        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            self.assertEqual((account.nickname, account.sec_uid, account.douyin_id),
                             ("API 名称", "sec-new", "api-id"))

    def test_hybrid_falls_back_to_browser_after_api_failure(self):
        main.cfg.engine.douyin_profile_mode = "hybrid"
        client = self._client(error="empty_body")
        browser_profile = AsyncMock(return_value=({
            "nickname": "浏览器名称", "sec_uid": "sec-browser",
        }, ""))

        with patch("app.main.DouyinClient", return_value=client), \
                patch("app.main.fetch_self_profile", browser_profile):
            result = asyncio.run(main._enrich_account_profile(
                self.account_id,
                '{"cookies":[{"name":"sid_tt","value":"fixture"}]}',
                detailed=True))

        self.assertEqual(result, ("ok", ""))
        self.assertEqual(main.browser.identity_calls, 1)
        browser_profile.assert_awaited_once()

    def test_api_only_dm_sync_does_not_create_browser_identity(self):
        main.cfg.engine.douyin_dm_sync_mode = "api"
        client = SimpleNamespace(last_error="")
        client.fetch_dm_conversations = AsyncMock(return_value=[{
            "conv_id": "conv-api", "peer_uid": "peer", "peer_sec_uid": "sec-peer",
            "peer_nickname": "协议会话", "peer_avatar": "", "last_text": "hello",
            "last_time": 123, "unread_count": 0, "conv_short_id": "42",
            "ticket": "ticket", "raw_json": "{}",
        }])

        @asynccontextmanager
        async def session_scope():
            yield client

        client.session_scope = session_scope

        class Engine:
            async def guarded_read_pair(self, _account_id, _kind, _key,
                                        operation, empty_result=None):
                return await operation()

        main.engine = Engine()
        with patch("app.main.DouyinClient", return_value=client), \
                patch("app.main.fetch_dm_conversations", AsyncMock(
                    side_effect=AssertionError("API-only must not use browser"))):
            result = asyncio.run(main.sync_dm(self.account_id))

        self.assertEqual(result["source"], "api")
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(main.browser.identity_calls, 0)
        with db.get_session() as session:
            row = session.query(DmConversation).filter_by(
                account_id=self.account_id, conv_id="conv-api").one()
            self.assertEqual(row.peer_nickname, "协议会话")

    def test_api_only_creator_danmaku_uses_direct_client_without_creator_login(self):
        cfg = Config()
        cfg.engine.media_dir = self.tmp.name
        cfg.engine.douyin_creator_danmaku_mode = "api"
        browser = _Browser()
        engine = MonitorEngine(cfg, browser)
        client = SimpleNamespace(last_error="")
        client.fetch_all_danmaku = AsyncMock(return_value=[{
            "danmaku_id": "dm-api", "item_id": "work-1", "text": "协议弹幕",
            "offset_time": 1000,
        }])

        @asynccontextmanager
        async def session_scope():
            yield client

        client.session_scope = session_scope
        with patch("app.engine.monitor.DouyinClient", return_value=client), \
                patch("app.engine.monitor.fetch_creator_danmaku", AsyncMock(
                    side_effect=AssertionError("API-only must not use browser"))):
            result = asyncio.run(engine.sync_work_danmaku(
                self.account_id, "douyin", "work-1"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "api")
        self.assertEqual(browser.identity_calls, 0)
        with db.get_session() as session:
            row = session.query(DanmakuRecord).filter_by(
                aweme_id="work-1", danmaku_id="dm-api").one()
            self.assertEqual(row.text, "协议弹幕")


if __name__ == "__main__":
    unittest.main()
