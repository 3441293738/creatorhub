"""Central, deterministic-testable semantics for visible XHS page actions."""
from __future__ import annotations

import asyncio
import inspect
import random
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable


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
    """Page-native clicks, input and finite scrolling with bounded cadence."""

    def __init__(
            self, *, rng: random.Random | None = None,
            sleep: Callable[[float], Awaitable[None]] | None = None):
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep or asyncio.sleep

    async def _pause(self, low: float, high: float) -> None:
        await self.sleep(float(self.rng.uniform(low, high)))

    async def pause(self, low: float, high: float) -> None:
        """Public bounded pause for condition-polling loops."""
        if low < 0 or high < low:
            raise ValueError("交互停顿范围无效")
        await self._pause(low, high)

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
        await locator.hover()
        await self._pause(0.08, 0.22)
        await locator.click()

    async def _prepare_input(self, locator: Any) -> None:
        await locator.wait_for(state="visible", timeout=10_000)
        await locator.scroll_into_view_if_needed(timeout=10_000)
        await locator.focus()
        await self._pause(0.06, 0.16)
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        await locator.press(f"{modifier}+A")
        await locator.press("Backspace")

    async def type_short(self, locator: Any, text: str) -> None:
        await self._prepare_input(locator)
        for character in str(text):
            await locator.press_sequentially(character, delay=0)
            await self._pause(0.035, 0.095)
        await self._verify_text(locator, str(text))

    async def insert_long(
            self, locator: Any, text: str, *, page: Any | None = None) -> None:
        await self._prepare_input(locator)
        target_page = page or getattr(locator, "page", None)
        if target_page is None or not hasattr(target_page, "keyboard"):
            raise RuntimeError("长文本输入缺少活动页面")
        await target_page.keyboard.insert_text(str(text))
        await self._pause(0.08, 0.18)
        await self._verify_text(locator, str(text))

    async def _verify_text(self, locator: Any, expected: str) -> None:
        try:
            actual = await locator.input_value()
        except Exception:
            actual = await locator.evaluate(
                "el => el.value ?? el.innerText ?? el.textContent ?? ''")
        if str(actual) != expected:
            raise RuntimeError("小红书文本输入结果与预期不一致")

    async def scroll_step(self, page: Any, *, direction: int = 1) -> int:
        amount = int(self.rng.randint(350, 900))
        signed = amount if direction >= 0 else -amount
        await page.mouse.wheel(0, signed)
        await self._pause(0.18, 0.52)
        return signed

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
