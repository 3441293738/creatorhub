"""Rednote (海外小红书) domain isolation, capabilities, and login heuristics.

Grounded in the verified Rednote adapter specification:
  - Separate 4-domain configuration (`creator.rednote.com`, `webapi.rednote.com`,
    `www.rednote.com`, `rednote.com`), never sharing cookies or hosts with
    domestic `xiaohongshu.com`.
  - Explicit capability boundaries: image/video publishing, works sync, note
    deletion, visibility toggling, public search/creator/favorites collection,
    works monitoring, and link download are enabled; unverified domestic-only
    modules (RWP IM WebSocket, automated comment/DM plans, Qianfan/Pugongying
    commerce orders, danmaku, follow/fan lists, daily creator statics) remain
    explicitly disabled.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

PLATFORM_KEY = "rednote"
PLATFORM_TITLE = "海外小红书"
PLATFORM_SUBTITLE = "Rednote"

CREATOR_DOMAIN = "creator.rednote.com"
EDIT_DOMAIN = "webapi.rednote.com"
WWW_DOMAIN = "www.rednote.com"
COOKIE_ROOT = "rednote.com"

CREATOR_URL = f"https://{CREATOR_DOMAIN}"
WEBAPI_URL = f"https://{EDIT_DOMAIN}"
WWW_URL = f"https://{WWW_DOMAIN}"

HOME_PAGE_URL = f"{CREATOR_URL}/new/note-manager"
CREATOR_LOGIN_URL = f"{CREATOR_URL}/login"
PUBLISH_URL = f"{CREATOR_URL}/publish/publish?source=official"
# Note: unprefixed /explore and /login on www.rednote.com 302-redirect to
# domestic www.xiaohongshu.com via Tencent EdgeOne CDN. Locale-prefixed /zh
# stays on www.rednote.com with HTTP 200 and full Rednote web login/SSR state.
LOGIN_PAGE_URL = f"{WWW_URL}/zh"
FRONT_HOME_PAGE_URL = f"{WWW_URL}/zh"

SHIELD_WEBPROFILE_URL = "https://as.rednote.com/api/sec/v1/shield/webprofile"
QRCODE_CREATE_URL = f"{WEBAPI_URL}/api/sns/web/v1/login/qrcode/create"
QRCODE_STATUS_API = "/api/sns/web/v1/login/qrcode/status"
USER_ME_API = "/api/sns/web/v2/user/me"

AVATAR_CDN_PREFIX = "https://sns-avatar-qc.xhscdn.com/avatar/"
AVATAR_WEBP_SUFFIX = "imageView2/2/w/360/format/webp"
LOGOUT_VIDEO_MARKER = "/a00472903313b9a87fedd3a69a854e37c6037a8a.mp4_video_original_mp4"

# DOM helpers for detecting/switching Rednote login modal to QR mode and reading QR src.
SHOW_QRCODE_SCRIPT = """() => {
  var qrImg = document.querySelector('.login-container img.qrcode-img, .login-modal img.qrcode-img, .login-box-container img.qrcode-img');
  if (qrImg && qrImg.getAttribute('src')) {
    return 1;
  }
  var _templ_el = document.querySelector('.css-wemwzq');
  if (_templ_el) {
    _templ_el.click();
    return 1;
  }
  return 0;
}"""

GET_QRCODE_SCRIPT = """() => {
  var imgs = document.querySelectorAll('.login-box-container img');
  return (imgs && imgs.length > 1 && imgs[1].src) ? imgs[1].src : '';
}"""

REDNOTE_CAPABILITIES: Mapping[str, bool] = MappingProxyType({
    "can_post_pic": True,
    "can_post_video": True,
    "can_statics": False,
    "publish": True,
    "published_list": True,
    "account_works": True,
    "delete_work": True,
    "update_visibility": True,
    "collection": True,
    "monitor": True,
    "comment_read": True,
    "share_download": True,
    "comment_write": False,
    "auto_comment": False,
    "dm": False,
    "commerce": False,
    "danmaku": False,
    "follows": False,
})


def is_rednote_host(host: str | None) -> bool:
    """Return True iff ``host`` is ``rednote.com`` or a subdomain of it."""
    value = str(host or "").strip().lower().rstrip(".")
    return bool(value) and (value == COOKIE_ROOT or value.endswith(f".{COOKIE_ROOT}"))


def is_rednote_url(url: str | None) -> bool:
    """Return True iff ``url`` is an HTTP(S) URL on ``rednote.com``."""
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and is_rednote_host(parsed.hostname)


def rednote_listener_decision(url: str, method: str = "GET") -> dict[str, Any] | None:
    """Classify a webview/page request URL using Rednote's listener rules.

    Note: ``method`` is accepted for call-signature parity even though only
    ``url`` drives the state transition; heuristic signals indicate login/QR
    transitions and must still be paired with session/identity verification.
    """
    _ = method
    target = str(url or "")
    if not target:
        return None
    if AVATAR_CDN_PREFIX in target and AVATAR_WEBP_SUFFIX in target:
        return {
            "frontLogin": True,
            "redirectTo": HOME_PAGE_URL,
            "waitRedirect": True,
        }
    if LOGOUT_VIDEO_MARKER in target:
        return {
            "logout": True,
            "redirectTo": FRONT_HOME_PAGE_URL,
        }
    if target.startswith(SHIELD_WEBPROFILE_URL):
        return {"login": True}
    if target.startswith(QRCODE_CREATE_URL):
        return {"frontLogout": True, "qrcode": True}
    return None
