import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlmodel import select

import app.db as db
import app.main as main
from app.browser.tiktok_fetcher import fetch_tiktok_self_profile
from app.browser.account_hub import _norm_tiktok_work
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.models import CommentTask, DouyinAccount, PublishTask


@pytest.fixture()
def tiktok_account():
    previous_engine = db._engine
    with tempfile.TemporaryDirectory() as root:
        db.init_db(str(Path(root) / "tiktok-integration.db"))
        with db.get_session() as session:
            account = DouyinAccount(
                platform="tiktok", nickname="TikTok Test",
                status="active", storage_state='{"cookies": []}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id
        try:
            yield account_id
        finally:
            if db._engine is not None:
                db._engine.dispose()
            db._engine = previous_engine


def test_tiktok_collection_validation_is_keyword_only(tiktok_account):
    platform, keywords, _, _, options = main._validated_collection_input(
        main.KeywordCollectionIn(
            platform="tiktok", account_id=tiktok_account,
            keywords=["creator tools"], source_type="keyword",
        )
    )
    assert platform == "tiktok"
    assert keywords == ["creator tools"]
    assert options["source_type"] == "keyword"

    with pytest.raises(HTTPException, match="TikTok 首版批量采集仅支持关键词视频搜索"):
        main._validated_collection_input(
            main.KeywordCollectionIn(
                platform="tiktok", account_id=tiktok_account,
                source_type="creator", source_ref="creator-id",
            )
        )


def test_tiktok_user_search_route_uses_selected_logged_in_account(tiktok_account):
    fake_browser = SimpleNamespace(identity_for=lambda account: ("identity", account.id))
    fetch = AsyncMock(return_value=([{
        "uid": "1", "sec_uid": "sec", "unique_id": "alice",
        "nickname": "Alice", "follower_count": 12,
    }], ""))
    with patch.object(main, "browser", fake_browser), patch.object(
            main, "fetch_tiktok_users", fetch):
        result = asyncio.run(main.search_tiktok_users(
            main.TikTokUserSearchIn(
                account_id=tiktok_account, keyword="alice", max_results=20,
            )
        ))

    assert result["items"][0]["unique_id"] == "alice"
    assert result["error"] == ""
    fetch.assert_awaited_once_with(
        fake_browser, ("identity", tiktok_account), "alice",
        max_results=20, block_media=main.cfg.engine.block_media_resources,
    )


def test_tiktok_protocol_probe_uses_visible_authorized_context(tiktok_account):
    lease = AsyncMock()
    lease.__aenter__ = AsyncMock(return_value="context")
    lease.__aexit__ = AsyncMock(return_value=None)
    fake_browser = SimpleNamespace(
        identity_for=lambda account: ("identity", account.id),
        temporary_headed_context=lambda _identity: lease,
    )
    probe = AsyncMock(return_value={
        "ok": True, "mode": "browser_api", "signer": "page_runtime",
    })
    with patch.object(main, "browser", fake_browser), patch.object(
            main, "probe_tiktok_protocol", probe):
        result = asyncio.run(main.tiktok_protocol_probe(
            main.TikTokProtocolProbeIn(
                account_id=tiktok_account, keyword="claude", timeout_ms=18000,
                mode="browser",
            )
        ))
    assert result["ok"] is True
    probe.assert_awaited_once_with(
        fake_browser, ("identity", tiktok_account), "claude", timeout_ms=18000)
    lease.__aenter__.assert_awaited_once()
    lease.__aexit__.assert_awaited_once()


def test_tiktok_protocol_probe_native_mode_does_not_open_browser(tiktok_account):
    identity = Identity(
        account_id=tiktok_account, profile_dir="profile", platform="tiktok",
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0.0.0",
    )
    fake_browser = SimpleNamespace(
        identity_for=lambda _account: identity,
        temporary_headed_context=AsyncMock(),
    )
    native = SimpleNamespace(
        items=[{"itemStruct": {"id": "731", "author": {"uniqueId": "alice"}}}],
        error="", category="success", http_status=200, body_length=1234,
        tt_orcas_res="", signature_lengths={"X-Dynosaur": 396, "msToken": 0,
                                            "X-Bogus": 1, "X-Gnarly": 324},
    )
    client = SimpleNamespace(search=AsyncMock(return_value=native))
    with patch.object(main, "browser", fake_browser), patch.object(
            main, "TikTokNativeClient", return_value=client):
        result = asyncio.run(main.tiktok_protocol_probe(
            main.TikTokProtocolProbeIn(
                account_id=tiktok_account, keyword="claude", mode="native",
            )
        ))
    assert result["ok"] is True
    assert result["signer"] == "offline_python"
    assert result["sample_item_ids"] == ["731"]
    fake_browser.temporary_headed_context.assert_not_called()


def test_tiktok_self_profile_reads_hydrated_logged_in_user():
    page = SimpleNamespace(
        goto=AsyncMock(), wait_for_timeout=AsyncMock(),
        evaluate=AsyncMock(return_value={
            "user": {
                "uid": "1", "secUid": "sec", "uniqueId": "alice",
                "nickname": "Alice",
            },
            "userStats": {"followerCount": 7, "followingCount": 4, "videoCount": 2},
        }),
        locator=lambda _selector: SimpleNamespace(inner_text=AsyncMock(return_value="")),
        close=AsyncMock(), url="https://www.tiktok.com/",
    )
    manager = SimpleNamespace(new_page=AsyncMock(return_value=page))
    profile, error = asyncio.run(fetch_tiktok_self_profile(manager, "identity"))
    assert error == ""
    assert profile["unique_id"] == "alice"
    assert profile["following_count"] == 4
    page.goto.assert_awaited_once()
    page.close.assert_awaited_once()


def test_tiktok_account_work_normalizes_rendered_profile_card():
    work = _norm_tiktok_work({
        "item": {
            "id": "731", "desc": "own post", "createTime": 1700000000,
            "author": {"uniqueId": "alice"},
            "stats": {"diggCount": 5, "commentCount": 2},
            "video": {"cover": {"urlList": ["https://img/c.jpg"]}},
        }
    })
    assert work["item_id"] == "731"
    assert work["like_count"] == 5
    assert work["cover_url"] == "https://img/c.jpg"


def test_tiktok_comment_route_creates_pending_reply_task(tiktok_account):
    result = asyncio.run(main.create_tiktok_comment_task(
        main.TikTokCommentTaskIn(
            account_id=tiktok_account,
            aweme_id="https://www.tiktok.com/@alice/video/731",
            content="Thanks", target_comment_id="c-1",
            target_text="Original comment", target_nick="Alice",
        )
    ))

    assert result["platform"] == "tiktok"
    assert result["status"] == "pending"
    with db.get_session() as session:
        task = session.exec(select(CommentTask).where(
            CommentTask.id == result["id"])).one()
        assert task.aweme_id == "731"
        assert task.target_url == "https://www.tiktok.com/@alice/video/731"
        assert task.target_comment_id == "c-1"
        assert task.target_text == "Original comment"


def test_tiktok_ui_exposes_supported_features_and_video_publish():
    root = Path(__file__).parents[1]
    html = (root / "app" / "web" / "index.html").read_text(encoding="utf-8")
    js = (root / "app" / "web" / "app.js").read_text(encoding="utf-8")

    assert 'data-pf="tiktok"' in html
    assert 'class="tk-only" onclick="startTikTokLogin()"' in html
    assert 'data-tab="hub"' in html
    assert '<span class="nav-label">本账号管理</span>' in html
    assert 'data-tab="tiktok-tools"' in html
    assert 'id="tk-user-results"' in html
    assert 'id="tk-comment-content"' in html
    assert 'id="tk-author-id"' in html
    assert '"/api/tiktok/users/search"' in js
    assert '"/api/tiktok/comment-tasks"' in js
    assert 'target_sec_uid: user.unique_id || ""' in js
    assert 'function pfHasPublish(pf)' in js
    publish_body = js.split("function pfHasPublish(pf)", 1)[1].split("}", 1)[0]
    assert '"tiktok"' in publish_body
    assert 'id="pub-tiktok-opts"' in html
    assert 'id="pub-tk-comment"' in html
    assert 'id="pub-tk-duet"' in html
    assert 'id="pub-tk-stitch"' in html
    assert '上传视频到 TikTok Studio' in js
    assert 'class="navitem dy-only" data-tab="danmaku"' in html
    assert 'class="navitem pub-only" data-tab="publish"' in html
    for tab in ("monitors", "comments"):
        assert f'class="navitem notsh-only" data-tab="{tab}"' in html
        assert f'class="navitem notsh-only tk-hide" data-tab="{tab}"' not in html
    assert 'class="navitem notsh-only tk-hide" data-tab="autocomment"' in html
    assert 'class="navitem tk-hide" data-tab="hub"' not in html
    assert 'TikTok 用户搜索与互动' in html
    assert 'TikTok 手动评论任务' in html
    assert '代理已绑定（未检测）' in js
    assert 'rotating: "代理正常（轮换出口）"' in js
    assert '["ok", "rotating", "drifted"].includes(a.proxy_status)' in js
    assert 'if (PLATFORM === "tiktok" && CURRENT_TAB === "autocomment")' in js
    main_source = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert "await browser.close_context(identity.key)" in main_source


def test_tiktok_publish_route_accepts_one_video_and_persists_permissions(tiktok_account):
    with tempfile.TemporaryDirectory() as root:
        video = Path(root) / "clip.mp4"
        video.write_bytes(b"fixture-video")
        result = asyncio.run(main.add_publish(main.PublishIn(
            account_id=tiktok_account,
            media_type="video",
            media_paths=[str(video)],
            title="Launch",
            desc="Creator update",
            topics="tools,news",
            visibility="friends",
            allow_comment=False,
            allow_duet=False,
            allow_stitch=True,
        )))

        assert result["platform"] == "tiktok"
        assert result["media_type"] == "video"
        assert result["visibility"] == "friends"
        assert result["allow_comment"] is False
        assert result["allow_duet"] is False
        assert result["allow_stitch"] is True
        with db.get_session() as session:
            task = session.get(PublishTask, result["id"])
            assert json.loads(task.media_json) == [str(video)]


def test_tiktok_publish_route_rejects_photo_posts(tiktok_account):
    with tempfile.TemporaryDirectory() as root:
        image = Path(root) / "photo.jpg"
        image.write_bytes(b"fixture-image")
        with pytest.raises(HTTPException, match="仅支持视频") as caught:
            asyncio.run(main.add_publish(main.PublishIn(
                account_id=tiktok_account,
                media_type="images",
                media_paths=[str(image)],
            )))
        assert caught.value.status_code == 422


def test_tiktok_write_contract_keeps_uncertain_terminal_state():
    actions = Path("app/browser/tiktok_actions.py").read_text(encoding="utf-8")
    monitor = Path("app/engine/monitor.py").read_text(encoding="utf-8")
    assert "write_uncertain:评论已提交但未取得评论 ID" in actions
    assert "write_uncertain:私信已提交" in actions
    assert 't.status = "uncertain"' in monitor
    assert "t.scheduled_at = None" in monitor


def test_tiktok_headed_context_is_registered_for_background_reuse():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            manager = BrowserManager("UA", tmp)
            close_handlers = []
            context = SimpleNamespace(
                pages=[], close=AsyncMock(),
                on=lambda event, callback: close_handlers.append(callback)
                if event == "close" else None)
            manager._launch_persistent = AsyncMock(return_value=context)
            manager.close_context = AsyncMock()
            manager.effective_browser_backend = lambda _identity: "local"
            manager.proxy_signature = lambda _proxy: "proxy-signature"
            manager._context_signature = lambda *_args: "context-signature"
            identity = Identity(
                account_id=6,
                profile_dir=str(Path(tmp) / "account-6"),
                platform="tiktok",
                proxy="http://user:pass@proxy.example:8080",
            )

            with patch("app.browser.manager.capture_window_snapshot", return_value=set()), \
                    patch("app.browser.manager.bring_window_to_front", return_value=True):
                opened = await manager.open_headed(identity)

            assert opened is context
            assert manager._contexts[identity.key] is context
            assert manager._proxy_signature_by_key[identity.key] == "context-signature"
            assert manager._backend_by_key[identity.key] == "patchright"
            manager.close_context.assert_awaited_once_with(identity.key)
            assert close_handlers
            close_handlers[-1]()
            assert identity.key not in manager._contexts
            assert identity.key not in manager._proxy_signature_by_key

    asyncio.run(scenario())


def test_tiktok_keyword_collection_uses_native_first_and_browser_fallback():
    source = Path("app/engine/collection.py").read_text(encoding="utf-8")
    tiktok_branch = source.split('if job.platform == "tiktok":', 1)[1]
    tiktok_branch = tiktok_branch.split('if job.platform == "shipinhao":', 1)[0]
    assert 'tiktok_read_mode' in tiktok_branch
    assert 'if mode == "browser":' in tiktok_branch
    assert "temporary_headed_context(identity)" in tiktok_branch
    assert "启动 TikTok 离线签名 API 采集" in tiktok_branch
    assert "identity=identity, context=None" in tiktok_branch


def test_tiktok_native_mode_routes_comments_without_browser_fetcher():
    source = Path("app/engine/collection.py").read_text(encoding="utf-8")
    body = source.split("async def _tiktok_comments", 1)[1]
    body = body.split("@staticmethod", 1)[0]
    assert 'mode in {"auto", "native"}' in body
    assert "await client.comments(" in body
    assert 'or mode == "native"' in body


def test_tiktok_empty_preview_response_still_triggers_visible_search():
    source = Path("app/browser/tiktok_fetcher.py").read_text(encoding="utf-8")
    body = source.split("async def fetch_tiktok_search", 1)[1]
    body = body.split("async def fetch_tiktok_profile_works", 1)[0]
    assert "if not collected:" in body
    assert "if not collected and not seen_responses:" not in body
    assert "await _trigger_video_search(page, keyword)" in body


def test_native_ua_never_persists_headless_marker():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            manager = BrowserManager("UA", tmp)
            persisted = []
            manager._native_ua_callback = lambda account_id, ua: persisted.append(
                (account_id, ua))
            page = SimpleNamespace(
                evaluate=AsyncMock(return_value=(
                    "Mozilla/5.0 Chrome/156.0.0.0 HeadlessChrome/156.0.0.0 Safari/537.36")))
            context = SimpleNamespace(pages=[page])
            identity = Identity(account_id=6, profile_dir=str(Path(tmp) / "p"))

            actual = await manager._capture_context_ua(context, identity)

            assert "HeadlessChrome" not in actual
            assert "HeadlessChrome" not in identity.ua
            assert persisted == [(6, actual)]

    asyncio.run(scenario())
