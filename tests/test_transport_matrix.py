import json
from types import SimpleNamespace

from app.config import Config
from app.transport_matrix import (
    build_transport_matrix,
    douyin_client_environment,
    resolve_transport,
)


def account(account_id, *, profile, proxy="", state="fixture", identity_mode="legacy"):
    return SimpleNamespace(
        id=account_id, platform="douyin", nickname=f"account-{account_id}",
        status="active", profile_dir=profile, proxy=proxy,
        storage_state=json.dumps({"cookie": state}), creator_storage_state="",
        ua="Mozilla/5.0 Chrome/152.0.0.0", locale="zh-CN",
        viewport_w=1366, viewport_h=768, fp_accept_languages="",
        identity_mode=identity_mode,
    )


def test_api_only_is_hard_for_supported_and_unsupported_operations():
    cfg = Config()
    cfg.engine.douyin_read_mode = "api"
    cfg.engine.douyin_followers_mode = "api"
    cfg.engine.douyin_dm_sync_mode = "api"
    cfg.engine.douyin_creator_danmaku_mode = "api"
    cfg.engine.douyin_publish_mode = "api"

    comments = resolve_transport(cfg, "douyin", "own_work_comments")
    followers = resolve_transport(cfg, "douyin", "followers_list")
    dm_sync = resolve_transport(cfg, "douyin", "dm_sync")
    creator_danmaku = resolve_transport(cfg, "douyin", "creator_danmaku")
    publish = resolve_transport(cfg, "douyin", "publish")

    assert comments["effective_mode"] == "api"
    assert not comments["opens_browser"]
    assert followers["effective_mode"] == "api"
    assert not followers["opens_browser"]
    assert dm_sync["effective_mode"] == "api"
    assert not dm_sync["opens_browser"]
    assert creator_danmaku["effective_mode"] == "api"
    assert not creator_danmaku["opens_browser"]
    assert publish["effective_mode"] == "unavailable"
    assert not publish["opens_browser"]


def test_hybrid_resolves_browser_only_capability_without_false_api_claim():
    cfg = Config()
    cfg.engine.douyin_read_mode = "hybrid"
    cfg.engine.douyin_followers_mode = "hybrid"

    following = resolve_transport(cfg, "douyin", "following_list")
    followers = resolve_transport(cfg, "douyin", "followers_list")

    assert following["effective_mode"] == "hybrid"
    assert following["fallback"] == "browser_on_confirmed_failure"
    assert followers["effective_mode"] == "hybrid"
    assert followers["fallback"] == "browser_on_confirmed_failure"


def test_matrix_detects_profile_cookie_and_network_collisions_without_secrets():
    cfg = Config()
    accounts = [
        account(1, profile="profiles/shared", proxy="http://proxy-a", state="same"),
        account(2, profile="profiles/shared", proxy="http://proxy-a", state="same"),
        account(3, profile="profiles/unique", proxy="http://proxy-b", state="unique"),
    ]

    matrix = build_transport_matrix(cfg, accounts)
    first, second, third = matrix["accounts"]

    assert not first["profile_isolated"] and not second["profile_isolated"]
    assert not first["credential_isolated"] and not second["credential_isolated"]
    assert not first["network_isolated"] and not second["network_isolated"]
    assert third["profile_isolated"] and third["credential_isolated"]
    assert third["network_isolated"] and third["api_session_isolated"]
    serialized = json.dumps(matrix, ensure_ascii=False)
    assert "http://proxy-a" not in serialized
    assert '"cookie": "same"' not in serialized


def test_direct_environment_uses_account_locale_and_viewport():
    env = douyin_client_environment(SimpleNamespace(
        locale="zh-TW", fp_accept_languages="zh-TW,zh;q=0.8",
        viewport_w=1440, viewport_h=900))
    assert env == {
        "locale": "zh-TW", "accept_language": "zh-TW,zh;q=0.8",
        "screen_width": 1440, "screen_height": 900,
    }
