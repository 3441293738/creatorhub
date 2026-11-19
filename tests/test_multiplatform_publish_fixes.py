"""Multi-platform browser & API publish regression tests."""
from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlmodel import select

import app.db as db
import app.main as main
from app.browser.identity import Identity
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import ContentRecord, DouyinAccount, PublishTask, RiskEvent
from app.platforms.channels.publish import _parse_channels_post_create, publish_channels
from app.platforms.douyin.publish import (
    _parse_create_payload,
    _parse_passport_decision,
    publish_douyin,
)
from app.platforms.kuaishou.publish import _is_kuaishou_submit_path, publish_kuaishou
from app.platforms.tiktok.publish import publish_tiktok
from app.platforms.xhs import creator_api
from app.platforms.xhs.browser_writes import publish_xhs_browser


class _FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        enabled: bool = True,
        text: str = "",
        attrs: dict | None = None,
        on_click=None,
        accept_fill: bool = True,
    ):
        self.visible = visible
        self.enabled = enabled
        self.text = text
        self.attrs = dict(attrs or {})
        self.on_click = on_click
        self.accept_fill = accept_fill
        self.files = None
        self.value = ""
        self.clicks = 0

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if (self.visible or self.on_click is not None or self.files is not None or self.attrs.get("attached")) else 0

    def nth(self, index):
        return self

    async def is_visible(self, **_kwargs):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def wait_for(self, **_kwargs):
        if not self.visible and not self.attrs.get("attached"):
            raise TimeoutError("not visible")

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        self.clicks += 1
        if self.on_click is not None:
            res = self.on_click(self)
            if asyncio.iscoroutine(res):
                await res

    async def fill(self, text, **_kwargs):
        if not self.accept_fill:
            raise RuntimeError("contenteditable does not support fill")
        self.value = text

    async def inner_text(self, **_kwargs):
        return self.text

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def set_input_files(self, files, **_kwargs):
        self.files = list(files)

    async def check(self, **_kwargs):
        await self.click()


class _EmptyLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def is_visible(self, **_kwargs):
        return False


class _FakeResponse:
    def __init__(self, url: str, payload=None, *, method: str = "POST", status: int = 200, headers: dict | None = None):
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.request = SimpleNamespace(method=method)
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakePageBase:
    def __init__(self, url: str):
        self.url = url
        self.closed = False
        self._listeners: dict[str, list] = {}
        self.keyboard = SimpleNamespace(
            typed=[],
            pressed=[],
            type=AsyncMock(side_effect=lambda txt, **_kw: self.keyboard.typed.append(txt)),
            insert_text=AsyncMock(side_effect=lambda txt: self.keyboard.typed.append(txt)),
            press=AsyncMock(side_effect=lambda key: self.keyboard.pressed.append(key)),
        )
        self.frames = [self]

    def on(self, event, listener):
        self._listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        if event in self._listeners and listener in self._listeners[event]:
            self._listeners[event].remove(listener)

    async def emit_response(self, resp: _FakeResponse):
        for fn in list(self._listeners.get("response", [])):
            res = fn(resp)
            if asyncio.iscoroutine(res):
                await res

    async def goto(self, url, **_kwargs):
        return None

    async def reload(self, **_kwargs):
        return None

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def screenshot(self, **_kwargs):
        return None

    async def inner_text(self, _sel="body"):
        return ""

    async def evaluate(self, _script):
        return "mock"

    def is_closed(self):
        return self.closed


class _FakeManager:
    def __init__(self, page):
        self.page = page
        self.ctx = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            close=AsyncMock(),
        )

        async def _click(loc):
            return await loc.click()

        async def _type_short(loc, val):
            loc.value = val

        async def _insert_long(loc, val, **_kw):
            loc.value = val

        self.xhs_interaction = SimpleNamespace(
            pause=AsyncMock(),
            click_visible=AsyncMock(side_effect=_click),
            type_short=AsyncMock(side_effect=_type_short),
            insert_long=AsyncMock(side_effect=_insert_long),
            clear_and_type=AsyncMock(),
        )

    async def open_headed(self, _identity):
        return self.ctx

    @asynccontextmanager
    async def visible_page(self, _identity, *, url=""):
        yield self.page


