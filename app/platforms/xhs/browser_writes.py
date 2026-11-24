"""Visible, page-native Xiaohongshu writes with explicit ambiguity semantics."""
from __future__ import annotations

import inspect
import math
import random
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlencode, urlsplit

from ...browser.human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_wait,
)
from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .._submit import notify_stage as _notify_stage
from .media import validate_publish_files
from ...browser.xhs_selectors import (
    candidate_locator,
    find_present,
    find_visible,
    selector_candidates,
    selector_diagnostic,
)


PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
PUBLISH_MANAGER_URL = "https://creator.xiaohongshu.com/new/note-manager"
_SUCCESS_TEXTS = ("发布成功", "发布完成")


def _publish_url(media_type: str) -> str:
    target = "video" if media_type == "video" else "image"
    return f"{PUBLISH_URL}&from=tab_switch&target={target}"


def _node_attributes(node: dict) -> dict[str, str]:
    raw = list(node.get("attributes") or ())
    return {
        str(raw[index]).lower(): str(raw[index + 1])
        for index in range(0, len(raw) - 1, 2)
    }


def _node_children(node: dict):
    yield from node.get("children") or ()
    yield from node.get("shadowRoots") or ()
    content = node.get("contentDocument")
    if isinstance(content, dict):
        yield content


def _node_text(node: dict) -> str:
    value = str(node.get("nodeValue") or "")
    return value + "".join(_node_text(child) for child in _node_children(node))


def _find_publish_component_button(root: dict) -> dict | None:
    """Return the real button even when it lives in a closed shadow root."""
    hosts: list[dict] = []

    def collect(node: dict) -> None:
        if str(node.get("nodeName") or "").upper() in {
            "XHS-PUBLISH-BTN",
            "REDNOTE-PUBLISH-BTN",
        }:
            hosts.append(node)
        for child in _node_children(node):
            collect(child)

    collect(root)
    for host in hosts:
        buttons: list[dict] = []

        def find_buttons(node: dict) -> None:
            if str(node.get("nodeName") or "").upper() == "BUTTON":
                buttons.append(node)
            for child in _node_children(node):
                find_buttons(child)

        for shadow in host.get("shadowRoots") or ():
            find_buttons(shadow)
        if not buttons:
            continue
        enabled = [
            button for button in buttons
            if "disabled" not in _node_attributes(button)
        ] or buttons
        for button in enabled:
            txt_low = _node_text(button).strip().lower()
            if "发布" in txt_low or "publish" in txt_low or "post" in txt_low:
                return button
        return enabled[0]
    return None


def _quad_center(model: dict) -> tuple[float, float] | None:
    box = model.get("model") or {}
    quad = box.get("border") or box.get("content") or ()
    if len(quad) < 8:
        return None
    xs = [float(quad[index]) for index in range(0, 8, 2)]
    ys = [float(quad[index]) for index in range(1, 8, 2)]
    return sum(xs) / 4, sum(ys) / 4


def _sample_quad_point(
    model: dict,
    rng: random.Random | None = None,
) -> tuple[float, float] | None:
    """Sample a 2D truncated Gaussian landing point within the inner 50% of the quad."""
    box = model.get("model") or {}
    quad = box.get("border") or box.get("content") or ()
    if len(quad) < 8:
        return None
    xs = [float(quad[index]) for index in range(0, 8, 2)]
    ys = [float(quad[index]) for index in range(1, 8, 2)]
    cx = sum(xs) / 4.0
    cy = sum(ys) / 4.0
    w = max(0.0, max(xs) - min(xs))
    h = max(0.0, max(ys) - min(ys))
    half_w = w * 0.25
    half_h = h * 0.25
    if half_w < 0.5 or half_h < 0.5:
        return round(cx, 2), round(cy, 2)
    r = rng or DEFAULT_HUMAN_ENGINE.rng
    px = r.gauss(cx, max(0.6, w * 0.10))
    py = r.gauss(cy, max(0.6, h * 0.10))
    px = max(cx - half_w, min(cx + half_w, px))
    py = max(cy - half_h, min(cy + half_h, py))
    return round(px, 2), round(py, 2)


