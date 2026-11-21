"""交互式浏览器登录公共焦点控制、首次环境自检页与昵称读取辅助。"""
from __future__ import annotations

import logging

from app.browser.identity import Identity
from app.browser.manager import BrowserManager


_LOG = logging.getLogger(__name__)


# 登录后才会出现的 Cookie(用于判断是否已登录)
_LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}


async def _focus(page):
    """把扫码窗口提到前台(否则有头浏览器常开在其它窗口后面)。"""
    try:
        await page.bring_to_front()
    except Exception:
        pass


async def _open_first_environment_check(
        mgr: BrowserManager, identity: Identity, ctx) -> None:
    """Best-effort first-run diagnostic; never block the platform login flow."""
    opener = getattr(mgr, "open_environment_check", None)
    if not callable(opener):
        return
    try:
        await opener(identity, context=ctx)
    except Exception as exc:
        _LOG.warning("Fingerprint environment check page failed to open: %r", exc)


async def _reuse_or_create_login_page(ctx):
    """兼容旧调用：复用持久 Context 的空白页，否则创建一个新页。"""
    for candidate in ctx.pages:
        try:
            if candidate.url == "about:blank" and not candidate.is_closed():
                return candidate
        except Exception:
            continue
    return await ctx.new_page()


async def _read_ks_nickname(page) -> str:
    for sel in ('.profile-user-name', '[class*="user-name"]', '[class*="userName"]',
                '.user-name', 'span.name'):
        try:
            t = await page.inner_text(sel, timeout=1500)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""


async def _read_xhs_nickname(page) -> str:
    for sel in ('.user .name', '.reds-avatar + * .name', 'span.name'):
        try:
            t = await page.inner_text(sel, timeout=1500)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""


async def _read_nickname(page) -> str:
    # 新版抖音会把登录用户写进 localStorage；优先读它，避免三个 DOM 选择器
    # 逐个超时（旧实现最坏会额外等待 4.5 秒）。
    try:
        nickname = await page.evaluate(
            """() => {
              for (const store of [window.localStorage, window.sessionStorage]) {
                for (const key of ['user_info', 'userInfo', 'user_info_passport']) {
                  try {
                    const raw = store.getItem(key);
                    if (!raw) continue;
                    let value = JSON.parse(raw);
                    if (typeof value === 'string') value = JSON.parse(value);
                    const name = value && (value.nickname || value.name);
                    if (name) return String(name).trim().slice(0, 40);
                  } catch (_) {}
                }
              }
              return '';
            }"""
        )
        if nickname and str(nickname).strip():
            return str(nickname).strip()[:40]
    except Exception:
        pass

    for sel in ('[data-e2e="user-info-nickname"]', 'span.nickname',
                '[data-e2e="live-avatar"] + * span'):
        try:
            t = await page.inner_text(sel, timeout=600)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""
