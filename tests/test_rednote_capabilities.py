"""Offline unit and integration tests for Rednote (海外小红书) platform capabilities."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from app import db, main
from app.browser import account_hub
from app.browser.identity import Identity
from app.browser.login import interactive_rednote_login
from app.browser.manager import cookie_string_to_state, install_rednote_domain_guard
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import AccountWork, ContentRecord, DouyinAccount, ProxyPool
from app.platforms.rednote import (
    COOKIE_ROOT,
    CREATOR_DOMAIN,
    EDIT_DOMAIN,
    FRONT_HOME_PAGE_URL,
    HOME_PAGE_URL,
    MAX_IMAGES,
    MAX_IMAGE_DESC_UTF16,
    MAX_TITLE_WEIGHT,
    MAX_TOPICS,
    PLATFORM_KEY,
    QRCODE_CREATE_URL,
    REDNOTE_CAPABILITIES,
    SHIELD_WEBPROFILE_URL,
    WWW_DOMAIN,
    RednoteApiClient,
    RednoteApiError,
    RednoteCreatorApi,
    RednotePublishError,
    RednoteSessionManager,
    RednoteSigner,
    build_rednote_note_url,
    build_rednote_profile_url,
    clear_rednote_sign_caches,
    clip_rednote_images,
    common_checksum,
    common_to_rednote_form,
    cookie_str_from_state,
    custom_hash_v2,
    default_rednote_form,
    direction,
    extract_sign_context_from_state,
    flatten_rednote_comments,
    format_rednote_form,
    generate_rednote_x_rap_param,
    has_a1,
    has_creator_cookies,
    has_web_session,
    is_rednote_host,
    is_rednote_url,
    looks_like_rednote_note,
    parse_rednote_brief,
    parse_rednote_comment,
    parse_rednote_creator_user,
    parse_rednote_detail,
    parse_rednote_self_user,
    publish_rednote,
    publish_rednote_browser,
    rednote_listener_decision,
    refresh_rednote_sign_context,
    resolve_rednote_note,
    resolve_rednote_sign_context,
    resolve_rednote_user,
    strip_literal_undefined,
    synthesize_rednote_b1,
    utf16_code_units,
    validate_rednote_form,
    weighted_length,
    weighted_truncate,
)
from app.profiles import DOMESTIC_PLATFORMS, OVERSEAS_PLATFORMS, assign_proxy_from_pool
from app.transport_matrix import build_transport_matrix, resolve_transport
from test_project_optimizations import local_project


# ═══════════ 1. Four-Domain & Cookie Isolation ═══════════


def test_rednote_four_domain_and_cookie_isolation():
    assert PLATFORM_KEY == "rednote"
    assert CREATOR_DOMAIN == "creator.rednote.com"
    assert EDIT_DOMAIN == "webapi.rednote.com"
    assert WWW_DOMAIN == "www.rednote.com"
    assert COOKIE_ROOT == "rednote.com"

    for host in ("rednote.com", "www.rednote.com", "creator.rednote.com", "webapi.rednote.com", ".rednote.com"):
        assert is_rednote_host(host)
    for bad_host in ("xiaohongshu.com", "www.xiaohongshu.com", "edith.xiaohongshu.com", "creator.xiaohongshu.com", "evil-rednote.com"):
        assert not is_rednote_host(bad_host)

    assert is_rednote_url("https://www.rednote.com/explore/67a1b2c3d4e5f60000000001")
    assert is_rednote_url("https://webapi.rednote.com/api/sns/web/v2/user/me")
    assert not is_rednote_url("https://www.xiaohongshu.com/explore/67a1b2c3d4e5f60000000001")
    assert not is_rednote_url("http://evilrednote.com/api")

    mixed_state = json.dumps({
        "cookies": [
            {"name": "a1", "value": "xhs_domestic_a1", "domain": ".xiaohongshu.com"},
            {"name": "web_session", "value": "xhs_domestic_ws", "domain": ".xiaohongshu.com"},
            {"name": "a1", "value": "rn_overseas_a1", "domain": ".rednote.com"},
            {"name": "web_session", "value": "rn_overseas_ws", "domain": "www.rednote.com"},
            {"name": "galaxy_creator_session_id", "value": "creator_sid", "domain": "creator.rednote.com"},
        ]
    })
    ck_str = cookie_str_from_state(mixed_state)
    assert "rn_overseas_a1" in ck_str
    assert "rn_overseas_ws" in ck_str
    assert "xhs_domestic_a1" not in ck_str
    assert "xhs_domestic_ws" not in ck_str
    assert has_a1(ck_str)
    assert has_creator_cookies(mixed_state)

    domestic_only = json.dumps({
        "cookies": [
            {"name": "a1", "value": "xhs_domestic_a1", "domain": ".xiaohongshu.com"},
            {"name": "web_session", "value": "xhs_domestic_ws", "domain": ".xiaohongshu.com"},
        ]
    })
    assert cookie_str_from_state(domestic_only) == ""
    assert not has_creator_cookies(domestic_only)
    assert not has_web_session(domestic_only)
    valid_rn_ws = json.dumps({
        "cookies": [{"name": "web_session", "value": "rn_valid_session_token_1234", "domain": ".rednote.com"}]
    })
    assert has_web_session(valid_rn_ws)

    imported = json.loads(cookie_string_to_state("a1=rn123; web_session=ws456", "rednote"))
    assert all(c["domain"] == ".rednote.com" for c in imported["cookies"])


def test_rednote_listener_decision_and_url_resolution():
    avatar_hit = rednote_listener_decision(
        "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31?imageView2/2/w/360/format/webp"
    )
    assert avatar_hit == {
        "frontLogin": True,
        "redirectTo": HOME_PAGE_URL,
        "waitRedirect": True,
    }
    logout_hit = rednote_listener_decision(
        "https://cdn.example/a00472903313b9a87fedd3a69a854e37c6037a8a.mp4_video_original_mp4"
    )
    assert logout_hit == {"logout": True, "redirectTo": FRONT_HOME_PAGE_URL}
    assert rednote_listener_decision(f"{SHIELD_WEBPROFILE_URL}?foo=1") == {"login": True}
    assert rednote_listener_decision(QRCODE_CREATE_URL) == {"frontLogout": True, "qrcode": True}
    assert rednote_listener_decision("https://www.xiaohongshu.com/explore") is None

    share_text = (
        "Check out this post on Rednote! "
        "https://www.rednote.com/discovery/item/67a1b2c3d4e5f60000000001?xsec_token=TOK_ABC&xsec_source=pc_feed"
    )
    note_ref = asyncio.run(resolve_rednote_note(share_text))
    assert note_ref is not None
    assert note_ref.note_id == "67a1b2c3d4e5f60000000001"
    assert note_ref.xsec_token == "TOK_ABC"
    assert looks_like_rednote_note(share_text)
    assert not looks_like_rednote_note("https://www.xiaohongshu.com/explore/67a1b2c3d4e5f60000000001")

    clean_url = build_rednote_note_url("67a1b2c3d4e5f60000000001", xsec_token="undefined")
    assert clean_url == "https://www.rednote.com/zh/discovery/item/67a1b2c3d4e5f60000000001"
    token_url = build_rednote_note_url("67a1b2c3d4e5f60000000001", xsec_token="REAL_TOK")
    assert "xsec_token=REAL_TOK" in token_url

    user_ref = asyncio.run(resolve_rednote_user("https://www.rednote.com/user/profile/5f0123456789abcdef012345?xsec_token=UTOK"))
    assert user_ref is not None
    assert user_ref.user_id == "5f0123456789abcdef012345"
    assert user_ref.xsec_token == "UTOK"
    assert build_rednote_profile_url(user_ref.user_id) == "https://www.rednote.com/zh/user/profile/5f0123456789abcdef012345"


def test_interactive_rednote_login_opens_zh_guards_xhs_redirect_and_warms_creator_sso(tmp_path: Path):
    visited_urls: list[str] = []
    evaluated_scripts: list[str] = []
    registered_routes: list[tuple] = []

    class FakeLoginPage:
        def __init__(self):
            self.url = "about:blank"

        def is_closed(self):
            return False

        def on(self, *_args, **_kw):
            return None

        async def bring_to_front(self):
            return None

        async def goto(self, url: str, **_kw):
            visited_urls.append(url)
            self.url = url

        async def reload(self, **_kw):
            return None

        async def evaluate(self, script: str, *_args):
            evaluated_scripts.append(script)
            if ".css-wemwzq" in script:
                return 1
            if "__INITIAL_STATE__" in script:
                return {"user_id": "5f0123456789abcdef012345", "red_id": "rn_99", "nickname": "OverseasUser"}
            return None

    fake_page = FakeLoginPage()

    class FakeCtx:
        pages = [fake_page]

        def on(self, *_args, **_kw):
            return None

        async def route(self, pattern, handler):
            registered_routes.append((pattern, handler))

        async def cookies(self):
            base = [{"name": "web_session", "value": "040069b2c3d4e5f6001122334455", "domain": ".rednote.com"}]
            if any("creator.rednote.com" in u for u in visited_urls):
                base.append({"name": "galaxy_creator_session_id", "value": "creator_sso_1", "domain": "creator.rednote.com"})
            return base

        async def storage_state(self):
            return {"cookies": await self.cookies(), "origins": []}

    class FakeLoginMgr:
        xhs_interaction = SimpleNamespace(pause=lambda *_a, **_kw: asyncio.sleep(0))

        async def context_for(self, _ident):
            return FakeCtx()

        @asynccontextmanager
        async def visible_page(self, _ident):
            yield fake_page

    ident = Identity(account_id=10, platform="rednote", profile_dir=tmp_path / "rn_login")
    ok, state_json, nick = asyncio.run(
        interactive_rednote_login(FakeLoginMgr(), ident, timeout_seconds=5)
    )
    assert ok is True
    assert nick == "OverseasUser"
    assert visited_urls[0] == "https://www.rednote.com/zh"
    assert visited_urls[-1] == "https://creator.rednote.com/new/note-manager"
    assert any(".css-wemwzq" in s and s.strip().startswith("() =>") for s in evaluated_scripts)
    assert has_creator_cookies(state_json)
    assert len(registered_routes) == 1

    pattern, handler = registered_routes[0]
    for blocked in (
        "https://www.xiaohongshu.com/explore",
        "https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
        "https://www.rednote.com/explore",
        "https://www.rednote.com/login?redirectPath=x",
    ):
        assert pattern.search(blocked), f"Expected guard pattern to match {blocked}"
    for allowed in (
        "https://www.rednote.com/zh",
        "https://www.rednote.com/zh/discovery/item/67a1b2c3d4e5f60000000001",
        "https://webapi.rednote.com/api/sns/web/v2/user/me",
        "https://creator.rednote.com/login",
        "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31",
    ):
        assert not pattern.search(allowed), f"Expected guard pattern NOT to match {allowed}"

    fulfilled: list[dict] = []
    aborted: list[bool] = []
    main_frame = object()
    nav_req = SimpleNamespace(
        is_navigation_request=lambda: True,
        frame=SimpleNamespace(page=SimpleNamespace(main_frame=main_frame)),
        resource_type="document",
    )
    nav_req.frame = SimpleNamespace(page=SimpleNamespace(main_frame=None))
    nav_req.frame.page.main_frame = nav_req.frame

    class FakeRoute:
        def __init__(self, req):
            self.request = req

        async def fulfill(self, **kw):
            fulfilled.append(kw)

        async def abort(self):
            aborted.append(True)

    asyncio.run(handler(FakeRoute(nav_req)))
    assert len(fulfilled) == 1
    assert "https://www.rednote.com/zh" in fulfilled[0]["body"]

    sub_req = SimpleNamespace(
        is_navigation_request=lambda: False,
        frame=None,
        resource_type="xhr",
    )
    asyncio.run(handler(FakeRoute(sub_req)))
    assert aborted == [True]


# ═══════════ 2. Shared Formatter (`exV` / `ePc`) ═══════════


def test_rednote_formatter_weighted_truncation_and_payload_rules():
    # ASCII counts 0.5 -> budget 20 allows 40 ASCII chars
    ascii_45 = "A" * 45
    assert weighted_length(ascii_45) == 22.5
    truncated_ascii = weighted_truncate(ascii_45, MAX_TITLE_WEIGHT)
    assert len(truncated_ascii) == 40
    assert weighted_length(truncated_ascii) == 20.0

    # CJK counts 1.0 -> budget 20 allows 20 CJK chars
    cjk_25 = "海" * 25
    truncated_cjk = weighted_truncate(cjk_25, MAX_TITLE_WEIGHT)
    assert len(truncated_cjk) == 20
    assert weighted_length(truncated_cjk) == 20.0

    # Emoji (astral plane) counts 1 UTF-32 code point in exV (weight 1.0), 2 UTF-16 code units
    assert utf16_code_units("😀") == 2
    assert weighted_length("😀") == 1.0

    assert strip_literal_undefined({"a": 1, "b": "undefined", "c": ""}) == {"a": 1, "c": ""}
    assert len(clip_rednote_images([f"{i}.jpg" for i in range(25)])) == MAX_IMAGES == 18

    converted = common_to_rednote_form({
        "title": "T" * 50,
        "desc": "Body copy",
        "tags": ",".join(f"tag{i}" for i in range(15)),
    })
    assert len(converted["title"]) == 40
    assert len(converted["CAT_tags"]) == MAX_TOPICS == 10

    form = default_rednote_form(converted, {
        "CAT_tags": [{"id": "t1", "name": "Travel"}, "OOTD"],
        "CAT_mention": [
            {"nickname": "Alice", "user_id": "u1"},
            {"nickname": "Bob", "user_id": "u2"},
            {"nickname": "Carol", "user_id": "u3"},
        ],
        "CAT_timing": {"type": 2, "time": 1760000000},
    })
    validate_rednote_form(form, media_type="images")
    formatted = format_rednote_form(form)
    assert formatted["desc"] == "Body copy"
    fd = formatted["formData"]
    assert fd["postTime"] == 1760000000 * 1000
    assert fd["hash_tag"] == [{"id": "t1", "name": "Travel", "link": "", "type": "topic"}]
    assert "#Travel[话题]#" in fd["desc"] and "#OOTD[话题]#" in fd["desc"]
    # Exact client @mention index sequence: "0", "2", "3"
    assert [a["index"] for a in fd["common"]["ats"]] == ["0", "2", "3"]
    assert fd["desc"].endswith("@Alice @Bob @Carol")

    formatted_begin = format_rednote_form(form, mention_append_position="begin", legacy_at_index=False)
    assert [a["index"] for a in formatted_begin["formData"]["common"]["ats"]] == ["0", "1", "2"]
    assert formatted_begin["formData"]["desc"].startswith("@Alice @Bob @Carol  Body copy")

    # Image-mode > 900 UTF-16 code units must fail validation
    over_900 = default_rednote_form(common_to_rednote_form({"desc": "a" * (MAX_IMAGE_DESC_UTF16 + 1)}))
    with pytest.raises(ValueError, match="900"):
        validate_rednote_form(over_900, media_type="images")
    # Video mode allows > 900 ASCII chars as long as weighted_length <= 1000
    validate_rednote_form(over_900, media_type="video")

    # Unverified domestic extensions rejected by default
    with pytest.raises(ValueError, match="CAT_goods"):
        validate_rednote_form({"title": "ok", "CAT_desc": "ok", "CAT_goods": [{"goods_id": "1"}]})


# ═══════════ 3. Extractors & Overseas Proxy Filtering ═══════════


def test_rednote_extractors_and_overseas_proxy_filtering(local_project):
    brief = parse_rednote_brief({
        "id": "67a1b2c3d4e5f60000000001",
        "xsec_token": "BTOK",
        "note_card": {
            "display_title": "Tokyo Coffee Guide",
            "type": "normal",
            "user": {"user_id": "u_rn_1", "nickname": "Mika"},
            "cover": {"url_default": "https://sns-img.xhscdn.com/cover1.jpg"},
        },
    })
    assert brief["note_id"] == "67a1b2c3d4e5f60000000001"
    assert brief["xsec_token"] == "BTOK"
    assert brief["cover"].startswith("https://")

    detail = parse_rednote_detail(
        {
            "note_id": "67a1b2c3d4e5f60000000001",
            "title": "Tokyo Coffee Guide",
            "desc": "Best pour-over spots",
            "type": "normal",
            "time": 1750000000000,
            "user": {"user_id": "u_rn_1", "nickname": "Mika"},
            "interact_info": {"liked_count": "42", "comment_count": "5", "collected_count": "18"},
            "image_list": [{"url_default": "https://sns-img.xhscdn.com/img1.jpg", "width": 1080, "height": 1440}],
        },
        brief=brief,
    )
    assert detail.platform == "rednote"
    assert detail.aweme_id == "67a1b2c3d4e5f60000000001"
    assert detail.like_count == 42
    assert detail.comment_count == 5
    assert len(detail.medias) == 1

    raw_comments = [{
        "id": "c1",
        "content": "Love this place!",
        "create_time": 1750000100000,
        "like_count": "9",
        "user_info": {"user_id": "u2", "nickname": "Leo"},
        "sub_comments": [{
            "id": "c2",
            "content": "Same here!",
            "create_time": 1750000200000,
            "like_count": "2",
            "user_info": {"user_id": "u3", "nickname": "Ana"},
        }],
    }]
    flat = flatten_rednote_comments(raw_comments)
    assert len(flat) == 2
    assert parse_rednote_comment(flat[0])["comment_id"] == "c1"
    assert parse_rednote_comment(flat[1])["reply_to"] == "c1"
    assert parse_rednote_self_user({"user_id": "u1", "red_id": "rn100", "nickname": "Mika"})["douyin_id"] == "rn100"
    assert parse_rednote_creator_user({"userId": "u1", "redId": "rn100", "userName": "Mika"})["sec_uid"] == "u1"

    assert "rednote" in OVERSEAS_PLATFORMS
    assert "rednote" not in DOMESTIC_PLATFORMS

    with db.get_session() as s:
        s.add(ProxyPool(
            url="http://cn.example:8080",
            label="CN Node",
            enabled=True,
            status="ok",
            exit_ip="113.100.20.10",
            country="CN",
            region="Guangdong",
            city="Shenzhen",
        ))
        s.add(ProxyPool(
            url="socks5://sg.example:1080",
            label="SG Node",
            enabled=True,
            status="ok",
            exit_ip="203.0.113.55",
            country="SG",
            region="Singapore",
            city="Singapore",
        ))
        s.commit()

        picked_rn = assign_proxy_from_pool(s, local_project.cfg, platform="rednote")
        assert picked_rn == "socks5://sg.example:1080"
        picked_xhs = assign_proxy_from_pool(s, local_project.cfg, platform="xhs")
        assert picked_xhs == "http://cn.example:8080"


# ═══════════ 4. RednoteApiClient Host Guard & Work Actions ═══════════


def test_rednote_api_client_host_guard_and_actions(monkeypatch):
    sent_requests: list[tuple[str, str, dict]] = []

    class FakeResp:
        def __init__(self, payload: dict):
            self.status_code = 200
            self.headers = {"content-type": "application/json"}
            self.text = json.dumps(payload)

        def json(self):
            return json.loads(self.text)

    class FakeSession:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            sent_requests.append(("GET", url, kwargs))
            return FakeResp({"code": 0, "success": True, "data": {"user_id": "u_rn_1", "nickname": "Mika"}})

        async def post(self, url, **kwargs):
            sent_requests.append(("POST", url, kwargs))
            return FakeResp({"code": 0, "success": True, "data": {"result": True}})

    client = RednoteApiClient(
        "a1=rn_a1_value_123456; web_session=rn_ws_value",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
    )
    client._session_cls = FakeSession

    async def exercise():
        with pytest.raises(RednoteApiError) as exc_info:
            await client._request("get", "https://edith.xiaohongshu.com/api/sns/web/v2/user/me")
        assert exc_info.value.signal == "cross_domain_blocked"

        me = await client.self_info()
        assert me["user_id"] == "u_rn_1"
        await client.set_note_privacy("67a1b2c3d4e5f60000000001", 1)
        await client.delete_note("67a1b2c3d4e5f60000000001")

    asyncio.run(exercise())
    assert all(is_rednote_url(url) for _, url, _ in sent_requests)
    urls = [u for _, u, _ in sent_requests]
    assert any("/web_api/sns/v1/note/privacy" in u for u in urls)
    assert any("/web_api/sns/capa/postgw/note/delete" in u for u in urls)


# ═══════════ 5. RPA Publish & Single-Click `write_uncertain:` Safety ═══════════


class _FakeLocator:
    def __init__(self, *, count_val: int = 1, visible: bool = True, text: str = ""):
        self._count = count_val
        self._visible = visible
        self._text = text
        self.first = self
        self.files_set: list[str] = []

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return True

    async def get_attribute(self, name: str):
        if name in ("disabled", "aria-disabled"):
            return None
        if name == "class":
            return "d-button"
        return None

    async def inner_text(self):
        return self._text

    async def set_input_files(self, paths, **_kw):
        self.files_set = list(paths)

    async def click(self, *_args, **_kw):
        return None

    def nth(self, _idx: int):
        return self


class _FakePublishPage:
    def __init__(self, *, emit_v2_note: bool = True):
        self.url = "https://creator.rednote.com/publish/publish?source=official&from=tab_switch&target=image"
        self._emit_v2_note = emit_v2_note
        self._listeners: dict[str, list] = {}
        self.file_loc = _FakeLocator()
        self.title_loc = _FakeLocator()
        self.body_loc = _FakeLocator()
        self.submit_loc = _FakeLocator(text="Publish")

    def locator(self, selector: str):
        if 'type="file"' in selector:
            return self.file_loc
        if "Title" in selector or "标题" in selector:
            return self.title_loc
        if "contenteditable" in selector or "正文" in selector:
            return self.body_loc
        if "Publish" in selector or "发布" in selector:
            return self.submit_loc
        return _FakeLocator(count_val=0, visible=False)

    def get_by_placeholder(self, _text: str, **_kw):
        return _FakeLocator(count_val=0, visible=False)

    def get_by_role(self, _role: str, **_kw):
        return _FakeLocator(count_val=0, visible=False)

    def get_by_text(self, _text: str, **_kw):
        return _FakeLocator(count_val=0, visible=False)

    def on(self, event: str, callback):
        self._listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback):
        if event in self._listeners and callback in self._listeners[event]:
            self._listeners[event].remove(callback)

    def is_closed(self):
        return False

    async def trigger_submit_response(self):
        if not self._emit_v2_note:
            return

        class FakeNetResp:
            url = "https://webapi.rednote.com/web_api/sns/v2/note"
            status = 200
            request = SimpleNamespace(method="POST")

            async def json(self):
                return {"code": 0, "success": True, "data": {"id": "67a1b2c3d4e5f60000000099"}}

        for cb in list(self._listeners.get("response", [])):
            cb(FakeNetResp())


class _FakeInteraction:
    def __init__(self, page: _FakePublishPage):
        self.page = page
        self.typed: list[str] = []
        self.inserted: list[str] = []
        self.clicks = 0

    async def pause(self, *_args, **_kw):
        return None

    async def type_short(self, _loc, text: str):
        self.typed.append(text)

    async def insert_long(self, _loc, text: str, **_kw):
        self.inserted.append(text)

    async def click_visible(self, loc):
        self.clicks += 1
        if hasattr(loc, "click"):
            await loc.click()
        await self.page.trigger_submit_response()


class _FakeMgr:
    def __init__(self, page: _FakePublishPage):
        self.page = page
        self.xhs_interaction = _FakeInteraction(page)
        self.opened_urls: list[str] = []

    @asynccontextmanager
    async def visible_page(self, _identity, *, url: str = ""):
        self.opened_urls.append(url)
        yield self.page


def test_rednote_rpa_publish_and_single_click_uncertainty(tmp_path: Path):
    img_path = tmp_path / "card.png"
    Image.new("RGB", (64, 64), color=(220, 40, 80)).save(img_path)
    ident = Identity(account_id=1, platform="rednote", profile_dir=tmp_path / "prof")

    page_ok = _FakePublishPage(emit_v2_note=True)
    mgr_ok = _FakeMgr(page_ok)
    submitted_count = 0

    def _on_submit():
        nonlocal submitted_count
        submitted_count += 1

    outcome = asyncio.run(
        publish_rednote_browser(
            mgr_ok,
            ident,
            "images",
            "A" * 45,
            "Overseas cafe hop",
            ["Coffee", "Travel"],
            [str(img_path)],
            timeout_seconds=4,
            on_submit=_on_submit,
        )
    )
    assert outcome.status == "success"
    assert outcome.result == "https://www.rednote.com/zh/discovery/item/67a1b2c3d4e5f60000000099"
    assert mgr_ok.opened_urls[0].startswith("https://creator.rednote.com/publish/publish?source=official")
    assert mgr_ok.xhs_interaction.typed == ["A" * 40]
    assert mgr_ok.xhs_interaction.clicks == 1
    assert submitted_count == 1

    # When v2/note response never arrives after the single click, return uncertain without clicking twice
    page_unconfirmed = _FakePublishPage(emit_v2_note=False)
    mgr_unconfirmed = _FakeMgr(page_unconfirmed)
    uncertain = asyncio.run(
        publish_rednote_browser(
            mgr_unconfirmed,
            ident,
            "images",
            "Single Click Check",
            "Body",
            [],
            [str(img_path)],
            timeout_seconds=2,
        )
    )
    assert uncertain.status == "uncertain"
    assert mgr_unconfirmed.xhs_interaction.clicks == 1
    ok_legacy, _, err_legacy = uncertain.legacy()
    assert not ok_legacy and err_legacy.startswith("write_uncertain:")


# ═══════════ 6. Transport Matrix, AccountHub Bounds & FastAPI Integration ═══════════


def test_rednote_transport_matrix_and_fastapi_routes(local_project, monkeypatch):
    cfg = Config()
    assert cfg.engine.rednote_read_mode == "hybrid"
    assert cfg.engine.rednote_publish_mode == "browser"
    assert REDNOTE_CAPABILITIES["dm"] is False
    assert REDNOTE_CAPABILITIES["auto_comment"] is False
    assert REDNOTE_CAPABILITIES["commerce"] is False

    matrix = build_transport_matrix(cfg, [])
    rn_rows = {r["operation"]: r for r in matrix["rows"] if r["platform"] == "rednote"}
    assert rn_rows["public_monitor"]["effective_mode"] == "hybrid"
    assert rn_rows["account_works"]["effective_mode"] == "hybrid"
    assert rn_rows["publish"]["effective_mode"] == "browser"
    cfg.engine.rednote_publish_mode = "api"
    assert resolve_transport(cfg, "rednote", "publish")["effective_mode"] == "api"
    cfg.engine.rednote_publish_mode = "browser"
    for blocked_op in ("comment_write", "dm_sync", "follows"):
        assert rn_rows[blocked_op]["effective_mode"] == "unavailable"
        assert resolve_transport(cfg, "rednote", blocked_op)["effective_mode"] == "unavailable"

    ident = Identity(account_id=9, platform="rednote", profile_dir=local_project.root / "rn_prof")
    assert asyncio.run(account_hub.fetch_follows(local_project.browser, ident, "rednote", "u1", "following", set()))[1].startswith("unavailable:")
    assert asyncio.run(account_hub.fetch_dm_conversations(local_project.browser, ident, "rednote"))[1].startswith("unavailable:")
    assert asyncio.run(account_hub.send_dm(local_project.browser, ident, "rednote", target_uid="u1", text="hi"))[1].startswith("unavailable:")

    local_project.cfg.risk_control.operation_gap_min_seconds = 0
    local_project.cfg.risk_control.operation_gap_max_seconds = 0
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(main, "engine", engine)

    img_file = local_project.root / "rn_post.png"
    Image.new("RGB", (48, 48), color=(200, 30, 60)).save(img_file)
    monkeypatch.setattr(main, "UPLOAD_DIR", local_project.root)

    async def call_api():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            ck_resp = await client.post(
                "/api/login/cookie",
                json={
                    "platform": "rednote",
                    "cookie": "a1=rn_cookie_a1_123456; web_session=rn_cookie_ws_7890",
                    "nickname": "Rednote Tester",
                },
            )
            assert ck_resp.status_code == 200, ck_resp.text
            account_id = ck_resp.json()["account_id"]

            summary = await client.get("/api/overview/summary?platform=rednote")
            assert summary.status_code == 200
            assert summary.json()["platform"] == "rednote"
            assert summary.json()["accounts"] == 1

            # Automated comment rules are explicitly rejected for rednote
            rule_resp = await client.post(
                "/api/comment-rules",
                json={
                    "platform": "rednote",
                    "name": "blocked",
                    "account_id": account_id,
                    "templates": ["hello"],
                },
            )
            assert rule_resp.status_code == 400

            # Keyword monitor creation works on rednote
            mon_resp = await client.post(
                "/api/monitors",
                json={
                    "platform": "rednote",
                    "target_kind": "keyword",
                    "url_or_secuid": "Matcha Latte",
                    "account_id": account_id,
                    "interval_seconds": 900,
                    "download_enabled": False,
                },
            )
            assert mon_resp.status_code == 200, mon_resp.text
            assert mon_resp.json()["platform"] == "rednote"
            assert mon_resp.json()["target_kind"] == "keyword"

            # Publish preserves up to 40 ASCII chars via weighted_truncate(title, 20)
            pub_resp = await client.post(
                "/api/publish",
                json={
                    "account_id": account_id,
                    "media_type": "images",
                    "media_paths": [str(img_file)],
                    "title": "R" * 45,
                    "desc": "Hello Rednote",
                    "topics": "Travel,Food",
                },
            )
            assert pub_resp.status_code == 200, pub_resp.text
            pub_rows = (await client.get("/api/publish?platform=rednote")).json()
            assert len(pub_rows) == 1
            assert pub_rows[0]["title"] == "R" * 40

            # Own-work visibility toggle and delete routes
            with db.get_session() as s:
                work = AccountWork(
                    account_id=account_id,
                    platform="rednote",
                    item_id="67a1b2c3d4e5f60000000088",
                    desc="My Rednote Post",
                    media_type="images",
                    status="正常",
                )
                s.add(work)
                src_rec = ContentRecord(
                    platform="douyin",
                    target_id=0,
                    aweme_id="dy_001",
                    desc="Cross-post source",
                    media_type="images",
                    download_status="done",
                    local_path=str(img_file),
                )
                s.add(src_rec)
                s.commit()
                s.refresh(work)
                s.refresh(src_rec)
                work_id = work.id
                src_cid = src_rec.id

            async def _fake_vis(_state, note_id, visibility, **_kw):
                assert note_id == "67a1b2c3d4e5f60000000088"
                assert visibility == "private"
                return True, ""

            async def _fake_del(_state, note_id, **_kw):
                assert note_id == "67a1b2c3d4e5f60000000088"
                return True, ""

            monkeypatch.setattr(main, "update_rednote_visibility", _fake_vis)
            monkeypatch.setattr(main, "delete_rednote_work", _fake_del)

            vis_resp = await client.post(
                f"/api/account-works/{work_id}/rednote-visibility",
                json={"visibility": "private"},
            )
            assert vis_resp.status_code == 200, vis_resp.text
            assert vis_resp.json()["visibility"] == "private"
            with db.get_session() as s:
                assert s.get(AccountWork, work_id).status == "仅自己可见"

            repost_resp = await client.post(
                f"/api/contents/{src_cid}/repost-rednote",
                json={"account_id": account_id, "title": "Repost Title", "desc": "Repost Desc"},
            )
            assert repost_resp.status_code == 200, repost_resp.text

            del_resp = await client.post(f"/api/account-works/{work_id}/rednote-delete")
            assert del_resp.status_code == 200, del_resp.text
            assert del_resp.json()["item_id"] == "67a1b2c3d4e5f60000000088"
            with db.get_session() as s:
                assert s.get(AccountWork, work_id) is None

    asyncio.run(call_api())


# ═══════════ 7. XYS / XYW + Rednote b1 Extraction & Pure-Protocol API Publish ═══════════


def test_rednote_xys_xyw_signer_and_b1_environment_isolation():
    clear_rednote_sign_caches()
    mixed_origins_state = json.dumps({
        "cookies": [
            {"name": "a1", "value": "rn_a1_value_52chars_00000000000000000000000000000000", "domain": ".rednote.com"},
        ],
        "origins": [
            {
                "origin": "https://www.xiaohongshu.com",
                "localStorage": [
                    {"name": "b1", "value": "DOMESTIC_XHS_B1_MUST_BE_IGNORED"},
                    {"name": "dsllt", "value": "999999"},
                    {"name": "sc", "value": "88"},
                ],
            },
            {
                "origin": "https://www.rednote.com",
                "localStorage": [
                    {"name": "b1", "value": "OVERSEAS_REDNOTE_B1_TOKEN"},
                    {"name": "dsllt", "value": "1795070176379"},
                    {"name": "sc", "value": "7"},
                ],
            },
        ],
    })
    ctx = extract_sign_context_from_state(mixed_origins_state)
    assert ctx.b1 == "OVERSEAS_REDNOTE_B1_TOKEN"
    assert ctx.dsl_pair == "1795070176379"
    assert ctx.sign_count == 7

    # Domestic-only origins must never leak b1 into Rednote
    domestic_only_origins = json.dumps({
        "origins": [
            {
                "origin": "https://www.xiaohongshu.com",
                "localStorage": [{"name": "b1", "value": "DOMESTIC_XHS_B1"}],
            }
        ]
    })
    empty_ctx = extract_sign_context_from_state(domestic_only_origins)
    assert empty_ctx.b1 == ""

    synth_b1 = synthesize_rednote_b1(
        {"a1": "rn_a1_test"},
        user_agent="Mozilla/5.0 Chrome/138.0.0.0",
        locale="en-US",
        timezone="America/New_York",
    )
    assert isinstance(synth_b1, str) and len(synth_b1) > 32

    signer = RednoteSigner(
        now=lambda: 1795070000.125,
        rng=lambda: 0.25,
        random_bytes=lambda n: b"\x11\x22\x33\x44"[:n],
    )
    sess = RednoteSessionManager(now_ms=lambda: 1795070000125, rng=lambda: 0.5)
    cookies = "a1=19ab1e5ce48c3b3c5c50e2c040a801cfcca0a4ac50000571046; web_session=ws_ok"

    # GET routes default to XYS_ on live Rednote; explicit sign_format="xyw" emits XYW_
    get_hdrs = signer.sign_headers(
        "GET",
        "/api/sns/web/v1/user_posted",
        cookies,
        params={"num": 30, "user_id": "u1"},
        b1="OVERSEAS_REDNOTE_B1_TOKEN",
        dsl_pair="1795070176379",
        sign_count=7,
    )
    assert get_hdrs["x-s"].startswith("XYS_")
    assert get_hdrs["x-s-common"]
    assert "x-rap-param" not in get_hdrs
    xyw_hdrs = signer.sign_headers(
        "GET",
        "/api/sns/web/v1/user_posted",
        cookies,
        params={"num": 30, "user_id": "u1"},
        sign_format="xyw",
        b1="OVERSEAS_REDNOTE_B1_TOKEN",
    )
    assert xyw_hdrs["x-s"].startswith("XYW_")

    # Publish / feed / search POST routes automatically select XYS_ + //webapi.rednote.com x-rap-param
    post_hdrs = signer.sign_headers(
        "POST",
        "/web_api/sns/v2/note",
        cookies,
        payload={"common": {"title": "Overseas API Post"}},
        session=sess,
        b1="OVERSEAS_REDNOTE_B1_TOKEN",
        dsl_pair="1795070176379",
        sign_count=8,
    )
    assert post_hdrs["x-s"].startswith("XYS_")
    assert post_hdrs["x-rap-param"]
    assert common_checksum("OVERSEAS_REDNOTE_B1_TOKEN") != 0
    assert isinstance(direction("5f0123456789abcdef012345"), int)
    assert len(custom_hash_v2(b"0123456789abcdef")) == 16
    assert generate_rednote_x_rap_param("/api/sns/web/v1/feed", {"source_note_id": "123"})


def test_rednote_pure_api_publish_mode_and_upload_isolation(tmp_path: Path, monkeypatch):
    from app.platforms.rednote import creator_api as rn_capi

    # Domestic xiaohongshu.com upload destinations must be strictly rejected
    with pytest.raises(RednotePublishError):
        rn_capi._normalize_rednote_upload_origin("https://ros-upload.xiaohongshu.com")
    # ap-shanghai Tencent COS buckets rewrite to accelerate (literal 24352)
    cos_origin, is_cos = rn_capi._normalize_rednote_upload_origin("spectrum-125.cos.ap-shanghai.myqcloud.com")
    assert cos_origin == "https://spectrum-125.cos.accelerate.myqcloud.com"
    assert is_cos is True
    ros_origin, is_ros_cos = rn_capi._normalize_rednote_upload_origin("ros-upload.rednote.com")
    assert ros_origin == "https://ros-upload.rednote.com"
    assert is_ros_cos is False

    # Multipart XML roundtrip (literals 24355 & 24363)
    uid = rn_capi._parse_initiate_upload_id(
        '<?xml version="1.0"?><InitiateMultipartUploadResult><UploadId>UP_RN_99</UploadId></InitiateMultipartUploadResult>'
    )
    assert uid == "UP_RN_99"
    xml_bytes = rn_capi._build_complete_multipart_xml([(1, '"etag1"'), (2, '"etag2"')])
    assert b"<UploadId>" not in xml_bytes and b"<CompleteMultipartUpload>" in xml_bytes and b'"etag1"' in xml_bytes

    img_path = tmp_path / "api_post.png"
    Image.new("RGB", (64, 64), color=(40, 120, 210)).save(img_path)

    sent_requests: list[tuple[str, str, dict]] = []

    class FakeResp:
        def __init__(self, status_code: int, payload=None, headers=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {"content-type": "application/json"}
            self.text = text or json.dumps(self._payload)

        def json(self):
            return self._payload

    class FakeCurlSession:
        def __init__(self, **_opts):
            self.curl_options = {}

        def close(self):
            return None

        def get(self, url: str, **kw):
            sent_requests.append(("GET", url, kw.get("headers") or {}))
            if "/upload/creator/permit" in url:
                return FakeResp(200, {
                    "success": True,
                    "code": 0,
                    "data": {
                        "uploadTempPermits": [{
                            "fileIds": ["rn_img_file_001"],
                            "token": "cos_tok_abc",
                            "expireTime": 1899999999,
                            "uploadAddr": "ros-upload.rednote.com",
                        }]
                    },
                })
            raise AssertionError(f"Unexpected GET {url}")

        def put(self, url: str, **kw):
            from curl_cffi import CurlOpt

            read_fn = self.curl_options.get(CurlOpt.READFUNCTION)
            if callable(read_fn):
                while read_fn(65536):
                    pass
            sent_requests.append(("PUT", url, kw.get("headers") or {}))
            return FakeResp(200, {}, headers={"ETag": '"etag_ok"'})

        def post(self, url: str, **kw):
            sent_requests.append(("POST", url, kw.get("headers") or {}))
            if "/search/topic" in url:
                return FakeResp(200, {
                    "success": True,
                    "code": 0,
                    "data": {
                        "topic_info_dtos": [{"id": "tp_1", "name": "Coffee", "link": "https://www.rednote.com/page/topics/tp_1"}]
                    },
                })
            if "/web_api/sns/v2/note" in url:
                return FakeResp(200, {
                    "success": True,
                    "code": 0,
                    "data": {"id": "67a1b2c3d4e5f60000000123"},
                })
            raise AssertionError(f"Unexpected POST {url}")

    monkeypatch.setattr(rn_capi, "Session", FakeCurlSession)

    state_with_b1 = json.dumps({
        "cookies": [
            {"name": "a1", "value": "19ab1e5ce48c3b3c5c50e2c040a801cfcca0a4ac50000571046", "domain": ".rednote.com"},
            {"name": "web_session", "value": "040069b2c3d4e5f6001122334455", "domain": ".rednote.com"},
        ],
        "origins": [
            {
                "origin": "https://www.rednote.com",
                "localStorage": [
                    {"name": "b1", "value": "REAL_REDNOTE_B1_FROM_ORIGIN"},
                    {"name": "dsllt", "value": "1795070176379"},
                    {"name": "sc", "value": "3"},
                ],
            }
        ],
    })
    ident = Identity(account_id=77, platform="rednote", profile_dir=tmp_path / "rn_api_prof")
    submit_marks = 0

    def _mark():
        nonlocal submit_marks
        submit_marks += 1

    ok, result_url, err = asyncio.run(
        publish_rednote(
            None,
            ident,
            state_with_b1,
            "images",
            "B" * 45,
            "Pure API post body",
            [str(img_path)],
            topics="Coffee",
            mode="api",
            on_submit=_mark,
        )
    )
    assert ok is True, err
    assert err == ""
    assert result_url == "https://www.rednote.com/zh/discovery/item/67a1b2c3d4e5f60000000123"
    assert submit_marks == 1
    assert all("xiaohongshu.com" not in u for _, u, _ in sent_requests)
    note_posts = [(m, u, h) for m, u, h in sent_requests if "/web_api/sns/v2/note" in u]
    assert len(note_posts) == 1
    assert note_posts[0][1] == "https://webapi.rednote.com/web_api/sns/v2/note"
    assert note_posts[0][2]["x-s"].startswith("XYS_")
    assert note_posts[0][2].get("x-rap-param")


def test_rednote_hybrid_comments_fallback_on_http_461(local_project, monkeypatch):
    from app.engine import collection as col_mod
    from app.engine.collection import KeywordCollector

    signer = RednoteSigner()
    comment_hdrs = signer.sign_headers(
        "GET",
        "/api/sns/web/v2/comment/page",
        "a1=19ab1e5ce48c3b3c5c50e2c040a801cfcca0a4ac50000571046",
        params={"note_id": "6af7eba3000000002b0065cb", "cursor": ""},
    )
    assert comment_hdrs["x-s"].startswith("XYS_")

    local_project.cfg.engine.rednote_read_mode = "hybrid"
    col_engine = KeywordCollector(local_project.cfg, local_project.browser, None)
    rn_client = RednoteApiClient(
        "a1=19ab1e5ce48c3b3c5c50e2c040a801cfcca0a4ac50000571046; web_session=ws_ok",
        "Mozilla/5.0 Chrome/138.0.0.0",
    )

    api_calls = 0

    async def _fake_get(uri, params=None, **_kw):
        nonlocal api_calls
        api_calls += 1
        raise RednoteApiError(
            "触发验证码/风控(HTTP 461),请稍后再试",
            category="risk",
            status_code=461,
            signal="http_461",
        )

    monkeypatch.setattr(rn_client, "_get", _fake_get)

    browser_calls = 0

    async def _fake_browser_comments(_mgr, _ident, note_id, *_args, **_kw):
        nonlocal browser_calls
        browser_calls += 1
        return (
            [
                {
                    "id": f"c_{note_id}_1",
                    "content": "RTX 5070 7280 可以入手",
                    "create_time": 1795070000000,
                    "like_count": "5",
                    "user_info": {"user_id": "u_commenter", "nickname": "装机老哥"},
                    "sub_comments": [
                        {
                            "id": f"c_{note_id}_sub1",
                            "content": "同问哪家店",
                            "create_time": 1795070100000,
                            "like_count": "1",
                            "user_info": {"user_id": "u_sub", "nickname": "路人甲"},
                            "target_comment": {"id": f"c_{note_id}_1"},
                        }
                    ],
                }
            ],
            "",
        )

    monkeypatch.setattr(col_mod, "fetch_rednote_comments", _fake_browser_comments)
    ident = Identity(account_id=88, platform="rednote", profile_dir=local_project.root / "rn_col")

    # Note #1 hits HTTP 461 on API -> marks _comment_api_blocked and falls back to browser cleanly
    c1, err1 = asyncio.run(
        col_engine._rednote_comments(
            ident,
            "6af7eba3000000002b0065cb",
            "TOK_1",
            "pc_search",
            20,
            True,
            rednote_client=rn_client,
        )
    )
    assert err1 == ""
    assert len(c1) == 2
    assert rn_client._comment_api_blocked is True
    assert api_calls == 1
    assert browser_calls == 1

    # Note #2 skips sending another HTTP 461 network call and goes straight to browser fallback
    c2, err2 = asyncio.run(
        col_engine._rednote_comments(
            ident,
            "6af7eba3000000002b0065cc",
            "TOK_2",
            "pc_search",
            20,
            True,
            rednote_client=rn_client,
        )
    )
    assert err2 == ""
    assert len(c2) == 2
    assert api_calls == 1
    assert browser_calls == 2


def test_rednote_https_cdn_upgrade_and_downloader_502_fallback(tmp_path: Path, monkeypatch):
    from contextlib import asynccontextmanager
    from app.engine.downloader import Downloader
    from app.platforms.rednote.extract import upgrade_rednote_media_https

    assert (
        upgrade_rednote_media_https("http://sns-web-i10.rednotecdn.com/202611191512/9840ae9c4b/img!nc")
        == "https://sns-web-i10.rednotecdn.com/202611191512/9840ae9c4b/img!nc"
    )
    card = {
        "note_id": "6af9520900000000180269aa",
        "type": "normal",
        "title": "5060Ti到手了",
        "user": {"nickname": "测试作者"},
        "image_list": [
            {
                "info_list": [
                    {"image_scene": "WB_DFT", "url": "http://sns-web-i10.rednotecdn.com/202611191512/bad/img1!nc"},
                    {"image_scene": "WB_PRV", "url": "http://sns-webpic-qc.xhscdn.com/202611191512/ok/img1_prv!nc"},
                ],
                "url_default": "http://sns-web-i1.rednotecdn.com/202611191512/ok/img1_def!nc",
            }
        ],
    }
    aw = parse_rednote_detail(card)
    assert aw is not None
    assert aw.medias[0].url == "https://sns-web-i10.rednotecdn.com/202611191512/bad/img1!nc"
    assert aw.medias[0].backup_urls == [
        "https://sns-webpic-qc.xhscdn.com/202611191512/ok/img1_prv!nc",
        "https://sns-web-i1.rednotecdn.com/202611191512/ok/img1_def!nc",
    ]

    # Verify Downloader upgrades http->https, sets Connection: close on 502, and falls back to backup_urls
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_kw: asyncio.Future())

    async def _fast_sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    dl = Downloader(str(tmp_path), "Mozilla/5.0")
    stream_calls: list[tuple[str, dict]] = []

    class FakeStreamResp:
        def __init__(self, code: int, body: bytes = b""):
            self.status_code = code
            self._body = body
            self.headers = {"content-length": str(len(body))} if body else {}

        async def aiter_bytes(self, _chunk_size: int):
            if self._body:
                yield self._body

    class FakeHttpClient:
        @asynccontextmanager
        async def stream(self, _method: str, url: str, headers: dict | None = None):
            stream_calls.append((url, dict(headers or {})))
            if "sns-web-i10" in url:
                yield FakeStreamResp(502)
            else:
                yield FakeStreamResp(200, b"\x89PNG_FAKE_BYTES")

    dest = tmp_path / "out.jpeg"
    err = asyncio.run(
        dl._download_one(
            FakeHttpClient(),
            "http://sns-web-i10.rednotecdn.com/202611191512/bad/img1!nc",
            dest,
            backup_urls=aw.medias[0].backup_urls,
        )
    )
    assert err == ""
    assert dest.read_bytes() == b"\x89PNG_FAKE_BYTES"
    assert all(u.startswith("https://") for u, _ in stream_calls)
    assert stream_calls[1][1].get("Connection") == "close"
    assert stream_calls[-1][0] == "https://sns-webpic-qc.xhscdn.com/202611191512/ok/img1_prv!nc"


def test_rednote_creator_monitor_hybrid_fallback_on_http_461(local_project, monkeypatch):
    from app.engine import monitor as mon_mod
    from app.models import MonitorTarget

    local_project.cfg.engine.rednote_read_mode = "hybrid"
    local_project.cfg.risk_control.operation_gap_min_seconds = 0
    local_project.cfg.risk_control.operation_gap_max_seconds = 0
    engine = MonitorEngine(local_project.cfg, local_project.browser)

    with db.get_session() as s:
        acc = DouyinAccount(
            platform="rednote",
            nickname="丹麦Happy",
            status="active",
            storage_state=json.dumps({
                "cookies": [
                    {"name": "a1", "value": "19ab1e5ce48c3b3c5c50e2c040a801cfcca0a4ac50000571046", "domain": ".rednote.com"},
                    {"name": "web_session", "value": "040069b2c3d4e5f6001122334455", "domain": ".rednote.com"},
                ]
            }),
        )
        s.add(acc)
        s.commit()
        s.refresh(acc)
        target = MonitorTarget(
            platform="rednote",
            target_kind="creator",
            sec_uid="5604f1e6e4b1000000000001",
            url="https://www.rednote.com/zh/user/profile/5604f1e6e4b1000000000001",
            account_id=acc.id,
            download_enabled=False,
        )
        s.add(target)
        s.commit()
        s.refresh(target)
        target_id = target.id

    class FakeHybridRednoteClient:
        @asynccontextmanager
        async def session_scope(self):
            yield self

        async def notes_by_creator(self, *_a, **_kw):
            raise RednoteApiError(
                "触发验证码/风控(HTTP 461),请稍后再试",
                category="risk",
                status_code=461,
                signal="http_461",
            )

        async def note_detail(self, note_id: str, **_kw):
            return {
                "note_id": note_id,
                "type": "normal",
                "title": "哥本哈根日常",
                "user": {"nickname": "创作者A"},
                "image_list": [
                    {"url_default": "http://sns-web-i10.rednotecdn.com/202611191520/abc/pic1!nc"}
                ],
                "interact_info": {"liked_count": "12", "comment_count": "3"},
            }

    async def _fake_fetch_notes(_mgr, _ident, _uid, _known, **_kw):
        return (
            [
                {
                    "note_id": "67a1b2c3d4e5f60000000777",
                    "xsec_token": "TOK_777",
                    "type": "normal",
                    "display_title": "哥本哈根日常",
                }
            ],
            {"basic_info": {"nickname": "创作者A"}},
            "",
        )

    monkeypatch.setattr(engine, "_rednote_client", lambda *_a, **_kw: FakeHybridRednoteClient())
    monkeypatch.setattr(mon_mod, "fetch_rednote_notes", _fake_fetch_notes)

    res = asyncio.run(engine.scan_target(target_id))
    assert res["ok"] is True, res
    assert res["new"] == 1
    assert not res.get("error")


def test_rednote_page_failure_ignores_note_caption_false_positive_and_comment_watch_scans(
    local_project, monkeypatch
):
    from app.browser.rednote_fetcher import _rednote_page_failure
    from app.engine import monitor as mon_mod
    from app.models import CommentWatch

    class FakeLoadedNotePage:
        url = "https://www.rednote.com/zh/discovery/item/6ad71d74000000002a00da73?xsec_token=TOK"

        async def evaluate(self, _js):
            # Simulates a normal note page whose caption contains "安全验证" / "操作频繁"
            return {
                "hasContent": True,
                "captchaUi": "",
                "loginModal": "",
                "toastText": "",
                "bodyShort": "",
            }

    assert asyncio.run(_rednote_page_failure(FakeLoadedNotePage())) == ""

    class FakeToastRiskPage:
        url = "https://www.rednote.com/zh/discovery/item/6ad71d74000000002a00da73"

        async def evaluate(self, _js):
            return {
                "hasContent": True,
                "captchaUi": "",
                "loginModal": "",
                "toastText": "请求太频繁，请稍后再试",
                "bodyShort": "",
            }

    assert asyncio.run(_rednote_page_failure(FakeToastRiskPage())).startswith("captcha:")

    local_project.cfg.engine.rednote_read_mode = "hybrid"
    local_project.cfg.risk_control.operation_gap_min_seconds = 0
    local_project.cfg.risk_control.operation_gap_max_seconds = 0
    engine = MonitorEngine(local_project.cfg, local_project.browser)

    with db.get_session() as s:
        acc = DouyinAccount(
            platform="rednote",
            nickname="丹麦Happy",
            status="active",
            storage_state=json.dumps({
                "cookies": [
                    {"name": "a1", "value": "19ab1e5ce48c3b3c5c50e2c040a801cfcca0a4ac50000571046", "domain": ".rednote.com"},
                    {"name": "web_session", "value": "040069b2c3d4e5f6001122334455", "domain": ".rednote.com"},
                ]
            }),
        )
        s.add(acc)
        s.commit()
        s.refresh(acc)
        watch = CommentWatch(
            platform="rednote",
            kind="video",
            aweme_id="6ad71d74000000002a00da73",
            xsec_token="TOK_CW",
            mode="public",
            account_id=acc.id,
            title="笔记 6ad71d74000000002a00da73",
        )
        s.add(watch)
        s.commit()
        s.refresh(watch)
        watch_id = watch.id

    class FakeWatchRednoteClient:
        @asynccontextmanager
        async def session_scope(self):
            yield self

        async def note_detail(self, note_id: str, **_kw):
            return {
                "note_id": note_id,
                "type": "normal",
                "title": "小韩KR美俩月发力了",
                "desc": "人只有在坐高铁或者坐飞机的时候才会触发安全验证",
                "user": {"nickname": "椰椰早点睡"},
            }

        async def note_comments(self, *_a, **_kw):
            raise RednoteApiError(
                "海外小红书接口 /api/sns/web/v2/comment/page 需页面实时签名(HTTP 461)，建议切换混合/浏览器读取",
                category="risk",
                status_code=461,
                signal="http_461",
            )

    async def _fake_fetch_comments(_mgr, _ident, _nid, _known, **_kw):
        return (
            [
                {
                    "id": "c_cw_1",
                    "content": "求分享医院名字！",
                    "create_time": 1763500000000,
                    "like_count": "6",
                    "user_info": {"user_id": "u_cw_1", "nickname": "小丸子"},
                }
            ],
            "",
        )

    monkeypatch.setattr(engine, "_rednote_client", lambda *_a, **_kw: FakeWatchRednoteClient())
    monkeypatch.setattr(mon_mod, "fetch_rednote_comments", _fake_fetch_comments)

    res = asyncio.run(engine.scan_comment_watch(watch_id))
    assert res["ok"] is True, res
    assert res["new_comments"] == 1
    with db.get_session() as s:
        updated = s.get(CommentWatch, watch_id)
        assert updated is not None
        assert "小韩KR美" in updated.title


def test_rednote_publish_visibility_handles_multiple_selects_d_text_and_english_ui():
    from app.platforms.rednote.publish import _set_publish_visibility

    class _FakeInteraction:
        async def pause(self, *_a, **_kw):
            return None

        async def click_visible(self, locator, **_kw):
            await locator.click()

    class _FakeNode:
        def __init__(self, text: str, cls: str = "", visible: bool = True, on_click=None):
            self.text = text
            self.cls = cls
            self.visible = visible
            self._on_click = on_click

        async def is_visible(self):
            return self.visible

        async def inner_text(self):
            return self.text

        async def get_attribute(self, name: str):
            return self.cls if name == "class" else None

        async def scroll_into_view_if_needed(self, **_kw):
            return None

        async def click(self):
            if self._on_click:
                self._on_click()

    class _FakeList:
        def __init__(self, items):
            self._items = list(items)

        async def count(self):
            return len(self._items)

        def nth(self, idx: int):
            return self._items[idx]

        @property
        def first(self):
            return self._items[0]

    # Case 1: Video publish form has "选择合集" at index 0, "公开可见" at index 1,
    # and dropdown option uses `@xhscdn/d-design` `class="d-text --color-text-title"`
    state = {"open": False}
    collection_select = _FakeNode("选择合集", cls="d-select-description", visible=True)
    vis_select = _FakeNode(
        "公开可见",
        cls="d-select-description",
        visible=True,
        on_click=lambda: state.update(open=True),
    )
    priv_option = _FakeNode(
        "仅自己可见",
        cls="d-text --color-text-title",
        visible=False,
        on_click=lambda: setattr(vis_select, "text", "仅自己可见"),
    )

    class _FakePublishPage:
        def locator(self, sel: str):
            if sel == ".d-select-description":
                return _FakeList([collection_select, vis_select])
            return _FakeList([])

        def get_by_text(self, label: str, exact: bool = False):
            priv_option.visible = state["open"]
            matches = []
            for node in (collection_select, vis_select, priv_option):
                if (exact and node.text == label) or (not exact and label in node.text):
                    matches.append(node)
            return _FakeList(matches)

    asyncio.run(_set_publish_visibility(_FakePublishPage(), _FakeInteraction(), "private"))
    assert vis_select.text == "仅自己可见"
    assert collection_select.text == "选择合集"

    # Case 2: English Creator Center ("Everyone" -> "Only me")
    en_state = {"open": False}
    en_select = _FakeNode(
        "Everyone",
        cls="d-select-description",
        visible=True,
        on_click=lambda: en_state.update(open=True),
    )
    en_priv = _FakeNode(
        "Only me",
        cls="d-option-item",
        visible=False,
        on_click=lambda: setattr(en_select, "text", "Only me"),
    )

    class _FakeEnPage:
        def locator(self, sel: str):
            return _FakeList([en_select] if sel == ".d-select-description" else [])

        def get_by_text(self, label: str, exact: bool = False):
            en_priv.visible = en_state["open"]
            matches = [
                n for n in (en_select, en_priv)
                if (exact and n.text == label) or (not exact and label in n.text)
            ]
            return _FakeList(matches)

    asyncio.run(_set_publish_visibility(_FakeEnPage(), _FakeInteraction(), "private"))
    assert en_select.text == "Only me"