@pytest.fixture(autouse=True)
def _isolate_debug_dir(tmp_path, monkeypatch):
    import app.platforms.douyin.publish as dy_pub
    import app.platforms.kuaishou.publish as ks_pub
    import app.platforms.tiktok.publish as tk_pub

    monkeypatch.setattr(dy_pub, "_DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(ks_pub, "_DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(tk_pub, "_DEBUG_DIR", tmp_path / "debug")


def _identity(tmp: Path, platform: str) -> Identity:
    return Identity(account_id=1, profile_dir=str(tmp / "profile"), platform=platform)


def test_douyin_passport_and_create_payload_parsers():
    decision = {
        "common_params": {
            "copywriting": json.dumps({"title": "账号安全验证", "desc": "请完成短信验证"}).encode("utf-8").decode("latin1")
        },
        "verify_way_name_list": "mobile_sms,qrcode",
    }
    hint = _parse_passport_decision({"X-Tt-Verify-Passport-Decision": json.dumps(decision)})
    assert "账号安全验证" in hint
    assert "mobile_sms" in hint

    v4_decision = {
        "common_params": {"copywriting": json.dumps({
            "title": "身份验证".encode("utf-8").decode("latin1"),
            "desc": "为确保是本人操作，请输入验证码".encode("utf-8").decode("latin1"),
        })},
        "verify_way_name_list": "mobile_sms_verify,qr_code_verify,face_verify",
    }
    v4_hint = _parse_passport_decision({"x-tt-verify-passport-decision": json.dumps(v4_decision)})
    assert "身份验证：为确保是本人操作，请输入验证码" in v4_hint
    assert "è" not in v4_hint
    assert "短信验证、扫码验证、人脸验证" in v4_hint

    assert _parse_create_payload({"status_code": 0, "aweme": {"aweme_id": "74123456789"}}) == (
        True, "74123456789", ""
    )
    assert _parse_create_payload({"status_code": 8, "status_msg": "视频已发布", "item_id": "998877"}) == (
        True, "998877", ""
    )
    assert _parse_create_payload({"status_code": 2154, "status_msg": "标题包含敏感词"}) == (
        False, "", "标题包含敏感词"
    )


def test_douyin_publish_intercepts_create_v2_and_marks_uncertain_on_verify_timeout(tmp_path):
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"img")

    # 1) Happy path with image input accept scoping + aweme/create_v2 interception
    class DyPage(_FakePageBase):
        def __init__(self, *, verify_only=False):
            super().__init__("https://creator.douyin.com/creator-micro/content/upload?default-tab=3")
            self.verify_only = verify_only
            self.img_input = _FakeLocator(attrs={"attached": True})
            self.generic_input = _FakeLocator(attrs={"attached": True})
            self.title_input = _FakeLocator()
            self.desc_input = _FakeLocator()
            self.pub_btn = _FakeLocator(text="发布", on_click=self._on_publish)

        async def _on_publish(self, _loc):
            if self.verify_only:
                await self.emit_response(_FakeResponse(
                    "https://creator.douyin.com/web/api/media/aweme/create_v2/",
                    {"status_code": 4001},
                    headers={"x-tt-verify-passport-decision": json.dumps({"verify_way_name_list": "sms"})},
                ))
            else:
                await self.emit_response(_FakeResponse(
                    "https://creator.douyin.com/web/api/media/aweme/create_v2/",
                    {"status_code": 0, "aweme": {"aweme_id": "7500112233"}},
                ))

        def locator(self, selector):
            if selector == 'input[type="file"][accept*="image"]':
                return self.img_input
            if selector == 'input[type="file"]':
                return self.generic_input
            if "标题" in selector:
                return self.title_input
            if "contenteditable" in selector or "简介" in selector:
                return self.desc_input
            if selector == "button":
                return self.pub_btn
            return _EmptyLocator()

        async def click(self, *_args, **_kwargs):
            return None

        def get_by_role(self, *_args, **_kwargs):
            return _EmptyLocator()

        def get_by_text(self, text, **_kwargs):
            if self.verify_only and text == "短信验证码":
                return _FakeLocator(visible=True, text=text)
            return _EmptyLocator()

    events = []
    page = DyPage(verify_only=False)
    ok, url, err = asyncio.run(publish_douyin(
        _FakeManager(page), _identity(tmp_path, "douyin"), "{}", "images",
        "这是一个超过二十字但是不超过三十字的抖音测试标题啊", "正文", [str(img)],
        topics="户外", timeout_seconds=2,
        on_submit=lambda: events.append("submitted"),
    ))
    assert ok is True and err == ""
    assert url == "https://www.douyin.com/video/7500112233"
    assert events == ["submitted"]
    assert page.img_input.files == [str(img)]
    assert page.generic_input.files is None
    assert len(page.title_input.value) == 25

    # 2) Verification timeout after publish click MUST return write_uncertain
    page2 = DyPage(verify_only=True)
    ok2, _url2, err2 = asyncio.run(publish_douyin(
        _FakeManager(page2), _identity(tmp_path, "douyin"), "{}", "images",
        "标题", "正文", [str(img)], timeout_seconds=1,
        on_submit=lambda: events.append("submitted2"),
    ))
    assert ok2 is False
    assert err2.startswith("write_uncertain:")
    assert "sms" in err2

    # 3) Private visibility unavailable aborts BEFORE on_submit
    pre_events = []
    page3 = DyPage(verify_only=False)
    ok3, _url3, err3 = asyncio.run(publish_douyin(
        _FakeManager(page3), _identity(tmp_path, "douyin"), "{}", "images",
        "标题", "正文", [str(img)], visibility="private", timeout_seconds=1,
        on_submit=lambda: pre_events.append("should-not-fire"),
    ))
    assert ok3 is False
    assert not err3.startswith("write_uncertain:")
    assert pre_events == []
    assert page3.pub_btn.clicks == 0


