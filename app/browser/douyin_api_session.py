"""Passively synchronize a verified account page with its direct API session."""
from __future__ import annotations

import asyncio
import inspect
import time
from urllib.parse import parse_qs, urlsplit

from ..platforms.douyin.auth import parse_self_profile_response
from ..platforms.douyin.detail import parse_detail_response
from ..platforms.douyin.web_session import DETAIL_ORIGINS, DETAIL_PATH, observed_detail_context
from .storage_state import capture_context_storage_state


_ENVIRONMENT_JS = """() => ({
  ua: navigator.userAgent,
  fp: [innerWidth, innerHeight, outerWidth, outerHeight, screenX, screenY, 0, 0,
       screen.width, screen.height, screen.availWidth, screen.availHeight,
       innerWidth, innerHeight, screen.colorDepth, screen.pixelDepth,
       navigator.platform].join('|')
})"""


def observe_douyin_api_session(context, identity, callback):
    """Observe existing page traffic only; never trigger a fetch or navigation.

    The callback must match the authoritative self profile to the selected DB
    account before saving anything. Public work/author data is not auth proof.
    """
    profile = None
    recipe = {}
    lock = asyncio.Lock()
    last_saved = 0.0
    pending = set()

    async def on_response(response):
        nonlocal profile, recipe, last_saved
        try:
            parsed = urlsplit(response.url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in DETAIL_ORIGINS:
                return
            is_self = parsed.path == "/aweme/v1/web/user/profile/self/"
            is_detail = parsed.path == DETAIL_PATH
            if not (is_self or is_detail):
                return
            if response.status != 200:
                if is_self or response.status in {401, 403, 429}:
                    profile = None
                    recipe = {}
                return
            data = await response.json()
            if is_self:
                profile, _ = parse_self_profile_response(data)
                if not profile:
                    recipe = {}
                    return
            else:
                aweme_id = parse_qs(parsed.query).get("aweme_id", [""])[0]
                if not aweme_id or not parse_detail_response(data, aweme_id)[0]:
                    return
                headers = await response.request.all_headers()
                environment = await response.request.frame.page.evaluate(_ENVIRONMENT_JS)
                candidate = observed_detail_context(response.url, headers,
                    ua=str(environment.get("ua") or identity.ua),
                    fingerprint=str(environment.get("fp") or ""))
                if recipe.get("saved") and all(candidate.get(k) == recipe.get(k)
                        for k in ("origin", "params", "headers", "ua", "fingerprint")):
                    candidate["saved"] = True
                recipe = candidate
            if not profile:
                return
            async with lock:
                if not is_self and time.monotonic() - last_saved < 2:
                    # A newly observed recipe must still be persisted even if
                    # the self response arrived immediately before the detail.
                    if not recipe or recipe.get("saved"):
                        return
                state = await capture_context_storage_state(context, platform="douyin")
                result = callback(identity, profile, state, recipe)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    last_saved = time.monotonic()
                    if recipe:
                        recipe["saved"] = True
        except Exception:
            # Do not expose request URLs, headers, cookies or exception strings.
            return

    async def tracked_response(response):
        task = asyncio.current_task()
        pending.add(task)
        try:
            await on_response(response)
        finally:
            pending.discard(task)

    async def drain_pending():
        tasks = [task for task in pending if task is not asyncio.current_task() and not task.done()]
        if tasks:
            # Finish already-observed cookie/context saves before the caller
            # closes a one-shot task page. Do not initiate any new requests.
            await asyncio.wait(tasks, timeout=2.0)

    tracked_response.drain_pending = drain_pending
    context.on("response", tracked_response)
    return tracked_response