async def _click_closed_shadow_publish_button(page: Any) -> None:
    """Dispatch one trusted human-curved click to the closed-shadow publish button."""
    session = await page.context.new_cdp_session(page)
    try:
        await session.send("DOM.enable")
        document = await session.send(
            "DOM.getDocument", {"depth": -1, "pierce": True})
        button = _find_publish_component_button(document.get("root") or {})
        if button is None:
            raise RuntimeError("新版发布组件的内部按钮尚未就绪")
        backend_node_id = button.get("backendNodeId")
        if backend_node_id is None:
            raise RuntimeError("新版发布组件缺少可点击节点")
        with suppress(Exception):
            await session.send(
                "DOM.scrollIntoViewIfNeeded",
                {"backendNodeId": backend_node_id},
            )
        rng = DEFAULT_HUMAN_ENGINE.rng
        target = _sample_quad_point(
            await session.send("DOM.getBoxModel", {"backendNodeId": backend_node_id}),
            rng=rng,
        )
        if target is None:
            raise RuntimeError("新版发布按钮当前没有可见点击区域")
        tx, ty = target
        sx, sy = DEFAULT_HUMAN_ENGINE._get_cursor(page)
        if math.hypot(tx - sx, ty - sy) < 12.0:
            sx = max(8.0, tx + rng.choice((-1.0, 1.0)) * rng.uniform(48.0, 140.0))
            sy = max(8.0, ty + rng.choice((-1.0, 1.0)) * rng.uniform(24.0, 90.0))

        steps = int(rng.randint(6, 10))
        dx, dy = tx - sx, ty - sy
        dist = max(1.0, math.hypot(dx, dy))
        perp_x, perp_y = -dy / dist, dx / dist
        arc = (
            min(65.0, max(6.0, dist * rng.uniform(0.10, 0.24)))
            * (1.0 if rng.random() < 0.5 else -1.0)
        )
        c1x = sx + dx * rng.uniform(0.24, 0.40) + perp_x * arc
        c1y = sy + dy * rng.uniform(0.24, 0.40) + perp_y * arc
        c2x = sx + dx * rng.uniform(0.64, 0.82) + perp_x * arc * rng.uniform(-0.30, 0.65)
        c2y = sy + dy * rng.uniform(0.64, 0.82) + perp_y * arc * rng.uniform(-0.30, 0.65)

        for step_idx in range(1, steps + 1):
            u = step_idx / steps
            s = u * u * u * (10.0 + u * (-15.0 + 6.0 * u))
            inv = 1.0 - s
            px = (
                inv * inv * inv * sx
                + 3.0 * inv * inv * s * c1x
                + 3.0 * inv * s * s * c2x
                + s * s * s * tx
            )
            py = (
                inv * inv * inv * sy
                + 3.0 * inv * inv * s * c1y
                + 3.0 * inv * s * s * c2y
                + s * s * s * ty
            )
            if step_idx < steps:
                jitter = (1.0 - s) * 0.9
                px += rng.uniform(-jitter, jitter)
                py += rng.uniform(-jitter, jitter)
            else:
                px, py = tx, ty
            await session.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": round(px, 2), "y": round(py, 2),
            })
            await DEFAULT_HUMAN_ENGINE.pause(0.006, 0.018)

        DEFAULT_HUMAN_ENGINE._set_cursor(page, (tx, ty))
        await DEFAULT_HUMAN_ENGINE.pause(0.090, 0.220)
        await session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": tx, "y": ty,
            "button": "left", "clickCount": 1,
        })
        await DEFAULT_HUMAN_ENGINE.pause(0.055, 0.135)
        await session.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": tx, "y": ty,
            "button": "left", "clickCount": 1,
        })
    finally:
        with suppress(Exception):
            await session.detach()


class _PublishWebComponentLocator:
    """Locator facade for the creator-center closed-shadow publish control."""

    def __init__(self, page: Any, host: Any):
        self._page = page
        self._host = host

    def __getattr__(self, name: str):
        return getattr(self._host, name)

    async def is_enabled(self) -> bool:
        try:
            disabled = str(
                await self._host.get_attribute("submit-disabled") or ""
            ).strip().lower()
            loading = str(
                await self._host.get_attribute("submit-loading") or ""
            ).strip().lower()
            return disabled not in {"true", "1"} and loading not in {
                "true", "1"}
        except Exception:
            return True

    async def click(self, *_args, **_kwargs):
        await _click_closed_shadow_publish_button(self._page)


@dataclass(frozen=True)
class XhsWriteOutcome:
    status: Literal["success", "failed", "uncertain"]
    result: str = ""
    error: str = ""
    method: str = "browser"

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def legacy(self) -> tuple[bool, str, str]:
        if self.status == "success":
            return True, self.result, ""
        if self.status == "uncertain":
            detail = self.error or "页面提交后未取得明确成功证据，请先到平台核对"
            return False, "", f"write_uncertain:{detail}"
        return False, "", self.error or "页面操作失败"


async def _wait_until_enabled(locator: Any, interaction: Any,
                              *, attempts: int = 80) -> bool:
    for _ in range(max(1, attempts)):
        try:
            if await locator.count() and await locator.is_visible() \
                    and await locator.is_enabled():
                return True
        except Exception:
            pass
        await interaction.pause(0.2, 0.45)
    return False


async def _wait_for_visible(page: Any, group: str, interaction: Any,
                            *, attempts: int = 40) -> tuple[Any | None, str]:
    """Poll editors/widgets that are mounted after asynchronous upload work."""
    for index in range(max(1, attempts)):
        locator, name = await find_visible(page, group)
        if locator is not None:
            return locator, name
        if index + 1 < attempts:
            await interaction.pause(0.18, 0.32)
    return None, ""


async def _wait_for_present(page: Any, group: str, interaction: Any,
                            *, attempts: int = 60) -> tuple[Any | None, str]:
    """Poll hidden controls that are mounted shortly after a tab transition."""
    for index in range(max(1, attempts)):
        locator, name = await find_present(page, group)
        if locator is not None:
            return locator, name
        if index + 1 < attempts:
            await interaction.pause(0.18, 0.32)
    return None, ""


_VISIBILITY_LABELS = {
    "public": "公开可见",
    "private": "仅自己可见",
    "friends": "仅互关好友可见",
}