def test_kuaishou_video_submit_matching_and_submit_boundary(tmp_path):
    assert _is_kuaishou_submit_path("/rest/cp/works/atlas/pc/publish/submit")
    assert _is_kuaishou_submit_path("/rest/cp/works/v2/video/pc/submit")
    assert _is_kuaishou_submit_path("/rest/cp/works/v2/video/pc/isolation/submit")
    assert not _is_kuaishou_submit_path("/rest/cp/works/v2/video/pc/upload/pre")
    assert not _is_kuaishou_submit_path("/rest/cp/works/v2/video/pc/config")

    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"mp4")

    class KsPage(_FakePageBase):
        def __init__(self, *, reject=False):
            super().__init__("https://cp.kuaishou.com/article/publish/video")
            self.reject = reject
            self.file_input = _FakeLocator(attrs={"attached": True})
            self.desc_input = _FakeLocator()
            self.pub_btn = _FakeLocator(text="发布", on_click=self._on_publish)

        async def _on_publish(self, _loc):
            payload = (
                {"result": 100, "error_msg": "触发风控"}
                if self.reject
                else {"result": 1, "data": {"photoId": "3xKUAISHOU999"}}
            )
            await self.emit_response(_FakeResponse(
                "https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit",
                payload,
            ))

        def locator(self, selector):
            if selector.startswith('input[type="file"]'):
                return self.file_input
            if selector == "#work-description-edit":
                return self.desc_input
            if selector == 'div[class*="button-primary"]':
                return self.pub_btn
            return _EmptyLocator()

        def expect_file_chooser(self, **_kwargs):
            raise RuntimeError("fallback to input")

        def get_by_text(self, *_args, **_kwargs):
            return _EmptyLocator()

    marks = []
    p_ok = KsPage(reject=False)
    ok, url, err = asyncio.run(publish_kuaishou(
        _FakeManager(p_ok), _identity(tmp_path, "kuaishou"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=2,
        on_submit=lambda: marks.append("submit"),
    ))
    assert ok is True and err == ""
    assert url == "https://www.kuaishou.com/short-video/3xKUAISHOU999"
    assert marks == ["submit"]

    p_rej = KsPage(reject=True)
    ok2, _url2, err2 = asyncio.run(publish_kuaishou(
        _FakeManager(p_rej), _identity(tmp_path, "kuaishou"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=2,
        on_submit=lambda: marks.append("submit2"),
    ))
    assert ok2 is False
    assert err2.startswith("write_uncertain:")
    assert "触发风控" in err2


def test_channels_publish_upload_wait_no_swallow_post_create_and_verify(tmp_path):
    assert _parse_channels_post_create({"errCode": 0, "data": {"exportId": "export_1"}}) == (
        True, "export_1", ""
    )
    assert _parse_channels_post_create({"baseResp": {"errcode": -1, "errmsg": "拦截"}}) == (
        False, "", "拦截"
    )

    vid = tmp_path / "sph.mp4"
    vid.write_bytes(b"mp4")

    class SphPage(_FakePageBase):
        def __init__(self, *, scenario="ok"):
            super().__init__("https://channels.weixin.qq.com/platform/post/create")
            self.scenario = scenario
            self.poll_count = 0
            self.file_input = _FakeLocator(attrs={"attached": True})
            self.short_title = _FakeLocator()
            self.desc_input = _FakeLocator(accept_fill=False)
            self.pub_btn = _FakeLocator(
                text="发表", enabled=False,
                attrs={"class": "weui-desktop-btn_primary disabled"},
                on_click=self._on_publish,
            )

        async def _on_publish(self, _loc):
            if self.scenario == "ok":
                await self.emit_response(_FakeResponse(
                    "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create",
                    {"errCode": 0, "data": {"exportId": "exp_888"}},
                ))
            elif self.scenario == "face":
                await self.emit_response(_FakeResponse(
                    "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/check_finder_comm_face",
                    {"data": {"blockLevel": 2}},
                ))

        def locator(self, selector):
            if selector == '.post-view .upload input[type="file"]':
                return self.file_input
            if selector == ".post-view .short-title-wrap input":
                return self.short_title
            if selector == ".post-view .post-desc-box .input-editor":
                return _EmptyLocator() if self.scenario == "no_desc" else self.desc_input
            if selector == ".post-view .form-btns button.weui-desktop-btn_primary":
                self.poll_count += 1
                # Becomes enabled on 2nd poll to verify we don't click disabled publish
                if self.poll_count >= 2:
                    self.pub_btn.enabled = True
                    self.pub_btn.attrs["class"] = "weui-desktop-btn_primary"
                return self.pub_btn
            return _EmptyLocator()

        def get_by_text(self, text, **_kwargs):
            if self.scenario == "upload_fail" and text == "上传失败":
                return _FakeLocator(visible=True, text=text)
            return _EmptyLocator()

    # 1) Normal publish waits for button enabled, clears & fills desc, catches post_create
    submitted = []
    ok, url, err = asyncio.run(publish_channels(
        _FakeManager(SphPage(scenario="ok")), _identity(tmp_path, "shipinhao"), "{}", "video",
        "短标题验证十六字上限超出部分裁掉", "视频号描述", [str(vid)],
        topics="日常", timeout_seconds=4,
        on_submit=lambda: submitted.append("channels"),
    ))
    assert ok is True and err == ""
    assert url.endswith("/platform/post/list")
    assert submitted == ["channels"]

    # 2) Upload error fails BEFORE filling or clicking publish
    pre_marks = []
    ok_up, _url_up, err_up = asyncio.run(publish_channels(
        _FakeManager(SphPage(scenario="upload_fail")), _identity(tmp_path, "shipinhao"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=2,
        on_submit=lambda: pre_marks.append("no"),
    ))
    assert ok_up is False
    assert "上传失败" in err_up
    assert not err_up.startswith("write_uncertain:")
    assert pre_marks == []

    # 3) Missing desc fails BEFORE submit (never swallows exception to publish blank)
    ok_nd, _url_nd, err_nd = asyncio.run(publish_channels(
        _FakeManager(SphPage(scenario="no_desc")), _identity(tmp_path, "shipinhao"), "{}", "video",
        "标题", "必须有描述", [str(vid)], timeout_seconds=2,
        on_submit=lambda: pre_marks.append("no"),
    ))
    assert ok_nd is False
    assert "描述输入框" in err_nd
    assert not err_nd.startswith("write_uncertain:")
    assert pre_marks == []

    # 4) Face verification + timeout returns write_uncertain with hint
    ok_face, _url_face, err_face = asyncio.run(publish_channels(
        _FakeManager(SphPage(scenario="face")), _identity(tmp_path, "shipinhao"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=1,
        on_submit=lambda: submitted.append("face"),
    ))
    assert ok_face is False
    assert err_face.startswith("write_uncertain:")
    assert "管理员扫码/过脸验证" in err_face


def test_xhs_empty_title_with_accepted_api_and_creator_biz_binds(tmp_path):
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"img")

    # Check _biz_binds(post_time_ms) produces valid JSON
    parsed = json.loads(creator_api._biz_binds(1700000000000))
    assert parsed["bizType"] == 13
    assert parsed["notePostTiming"]["postTime"] == "1700000000000"

    class XhsPage(_FakePageBase):
        def __init__(self, *, has_note_id: bool, has_vis_ctrl: bool):
            super().__init__("https://creator.xiaohongshu.com/publish/publish")
            self.has_note_id = has_note_id
            self.has_vis_ctrl = has_vis_ctrl
            self.file_input = _FakeLocator(visible=False, attrs={"attached": True})
            self.body_input = _FakeLocator()
            self.vis = _FakeLocator(visible=has_vis_ctrl, text="公开可见")
            self.pub = _FakeLocator(text="发布", on_click=self._click_pub)

        async def _click_pub(self, _loc):
            if self.has_note_id:
                await self.emit_response(_FakeResponse(
                    "https://edith.xiaohongshu.com/web_api/sns/v2/note",
                    {"success": True, "data": {"id": "note_6677"}},
                ))
            self.url = "https://creator.xiaohongshu.com/publish/success"

        def locator(self, selector):
            if selector == 'input[type="file"]':
                return self.file_input
            if selector == ".d-select-description":
                return self.vis if self.has_vis_ctrl else _EmptyLocator()
            if "contenteditable" in selector or selector == "textarea":
                return self.body_input
            if "发布" in selector or "submit" in selector:
                return self.pub
            return _EmptyLocator()

        def get_by_text(self, text, **_kwargs):
            if text in {"发布成功", "发布完成"} and "success" in self.url:
                return _FakeLocator(visible=True, text=text)
            return _EmptyLocator()

    # Empty title + API accepted -> success with explore URL (even if visibility control missing for public)
    p = XhsPage(has_note_id=True, has_vis_ctrl=False)
    outcome = asyncio.run(publish_xhs_browser(
        _FakeManager(p), _identity(tmp_path, "xhs"), "images",
        "", "纯正文图集", [], [str(media)], visibility="public", timeout_seconds=2,
    ))
    assert outcome.status == "success"
    assert outcome.result == "https://www.xiaohongshu.com/explore/note_6677"

    # Non-public visibility with missing control still fails cleanly before submit
    p2 = XhsPage(has_note_id=True, has_vis_ctrl=False)
    outcome2 = asyncio.run(publish_xhs_browser(
        _FakeManager(p2), _identity(tmp_path, "xhs"), "images",
        "", "纯正文图集", [], [str(media)], visibility="private", timeout_seconds=2,
    ))
    assert outcome2.status == "failed"
    assert p2.pub.clicks == 0


def test_tiktok_uploaded_banner_does_not_false_positive_while_form_open(tmp_path):
    vid = tmp_path / "tt.mp4"
    vid.write_bytes(b"mp4")

    class TkPage(_FakePageBase):
        def __init__(self):
            super().__init__("https://www.tiktok.com/creator-center/upload?lang=en")
            self.file_input = _FakeLocator(visible=False, attrs={"attached": True})
            self.caption = _FakeLocator(visible=True)
            self.pub = _FakeLocator(text="Post", visible=True, on_click=self._on_click)
            self.polls = 0

        async def _on_click(self, _loc):
            return None

        def locator(self, selector):
            if 'input[type="file"]' in selector:
                return self.file_input
            if "contenteditable" in selector or "textarea" in selector:
                return self.caption
            if "button" in selector:
                return self.pub
            return _EmptyLocator()

        def get_by_text(self, text, **_kwargs):
            # Pre-publish banner shows "Your video has been uploaded" while form stays open!
            if text == "Your video has been uploaded":
                self.polls += 1
                if self.polls >= 3:
                    # Form disappears on 3rd poll -> real completion
                    self.caption.visible = False
                    self.pub.visible = False
                return _FakeLocator(visible=True, text=text)
            return _EmptyLocator()

    page = TkPage()
    ok, url, err = asyncio.run(publish_tiktok(
        _FakeManager(page), _identity(tmp_path, "tiktok"), "{}", "video",
        "Title", "Desc", [str(vid)], timeout_seconds=5,
    ))
    assert ok is True and err == ""
    assert url == "https://www.tiktok.com/tiktokstudio/content"
    assert page.polls >= 3


@pytest.fixture()
def isolated_engine(tmp_path):
    previous = db._engine
    db.init_db(str(tmp_path / "monitor-fixes.db"))
    cfg = Config()
    cfg.risk_control.enabled = True
    cfg.engine.quiet_hours_enabled = False
    cfg.engine.native_write_gate_enabled = False
    browser = SimpleNamespace(
        lock_for=lambda _key: asyncio.Lock(),
        identity_for=lambda acc: Identity(account_id=acc.id, profile_dir=str(tmp_path / f"acc-{acc.id}"), platform=acc.platform),
        native_write_gate_error=lambda *_args, **_kwargs: "",
    )
    engine = MonitorEngine(cfg, browser)
    try:
        yield cfg, engine, tmp_path
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = previous


@pytest.mark.parametrize("platform,patch_name", [
    ("douyin", "app.engine.monitor.publish_douyin"),
    ("kuaishou", "app.engine.monitor.publish_kuaishou"),
    ("shipinhao", "app.engine.monitor.publish_channels"),
])
def test_monitor_wires_submit_boundary_for_douyin_kuaishou_and_channels(isolated_engine, platform, patch_name):
    _cfg, engine, tmp_path = isolated_engine
    media = tmp_path / "item.mp4"
    media.write_bytes(b"video-data")
    with db.get_session() as session:
        acc = DouyinAccount(platform=platform, nickname="Test", status="active", storage_state='{"cookies":[]}')
        session.add(acc); session.commit(); session.refresh(acc)
        task = PublishTask(
            platform=platform, account_id=acc.id, media_type="video",
            title="t", desc="d", media_json=json.dumps([str(media)]),
        )
        session.add(task); session.commit(); session.refresh(task)
        task_id = task.id

    async def post_submit_verify_timeout(*_args, **kwargs):
        kwargs["on_submit"]()
        with db.get_session() as s:
            assert s.get(PublishTask, task_id).error == "write_submitted:browser"
        return False, "", "write_uncertain:验证超时"

    with patch(patch_name, post_submit_verify_timeout):
        result = asyncio.run(engine.publish_task(task_id))

    assert result["ok"] is False
    with db.get_session() as session:
        saved = session.get(PublishTask, task_id)
        assert saved.status == "uncertain"
        assert saved.scheduled_at is None
        assert session.exec(select(RiskEvent)).all() == []


def test_api_title_caps_empty_file_validation_and_repost_routes(isolated_engine):
    _cfg, engine, tmp_path = isolated_engine
    prev_engine = main.engine
    main.engine = engine
    try:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        empty = tmp_path / "empty.jpg"
        empty.write_bytes(b"")

        with db.get_session() as s:
            dy = DouyinAccount(platform="douyin", nickname="DY", status="active", storage_state="{}")
            ks = DouyinAccount(platform="kuaishou", nickname="KS", status="active", storage_state="{}")
            tk = DouyinAccount(platform="tiktok", nickname="TK", status="active", storage_state="{}")
            s.add(dy); s.add(ks); s.add(tk); s.commit()
            s.refresh(dy); s.refresh(ks); s.refresh(tk)
            content = ContentRecord(
                platform="douyin", target_id=0, aweme_id="w1", desc="raw",
                media_type="video", download_status="done", local_path=str(clip),
            )
            s.add(content); s.commit(); s.refresh(content)
            dy_id, ks_id, tk_id, cid = dy.id, ks.id, tk.id, content.id

        # Douyin keeps up to 30 chars via /api/publish and PUT /api/publish/{tid}
        long_title = "12345678901234567890123456789012345"
        created = asyncio.run(main.add_publish(main.PublishIn(
            account_id=dy_id, media_type="video", title=long_title, media_paths=[str(clip)],
        )))
        assert len(created["title"]) == 30
        updated = asyncio.run(main.update_publish(created["id"], main.PublishUpdate(title=long_title)))
        assert len(updated["title"]) == 30

        # Empty 0-byte file rejected cleanly
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.add_publish(main.PublishIn(
                account_id=ks_id, media_type="images", media_paths=[str(empty)],
            )))
        assert exc.value.status_code == 422

        # New repost-kuaishou and repost-tiktok endpoints work
        r_ks = asyncio.run(main.repost_to_kuaishou(cid, main.RepostIn(account_id=ks_id)))
        r_tk = asyncio.run(main.repost_to_tiktok(cid, main.RepostIn(account_id=tk_id)))
        assert r_ks["ok"] is True and r_tk["ok"] is True

        # has_creator includes tiktok
        accounts = asyncio.run(main.list_accounts(platform="tiktok"))
        assert accounts[0]["has_creator"] is True
    finally:
        main.engine = prev_engine


@pytest.mark.parametrize("platform,patch_name", [
    ("douyin", "app.engine.monitor.publish_douyin"),
    ("kuaishou", "app.engine.monitor.publish_kuaishou"),
    ("shipinhao", "app.engine.monitor.publish_channels"),
])
def test_monitor_cancellation_marks_uncertain_for_douyin_kuaishou_channels(
        isolated_engine, platform, patch_name):
    _cfg, engine, tmp_path = isolated_engine
    media = tmp_path / "item.mp4"
    media.write_bytes(b"video-data")
    with db.get_session() as session:
        acc = DouyinAccount(platform=platform, nickname="CancelTest", status="active", storage_state='{"cookies":[]}')
        session.add(acc); session.commit(); session.refresh(acc)
        task = PublishTask(
            platform=platform, account_id=acc.id, media_type="video",
            title="t", desc="d", media_json=json.dumps([str(media)]),
        )
        session.add(task); session.commit(); session.refresh(task)
        task_id = task.id

    async def cancelled_run(*_args, **_kwargs):
        raise asyncio.CancelledError()

    with patch(patch_name, cancelled_run):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.publish_task(task_id))

    with db.get_session() as session:
        saved = session.get(PublishTask, task_id)
        assert saved.status == "uncertain"
        assert saved.error.startswith("write_uncertain:")


def test_xhs_api_unmatched_topic_and_ui_publish_parity(tmp_path):
    sent = []

    def fake_post(_url, **kwargs):
        sent.append(json.loads(kwargs["data"].decode("utf-8")))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"success": True, "data": {"id": "note_topic_1"}},
        )

    mock_session = SimpleNamespace(post=fake_post, close=lambda: None)
    with patch("app.platforms.xhs.creator_api.Session", return_value=mock_session), \
            patch("app.platforms.xhs.creator_sign.generate_xs_xs_common", return_value=("xs", 1, "common")), \
            patch("app.platforms.xhs.creator_sign.generate_x_rap_param", return_value="rap"):
        api = creator_api.XhsCreatorApi("a1=fixture")
        api.upload_media = lambda *_args: {"fileIds": "f1", "width": 100, "height": 100}
        api.get_topic = lambda _kw: None
        ok, _msg, body = api.post_note(
            media_type="image", title="标题", desc="正文",
            image_files=[b"jpegbytes"], topics=["露营", "户外"],
        )
    assert ok is True
    assert body["data"]["id"] == "note_topic_1"
    assert "#露营" in sent[0]["common"]["desc"] and "#户外" in sent[0]["common"]["desc"]

    html = Path("app/web/index.html").read_text(encoding="utf-8")
    js = Path("app/web/app.js").read_text(encoding="utf-8")
    assert 'id="pub-title-label-text"' in html
    assert '<fieldset class="publish-permissions" id="pub-dy-opts">' in html
    assert '<div class="publish-permission dy-only">' in html
    assert '上传图集 / 视频到视频号助手(实验性)' in js
    assert 'const titleCap = bili ? 80 : dy ? 30 : sph ? 16 : 20;' in js
    assert '["douyin", "xhs", "kuaishou"].includes(PLATFORM)' in js


