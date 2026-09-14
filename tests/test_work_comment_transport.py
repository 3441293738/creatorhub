import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.db as db
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import DouyinAccount


def _comment(cid="comment-1"):
    return {
        "cid": cid,
        "text": "fixture comment",
        "user": {"nickname": "visitor", "sec_uid": "visitor-sec"},
        "create_time": 1_700_000_000,
    }


class _Browser:
    def __init__(self, *, forbid_identity=False):
        self.forbid_identity = forbid_identity
        self.identity_calls = 0

    def identity_for(self, account):
        self.identity_calls += 1
        if self.forbid_identity:
            raise AssertionError("API-only comments must not construct browser identity")
        return SimpleNamespace(
            account_id=account.id, ua=account.ua, proxy=account.proxy,
            locale=account.locale, viewport_w=account.viewport_w,
            viewport_h=account.viewport_h,
            fp_accept_languages=account.fp_accept_languages,
        )


class WorkCommentTransportTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "work-comments.db"))
        self.cfg = Config()
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin", nickname="fixture", status="active",
                storage_state=json.dumps({"cookies": [{
                    "domain": ".douyin.com", "name": "sid_tt", "value": "fixture",
                }]}),
                ua="Mozilla/5.0 Chrome/152.0.0.0", locale="zh-TW",
                fp_accept_languages="zh-TW,zh;q=0.8", viewport_w=1440,
                viewport_h=900,
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_deferred_contract_contains_numeric_counts(self):
        engine = MonitorEngine(self.cfg, _Browser())
        engine._guarded_read_dict = AsyncMock(return_value={
            "ok": True, "skipped": True,
            "reason": "尚未达到该操作最小间隔",
        })

        result = asyncio.run(engine.sync_work_comments(
            self.account_id, "douyin", "work-1"))

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["source"], "deferred")
        self.assertNotIn("undefined", json.dumps(result))

    def test_api_mode_uses_account_environment_without_browser_identity(self):
        self.cfg.engine.douyin_read_mode = "api"
        browser = _Browser(forbid_identity=True)
        engine = MonitorEngine(self.cfg, browser)
        created = []

        class Client:
            last_error = ""

            def __init__(self, *_args, **kwargs):
                created.append(kwargs)

            @asynccontextmanager
            async def session_scope(self):
                yield self

            async def fetch_all_comments(self, _item_id):
                return [_comment()]

        with patch("app.engine.monitor.DouyinClient", Client), \
                patch("app.engine.monitor.fetch_comments", AsyncMock(
                    side_effect=AssertionError("API-only mode opened browser"))):
            result = asyncio.run(engine._sync_work_comments_locked(
                self.account_id, "douyin", "work-1", ""))

        self.assertTrue(result["ok"])
        self.assertEqual((result["fetched"], result["added"]), (1, 1))
        self.assertEqual(result["source"], "api")
        self.assertEqual(browser.identity_calls, 0)
        self.assertEqual(created[0]["locale"], "zh-TW")
        self.assertEqual(created[0]["accept_language"], "zh-TW,zh;q=0.8")
        self.assertEqual((created[0]["screen_width"], created[0]["screen_height"]),
                         (1440, 900))

    def test_browser_mode_never_constructs_direct_client(self):
        self.cfg.engine.douyin_read_mode = "browser"
        browser = _Browser()
        engine = MonitorEngine(self.cfg, browser)
        browser_fetch = AsyncMock(return_value=([_comment("browser-comment")], ""))

        with patch("app.engine.monitor.DouyinClient", side_effect=AssertionError(
                "browser mode constructed API client")), \
                patch("app.engine.monitor.fetch_comments", browser_fetch):
            result = asyncio.run(engine._sync_work_comments_locked(
                self.account_id, "douyin", "work-2", ""))

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "browser")
        browser_fetch.assert_awaited_once()

    def test_hybrid_falls_back_only_after_api_transport_failure(self):
        self.cfg.engine.douyin_read_mode = "hybrid"
        engine = MonitorEngine(self.cfg, _Browser())

        class Client:
            last_error = "empty_body"

            def __init__(self, *_args, **_kwargs):
                pass

            @asynccontextmanager
            async def session_scope(self):
                yield self

            async def fetch_all_comments(self, _item_id):
                return []

        browser_fetch = AsyncMock(return_value=([_comment("fallback-comment")], ""))
        with patch("app.engine.monitor.DouyinClient", Client), \
                patch("app.engine.monitor.fetch_comments", browser_fetch):
            result = asyncio.run(engine._sync_work_comments_locked(
                self.account_id, "douyin", "work-3", ""))

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "browser_fallback")
        browser_fetch.assert_awaited_once()

    def test_api_valid_empty_response_is_success_without_browser(self):
        self.cfg.engine.douyin_read_mode = "api"
        engine = MonitorEngine(self.cfg, _Browser(forbid_identity=True))

        class Client:
            last_error = ""

            def __init__(self, *_args, **_kwargs):
                pass

            @asynccontextmanager
            async def session_scope(self):
                yield self

            async def fetch_all_comments(self, _item_id):
                return []

        with patch("app.engine.monitor.DouyinClient", Client), \
                patch("app.engine.monitor.fetch_comments", AsyncMock(
                    side_effect=AssertionError("valid API empty opened browser"))):
            result = asyncio.run(engine._sync_work_comments_locked(
                self.account_id, "douyin", "work-empty", ""))

        self.assertTrue(result["ok"])
        self.assertEqual((result["fetched"], result["added"]), (0, 0))
        self.assertEqual(result["source"], "api")


class WorkCommentUiTests(unittest.TestCase):
    def test_deferred_comment_sync_never_formats_undefined(self):
        source = (Path(__file__).parents[1] / "app" / "web" / "app.js").read_text(
            encoding="utf-8")
        start = source.index("async function syncWorkComments()")
        body = source[start:source.index("// ── 关注 / 粉丝", start)]
        self.assertIn("if (r.skipped)", body)
        self.assertIn("Number(r.fetched) || 0", body)
        self.assertIn("transportSourceSuffix(r.source)", body)


if __name__ == "__main__":
    unittest.main()