async def _set_publish_visibility(page: Any, interaction: Any,
                                  visibility: str) -> None:
    desired = _VISIBILITY_LABELS.get(str(visibility or "public"), "公开可见")
    candidates = page.locator(".d-select-description")
    current = None
    for index in range(int(await candidates.count())):
        candidate = candidates.nth(index)
        try:
            if await candidate.is_visible():
                current = candidate
                break
        except Exception:
            continue
    if current is not None:
        try:
            if str(await current.inner_text()).strip() == desired:
                return
        except Exception:
            pass
        await interaction.click_visible(current)
        await interaction.pause(0.15, 0.3)
    elif str(visibility or "public") == "public":
        return
    else:
        raise RuntimeError("未找到可见范围设置")

    options = page.get_by_text(desired, exact=True)
    for index in range(int(await options.count())):
        option = options.nth(index)
        try:
            cls = str(await option.get_attribute("class") or "")
            if await option.is_visible() and "name" in cls.split():
                await interaction.click_visible(option)
                await interaction.pause(0.15, 0.3)
                if str(await current.inner_text()).strip() != desired:
                    raise RuntimeError("可见范围设置后读回不一致")
                return
        except RuntimeError:
            raise
        except Exception:
            continue
    raise RuntimeError(f"未找到可见范围选项：{desired}")


_XHS_OPERATOR_SEL = (
    ".publish-page-content-media .operator",
    "#creator-publish-dom .cover-container .operator",
)
_XHS_VERIFY_KW = ("安全验证", "扫码验证", "滑块验证", "请先完成验证", "短信验证码", "验证码")


async def _upload_operator_text(page: Any) -> str:
    """读取发布预览操作区的实时上传状态或失败提示。"""
    for sel in _XHS_OPERATOR_SEL:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                txt = str(await loc.inner_text() or "").replace("\n", " ").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


async def _upload_is_busy(page: Any) -> bool:
    op = await _upload_operator_text(page)
    if op and "上传中" in op:
        return True
    for candidate in selector_candidates("publish.progress"):
        try:
            locator = candidate_locator(page, candidate)
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_upload_complete(page: Any, interaction: Any,
                                *, attempts: int = 120,
                                on_stage: Any = None,
                                error_out: dict | None = None) -> bool:
    stable = 0
    last_op = ""
    for _ in range(max(1, attempts)):
        op = await _upload_operator_text(page)
        if op and "失败" in op:
            if error_out is not None:
                error_out["error"] = f"小红书媒体上传失败: {op}"
            return False
        if op and "上传中" in op and op != last_op:
            last_op = op
            await _notify_stage(on_stage, f"正在上传媒体（{op}）…")
        if not await _upload_is_busy(page):
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        await interaction.pause(0.25, 0.55)
    return False


class _SubmitLocator:
    """Mark the exact boundary where Patchright dispatches the sole submit click."""

    def __init__(self, locator: Any, state: dict, on_submit: Any = None):
        self._locator = locator
        self._state = state
        self._on_submit = on_submit

    def __getattr__(self, name: str):
        return getattr(self._locator, name)

    async def click(self, *args, **kwargs):
        if self._on_submit is not None:
            result = self._on_submit()
            if inspect.isawaitable(result):
                await result
        self._state["clicked"] = True
        return await self._locator.click(*args, **kwargs)


def _publish_response_handler(evidence: dict, submitted: dict):
    def on_response(response):
        try:
            url = str(response.url or "")
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if submitted["clicked"] and method == "POST" and any(
                    marker in url.lower() for marker in (
                        "/publish", "/note/create", "/web_api/sns/v2/note")):
                if 200 <= status < 300:
                    evidence["responses"].append(response)
                else:
                    evidence["business_rejected"] = True
                    evidence["reject_msg"] = f"HTTP {status}"
                    if status in (461, 471):
                        evidence["verify_hint"] = f"小红书安全验证(HTTP {status})"
        except Exception:
            return
    return on_response