def test_douyin_rejects_false_positive_keywords_disabled_button_and_blocked_toast(tmp_path):
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"mp4")

    class StaticBadgePage(_FakePageBase):
        """Upload page already showing '已发布'/'审核中' text without create_v2 or redirect."""

        def __init__(self, *, disabled_btn=False, blocked_text="", confirm_manage=False):
            super().__init__("https://creator.douyin.com/creator-micro/content/post/video")
            self.disabled_btn = disabled_btn
            self.blocked_text = blocked_text
            self.confirm_manage = confirm_manage
            self.goto_urls = []
            self.vid_input = _FakeLocator(attrs={"attached": True})
            self.title_input = _FakeLocator()
            self.desc_input = _FakeLocator()
            self.guide_btn = _FakeLocator(visible=True, text="我知道了")
            self.btn = _FakeLocator(
                text="发布",
                attrs={"class": "semi-button semi-button-disabled"} if disabled_btn else {},
            )

        async def goto(self, url, **_kwargs):
            self.goto_urls.append(url)
            if "content/manage" in url:
                self.url = url

        def locator(self, selector):
            if selector == 'input[type="file"][accept*="video"]':
                return self.vid_input
            if "标题" in selector:
                return self.title_input
            if "contenteditable" in selector or "简介" in selector:
                return self.desc_input
            if selector == "button":
                return self.btn
            return _EmptyLocator()

        def get_by_role(self, *_args, **_kwargs):
            return _EmptyLocator()

        def get_by_text(self, text, exact=False):
            # Mirror Douyin's real static/background page copy that caused false hangs/positives:
            # 1) "点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。"
            # 2) "Ai智能推荐封面生成中..." and "检测中5%" in helper panel
            if not exact and text in ("上传中", "发布完成", "已发布", "审核中"):
                return _FakeLocator(visible=True, text="点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。")
            if not exact and text in ("Ai智能推荐封面生成中", "AI智能推荐封面生成中", "检测中"):
                return _FakeLocator(visible=True, text=f"{text}...")
            if text == "我知道了":
                return self.guide_btn
            if self.blocked_text and text == self.blocked_text:
                return _FakeLocator(visible=True, text=text)
            if self.confirm_manage and text == "发布成功" and "post/video" in self.url:
                return _FakeLocator(visible=True, text=text)
            if self.confirm_manage and "豌豆射手" in text and "content/manage" in self.url:
                return _FakeLocator(visible=True, text=text)
            return _EmptyLocator()

    # 1) Static footer tip + AI cover generation + helper check MUST NOT hang or mark ok=True
    stages = []
    p1 = StaticBadgePage()
    ok1, url1, err1 = asyncio.run(publish_douyin(
        _FakeManager(p1), _identity(tmp_path, "douyin"), "{}", "video",
        "豌豆射手", "测试", [str(vid)], timeout_seconds=2,
        on_stage=stages.append,
    ))
    assert p1.guide_btn.clicks >= 1
    assert p1.title_input.value == "豌豆射手"
    assert p1.btn.clicks == 1
    assert any("已点击「发布」" in s for s in stages)
    assert ok1 is False and url1 == ""
    assert err1.startswith("write_uncertain:")

    # 2) semi-button-disabled button is never clicked (pre-submit nobtn failure)
    p2 = StaticBadgePage(disabled_btn=True)
    ok2, _url2, err2 = asyncio.run(publish_douyin(
        _FakeManager(p2), _identity(tmp_path, "douyin"), "{}", "video",
        "豌豆射手", "测试", [str(vid)], timeout_seconds=2,
    ))
    assert ok2 is False and p2.btn.clicks == 0
    assert not err2.startswith("write_uncertain:")

    # 3) Blocking toast like "请选择封面" fails with clear write_uncertain diagnostic
    p3 = StaticBadgePage(blocked_text="请选择封面")
    ok3, _url3, err3 = asyncio.run(publish_douyin(
        _FakeManager(p3), _identity(tmp_path, "douyin"), "{}", "video",
        "豌豆射手", "测试", [str(vid)], timeout_seconds=4,
    ))
    assert ok3 is False
    assert err3.startswith("write_uncertain:") and "请选择封面" in err3

    # 4) Toast "发布成功" without postId triggers active /content/manage verification
    p4 = StaticBadgePage(confirm_manage=True)
    ok4, url4, err4 = asyncio.run(publish_douyin(
        _FakeManager(p4), _identity(tmp_path, "douyin"), "{}", "video",
        "豌豆射手", "测试", [str(vid)], timeout_seconds=8,
    ))
    assert ok4 is True and err4 == ""
    assert "content/manage" in url4
    assert any("content/manage" in u for u in p4.goto_urls)


