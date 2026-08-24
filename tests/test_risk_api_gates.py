import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
import httpx
from sqlmodel import select

import app.db as db
import app.main as main
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import AccountRiskState, DouyinAccount, RiskEvent
from app.platforms.xhs import XhsApiError
from app.platforms.xhs.creator_api import XhsCreatorApi
from app.platforms.xhs.publish import creator_check, creator_profile
from app.risk import OperationKind


class _Identity:
    timezone_id = "Asia/Shanghai"


class _BrowserStub:
    def __init__(self):
        self._locks = {}
        self.closed_keys = []

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return _Identity()

    def anon_identity(self):
        return _Identity()

    async def close_context(self, key):
        self.closed_keys.append(key)


class _CreatorResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _CreatorCli:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *_args, **_kwargs):
        return _CreatorResponse(self.payload)

    def close(self):
        return None


class RiskApiGateTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_main_engine = main.engine
        self.previous_browser = main.browser
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "api-risk.db"))
        self.cfg = Config()
        self.browser = _BrowserStub()
        main.browser = self.browser
        main.engine = MonitorEngine(self.cfg, self.browser)
        with db.get_session() as session:
            account = DouyinAccount(
                platform="xhs",
                nickname="fixture",
                status="active",
                storage_state='{"cookies":[{"name":"a1","value":"fixture"}]}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    def tearDown(self):
        main.engine = self.previous_main_engine
        main.browser = self.previous_browser
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _cooldown(self):
        main.engine.risk.record_failure(
            self.account_id,
            OperationKind.READ_HEAVY,
            XhsApiError(
                "challenge", category="risk", status_code=429,
                signal="http_429"),
        )

    def _operation_kinds(self):
        with db.get_session() as session:
            return [event.operation_kind for event in session.exec(
                select(RiskEvent).where(
                    RiskEvent.account_id == self.account_id)).all()]

    def _event_outcomes(self):
        with db.get_session() as session:
            return [event.outcome for event in session.exec(
                select(RiskEvent).where(
                    RiskEvent.account_id == self.account_id)).all()]

    def _asgi_get(self, path):
        async def request():
            transport = httpx.ASGITransport(
                app=main.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://fixture") as client:
                return await client.get(path)

        return asyncio.run(request())

    def _assert_public_json(self, response, *, status_code):
        self.assertEqual(response.status_code, status_code)
        payload = response.json()
        json.dumps(payload, ensure_ascii=False)
        body = response.text.lower()
        for forbidden in (
                "_guard_error", "_error_category", "runtimeerror",
                "exception", "repr", "secret"):
            self.assertNotIn(forbidden, body)
        return payload

    def _profile_failure(self, error, expected_detail):
        with patch("app.main.XhsApiClient") as client_cls:
            profile = AsyncMock(side_effect=error)
            client_cls.return_value.self_info = profile
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))

            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(caught.exception.detail, expected_detail)
            calls_before_retry = profile.await_count
            retry = asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(retry["skipped"], True)
        self.assertEqual(profile.await_count, calls_before_retry)

    def _published_feed_failure(self, error, expected_outcome):
        account_uid = AsyncMock(return_value=("user-1", ""))
        fetch_notes = AsyncMock(return_value=([], None, error))
        fetch_creator = AsyncMock(return_value=([], ""))
        with patch("app.main._xhs_account_uid", account_uid), \
                patch("app.browser.fetch_xhs_notes", fetch_notes), \
                patch("app.browser.fetch_creator_published", fetch_creator):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        fetch_creator.assert_not_awaited()
        self.assertEqual(self._event_outcomes(), [expected_outcome])

    def _published_creator_profile_failure(self, error, expected_outcome):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls, \
                patch("app.browser.fetch_xhs_notes") as fetch_notes, \
                patch("app.browser.fetch_creator_published") as fetch_creator:
            client_cls.return_value.self_info = AsyncMock(return_value={})
            api_cls.return_value.my_info.side_effect = error
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        fetch_notes.assert_not_called()
        fetch_creator.assert_not_called()
        self.assertEqual(self._event_outcomes(), [expected_outcome])

    def _published_creator_profile_response(self, response, expected_outcome):
        def creator_api_init(api, *_args, **_kwargs):
            api.a1 = "fixture"
            api.cookies = {"a1": "fixture"}
            api.cli = _CreatorCli(response)

        with patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch.object(XhsCreatorApi, "__init__", creator_api_init), \
                patch("app.platforms.xhs.creator_api.sign.generate_xsc", return_value={}), \
                patch("app.browser.fetch_xhs_notes") as fetch_notes, \
                patch("app.browser.fetch_creator_published") as fetch_creator:
            client_cls.return_value.self_info = AsyncMock(return_value={})
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))

        self.assertIn(caught.exception.status_code, (400, 500))
        fetch_notes.assert_not_called()
        fetch_creator.assert_not_called()
        self.assertEqual(self._event_outcomes(), [expected_outcome])

    def _profile_creator_check_response(self, response, expected_outcome):
        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            account.creator_storage_state = account.storage_state
            session.add(account)
            session.commit()

        async def empty_profile(_state, proxy="", *, preserve_error=False):
            return (None, "empty") if preserve_error else None

        def creator_api_init(api, *_args, **_kwargs):
            api.a1 = "fixture"
            api.cookies = {"a1": "fixture"}
            api.cli = _CreatorCli(response)

        with patch("app.platforms.xhs.creator_profile", empty_profile), \
                patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch.object(XhsCreatorApi, "__init__", creator_api_init), \
                patch("app.platforms.xhs.creator_api.sign.generate_xsc", return_value={}):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self._event_outcomes(), [expected_outcome])

    def test_profile_refresh_does_not_call_platform_during_cooldown(self):
        self._cooldown()

        with patch("app.main._enrich_account_profile") as enrich:
            result = asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["skipped"], True)
        self.assertIn("风险冷却期", result["reason"])
        enrich.assert_not_called()

    def test_published_list_does_not_call_platform_during_cooldown(self):
        self._cooldown()

        with patch("app.main._xhs_account_uid") as account_uid, \
                patch("app.browser.fetch_xhs_notes") as fetch_notes, \
                patch("app.browser.fetch_creator_published") as fetch_creator:
            account_uid.return_value = ""
            fetch_creator.return_value = ([], "")
            result = asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(result["notes"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["good_tokens"], False)
        self.assertEqual(result["skipped"], True)
        self.assertIn("风险冷却期", result["reason"])
        self.assertNotIn("_guard_error", result)
        account_uid.assert_not_called()
        fetch_notes.assert_not_called()
        fetch_creator.assert_not_called()

    def test_note_media_does_not_call_platform_during_cooldown(self):
        self._cooldown()

        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client_cls.return_value.note_detail = AsyncMock(return_value={})
            result = asyncio.run(main.publish_note_media(
                self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(result["medias"], [])
        self.assertEqual(result["skipped"], True)
        self.assertIn("风险冷却期", result["reason"])
        client_cls.assert_not_called()

    def test_note_comments_do_not_call_platform_during_cooldown(self):
        self._cooldown()

        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client_cls.return_value.note_detail_raw = AsyncMock(return_value={})
            client_cls.return_value.note_comments = AsyncMock(
                return_value={"comments": [], "has_more": False})
            result = asyncio.run(main.publish_note_comments(
                self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(result["comments"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["has_more"], False)
        self.assertEqual(result["skipped"], True)
        self.assertIn("风险冷却期", result["reason"])
        client_cls.assert_not_called()

    def test_published_primary_and_fallback_share_one_logical_guard(self):
        lock = self.browser.lock_for(f"acc:{self.account_id}")

        async def no_public_uid(_state, _proxy, *, detailed=False):
            self.assertTrue(lock.locked())
            return ("", "") if detailed else ""

        async def creator_fallback(_browser, _identity):
            self.assertTrue(lock.locked())
            return ([{"id": "note-1", "title": "Fixture"}], "")

        with patch("app.main._xhs_account_uid", no_public_uid), \
                patch("app.browser.fetch_creator_published", creator_fallback):
            result = asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(result, {
            "notes": [{
                "note_id": "note-1",
                "title": "Fixture",
                "type": "normal",
                "cover": "",
                "images": [],
                "like": 0,
                "time": 0,
                "xsec_token": "",
                "xsec_source": "pc_note_detail",
            }],
            "total": 1,
            "good_tokens": False,
        })
        self.assertEqual(self._operation_kinds(), [OperationKind.READ_LIGHT.value])

    def test_profile_refresh_preserves_success_payload_and_uses_light_read(self):
        lock = self.browser.lock_for(f"acc:{self.account_id}")

        async def enrich(_account_id, _state, *, detailed=False):
            self.assertTrue(lock.locked())
            return ("ok", "") if detailed else "ok"

        with patch("app.main._enrich_account_profile", enrich):
            result = asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(result, {
            "ok": True,
            "nickname": "fixture",
            "platform": "xhs",
            "douyin_id": "",
            "sec_uid": "",
            "status": "active",
            "login_scope": "read",
            "has_read_login": False,
            "has_creator": False,
        })
        self.assertEqual(self.browser.closed_keys, [self.account_id])

    def test_profile_refresh_returns_immediately_while_manual_browser_is_open(self):
        class Lease:
            active = True

        main.open_browsers[self.account_id] = Lease()
        try:
            result = asyncio.run(main.refresh_account_profile(self.account_id))
        finally:
            main.open_browsers.pop(self.account_id, None)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["blocked_by"], "open_browser")
        self.assertIn("关闭窗口", result["reason"])
        self.assertEqual(self.browser.closed_keys, [])

    def test_invalid_account_profile_refresh_can_recover_status(self):
        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            account.status = "invalid"
            session.add(account)
            session.commit()

        async def enrich(account_id, _state, *, detailed=False):
            with db.get_session() as session:
                account = session.get(DouyinAccount, account_id)
                account.status = "active"
                session.add(account)
                session.commit()
            return ("ok", "") if detailed else "ok"

        with patch("app.main._enrich_account_profile", enrich):
            result = asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(result["status"], "active")
        with db.get_session() as session:
            self.assertEqual(
                session.get(DouyinAccount, self.account_id).status, "active")
        self.assertEqual(self._operation_kinds(), [OperationKind.READ_LIGHT.value])

    def test_note_media_preserves_success_payload(self):
        work = SimpleNamespace(
            media_type="image",
            desc="Fixture description",
            cover="https://fixture/cover.jpg",
            medias=[SimpleNamespace(
                url="https://fixture/image.jpg",
                kind="image",
                ext="jpg",
                index=0,
            )],
        )

        with patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.parse_note_detail", return_value=work):
            client_cls.return_value.note_detail = AsyncMock(
                return_value={"note_id": "note-1"})
            result = asyncio.run(main.publish_note_media(
                self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(result, {
            "media_type": "image",
            "desc": "Fixture description",
            "cover_url": "https://fixture/cover.jpg",
            "medias": [{
                "url": "https://fixture/image.jpg",
                "kind": "image",
                "ext": "jpg",
                "index": 0,
            }],
        })
        self.assertEqual(self._operation_kinds(), [OperationKind.READ_HEAVY.value])

    def test_note_media_browser_mode_reuses_account_browser(self):
        work = SimpleNamespace(
            media_type="image", desc="Fixture", cover="",
            medias=[SimpleNamespace(
                url="https://fixture/image.jpg", kind="image",
                ext="jpg", index=0)],
        )
        with patch.object(main.cfg.engine, "xhs_read_mode", "browser"), \
                patch.object(
                    self.browser, "visible_page", AsyncMock(), create=True), \
                patch("app.main.fetch_xhs_note_detail", AsyncMock(
                    return_value=({}, ""))) as browser_fetch, \
                patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.parse_note_detail", return_value=work):
            result = asyncio.run(main.publish_note_media(
                self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(result["media_type"], "image")
        browser_fetch.assert_awaited_once()
        client_cls.assert_not_called()

    def test_xhs_share_inspection_uses_browser_note_reader(self):
        identity = SimpleNamespace(proxy="", ua="", key="acc:1")
        work = SimpleNamespace(
            platform="xhs", aweme_id="0123456789abcdef01234567",
            desc="Fixture note", author_name="Fixture author",
            duration=0, create_time=0, like_count=0, comment_count=0,
            cover="", media_type="images", quality_label="original",
            medias=[SimpleNamespace(
                url="https://fixture/image.jpg", kind="image",
                ext="jpg", index=0)],
        )
        ref = SimpleNamespace(
            note_id=work.aweme_id, xsec_token="token",
            xsec_source="pc_feed")
        with patch("app.main._xhs_browser_reads_enabled", return_value=True), \
                patch.object(self.browser, "identity_for", return_value=identity), \
                patch("app.main.xhs_resolve_note", AsyncMock(
                    return_value=ref)), \
                patch("app.main.fetch_xhs_note_detail", AsyncMock(
                    return_value=({"note_id": work.aweme_id}, ""))) as fetcher, \
                patch("app.platforms.xhs.parse_note_detail", return_value=work):
            result = asyncio.run(main._xhs_native_share(
                f"https://www.xiaohongshu.com/explore/{work.aweme_id}",
                account_id=self.account_id,
                output_root=Path(self.tmp.name),
                quality="highest",
                should_download=False,
                save_metadata=True,
                save_thumbnail=True,
                proxy="",
            ))

        self.assertTrue(result["ok"])
        self.assertEqual(result["metadata"]["platform"], "xhs")
        fetcher.assert_awaited_once()

    def test_note_media_preserves_http_400_error_mapping(self):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client_cls.return_value.note_detail = AsyncMock(side_effect=XhsApiError(
                "fixture failure", category="business", status_code=400,
                signal="business_error"))

            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.publish_note_media(
                    self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "取笔记失败:fixture failure")

    def test_profile_auth_error_keeps_classification_and_invalid_contract(self):
        error = XhsApiError(
            "session expired", category="auth", status_code=401,
            signal="auth_expired")

        self._profile_failure(error, "登录态已失效,请点「重新登录」")

        self.assertEqual(self._event_outcomes(), ["auth"])

    def test_profile_risk_error_keeps_classification_and_cooldown(self):
        error = XhsApiError(
            "challenge", category="risk", status_code=429,
            signal="http_429")

        self._profile_failure(
            error,
            "未能获取账号资料:请看服务端控制台 [xhs_self_profile] 那行日志"
            "(含它实际看到的请求),把它发我即可定位")

        self.assertEqual(self._event_outcomes(), ["risk"])

    def test_profile_network_error_keeps_classification(self):
        error = XhsApiError(
            "connection timeout", category="network", status_code=503,
            signal="network_failure")

        with patch("app.main.XhsApiClient") as client_cls:
            client_cls.return_value.self_info = AsyncMock(side_effect=error)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail,
            "未能获取账号资料:请看服务端控制台 [xhs_self_profile] 那行日志"
            "(含它实际看到的请求),把它发我即可定位")
        self.assertEqual(self._event_outcomes(), ["network"])

    def test_profile_browser_exception_keeps_raw_network_classification(self):
        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            account.platform = "douyin"
            session.add(account)
            session.commit()
        fetch_profile = AsyncMock(side_effect=TimeoutError("connection timeout"))

        with patch("app.main.fetch_self_profile", fetch_profile):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail,
            "未能获取账号资料:请看服务端控制台 [self_profile] 那行日志"
            "(含它实际看到的请求),把它发我即可定位")
        self.assertEqual(self._event_outcomes(), ["network"])

    def test_profile_logged_out_maps_to_auth_but_default_interface_stays_string(self):
        profile = AsyncMock(return_value=({}, "logged_out"))

        with patch("app.main._xhs_profile", profile):
            result = asyncio.run(main._enrich_account_profile(
                self.account_id,
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))

        self.assertEqual(result, "invalid")

        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            account.status = "active"
            session.add(account)
            session.commit()
        with patch("app.main._xhs_profile", AsyncMock(
                return_value=({}, "logged_out"))):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "登录态已失效,请点「重新登录」")
        self.assertEqual(self._event_outcomes(), ["auth"])

    def test_creator_profile_default_contract_and_detailed_raw_error(self):
        error = XhsApiError(
            "challenge", category="risk", status_code=429,
            signal="http_429")
        with patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls:
            api_cls.return_value.my_info.side_effect = error

            default = asyncio.run(creator_profile(
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))
            detailed = asyncio.run(creator_profile(
                '{"cookies":[{"name":"a1","value":"fixture"}]}',
                preserve_error=True))

        self.assertIsNone(default)
        self.assertIsNone(detailed[0])
        self.assertIs(detailed[1], error)

    def test_creator_check_default_contract_and_detailed_raw_error(self):
        error = TimeoutError("connection timeout")
        with patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls:
            api_cls.return_value.ping.side_effect = error

            default = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))
            detailed = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}',
                preserve_error=True))

        self.assertIsNone(default)
        self.assertIsNone(detailed[0])
        self.assertIs(detailed[1], error)

    def test_creator_helpers_detailed_mode_preserves_failure_responses(self):
        response = {
            "success": False,
            "code": 429,
            "msg": "risk captcha",
            "data": {"partial": "fixture"},
        }
        api = XhsCreatorApi.__new__(XhsCreatorApi)
        api.a1 = "fixture"
        api.cookies = {"a1": "fixture"}
        api.cli = _CreatorCli(response)
        with patch("app.platforms.xhs.creator_api.sign.generate_xsc", return_value={}):
            default_result = api.my_info()
            detailed_result = api.my_info(detailed=True)
            default_ping = api.ping()
            detailed_ping = api.ping(detailed=True)

        self.assertEqual(default_result, (False, {"partial": "fixture"}))
        self.assertEqual(
            detailed_result,
            (False, {"partial": "fixture"}, response))
        self.assertEqual(default_ping, (False, "risk captcha"))
        self.assertEqual(detailed_ping, (False, "risk captcha", response))

        def creator_api_init(instance, *_args, **_kwargs):
            instance.a1 = "fixture"
            instance.cookies = {"a1": "fixture"}
            instance.cli = _CreatorCli(response)

        with patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch.object(XhsCreatorApi, "__init__", creator_api_init), \
                patch("app.platforms.xhs.creator_api.sign.generate_xsc", return_value={}):
            default_profile = asyncio.run(creator_profile(
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))
            default_check = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))

        self.assertIsNone(default_profile)
        self.assertIsNone(default_check)

        with patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls:
            api_cls.return_value.ping.return_value = (False, "risk captcha")

            default_check = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))
            detailed_check = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}',
                preserve_error=True))

        self.assertIsNone(default_check)
        self.assertIsNone(detailed_check[0])
        self.assertIsInstance(detailed_check[1], XhsApiError)
        self.assertEqual(str(detailed_check[1]), "risk captcha")
        self.assertEqual(detailed_check[1].category, "risk")

    def test_creator_check_login_expiry_returns_structured_auth_in_detailed_mode(self):
        response = {
            "success": False, "code": 0, "msg": "登录过期", "data": {}}
        with patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls:
            api_cls.return_value.ping.return_value = (
                False, "登录过期", response)

            default = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}'))
            detailed = asyncio.run(creator_check(
                '{"cookies":[{"name":"a1","value":"fixture"}]}',
                preserve_error=True))

        self.assertIs(default, False)
        self.assertIs(detailed[0], False)
        self.assertIsInstance(detailed[1], XhsApiError)
        self.assertEqual(str(detailed[1]), "登录过期")
        self.assertEqual(detailed[1].category, "auth")
        self.assertEqual(detailed[1].signal, "auth_expired")
        self.assertIs(detailed[1].payload, response)

    def test_creator_profile_auth_stops_published_browser_fallback(self):
        self._published_creator_profile_response(
            {"success": False, "code": 401, "msg": "", "data": {}},
            "auth")

    def test_creator_profile_proxy_auth_stops_published_browser_fallback(self):
        self._published_creator_profile_response(
            {"success": False, "code": 407, "msg": "", "data": {}},
            "network")

    def test_creator_profile_risk_stops_published_browser_fallback(self):
        self._published_creator_profile_response(
            {"success": False, "code": 429, "msg": "risk captcha", "data": {}},
            "risk")

    def test_creator_profile_network_stops_published_browser_fallback(self):
        self._published_creator_profile_response(
            {"success": False, "code": 503, "msg": "connection timeout", "data": {}},
            "network")

    def test_creator_check_401_response_is_auth(self):
        self._profile_creator_check_response(
            {"success": False, "code": 401, "msg": "", "data": {}},
            "auth")

    def test_creator_check_407_response_is_network(self):
        self._profile_creator_check_response(
            {"success": False, "code": 407, "msg": "", "data": {}},
            "network")

    def test_creator_check_429_response_is_risk(self):
        self._profile_creator_check_response(
            {"success": False, "code": 429, "msg": "", "data": {}},
            "risk")

    def test_creator_check_503_response_is_network(self):
        self._profile_creator_check_response(
            {"success": False, "code": 503, "msg": "", "data": {}},
            "network")

    def test_creator_check_login_expiry_marks_profile_auth_and_defers_retry(self):
        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            account.creator_storage_state = account.storage_state
            session.add(account)
            session.commit()

        async def empty_profile(_state, proxy="", *, preserve_error=False):
            return (None, "empty") if preserve_error else None

        with patch("app.platforms.xhs.creator_profile", empty_profile), \
                patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls:
            api_cls.return_value.ping.return_value = (False, "登录过期")
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))
            calls_before_retry = api_cls.return_value.ping.call_count
            retry = asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "登录态已失效,请点「重新登录」")
        self.assertEqual(self._event_outcomes(), ["auth"])
        self.assertEqual(retry["skipped"], True)
        self.assertEqual(api_cls.return_value.ping.call_count, calls_before_retry)

    def test_creator_check_risk_is_preserved_by_guarded_profile_refresh(self):
        with db.get_session() as session:
            account = session.get(DouyinAccount, self.account_id)
            account.creator_storage_state = account.storage_state
            session.add(account)
            session.commit()

        async def empty_profile(_state, proxy="", *, preserve_error=False):
            return (None, "empty") if preserve_error else None

        error = XhsApiError(
            "challenge", category="risk", status_code=429,
            signal="http_429")
        with patch("app.platforms.xhs.creator_profile", empty_profile), \
                patch("app.platforms.xhs.creator_sign.available", return_value=True), \
                patch("app.platforms.xhs.creator_api.XhsCreatorApi") as api_cls:
            api_cls.return_value.ping.side_effect = error
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.refresh_account_profile(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self._event_outcomes(), ["risk"])

    def test_published_uid_risk_does_not_call_either_fallback(self):
        error = XhsApiError(
            "challenge", category="risk", status_code=429,
            signal="http_429")
        with patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.creator_profile") as creator_profile, \
                patch("app.browser.fetch_xhs_notes") as fetch_notes, \
                patch("app.browser.fetch_creator_published") as fetch_creator:
            client_cls.return_value.self_info = AsyncMock(side_effect=error)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        creator_profile.assert_not_called()
        fetch_notes.assert_not_called()
        fetch_creator.assert_not_called()
        self.assertEqual(self._event_outcomes(), ["risk"])

    def test_published_feed_risk_stops_before_creator_fallback(self):
        self._published_feed_failure(
            XhsApiError(
                "challenge", category="risk", status_code=429,
                signal="http_429"),
            "risk")

    def test_published_feed_auth_stops_before_creator_fallback(self):
        self._published_feed_failure(
            XhsApiError(
                "session expired", category="auth", status_code=401,
                signal="auth_expired"),
            "auth")

    def test_published_feed_network_stops_before_creator_fallback(self):
        self._published_feed_failure(
            XhsApiError(
                "connection timeout", category="network", status_code=503,
                signal="network_failure"),
            "network")

    def test_published_feed_business_failure_may_use_creator_fallback(self):
        account_uid = AsyncMock(return_value=("user-1", ""))
        fetch_notes = AsyncMock(return_value=(
            [], None, XhsApiError(
                "empty feed", category="business", status_code=400,
                signal="business_error")))
        fetch_creator = AsyncMock(return_value=(
            [{"id": "note-1", "title": "Fixture"}], ""))

        with patch("app.main._xhs_account_uid", account_uid), \
                patch("app.browser.fetch_xhs_notes", fetch_notes), \
                patch("app.browser.fetch_creator_published", fetch_creator):
            result = asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(result["notes"][0]["note_id"], "note-1")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["good_tokens"], False)
        self.assertEqual(self._event_outcomes(), ["success"])

    def test_published_logged_out_preserves_http_400_mapping(self):
        async def no_uid(_state, _proxy, *, detailed=False):
            return ("", "") if detailed else ""

        with patch("app.main._xhs_account_uid", no_uid), \
                patch("app.browser.fetch_creator_published", AsyncMock(
                    return_value=([], "logged_out:创作平台未登录"))):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail, "登录态已失效,请对该账号点「重新登录」")

    def test_published_guard_caught_exception_is_stable_5xx_and_recorded_once(self):
        original_identity_for = self.browser.identity_for
        self.browser.identity_for = Mock(
            side_effect=RuntimeError("identity fixture failure"))
        try:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))
        finally:
            self.browser.identity_for = original_identity_for

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail, "读取已发布作品失败")
        self.assertNotIn("identity fixture failure", caught.exception.detail)
        self.assertNotIn("RuntimeError", caught.exception.detail)
        self.assertEqual(self._event_outcomes(), ["business"])

    def test_published_fallback_unknown_exception_is_stable_5xx(self):
        async def no_uid(_state, _proxy, *, detailed=False):
            return ("", "") if detailed else ""

        with patch("app.main._xhs_account_uid", no_uid), \
                patch("app.browser.fetch_creator_published", AsyncMock(
                    side_effect=RuntimeError("SECRET_PUBLISHED_MARKER"))):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail, "读取已发布作品失败")
        self.assertNotIn("SECRET_PUBLISHED_MARKER", caught.exception.detail)
        self.assertNotIn("RuntimeError", caught.exception.detail)
        self.assertEqual(self._event_outcomes(), ["business"])

    def test_published_tolerates_operation_business_error_without_marker_leak(self):
        async def no_uid(_state, _proxy, *, detailed=False):
            return ("", "") if detailed else ""

        with patch("app.main._xhs_account_uid", no_uid), \
                patch("app.browser.fetch_creator_published", AsyncMock(
                    return_value=([], "creator endpoint unavailable"))):
            result = asyncio.run(main.list_published_notes(self.account_id))

        self.assertEqual(result, {
            "notes": [],
            "total": 0,
            "good_tokens": False,
        })
        self.assertEqual(self._event_outcomes(), ["business"])

    def test_note_comments_preserves_success_payload_and_uses_heavy_read(self):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client_cls.return_value.note_detail_raw = AsyncMock(return_value={})
            client_cls.return_value.note_comments = AsyncMock(
                return_value={"comments": [], "has_more": False})
            result = asyncio.run(main.publish_note_comments(
                self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(result, {
            "comments": [],
            "total": 0,
            "has_more": False,
        })
        self.assertEqual(self._operation_kinds(), [OperationKind.READ_HEAVY.value])

    def test_note_comments_preserves_http_400_error_mapping(self):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client_cls.return_value.note_detail_raw = AsyncMock(return_value={})
            client_cls.return_value.note_comments = AsyncMock(
                side_effect=XhsApiError(
                    "fixture failure", category="business", status_code=400,
                    signal="business_error"))
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.publish_note_comments(
                    self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "取评论失败:fixture failure")

    def test_note_media_unknown_parser_exception_is_stable_5xx(self):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.parse_note_detail",
                      side_effect=RuntimeError("SECRET_MEDIA_MARKER")):
            client_cls.return_value.note_detail = AsyncMock(
                return_value={"note_id": "note-1"})
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.publish_note_media(
                    self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail, "取笔记失败")
        self.assertNotIn("SECRET_MEDIA_MARKER", caught.exception.detail)
        self.assertNotIn("RuntimeError", caught.exception.detail)
        self.assertEqual(self._event_outcomes(), ["business"])

    def test_note_comments_unknown_client_exception_is_stable_5xx(self):
        with patch("app.platforms.xhs.XhsApiClient",
                   side_effect=RuntimeError("SECRET_COMMENTS_MARKER")):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.publish_note_comments(
                    self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail, "取评论失败")
        self.assertNotIn("SECRET_COMMENTS_MARKER", caught.exception.detail)
        self.assertNotIn("RuntimeError", caught.exception.detail)
        self.assertEqual(self._event_outcomes(), ["business"])

    def test_note_comments_timeout_stops_before_comments_and_records_network_once(self):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client = client_cls.return_value
            client.note_detail_raw = AsyncMock(
                side_effect=TimeoutError("connection timeout"))
            client.note_comments = AsyncMock(
                return_value={"comments": [], "has_more": False})
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.publish_note_comments(
                    self.account_id, "note-1", "token", "pc_feed"))

        self.assertEqual(caught.exception.status_code, 400)
        client.note_comments.assert_not_awaited()
        self.assertEqual(self._event_outcomes(), ["network"])

    def test_asgi_success_response_is_serializable_and_has_no_internal_markers(self):
        work = SimpleNamespace(
            media_type="image", desc="Fixture", cover="",
            medias=[SimpleNamespace(
                url="https://fixture/image.jpg", kind="image", ext="jpg", index=0)],
        )
        with patch("app.platforms.xhs.XhsApiClient") as client_cls, \
                patch("app.platforms.xhs.parse_note_detail", return_value=work):
            client_cls.return_value.note_detail = AsyncMock(return_value={})
            response = self._asgi_get(
                f"/api/publish/note-media?account_id={self.account_id}"
                "&note_id=note-1&xsec_token=token")

        payload = self._assert_public_json(response, status_code=200)
        self.assertEqual(payload["media_type"], "image")

    def test_asgi_deferred_response_is_serializable_and_has_no_internal_markers(self):
        self._cooldown()
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            response = self._asgi_get(
                f"/api/publish/note-media?account_id={self.account_id}"
                "&note_id=note-1&xsec_token=token")

        payload = self._assert_public_json(response, status_code=200)
        self.assertTrue(payload["skipped"])
        client_cls.assert_not_called()

    def test_asgi_expected_business_error_is_a_clean_serializable_4xx(self):
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            client_cls.return_value.note_detail = AsyncMock(side_effect=XhsApiError(
                "expected fixture failure", category="business", status_code=400,
                signal="business_error"))
            response = self._asgi_get(
                f"/api/publish/note-media?account_id={self.account_id}"
                "&note_id=note-1&xsec_token=token")

        payload = self._assert_public_json(response, status_code=400)
        self.assertIn("expected fixture failure", payload["detail"])

    def test_asgi_unexpected_exception_is_a_clean_serializable_5xx(self):
        with patch("app.platforms.xhs.XhsApiClient",
                   side_effect=RuntimeError("SECRET_ASGI_MARKER")):
            response = self._asgi_get(
                f"/api/publish/note-media?account_id={self.account_id}"
                "&note_id=note-1&xsec_token=token")

        payload = self._assert_public_json(response, status_code=500)
        self.assertEqual(payload, {"detail": "取笔记失败"})


    def test_delete_account_removes_risk_rows_before_id_reuse(self):
        main.engine.risk.record_failure(
            self.account_id,
            OperationKind.COMMENT,
            XhsApiError("challenge", category="risk", status_code=429),
        )
        asyncio.run(main.del_account(self.account_id))

        with db.get_session() as session:
            self.assertIsNone(session.get(AccountRiskState, self.account_id))
            self.assertEqual(session.exec(select(RiskEvent).where(
                RiskEvent.account_id == self.account_id)).all(), [])
            replacement = DouyinAccount(nickname="replacement")
            session.add(replacement)
            session.commit()
            session.refresh(replacement)
            replacement_id = replacement.id

        self.assertEqual(replacement_id, self.account_id)
        decision = main.engine.risk.preflight(
            replacement_id, OperationKind.READ_LIGHT)
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