def _publish_payload_accepted(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return False
    if payload.get("success") is True:
        return True
    for key in ("code", "result_code"):
        if key not in payload:
            continue
        value = payload[key]
        if value == 0 or str(value).strip().lower() in {"0", "ok", "success"}:
            return True
    data = payload.get("data")
    return bool(isinstance(data, dict) and data.get("success") is True)


def _extract_publish_note_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("id", "note_id", "noteId"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("id", "note_id", "noteId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


async def _consume_publish_responses(evidence: dict) -> bool:
    while evidence["responses"]:
        response = evidence["responses"].pop(0)
        try:
            payload = await response.json()
        except Exception:
            continue
        if _publish_payload_accepted(payload):
            evidence["accepted"] = True
            evidence["url"] = str(getattr(response, "url", "") or "")
            note_id = _extract_publish_note_id(payload)
            if note_id:
                evidence["note_id"] = note_id
            return True
        evidence["business_rejected"] = True
        msg = str(payload.get("msg") or payload.get("message") or "").strip()
        if msg:
            evidence["reject_msg"] = msg
            if any(k in msg for k in ("验证", "滑块", "安全", "频繁", "异常")):
                evidence["verify_hint"] = msg
    return bool(evidence["accepted"])


async def _visible_verify_text(page: Any) -> str:
    for kw in _XHS_VERIFY_KW:
        try:
            loc = page.get_by_text(kw, exact=False).first
            if await loc.count() and await loc.is_visible():
                return kw
        except Exception:
            continue
    return ""


async def _visible_success(page: Any) -> bool:
    raw_url = str(getattr(page, "url", "") or "")
    lower_url = raw_url.lower()
    if any(marker in lower_url
           for marker in ("/publish/success", "publish_success")):
        return True
    # Current creator-center variants return to content management rather than
    # a dedicated success route. This check only runs after the one submit
    # click, and excludes auth/risk/error routes from success evidence.
    try:
        parsed = urlsplit(raw_url)
        path = (parsed.path or "/").lower()
        if parsed.hostname == "creator.xiaohongshu.com" \
                and "/publish/publish" not in path \
                and not any(marker in lower_url for marker in (
                    "login", "passport", "verify", "captcha", "error")):
            return True
    except Exception:
        pass
    for text in _SUCCESS_TEXTS:
        try:
            locator = page.get_by_text(text, exact=False).first
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _confirm_published_title(page: Any, title: str, interaction: Any,
                                   *, attempts: int = 40,
                                   reload_every: int = 10) -> bool:
    """Confirm the submitted note is present in creator note management."""
    title = str(title or "").strip()
    if not title:
        return False
    try:
        current = str(getattr(page, "url", "") or "")
        if "/new/note-manager" not in current:
            await page.goto(
                PUBLISH_MANAGER_URL,
                wait_until="domcontentloaded",
                timeout=40_000,
            )
    except Exception:
        return False
    interval = max(1, int(reload_every or 10))
    for index in range(max(1, attempts)):
        try:
            match = page.get_by_text(title, exact=True).first
            if await match.count() and await match.is_visible():
                return True
        except Exception:
            pass
        if index + 1 < attempts:
            await interaction.pause(0.6, 1.0)
            # Let the Vue SPA finish its note-list fetch past skeleton cards
            # instead of cancelling in-flight requests on every sub-second tick.
            if (index + 1) % interval == 0:
                with suppress(Exception):
                    await page.reload(
                        wait_until="domcontentloaded", timeout=40_000)
    return False


async def publish_xhs_browser(
        mgr: BrowserManager, identity: Identity, media_type: str,
        title: str, desc: str, topics: Sequence[str], files: Sequence[str],
        *, timeout_seconds: int = 180,
        visibility: str = "public",
        on_submit: Any = None,
        on_stage: Any = None) -> XhsWriteOutcome:
    """Submit one note once; never retry or switch transport after submission."""
    try:
        paths = validate_publish_files(media_type, files)
    except ValueError as exc:
        return XhsWriteOutcome("failed", error=str(exc))
    title = (title or "").strip()[:20]
    tags = [str(topic).strip().lstrip("#") for topic in topics if str(topic).strip()]
    body = ((desc or "") + (
        "\n" + " ".join(f"#{tag}" for tag in tags) if tags else ""
    )).strip()[:1000]
    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence = {
        "accepted": False,
        "url": "",
        "note_id": "",
        "responses": [],
        "business_rejected": False,
        "reject_msg": "",
        "verify_hint": "",
    }
    on_response = _publish_response_handler(evidence, submitted)

    try:
        await _notify_stage(on_stage, "正在打开小红书发布页并上传媒体…")
        async with mgr.visible_page(
                identity, url=_publish_url(media_type)) as page:
            if "login" in page.url or "passport" in page.url:
                return XhsWriteOutcome("failed", error="logged_out:创作平台未登录")

            # The URL already selects image/video mode.  New builds render the
            # active tab's text inside an overlaying child, so clicking that
            # text can be intercepted by its own active tab.  Do not click an
            # already-selected mode; wait for its file input instead.

            file_input, _ = await _wait_for_present(
                page, "publish.file", interaction)
            if file_input is None:
                diagnostic = await selector_diagnostic(page, "publish.file")
                return XhsWriteOutcome(
                    "failed", error=f"未找到媒体上传控件(发布页可能改版)；{diagnostic}")
            await file_input.set_input_files(paths, timeout=15_000)
            await _notify_stage(on_stage, "正在等待小红书媒体上传完成…")
            upload_err: dict[str, str] = {}
            if not await _wait_upload_complete(
                    page, interaction, on_stage=on_stage, error_out=upload_err):
                return XhsWriteOutcome(
                    "failed",
                    error=upload_err.get("error") or "媒体上传在限定时间内未完成",
                )

            await _notify_stage(on_stage, "正在填写小红书标题、正文与可见范围…")
            if title:
                title_input, _ = await _wait_for_visible(
                    page, "publish.title", interaction)
                if title_input is None:
                    diagnostic = await selector_diagnostic(
                        page, "publish.title")
                    return XhsWriteOutcome(
                        "failed", error=f"未找到标题输入框(发布页可能改版)；{diagnostic}")
                await interaction.type_short(title_input, title)
            if body:
                body_input, _ = await _wait_for_visible(
                    page, "publish.body", interaction)
                if body_input is None:
                    diagnostic = await selector_diagnostic(
                        page, "publish.body")
                    return XhsWriteOutcome(
                        "failed", error=f"未找到正文输入框(发布页可能改版)；{diagnostic}")
                await interaction.insert_long(body_input, body, page=page)

            try:
                await _set_publish_visibility(page, interaction, visibility)
            except Exception as exc:
                return XhsWriteOutcome(
                    "failed", error=f"设置可见范围失败: {exc}")

            await _notify_stage(on_stage, "正在准备点击「发布」…")
            publish_button, publish_selector = await _wait_for_visible(
                page, "publish.submit", interaction)
            if publish_button is None:
                diagnostic = await selector_diagnostic(page, "publish.submit")
                return XhsWriteOutcome(
                    "failed", error=f"未找到发布按钮(发布页可能改版)；{diagnostic}")
            if publish_selector == "publish_web_component":
                publish_button = _PublishWebComponentLocator(
                    page, publish_button)
            if not await _wait_until_enabled(
                    publish_button, interaction,
                    attempts=max(4, min(120, timeout_seconds * 2))):
                return XhsWriteOutcome("failed", error="发布按钮一直不可用，请检查页面提示")

            try:
                page.on("response", on_response)
            except Exception:
                pass
            try:
                # This wrapper records the precise click-dispatch boundary.  There
                # is intentionally no second click and no API fallback below it.
                await interaction.click_visible(
                    _SubmitLocator(publish_button, submitted, on_submit))
                await _notify_stage(on_stage, "已点击「发布」，等待小红书确认…")
            except Exception as exc:
                if submitted["clicked"]:
                    return XhsWriteOutcome(
                        "uncertain", error=f"发布已提交但连接中断: {exc!r}")
                return XhsWriteOutcome("failed", error=f"发布按钮点击失败: {exc!r}")

            attempts = max(4, min(80, int(timeout_seconds * 2)))
            verify_notified = False
            for _ in range(attempts):
                await _consume_publish_responses(evidence)
                accepted_note_id = str(evidence.get("note_id") or "").strip()
                if (evidence["accepted"] and accepted_note_id
                        and not evidence["business_rejected"]):
                    return XhsWriteOutcome(
                        "success",
                        result=f"https://www.xiaohongshu.com/explore/{accepted_note_id}",
                    )
                if not verify_notified:
                    verify_detail = evidence["verify_hint"] or await _visible_verify_text(page)
                    if verify_detail:
                        verify_notified = True
                        await _notify_stage(
                            on_stage,
                            f"需要人工安全验证（{verify_detail}），请在弹出的小红书窗口里完成验证",
                        )
                if await _visible_success(page):
                    await _consume_publish_responses(evidence)
                    note_id = str(evidence.get("note_id") or "").strip()
                    result = (
                        f"https://www.xiaohongshu.com/explore/{note_id}"
                        if note_id else
                        str(getattr(page, "url", "") or evidence["url"])
                    )
                    if evidence["accepted"] and note_id and not evidence["business_rejected"]:
                        return XhsWriteOutcome("success", result=result)
                    if not title:
                        if evidence["accepted"] and not evidence["business_rejected"]:
                            return XhsWriteOutcome("success", result=result)
                        return XhsWriteOutcome(
                            "uncertain",
                            error=("平台进入了发布成功页，但无标题笔记缺少接口确认回执，"
                                   "请到平台核对"),
                        )
                    await _notify_stage(on_stage, "已提交，正在笔记管理页确认发布结果…")
                    await interaction.pause(0.8, 1.2)
                    confirmed = await _confirm_published_title(
                        page, title, interaction,
                        attempts=max(
                            4,
                            min(
                                120,
                                timeout_seconds if media_type == "video"
                                else timeout_seconds // 3,
                            ),
                        ),
                    )
                    if confirmed:
                        return XhsWriteOutcome("success", result=result)
                    if evidence["accepted"] and note_id and not evidence["business_rejected"]:
                        return XhsWriteOutcome("success", result=result)
                    return XhsWriteOutcome(
                        "uncertain",
                        error=("平台进入了发布成功页，但笔记管理中未找到"
                               "本次标题，请到平台核对"),
                    )
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.25, 0.55)
            reject_note = f"（{evidence['reject_msg']}）" if evidence.get("reject_msg") else ""
            detail = (
                f"平台返回了业务拒绝结果{reject_note}，但提交状态仍需核对"
                if evidence["business_rejected"]
                else "发布按钮已点击一次，但未取得明确成功证据"
            )
            return XhsWriteOutcome(
                "uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "发布已提交后页面异常" if submitted["clicked"] else "发布页面异常"
        return XhsWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def _locator_visible(locator: Any) -> bool:
    try:
        return bool(await locator.count() and await locator.is_visible())
    except Exception:
        return False


_XHS_COMMENT_CLOSED_KW = (
    "作者已关闭评论", "评论已关闭", "暂不支持评论", "当前笔记已关闭评论",
)
_XHS_NOTE_UNAVAILABLE_KW = (
    "当前笔记暂时无法浏览", "笔记已删除", "该笔记不存在",
    "内容不存在", "页面不见了", "你访问的页面不见了",
)
_XHS_COMMENT_RISK_KW = (
    "请求太频繁", "请求频繁", "操作频繁", "访问频繁",
    "一分钟后再试", "安全验证", "扫码验证身份", "滑块验证",
)


async def _xhs_comment_page_error(
        page: Any, *, allow_body_scan: bool = False) -> str:
    """Detect explicit auth, risk, closed-comment or unavailable-note states."""
    current_url = str(getattr(page, "url", "") or "").lower()
    if "/website-login/captcha" in current_url or "error_code=300012" in current_url:
        return "captcha:小红书要求安全验证"
    if "login" in current_url or "passport" in current_url:
        return "logged_out:账号未登录"

    for sel in (
        ".engage-bar .not-active",
        ".engage-bar .inner-when-not-active",
        ".engage-bar .content-edit",
    ):
        try:
            loc = page.locator(sel).first
            if await _locator_visible(loc):
                txt = str(await loc.inner_text() or "").strip()
                if "登录后评论" in txt:
                    return "logged_out:账号未登录"
        except Exception:
            continue

    for sel in (".login-container", ".login-modal"):
        try:
            if await _locator_visible(page.locator(sel).first):
                return "logged_out:账号未登录"
        except Exception:
            continue

    try:
        engage = page.locator(".engage-bar").first
        if await _locator_visible(engage):
            engage_txt = str(await engage.inner_text() or "").strip()
            if any(marker in engage_txt for marker in _XHS_COMMENT_CLOSED_KW):
                return "该笔记已关闭评论，无法发送评论"
    except Exception:
        pass

    if not allow_body_scan:
        return ""

    # Only scan full-page body text when the note detail container itself is
    # absent, preventing user comment content from triggering false positives.
    note_visible = False
    try:
        note_visible = await _locator_visible(page.locator("#noteContainer").first)
    except Exception:
        note_visible = False
    if note_visible:
        return ""

    if "/404" in current_url:
        return "目标笔记页面不存在(404)，请核对笔记链接或访问参数"

    body_text = ""
    try:
        body_text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 2000)"
        )
    except Exception:
        try:
            body_text = await page.locator("body").inner_text(timeout=1200)
        except Exception:
            body_text = ""
    normalized = " ".join(str(body_text or "").split())
    if any(marker in normalized for marker in _XHS_COMMENT_RISK_KW):
        return f"captcha:小红书平台频控/安全验证：{normalized[:120]}"
    if any(marker in normalized for marker in _XHS_NOTE_UNAVAILABLE_KW):
        return "目标笔记当前无法浏览（可能已删除、私密或访问参数已过期）"
    return ""


async def _wait_comment_page_ready(
        page: Any, interaction: Any, *, attempts: int = 20) -> str:
    """Wait briefly for Xiaohongshu's Vue note detail and engage-bar to mount."""
    total = max(1, int(attempts))
    for index in range(total):
        page_err = await _xhs_comment_page_error(
            page, allow_body_scan=(index >= 3))
        if page_err:
            return page_err
        try:
            if await _locator_visible(page.locator(".engage-bar").first):
                return ""
        except Exception:
            pass
        editor, _ = await find_visible(page, "comment.editor")
        if editor is not None:
            return ""
        trigger, _ = await find_visible(page, "comment.trigger")
        if trigger is not None:
            return ""
        try:
            if page.is_closed():
                break
        except Exception:
            break
        if index + 1 < total:
            await interaction.pause(0.18, 0.32)
    return await _xhs_comment_page_error(page, allow_body_scan=False)


async def _scroll_comment_list(page: Any, interaction: Any) -> None:
    """Position the pointer inside the right-hand comment scroller before wheeling."""
    human = getattr(interaction, "_human", None)
    for sel in (".note-scroller", ".comments-container"):
        try:
            scroller = page.locator(sel).first
            if not await _locator_visible(scroller):
                continue
            box = await scroller.bounding_box()
            if (
                isinstance(box, dict)
                and float(box.get("width") or 0) >= 20.0
                and float(box.get("height") or 0) >= 20.0
            ):
                if (
                    human is not None
                    and hasattr(human, "sample_box_point")
                    and hasattr(human, "move_mouse")
                ):
                    tx, ty = human.sample_box_point(box)
                    await human.move_mouse(page, tx, ty)
                elif getattr(page, "mouse", None) is not None:
                    tx = float(box["x"]) + float(box["width"]) * 0.5
                    ty = float(box["y"]) + float(box["height"]) * 0.5
                    await page.mouse.move(tx, ty)
                break
        except Exception:
            continue
    await interaction.scroll_step(page)


async def _find_reply_target(page: Any, comment_id: str, target_text: str):
    if comment_id:
        safe_id = str(comment_id).replace("\\", "\\\\").replace('"', '\\"')
        for sel in (
            f'[id="comment-{safe_id}"]',
            f'[data-comment-id="{safe_id}"]',
        ):
            try:
                locator = page.locator(sel).first
                if await _locator_visible(locator):
                    return locator
            except Exception:
                continue
    if target_text:
        try:
            locator = page.locator(".comment-item").filter(
                has_text=target_text).first
            if await _locator_visible(locator):
                return locator
        except Exception:
            pass
        try:
            locator = page.get_by_text(target_text, exact=True).first
            if await _locator_visible(locator):
                return locator
        except Exception:
            pass
    return None


async def _find_reply_button(target: Any):
    for sel in (
        ".interactions .reply.icon-container",
        ".interactions .reply",
        ".reply.icon-container",
    ):
        try:
            reply = target.locator(sel).first
            if await _locator_visible(reply):
                return reply
        except Exception:
            continue
    try:
        reply = target.get_by_text("回复", exact=True).first
        if await _locator_visible(reply):
            return reply
    except Exception:
        pass
    try:
        reply = target.locator(
            'button:has-text("回复"),[class*="reply"]').first
        if await _locator_visible(reply):
            return reply
    except Exception:
        pass
    return None


async def _find_comment_input(page: Any):
    locator, _ = await find_visible(page, "comment.editor")
    return locator


async def _collapsed_overlay_visible(page: Any) -> bool:
    for sel in (
        ".engage-bar .not-active.inner-when-not-active",
        ".engage-bar .inner-when-not-active",
        "div.input-box div.content-edit .not-active",
    ):
        try:
            if await _locator_visible(page.locator(sel).first):
                return True
        except Exception:
            continue
    return False


async def _activate_comment_editor(
        page: Any, interaction: Any, *, allow_chat_wrapper: bool = True):
    """Expand a collapsed XHS .engage-bar overlay if needed, then return the editor."""
    submit_visible, _ = await find_visible(page, "comment.submit")
    if submit_visible is None:
        trigger, trigger_name = await find_visible(page, "comment.trigger")
        if trigger is not None and trigger_name != "comment_trigger_chat_wrapper":
            try:
                trigger_txt = str(await trigger.inner_text() or "").strip()
            except Exception:
                trigger_txt = ""
            if "登录后评论" in trigger_txt:
                return None
            await interaction.click_visible(trigger)
            await interaction.pause(0.2, 0.45)
            # If the first click only focused the window and the collapsed
            # overlay is still sitting over p#content-textarea, retry once on
            # the .content-edit wrapper.
            if (
                await _collapsed_overlay_visible(page)
                and not await _xhs_comment_page_error(page, allow_body_scan=False)
            ):
                content_edit = page.locator(".engage-bar .input-box .content-edit").first
                if await _locator_visible(content_edit):
                    await interaction.click_visible(content_edit)
                    await interaction.pause(0.2, 0.45)

    editor = await _find_comment_input(page)
    if editor is not None:
        return editor

    if allow_chat_wrapper:
        chat = page.locator(".engage-bar .chat-wrapper").first
        if await _locator_visible(chat):
            await interaction.click_visible(chat)
            await interaction.pause(0.25, 0.5)
            editor = await _find_comment_input(page)
    return editor


def _extract_comment_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if isinstance(data, dict):
        comment = data.get("comment")
        if isinstance(comment, dict):
            for key in ("id", "comment_id", "commentId"):
                value = str(comment.get(key) or "").strip()
                if value:
                    return value
        for key in ("comment_id", "commentId", "id"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("comment_id", "commentId", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _comment_payload_result(
        payload: Any) -> Literal["accepted", "rejected", "unknown"]:
    if not isinstance(payload, dict):
        return "unknown"
    root_success = payload.get("success")
    data = payload.get("data")
    data_success = data.get("success") if isinstance(data, dict) else None
    if root_success is False or data_success is False:
        return "rejected"
    code_results: list[bool] = []
    for key in ("code", "result_code"):
        if key not in payload:
            continue
        value = payload[key]
        accepted_code = not isinstance(value, bool) and (
            value == 0 or str(value).strip().lower() in {"0", "ok", "success"})
        code_results.append(accepted_code)
        if not accepted_code:
            return "rejected"
    if root_success is True or data_success is True or any(code_results):
        return "accepted"
    return "unknown"


def _comment_response_handler(evidence: dict, submitted: dict):
    def on_response(response):
        try:
            if not submitted["clicked"]:
                return
            url = str(response.url or "").lower()
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if method == "POST" and any(
                    marker in url for marker in (
                        "/comment/post", "/comment/create", "/comment/add")):
                if 200 <= status < 300:
                    evidence["responses"].append(response)
                else:
                    evidence["business_rejected"] = True
                    evidence["reject_msg"] = f"HTTP {status}"
                    if status in (461, 471):
                        evidence["verify_hint"] = f"小红书安全验证(HTTP {status})"
        except Exception:
            return
    return on_response


async def _consume_comment_responses(evidence: dict) -> bool:
    while evidence["responses"]:
        response = evidence["responses"].pop(0)
        try:
            payload = await response.json()
        except Exception:
            continue
        outcome = _comment_payload_result(payload)
        if outcome == "accepted":
            evidence["accepted"] = True
            comment_id = _extract_comment_id(payload)
            if comment_id:
                evidence["comment_id"] = comment_id
            return True
        if outcome == "rejected":
            evidence["business_rejected"] = True
            msg = str(payload.get("msg") or payload.get("message") or "").strip()
            if msg:
                evidence["reject_msg"] = msg
                if any(k in msg for k in ("验证", "滑块", "安全", "频繁", "异常")):
                    evidence["verify_hint"] = msg
    return bool(evidence["accepted"])


async def _rendered_comment_count(page: Any, text: str) -> int | None:
    """Count rendered comment items matching text, excluding the editor box."""
    try:
        return max(0, int(await page.evaluate(
            """value => {
              const target = String(value || '').trim();
              if (!target) return 0;
              const items = document.querySelectorAll(
                '.comments-container .comment-item, .list-container .comment-item, [id^="comment-"]'
              );
              if (items.length || document.querySelector('.comments-container, .list-container, .comments-el')) {
                let count = 0;
                for (const item of items) {
                  if (item.closest('.engage-bar, .input-box, .content-edit')) continue;
                  const textEl = item.querySelector('.note-text') || item.querySelector('.content') || item;
                  if ((textEl.textContent || '').trim() === target) count += 1;
                }
                return count;
              }
              const root = document.querySelector('#noteContainer') || document.body;
              if (!root) return 0;
              let count = 0;
              const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
              let node = walker.nextNode();
              while (node) {
                const parent = node.parentElement;
                if (
                  parent
                  && !parent.closest(
                    '.engage-bar, .input-box, .content-edit, [contenteditable="true"], textarea, input, script, style'
                  )
                  && (node.textContent || '').trim() === target
                ) {
                  count += 1;
                }
                node = walker.nextNode();
              }
              return count;
            }""",
            text,
        )))
    except Exception:
        return None


async def comment_xhs_browser(
        mgr: BrowserManager, identity: Identity, note_id: str, xsec_token: str,
        content: str, *, target_comment_id: str = "",
        target_text: str = "", max_scrolls: int = 16,
        timeout_seconds: int = 30,
        on_submit: Any = None) -> XhsWriteOutcome:
    """Post one visible comment/reply once, preserving an uncertain result."""
    note_id = str(note_id or "").strip()
    content = str(content or "").strip()
    if not note_id:
        return XhsWriteOutcome("failed", error="缺少目标笔记 ID")
    if not content:
        return XhsWriteOutcome("failed", error="评论内容为空")
    query = urlencode({
        "xsec_token": xsec_token or "", "xsec_source": "pc_comment"})
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?{query}"
    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence: dict[str, Any] = {
        "accepted": False,
        "comment_id": "",
        "responses": [],
        "business_rejected": False,
        "reject_msg": "",
        "verify_hint": "",
    }
    on_response = _comment_response_handler(evidence, submitted)
    page = None

    try:
        async with mgr.visible_page(identity, url=url) as page:
            ready_err = await _wait_comment_page_ready(page, interaction)
            if ready_err:
                return XhsWriteOutcome("failed", error=ready_err)

            if target_comment_id:
                target = await _find_reply_target(
                    page, target_comment_id, target_text)
                for _ in range(max(0, int(max_scrolls))):
                    if target is not None:
                        break
                    await _scroll_comment_list(page, interaction)
                    target = await _find_reply_target(
                        page, target_comment_id, target_text)
                if target is None:
                    return XhsWriteOutcome(
                        "failed", error="未找到目标评论，已停止且不会降级为顶层评论")

                reply = await _find_reply_button(target)
                if reply is None:
                    return XhsWriteOutcome(
                        "failed", error="已找到目标评论，但未找到该评论的回复入口")
                await interaction.click_visible(reply)
                await interaction.pause(0.25, 0.5)

            allow_chat_wrapper = not bool(target_comment_id)
            editor = await _activate_comment_editor(
                page, interaction, allow_chat_wrapper=allow_chat_wrapper)
            if editor is None:
                page_err = await _xhs_comment_page_error(
                    page, allow_body_scan=True)
                if page_err:
                    return XhsWriteOutcome("failed", error=page_err)
                for _ in range(max(0, int(max_scrolls))):
                    if editor is not None:
                        break
                    if target_comment_id:
                        await interaction.pause(0.2, 0.45)
                    else:
                        await _scroll_comment_list(page, interaction)
                    editor = await _activate_comment_editor(
                        page, interaction,
                        allow_chat_wrapper=allow_chat_wrapper)
            post_activate_err = await _xhs_comment_page_error(
                page, allow_body_scan=(editor is None))
            if post_activate_err:
                return XhsWriteOutcome("failed", error=post_activate_err)
            if editor is None:
                diagnostic = await selector_diagnostic(page, "comment.editor")
                return XhsWriteOutcome(
                    "failed", error=f"未找到评论输入框(页面可能改版)；{diagnostic}")

            existing_rendered = await _rendered_comment_count(page, content)
            if len(content) <= 80:
                await interaction.type_short(editor, content)
            else:
                await interaction.insert_long(editor, content, page=page)

            send, _ = await _wait_for_visible(
                page, "comment.submit", interaction, attempts=12)
            if send is None:
                page_err = await _xhs_comment_page_error(
                    page, allow_body_scan=True)
                if page_err:
                    return XhsWriteOutcome("failed", error=page_err)
                diagnostic = await selector_diagnostic(page, "comment.submit")
                return XhsWriteOutcome(
                    "failed", error=f"未找到评论发送按钮(页面可能改版)；{diagnostic}")
            if not await _wait_until_enabled(
                    send, interaction,
                    attempts=max(4, min(60, timeout_seconds * 2))):
                return XhsWriteOutcome("failed", error="评论发送按钮当前不可用")

            existing_matches = 0
            if existing_rendered is None:
                try:
                    existing_matches = int(await page.get_by_text(
                        content, exact=True).count())
                except Exception:
                    pass
            try:
                page.on("response", on_response)
            except Exception:
                pass
            try:
                await interaction.click_visible(
                    _SubmitLocator(send, submitted, on_submit))
            except Exception as exc:
                if submitted["clicked"]:
                    return XhsWriteOutcome(
                        "uncertain", error=f"评论已提交但连接中断: {exc!r}")
                return XhsWriteOutcome("failed", error=f"评论发送失败: {exc!r}")

            for _ in range(max(4, min(60, timeout_seconds * 2))):
                if await _consume_comment_responses(evidence):
                    if evidence["accepted"] and not evidence["business_rejected"]:
                        return XhsWriteOutcome(
                            "success", result=evidence["comment_id"] or "ok")
                if existing_rendered is not None:
                    current_rendered = await _rendered_comment_count(page, content)
                    if (
                        current_rendered is not None
                        and current_rendered > existing_rendered
                    ):
                        await _consume_comment_responses(evidence)
                        return XhsWriteOutcome(
                            "success", result=evidence["comment_id"] or "ok")
                else:
                    own_text_added = False
                    try:
                        own_text = page.get_by_text(content, exact=True)
                        own_text_added = int(await own_text.count()) > existing_matches
                    except Exception:
                        pass
                    if own_text_added:
                        return XhsWriteOutcome(
                            "success", result=evidence["comment_id"] or "ok")
                if evidence["business_rejected"] and not evidence["accepted"]:
                    break
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.2, 0.45)

            await _consume_comment_responses(evidence)
            if evidence["accepted"] and not evidence["business_rejected"]:
                return XhsWriteOutcome(
                    "success", result=evidence["comment_id"] or "ok")
            if evidence["verify_hint"]:
                return XhsWriteOutcome(
                    "uncertain",
                    error=(f"评论提交触发平台安全验证（{evidence['verify_hint']}），"
                           "请到平台核对"),
                )
            reject_note = (
                f"（{evidence['reject_msg']}）" if evidence.get("reject_msg") else ""
            )
            detail = (
                f"平台返回了业务拒绝结果{reject_note}，但提交状态仍需核对"
                if evidence["business_rejected"]
                else "评论按钮已点击一次，但未取得明确成功证据"
            )
            return XhsWriteOutcome(
                "uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "评论提交后页面异常" if submitted["clicked"] else "评论页面异常"
        return XhsWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        if page is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