def test_douyin_verify_stage_propagation_and_20s_retry_fallback(isolated_engine):
    _cfg, engine, tmp_path = isolated_engine
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"mp4")

    class VerifyEmptyBodyPage(_FakePageBase):
        def __init__(self, *, emit_verify=True):
            super().__init__("https://creator.douyin.com/creator-micro/content/post/video")
            self.emit_verify = emit_verify
            self.vid_input = _FakeLocator(attrs={"attached": True})
            self.title_input = _FakeLocator()
            self.desc_input = _FakeLocator()
            self.clicks = 0

            async def _on_click(_btn):
                self.clicks += 1
                if self.emit_verify and self.clicks == 1:
                    async def _empty_json():
                        raise ValueError("empty body")

                    resp = SimpleNamespace(
                        url="https://creator.douyin.com/web/api/media/aweme/create_v2",
                        status=200,
                        request=SimpleNamespace(method="POST"),
                        headers={
                            "content-length": "0",
                            "x-tt-verify-passport-decision": json.dumps({
                                "common_params": {"copywriting": json.dumps({"title": "短信验证", "desc": "请输入验证码"})},
                                "verify_way_name_list": "mobile_sms_verify,qr_code_verify",
                            }),
                        },
                        json=_empty_json,
                    )
                    await self.emit_response(resp)

            self.btn = _FakeLocator(text="发布", on_click=_on_click)

        def locator(self, selector):
            if "video" in selector:
                return self.vid_input
            if "标题" in selector:
                return self.title_input
            if "contenteditable" in selector or "简介" in selector:
                return self.desc_input
            if "button" in selector:
                return self.btn
            return _EmptyLocator()

        def get_by_role(self, *_args, **_kwargs):
            return _EmptyLocator()

        def get_by_text(self, *_args, **_kwargs):
            return _EmptyLocator()

    # 1) Empty-body passport decision sets seen=True (no retry click!) and pushes readable verify stage
    verify_stages = []
    pv = VerifyEmptyBodyPage(emit_verify=True)
    ok_v, _url_v, err_v = asyncio.run(publish_douyin(
        _FakeManager(pv), _identity(tmp_path, "douyin"), "{}", "video",
        "豌豆", "测试", [str(vid)], timeout_seconds=22,
        on_stage=verify_stages.append,
    ))
    assert ok_v is False
    assert err_v.startswith("write_uncertain:")
    assert pv.clicks == 1  # Seen verification response -> never re-clicked at 20s
    assert any("需要人工安全验证" in s and "短信验证、扫码验证" in s for s in verify_stages)

    # 2) No create response within 20s triggers single DOM retry fallback
    pr = VerifyEmptyBodyPage(emit_verify=False)
    ok_r, _url_r, err_r = asyncio.run(publish_douyin(
        _FakeManager(pr), _identity(tmp_path, "douyin"), "{}", "video",
        "豌豆", "测试", [str(vid)], timeout_seconds=22,
    ))
    assert ok_r is False and err_r.startswith("write_uncertain:")
    assert pr.clicks == 2  # Initial click + 1 retry click at 20s

    # 3) MonitorEngine exposes transient live_stage via _publish_dict while publishing and clears it afterward
    import app.main as main_mod
    with db.get_session() as session:
        acc = DouyinAccount(platform="douyin", nickname="Test", status="active", storage_state='{"cookies":[]}')
        session.add(acc); session.commit(); session.refresh(acc)
        task = PublishTask(
            platform="douyin", account_id=acc.id, media_type="video",
            title="t", desc="d", media_json=json.dumps([str(vid)]),
        )
        session.add(task); session.commit(); session.refresh(task)
        task_id = task.id

    observed_live = []

    async def fake_douyin(*_args, **kwargs):
        kwargs["on_submit"]()
        kwargs["on_stage"]("需要人工安全验证（可通过 短信验证 完成验证），请在弹出的浏览器窗口里完成短信验证码或原设备扫码")
        with db.get_session() as s:
            row = s.get(PublishTask, task_id)
            assert row.error == "write_submitted:browser"
            observed_live.append(main_mod._publish_dict(row)["live_stage"])
        return False, "", "write_uncertain:验证超时"

    with patch.object(main_mod, "engine", engine), \
            patch("app.engine.monitor.publish_douyin", fake_douyin):
        asyncio.run(engine.publish_task(task_id))
    assert observed_live and "需要人工安全验证" in observed_live[0]
    assert engine.get_publish_stage(task_id) == ""

    js = Path("app/web/app.js").read_text(encoding="utf-8")
    assert "_busyHint" in js and "pub-stage-${t.id}" in js
    assert 'row?.live_stage' in js


