"""Central, human-dynamics semantics for visible XHS & Rednote page actions."""
from __future__ import annotations

import asyncio
import inspect
import random
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from .human_interaction import HumanInteractionEngine


class XhsVisibleActionGate:
    """Permit one active, user-visible Xiaohongshu action on the machine."""

    def __init__(self):
        self._semaphore = asyncio.Semaphore(1)
        self.active_account: Any = None
        self._owner_task: asyncio.Task | None = None
        self._depth = 0

    @property
    def owned_by_current_task(self) -> bool:
        return self._owner_task is asyncio.current_task()

    @asynccontextmanager
    async def acquire(self, account_key: Any):
        current = asyncio.current_task()
        if self._owner_task is current:
            if self.active_account != account_key:
                raise RuntimeError("同一可见操作不可嵌套切换小红书账号")
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        acquired = False
        try:
            await self._semaphore.acquire()
            acquired = True
            self.active_account = account_key
            self._owner_task = current
            self._depth = 1
            yield
        finally:
            if acquired:
                self._depth = 0
                self._owner_task = None
                self.active_account = None
                self._semaphore.release()


class XhsInteractionPolicy:
    """Page-native clicks, input and finite scrolling with human Bezier/keystroke cadence."""

    def __init__(
            self, *, rng: random.Random | None = None,
            sleep: Callable[[float], Awaitable[None]] | None = None):
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep or asyncio.sleep
        self._human = HumanInteractionEngine(rng=self.rng, sleep=self.sleep)
        self._action_count = 0
        self._next_rest_at = int(self.rng.randint(14, 24))

    async def _pause(self, low: float, high: float) -> None:
        await self.sleep(float(self.rng.uniform(low, high)))

    async def pause(self, low: float, high: float) -> None:
        """Public bounded pause for condition-polling loops."""
        if low < 0 or high < low:
            raise ValueError("交互停顿范围无效")
        await self._pause(low, high)

    async def _after_action(self) -> None:
        """Occasionally insert a longer bounded break between action bursts."""
        self._action_count += 1
        if self._action_count < self._next_rest_at:
            return
        self._action_count = 0
        self._next_rest_at = int(self.rng.randint(14, 24))
        await self._pause(1.4, 3.8)

    async def reading_pause(self, *, content_length: int = 0) -> float:
        """Pause after navigation using a bounded content-aware dwell time."""
        length = max(0, min(4000, int(content_length or 0)))
        base = 0.9 + min(5.5, length / 600)
        delay = float(self.rng.uniform(base * 0.85, base * 1.2))
        await self.sleep(delay)
        await self._after_action()
        return delay

    async def click_visible(
            self, locator: Any, *, timeout: int = 10_000) -> None:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.scroll_into_view_if_needed(timeout=timeout)
        enabled = getattr(locator, "is_enabled", None)
        if callable(enabled):
            for _ in range(20):
                if await enabled():
                    break
                await self._pause(0.05, 0.12)
            else:
                raise RuntimeError("小红书页面控件当前不可用")

        target_page = getattr(locator, "page", None)
        bbox_fn = getattr(locator, "bounding_box", None)
        box = None
        if callable(bbox_fn):
            try:
                box = await bbox_fn()
            except Exception:
                box = None
        # Keep custom facade locators (_SubmitLocator / _PublishWebComponentLocator)
        # working via their overridden .click(), while adding Bezier approach first.
        is_custom_facade = locator.__class__.__name__.startswith("_")
        if (
            isinstance(box, dict)
            and float(box.get("width") or 0) >= 2.0
            and float(box.get("height") or 0) >= 2.0
            and target_page is not None
            and getattr(target_page, "mouse", None) is not None
        ):
            tx, ty = self._human.sample_box_point(box)
            await self._human.move_mouse(target_page, tx, ty)
            await self._pause(0.14, 0.42)
            if not is_custom_facade:
                mouse = target_page.mouse
                if hasattr(mouse, "down") and hasattr(mouse, "up"):
                    await mouse.down(button="left")
                    await self._pause(0.055, 0.135)
                    await mouse.up(button="left")
                    await self._after_action()
                    return
        else:
            await locator.hover()
            await self._pause(0.18, 0.48)

        hold_ms = int(self.rng.uniform(55, 132))
        try:
            await locator.click(delay=hold_ms)
        except TypeError:
            await locator.click()
        await self._after_action()

    async def _prepare_input(self, locator: Any) -> None:
        await self.click_visible(locator, timeout=10_000)
        await self._pause(0.06, 0.16)
        has_existing = True
        try:
            val = await locator.input_value()
            has_existing = bool(str(val or ""))
        except Exception:
            try:
                dom_txt = await locator.evaluate(
                    "el => String(el.value ?? el.innerText ?? el.textContent ?? '')"
                )
                has_existing = bool(str(dom_txt or "").strip())
            except Exception:
                has_existing = True
        if has_existing:
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            await locator.press(f"{modifier}+A")
            await self._pause(0.04, 0.11)
            await locator.press("Backspace")
            await self._pause(0.05, 0.14)

    async def type_short(self, locator: Any, text: str) -> None:
        await self._prepare_input(locator)
        burst_left = int(self.rng.randint(3, 7))
        for character in str(text):
            hold_ms = int(self.rng.triangular(24, 76, 42))
            await locator.press_sequentially(character, delay=hold_ms)
            await self._pause(0.05, 0.16)
            burst_left -= 1
            if character in ",，。.!！?？;；:：\n":
                await self._pause(0.2, 0.55)
            elif character.isspace() or burst_left <= 0:
                await self._pause(0.1, 0.3)
                burst_left = int(self.rng.randint(3, 7))
        await self._verify_text(locator, str(text))
        await self.reading_pause(content_length=len(str(text)))

    async def insert_long(
            self, locator: Any, text: str, *, page: Any | None = None) -> None:
        await self._prepare_input(locator)
        target_page = page or getattr(locator, "page", None)
        if target_page is None or not hasattr(target_page, "keyboard"):
            raise RuntimeError("长文本输入缺少活动页面")
        content = str(text)
        # Type opening prefix with real keydown/keyup events, then insert natural
        # clause chunks with human review pauses rather than 1000 chars in 0ms.
        head_len = min(len(content), int(self.rng.randint(8, 16)))
        for ch in content[:head_len]:
            hold_ms = int(self.rng.triangular(24, 72, 40))
            await locator.press_sequentially(ch, delay=hold_ms)
            await self._pause(0.04, 0.12)
        if len(content) > head_len:
            await self._pause(0.16, 0.38)
            rest = content[head_len:]
            pos = 0
            while pos < len(rest):
                step = int(self.rng.randint(42, 96))
                end = min(len(rest), pos + step)
                for idx in range(end - 1, pos + 14, -1):
                    if rest[idx] in "\n。！？；，,!?;":
                        end = idx + 1
                        break
                await target_page.keyboard.insert_text(rest[pos:end])
                pos = end
                if pos < len(rest):
                    await self._pause(0.14, 0.42)
        await self._pause(0.08, 0.18)
        await self._verify_text(locator, content)
        await self.reading_pause(content_length=len(content))

    @staticmethod
    def _normalize_verified_text(value: str, *, multiline: bool) -> str:
        text = (
            str(value or "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\xa0", " ")
        )
        if multiline:
            text = re.sub(r"\n+", "\n", text).strip("\n")
        return text

    async def _verify_text(self, locator: Any, expected: str) -> None:
        from_dom = False
        try:
            actual = await locator.input_value()
        except Exception:
            from_dom = True
            actual = await locator.evaluate(
                "el => el.value ?? el.innerText ?? el.textContent ?? ''")
        if self._normalize_verified_text(
                str(actual), multiline=from_dom) != self._normalize_verified_text(
                expected, multiline=from_dom):
            raise RuntimeError("小红书文本输入结果与预期不一致")

    async def scroll_step(self, page: Any, *, direction: int = 1) -> int:
        amount = int(self.rng.triangular(350, 900, 610))
        return await self._human.scroll_wheel(
            page, distance=amount, direction=direction, settle=True
        )

    async def scroll_until(
            self, page: Any,
            predicate: Callable[[], bool | Awaitable[bool]],
            *, max_steps: int, direction: int = 1) -> bool:
        if await self._resolve(predicate()):
            return True
        for _ in range(max(0, int(max_steps))):
            await self.scroll_step(page, direction=direction)
            if await self._resolve(predicate()):
                return True
        return False

    @staticmethod
    async def _resolve(value: bool | Awaitable[bool]) -> bool:
        if inspect.isawaitable(value):
            value = await value
        return bool(value)