def test_kuaishou_joyride_error_detection_and_20s_retry_fallback(tmp_path):
    vid = tmp_path / "ks.mp4"
    vid.write_bytes(b"mp4")

    class KsRefPage(_FakePageBase):
        def __init__(self, *, error_banner="", verify_kw=""):
            super().__init__("https://cp.kuaishou.com/article/publish/video")
            self.error_banner = error_banner
            self.verify_kw = verify_kw
            self.skip_btn = _FakeLocator(visible=True, text="Skip")
            self.file_input = _FakeLocator(attrs={"attached": True})
            self.desc = _FakeLocator()
            self.err_loc = _FakeLocator(visible=bool(error_banner), text=error_banner)
            self.clicks = 0

            async def _on_click(_btn):
                self.clicks += 1

            self.btn = _FakeLocator(text="发布", on_click=_on_click)

        def expect_file_chooser(self, **_kwargs):
            raise RuntimeError("use fallback input")

        def get_by_role(self, *_args, **_kwargs):
            return _EmptyLocator()

        def get_by_text(self, text, exact=False):
            if self.verify_kw and text == self.verify_kw:
                return _FakeLocator(visible=True, text=text)
            return _EmptyLocator()

        def locator(self, selector):
            if "react-joyride-step-0" in selector:
                return self.skip_btn
            if "#main-container .error" in selector and self.error_banner:
                return self.err_loc
            if "input" in selector and "file" in selector:
                return self.file_input
            if "work-description-edit" in selector or "描述" in selector:
                return self.desc
            if "section-btns" in selector or "button-primary" in selector:
                return self.btn
            return _EmptyLocator()

    # 1) Pre-submit #main-container .error fails fast without clicking publish
    p_err = KsRefPage(error_banner="上传失败：时长超出限制")
    ok_e, _u_e, err_e = asyncio.run(publish_kuaishou(
        _FakeManager(p_err), _identity(tmp_path, "kuaishou"), "{}", "video",
        "标题", "正文", [str(vid)], timeout_seconds=4,
    ))
    assert p_err.skip_btn.clicks >= 1
    assert p_err.clicks == 0
    assert ok_e is False and "时长超出限制" in err_e
    assert not err_e.startswith("write_uncertain:")

    # 2) Verification text pushes on_stage and suppresses duplicate retry click
    ks_stages = []
    p_ver = KsRefPage(verify_kw="安全验证")
    ok_v, _u_v, err_v = asyncio.run(publish_kuaishou(
        _FakeManager(p_ver), _identity(tmp_path, "kuaishou"), "{}", "video",
        "标题", "正文", [str(vid)], timeout_seconds=22,
        on_stage=ks_stages.append,
    ))
    assert ok_v is False and err_v.startswith("write_uncertain:")
    assert p_ver.clicks == 1
    assert any("需要人工安全验证" in s and "安全验证" in s for s in ks_stages)

    # 3) No response within 20s triggers single fallback click
    p_retry = KsRefPage()
    ok_r, _u_r, err_r = asyncio.run(publish_kuaishou(
        _FakeManager(p_retry), _identity(tmp_path, "kuaishou"), "{}", "video",
        "标题", "正文", [str(vid)], timeout_seconds=22,
    ))
    assert ok_r is False and err_r.startswith("write_uncertain:")
    assert p_retry.clicks == 2


def test_channels_intercept_dismiss_media_error_and_qr_verify_stage(tmp_path):
    vid = tmp_path / "ch.mp4"
    vid.write_bytes(b"mp4")

    class ChRefPage(_FakePageBase):
        def __init__(self, *, media_err="", qr_title="", emit_logged_out=False):
            super().__init__("https://channels.weixin.qq.com/platform/post/create")
            self.media_err = media_err
            self.qr_title = qr_title
            self.emit_logged_out = emit_logged_out
            self.file_input = _FakeLocator(attrs={"attached": True})
            self.title_input = _FakeLocator()
            self.desc = _FakeLocator()
            self.no_tip_btn = _FakeLocator(visible=True, text="不再提示")
            self.err_loc = _FakeLocator(visible=bool(media_err), text=media_err)
            self.qr_loc = _FakeLocator(visible=bool(qr_title), text=qr_title)
            self.clicks = 0

            async def _on_click(_btn):
                self.clicks += 1

            self.btn = _FakeLocator(text="发表", on_click=_on_click)

        async def goto(self, _url, **_kwargs):
            if self.emit_logged_out:
                await self.emit_response(_FakeResponse(
                    "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_login_code",
                    {"errCode": 0},
                    method="POST",
                ))

        async def evaluate(self, _script):
            return "mock"

        def get_by_text(self, _text, exact=False):
            return _EmptyLocator()

        def locator(self, selector):
            if "intercept-original-dialog" in selector:
                return self.no_tip_btn
            if "media-status-content .error" in selector and self.media_err:
                return self.err_loc
            if "mobile-guide-qr-code" in selector and self.qr_title:
                return self.qr_loc
            if 'input[type="file"]' in selector:
                return self.file_input
            if "标题" in selector:
                return self.title_input
            if "input-editor" in selector:
                return self.desc
            if "form-btns button" in selector:
                return self.btn
            return _EmptyLocator()

    # 1) Media status error fails fast pre-submit
    p_err = ChRefPage(media_err="转码异常，请重新上传")
    ok_e, _u_e, err_e = asyncio.run(publish_channels(
        _FakeManager(p_err), _identity(tmp_path, "shipinhao"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=4,
    ))
    assert p_err.no_tip_btn.clicks >= 1
    assert p_err.clicks == 0
    assert ok_e is False and "转码异常" in err_e
    assert not err_e.startswith("write_uncertain:")

    # 2) QR guide dialog emits verify stage to UI
    ch_stages = []
    p_qr = ChRefPage(qr_title="请用微信扫码验证后继续")
    ok_q, _u_q, err_q = asyncio.run(publish_channels(
        _FakeManager(p_qr), _identity(tmp_path, "shipinhao"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=2,
        on_stage=ch_stages.append,
    ))
    assert p_qr.no_tip_btn.clicks >= 1
    assert p_qr.clicks == 1
    assert ok_q is False and err_q.startswith("write_uncertain:")
    assert any("需要人工安全验证" in s and "微信扫码验证" in s for s in ch_stages)

    # 3) auth_login_code response reports logged_out
    p_lo = ChRefPage(emit_logged_out=True)
    ok_l, _u_l, err_l = asyncio.run(publish_channels(
        _FakeManager(p_lo), _identity(tmp_path, "shipinhao"), "{}", "video",
        "标题", "描述", [str(vid)], timeout_seconds=2,
    ))
    assert ok_l is False and err_l.startswith("logged_out:")


def test_xhs_operator_error_and_461_verify_stage_single_click(tmp_path):
    from app.browser.xhs_selectors import selector_candidates

    img = tmp_path / "xhs.jpg"
    img.write_bytes(b"img")
    prog_names = {c.name for c in selector_candidates("publish.progress")}
    assert "img_upload_area_uploading" in prog_names
    assert "img_list_uploading" in prog_names

    class XhsRefPage(_FakePageBase):
        def __init__(self, *, operator_text="", http_status=200):
            super().__init__("https://creator.xiaohongshu.com/publish/publish?source=official&from=tab_switch&target=image")
            self.operator_text = operator_text
            self.http_status = http_status
            self.file_input = _FakeLocator(attrs={"attached": True})
            self.title = _FakeLocator()
            self.body = _FakeLocator()
            self.op_loc = _FakeLocator(visible=bool(operator_text), text=operator_text)
            self.clicks = 0

            async def _on_click(_btn):
                self.clicks += 1
                if self.http_status >= 400:
                    await self.emit_response(_FakeResponse(
                        "https://edith.xiaohongshu.com/web_api/sns/v2/note",
                        {"success": False, "msg": "captcha"},
                        method="POST",
                        status=self.http_status,
                    ))

            self.btn = _FakeLocator(text="发布", on_click=_on_click)

        def is_closed(self):
            return self.closed

        def locator(self, selector):
            if selector == "xhs-publish-btn":
                return _EmptyLocator()
            if ".operator" in selector and self.operator_text:
                return self.op_loc
            if "upload-input" in selector or 'type="file"' in selector:
                return self.file_input
            if "标题" in selector:
                return self.title
            if "textbox" in selector or "tiptap" in selector or "contenteditable" in selector:
                return self.body
            if "发布" in selector or "submit" in selector:
                return self.btn
            return _EmptyLocator()

        def get_by_text(self, _text, exact=False):
            return _EmptyLocator()

    # 1) Operator error fails fast pre-submit without clicking submit
    res_err = asyncio.run(publish_xhs_browser(
        _FakeManager(XhsRefPage(operator_text="上传失败")),
        _identity(tmp_path, "xhs"),
        "images", "标题", "正文", [], [str(img)], timeout_seconds=4,
    ))
    assert res_err.status == "failed" and "上传失败" in res_err.error

    # 2) HTTP 461 after single click records uncertain + security verify stage, never clicks twice
    xhs_stages = []
    px = XhsRefPage(http_status=461)
    res_461 = asyncio.run(publish_xhs_browser(
        _FakeManager(px),
        _identity(tmp_path, "xhs"),
        "images", "标题", "正文", [], [str(img)], timeout_seconds=2,
        on_stage=xhs_stages.append,
    ))
    assert px.clicks == 1
    assert res_461.status == "uncertain" and "HTTP 461" in res_461.error
    assert any("需要人工安全验证" in s and "461" in s for s in xhs_stages)


def test_xhs_contenteditable_paragraph_newlines_verify_cleanly():
    from unittest.mock import AsyncMock
    from app.browser.xhs_interaction import XhsInteractionPolicy

    class _DomEditor:
        def __init__(self, dom_text):
            self.dom_text = dom_text

        async def input_value(self):
            raise RuntimeError("not an input element")

        async def evaluate(self, _expr):
            return self.dom_text

    async def scenario():
        policy = XhsInteractionPolicy(sleep=AsyncMock())
        await policy._verify_text(
            _DomEditor("稻香代码MV\n\n#代码MV\n"),
            "稻香代码MV\n#代码MV",
        )
        await policy._verify_text(
            _DomEditor("第一段\xa0空格\n\n#标签"),
            "第一段 空格\n#标签",
        )
        for bad in ("旧值\n\n稻香代码MV\n\n#代码MV", "稻香代码MV#代码MV", "错"):
            with pytest.raises(RuntimeError, match="小红书文本输入结果与预期不一致"):
                await policy._verify_text(
                    _DomEditor(bad),
                    "稻香代码MV\n#代码MV",
                )

    asyncio.run(scenario())


def test_xhs_publish_fast_return_and_note_manager_no_reload_loop(tmp_path):
    from app.platforms.xhs.browser_writes import _confirm_published_title

    vid = tmp_path / "xhs.mp4"
    vid.write_bytes(b"mp4")

    class FastApiSuccessPage(_FakePageBase):
        def __init__(self):
            super().__init__("https://creator.xiaohongshu.com/publish/publish?source=official&from=tab_switch&target=video")
            self.file_input = _FakeLocator(attrs={"attached": True})
            self.title = _FakeLocator()
            self.body = _FakeLocator()
            self.goto_calls = []
            self.reload_calls = 0

            async def _on_click(_btn):
                await self.emit_response(_FakeResponse(
                    "https://edith.xiaohongshu.com/web_api/sns/v2/note",
                    {"success": True, "code": 0, "data": {"id": "note_fast_1"}},
                    method="POST",
                    status=200,
                ))
                self.url = "https://creator.xiaohongshu.com/new/note-manager"

            self.btn = _FakeLocator(text="发布", on_click=_on_click)

        async def goto(self, url, **_kwargs):
            self.goto_calls.append(url)
            self.url = url

        async def reload(self, **_kwargs):
            self.reload_calls += 1

        def locator(self, selector):
            if selector == "xhs-publish-btn":
                return _EmptyLocator()
            if "upload-input" in selector or 'type="file"' in selector:
                return self.file_input
            if "标题" in selector:
                return self.title
            if "textbox" in selector or "tiptap" in selector or "contenteditable" in selector:
                return self.body
            if "发布" in selector or "submit" in selector:
                return self.btn
            return _EmptyLocator()

        def get_by_text(self, _text, exact=False):
            return _EmptyLocator()

    p_fast = FastApiSuccessPage()
    res_fast = asyncio.run(publish_xhs_browser(
        _FakeManager(p_fast),
        _identity(tmp_path, "xhs"),
        "video", "稻香代码MV", "稻香代码MV", ["代码MV"], [str(vid)],
        timeout_seconds=10,
    ))
    assert res_fast.status == "success"
    assert res_fast.result == "https://www.xiaohongshu.com/explore/note_fast_1"
    assert p_fast.goto_calls == []
    assert p_fast.reload_calls == 0

    # When fallback note-manager verification is needed, do not cancel the initial
    # SPA fetch with back-to-back reloads on the first several polls.
    class DelayedRenderNoteManagerPage(_FakePageBase):
        def __init__(self):
            super().__init__("https://creator.xiaohongshu.com/new/note-manager")
            self.checks = 0
            self.goto_calls = 0
            self.reloads = 0

        async def goto(self, url, **_kwargs):
            self.goto_calls += 1
            self.url = url

        async def reload(self, **_kwargs):
            self.reloads += 1

        def get_by_text(self, text, exact=False):
            if text == "稻香代码MV" and exact:
                self.checks += 1
                return _FakeLocator(visible=self.checks >= 4, text=text)
            return _EmptyLocator()

    p_delay = DelayedRenderNoteManagerPage()
    mgr = _FakeManager(p_delay)
    confirmed = asyncio.run(_confirm_published_title(
        p_delay, "稻香代码MV", mgr.xhs_interaction, attempts=12, reload_every=10,
    ))
    assert confirmed is True
    assert p_delay.goto_calls == 0
    assert p_delay.reloads == 0
    assert p_delay.checks == 4
