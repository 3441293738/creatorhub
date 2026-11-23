"""全平台通用真机真人浏览器交互引擎（HumanInteractionEngine）。

核心防风控设计：
1. 鼠标轨迹：基于三次贝塞尔曲线 + 最小加加速度（Minimum-Jerk）速度分布 + 生理微颤生成多步移动，
   彻底告别瞬移点击。
2. 落点分布：在目标控件内缩安全区内按二维截断高斯分布采样坐标，绝不死点几何正中心 (w/2, h/2)。
3. 按压驻留：真实人类鼠标按下到抬起（mousedown -> mouseup）保持 55–135ms 对数正态驻留，
   严禁使用 `element.click()` 合成事件（`event.isTrusted === false`）。
4. 键盘节律：短中文本按词组突发 + 对数正态键间间隔 + 25–75ms 按键按压时长 + 标点呼吸停顿逐字键入；
   仅在输入框非空时执行全选清空；长正文模拟输入法/分段粘贴与审阅停顿。
5. 滚轮动力学：模拟真实滚轮/触控板阻尼惯性的多帧连续脉冲滚动，替代 `scrollTo(0, scrollHeight)`
   与单次 `wheel(0, 4000)` 机械跳跃。
"""
from __future__ import annotations

import asyncio
import inspect
import math
import random
import sys
import weakref
from typing import Any, Awaitable, Callable, Sequence


class HumanInteractionEngine:
    """跨平台拟人鼠标、键盘、滚轮与阅读停顿控制器。"""

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep or asyncio.sleep
        self._cursors: weakref.WeakKeyDictionary[Any, tuple[float, float]] = (
            weakref.WeakKeyDictionary()
        )
        self._action_count = 0
        self._next_rest_at = int(self.rng.randint(14, 24))

    async def pause(self, low: float, high: float) -> float:
        """在 [low, high] 秒区间内按偏态分布随机停顿。"""
        low_f = max(0.0, float(low))
        high_f = max(low_f, float(high))
        if high_f <= 0.0:
            return 0.0
        mode = low_f + (high_f - low_f) * 0.38
        delay = float(self.rng.triangular(low_f, high_f, mode))
        await self.sleep(delay)
        return delay

    async def wait_ms(self, base_ms: float, *, jitter: float = 0.32) -> float:
        """替代固定 wait_for_timeout(N)，围绕基准毫秒数产生拟人抖动等待。"""
        base_s = max(0.02, float(base_ms) / 1000.0)
        spread = min(0.75, max(0.08, float(jitter)))
        low = max(0.02, base_s * (1.0 - spread * 0.65))
        high = base_s * (1.0 + spread * 1.15)
        return await self.pause(low, high)

    async def _after_action(self) -> None:
        self._action_count += 1
        if self._action_count < self._next_rest_at:
            return
        self._action_count = 0
        self._next_rest_at = int(self.rng.randint(14, 24))
        await self.pause(1.35, 3.6)

    async def reading_pause(self, *, content_length: int = 0) -> float:
        """按文本长度估算人类扫读与思考驻留时间。"""
        length = max(0, min(4000, int(content_length or 0)))
        base = 0.85 + min(5.2, length / 620.0)
        delay = await self.pause(base * 0.82, base * 1.24)
        await self._after_action()
        return delay

    def _initial_cursor(self, page: Any) -> tuple[float, float]:
        vp = getattr(page, "viewport_size", None)
        if callable(vp):
            vp = None
        w = int((vp or {}).get("width") or 1280)
        h = int((vp or {}).get("height") or 800)
        return (
            round(self.rng.uniform(w * 0.22, w * 0.76), 1),
            round(self.rng.uniform(h * 0.18, h * 0.68), 1),
        )

    def _get_cursor(self, page: Any) -> tuple[float, float]:
        try:
            pos = self._cursors.get(page)
            if pos is not None:
                return pos
        except TypeError:
            pass
        pos = self._initial_cursor(page)
        self._set_cursor(page, pos)
        return pos

    def _set_cursor(self, page: Any, pos: tuple[float, float]) -> None:
        try:
            self._cursors[page] = pos
        except TypeError:
            pass

    def sample_box_point(self, box: dict[str, Any]) -> tuple[float, float]:
        """在元素包围盒内缩安全区按二维截断高斯分布取点，避免死点几何中心。"""
        x = float(box.get("x") or 0.0)
        y = float(box.get("y") or 0.0)
        w = max(2.0, float(box.get("width") or 2.0))
        h = max(2.0, float(box.get("height") or 2.0))
        mx = min(w * 0.22, max(1.5, w * 0.15))
        my = min(h * 0.22, max(1.5, h * 0.15))
        cx = x + w * 0.5
        cy = y + h * 0.5
        px = self.rng.gauss(cx, max(0.8, w * 0.14))
        py = self.rng.gauss(cy, max(0.8, h * 0.14))
        px = max(x + mx, min(x + w - mx, px))
        py = max(y + my, min(y + h - my, py))
        return round(px, 2), round(py, 2)

    def _bezier_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> list[tuple[float, float]]:
        sx, sy = start
        ex, ey = end
        dx = ex - sx
        dy = ey - sy
        dist = math.hypot(dx, dy)
        if dist < 4.0:
            return [(round(ex, 2), round(ey, 2))]

        steps = max(7, min(25, int(dist / 48.0) + self.rng.randint(5, 9)))
        perp_x = -dy / dist
        perp_y = dx / dist
        arc_mag = min(95.0, max(8.0, dist * self.rng.uniform(0.08, 0.24)))
        arc_sign = 1.0 if self.rng.random() < 0.5 else -1.0

        t1 = self.rng.uniform(0.22, 0.38)
        t2 = self.rng.uniform(0.64, 0.84)
        c1x = sx + dx * t1 + perp_x * arc_mag * arc_sign * self.rng.uniform(0.6, 1.15)
        c1y = sy + dy * t1 + perp_y * arc_mag * arc_sign * self.rng.uniform(0.6, 1.15)
        c2x = sx + dx * t2 + perp_x * arc_mag * arc_sign * self.rng.uniform(-0.35, 0.75)
        c2y = sy + dy * t2 + perp_y * arc_mag * arc_sign * self.rng.uniform(-0.35, 0.75)

        points: list[tuple[float, float]] = []
        for idx in range(1, steps + 1):
            u = idx / steps
            # Minimum-jerk S-curve velocity profile: slow start, fast middle, precise landing
            s = u * u * u * (10.0 + u * (-15.0 + 6.0 * u))
            inv = 1.0 - s
            px = (
                inv * inv * inv * sx
                + 3.0 * inv * inv * s * c1x
                + 3.0 * inv * s * s * c2x
                + s * s * s * ex
            )
            py = (
                inv * inv * inv * sy
                + 3.0 * inv * inv * s * c1y
                + 3.0 * inv * s * s * c2y
                + s * s * s * ey
            )
            if idx < steps:
                tremor = (1.0 - s) * 1.15
                px += self.rng.uniform(-tremor, tremor)
                py += self.rng.uniform(-tremor, tremor)
            points.append((round(px, 2), round(py, 2)))
        return points

    async def move_mouse(self, page: Any, target_x: float, target_y: float) -> None:
        """沿三次贝塞尔曲线将鼠标平滑移动至目标坐标。"""
        mouse = getattr(page, "mouse", None)
        if mouse is None or not hasattr(mouse, "move"):
            return
        start = self._get_cursor(page)
        points = self._bezier_path(start, (target_x, target_y))
        for idx, (px, py) in enumerate(points):
            await mouse.move(px, py)
            # Decelerate slightly near the target (Fitts's law)
            if idx + 3 >= len(points):
                await self.pause(0.008, 0.018)
            else:
                await self.pause(0.004, 0.012)
        self._set_cursor(page, (round(target_x, 2), round(target_y, 2)))

    async def click_point(
        self,
        page: Any,
        x: float,
        y: float,
        *,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """平滑移入坐标后执行带真实按压驻留时长（55–135ms）的硬件级点击。"""
        mouse = getattr(page, "mouse", None)
        if mouse is None:
            return
        await self.move_mouse(page, x, y)
        await self.pause(0.08, 0.24)
        for index in range(max(1, int(click_count))):
            if hasattr(mouse, "down") and hasattr(mouse, "up"):
                await mouse.down(button=button)
                await self.pause(0.055, 0.135)
                await mouse.up(button=button)
            else:
                await mouse.click(x, y, button=button, delay=int(self.rng.uniform(55, 135)))
            if index + 1 < click_count:
                await self.pause(0.06, 0.13)
        await self._after_action()

    async def click_locator(
        self,
        locator: Any,
        *,
        page: Any | None = None,
        timeout: int = 8000,
    ) -> None:
        """等待元素可见可用后，采用随机落点 + 贝塞尔轨迹 + 按压驻留点击控件。"""
        wait_for = getattr(locator, "wait_for", None)
        if callable(wait_for):
            await wait_for(state="visible", timeout=timeout)
        scroll_fn = getattr(locator, "scroll_into_view_if_needed", None)
        if callable(scroll_fn):
            try:
                await scroll_fn(timeout=min(timeout, 5000))
            except Exception:
                pass
        enabled = getattr(locator, "is_enabled", None)
        if callable(enabled):
            for _ in range(20):
                try:
                    if await enabled():
                        break
                except Exception:
                    break
                await self.pause(0.05, 0.12)

        target_page = page or getattr(locator, "page", None)
        bbox_fn = getattr(locator, "bounding_box", None)
        box = None
        if callable(bbox_fn):
            try:
                box = await bbox_fn(timeout=min(timeout, 3000))
            except TypeError:
                try:
                    box = await bbox_fn()
                except Exception:
                    box = None
            except Exception:
                box = None

        if (
            isinstance(box, dict)
            and float(box.get("width") or 0) >= 2.0
            and float(box.get("height") or 0) >= 2.0
            and target_page is not None
            and getattr(target_page, "mouse", None) is not None
        ):
            tx, ty = self.sample_box_point(box)
            await self.click_point(target_page, tx, ty)
            return

        # Fallback for custom facade locators (e.g. _SubmitLocator / mocked tests)
        hover_fn = getattr(locator, "hover", None)
        if callable(hover_fn):
            try:
                await hover_fn()
            except Exception:
                pass
        await self.pause(0.14, 0.38)
        hold_ms = int(self.rng.uniform(55, 132))
        try:
            await locator.click(timeout=timeout, delay=hold_ms)
        except TypeError:
            await locator.click()
        await self._after_action()

    async def _field_has_content(self, locator: Any) -> bool:
        try:
            val = await locator.input_value(timeout=1200)
            return bool(str(val or ""))
        except Exception:
            pass
        try:
            txt = await locator.evaluate(
                "el => String(el.value ?? el.innerText ?? el.textContent ?? '')"
            )
            return bool(str(txt or "").strip())
        except Exception:
            return False

    async def clear_if_needed(self, locator: Any, *, page: Any | None = None) -> None:
        """仅在输入框已有内容时执行全选删除，避免空框每次机械触发 Ctrl+A+Backspace。"""
        if not await self._field_has_content(locator):
            return
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        target_page = page or getattr(locator, "page", None)
        await self.pause(0.05, 0.14)
        try:
            await locator.press(f"{modifier}+A")
            await self.pause(0.04, 0.11)
            await locator.press("Backspace")
        except Exception:
            if target_page is not None and hasattr(target_page, "keyboard"):
                await target_page.keyboard.press(f"{modifier}+A")
                await self.pause(0.04, 0.11)
                await target_page.keyboard.press("Backspace")
        await self.pause(0.06, 0.16)

    async def type_text(
        self,
        locator: Any,
        text: str,
        *,
        page: Any | None = None,
        clear: bool = True,
        timeout: int = 8000,
        max_sequential_chars: int = 140,
    ) -> None:
        """拟人文本输入：点击聚焦 -> 按需清空 -> 突发节奏逐字键入（超长文本分段输入）。"""
        content = str(text or "")
        target_page = page or getattr(locator, "page", None)
        await self.click_locator(locator, page=target_page, timeout=timeout)
        await self.pause(0.08, 0.22)
        if clear:
            await self.clear_if_needed(locator, page=target_page)
        if not content:
            return

        if len(content) <= max_sequential_chars:
            await self._type_char_stream(locator, content, page=target_page)
        else:
            # 超长正文：先自然逐字输入开头几个字建立真实键盘事件链，再按语义段落分块录入并穿插审阅停顿
            head_len = min(14, max(6, int(self.rng.randint(6, 14))))
            await self._type_char_stream(locator, content[:head_len], page=target_page)
            await self.pause(0.18, 0.42)
            rest = content[head_len:]
            cursor = 0
            keyboard = getattr(target_page, "keyboard", None) if target_page is not None else None
            while cursor < len(rest):
                chunk_size = int(self.rng.randint(36, 88))
                # 优先在换行或标点处切分段落，贴近人类复制/输入法整句上屏行为
                end = min(len(rest), cursor + chunk_size)
                for punc_idx in range(end - 1, cursor + 12, -1):
                    if rest[punc_idx] in "\n。！？；，,!?;":
                        end = punc_idx + 1
                        break
                chunk = rest[cursor:end]
                if keyboard is not None and hasattr(keyboard, "insert_text"):
                    await keyboard.insert_text(chunk)
                else:
                    await self._type_char_stream(locator, chunk, page=target_page)
                cursor = end
                if cursor < len(rest):
                    await self.pause(0.16, 0.44)
        await self.pause(0.12, 0.32)

    async def _type_char_stream(
        self,
        locator: Any,
        text: str,
        *,
        page: Any | None = None,
    ) -> None:
        burst_left = int(self.rng.randint(3, 7))
        target_page = page or getattr(locator, "page", None)
        keyboard = getattr(target_page, "keyboard", None) if target_page is not None else None
        press_seq = getattr(locator, "press_sequentially", None)

        for ch in text:
            hold_ms = int(self.rng.triangular(24, 78, 42))
            if callable(press_seq):
                await press_seq(ch, delay=hold_ms)
            elif keyboard is not None and hasattr(keyboard, "type"):
                await keyboard.type(ch, delay=hold_ms)
            else:
                await locator.fill(text)
                return

            await self.pause(0.035, 0.135)
            burst_left -= 1
            if ch in ",，。.!！?？;；:：\n":
                await self.pause(0.18, 0.52)
                burst_left = int(self.rng.randint(3, 7))
            elif ch.isspace() or burst_left <= 0:
                await self.pause(0.09, 0.28)
                burst_left = int(self.rng.randint(3, 7))

    async def scroll_wheel(
        self,
        page: Any,
        *,
        distance: int | None = None,
        direction: int = 1,
        settle: bool = True,
    ) -> int:
        """模拟真实鼠标滚轮/触控板阻尼惯性的多帧连续脉冲滚动。"""
        total = (
            int(distance)
            if distance is not None and int(distance) > 0
            else int(self.rng.triangular(380, 920, 610))
        )
        sign = 1 if direction >= 0 else -1
        mouse = getattr(page, "mouse", None)
        if mouse is None or not hasattr(mouse, "wheel"):
            try:
                await page.evaluate(
                    "(dy) => window.scrollBy({ top: dy, behavior: 'smooth' })",
                    sign * total,
                )
            except Exception:
                pass
            if settle:
                await self.pause(0.45, 1.15)
            return sign * total

        # 确保鼠标处于页面可视内容区内，否则容器滚动不会命中主列表
        cx, cy = self._get_cursor(page)
        await mouse.move(cx, cy)

        ticks = max(4, min(10, int(total / 95) + self.rng.randint(2, 4)))
        # 钟形脉冲权重：起手加速 -> 峰值 -> 惯性衰减
        raw_weights = [
            math.sin(math.pi * (i + 0.5) / ticks) * self.rng.uniform(0.82, 1.18)
            for i in range(ticks)
        ]
        weight_sum = sum(raw_weights) or 1.0
        dispatched = 0
        for idx, weight in enumerate(raw_weights):
            if idx == ticks - 1:
                step = max(8, total - dispatched)
            else:
                step = max(8, int(round(total * weight / weight_sum)))
            dispatched += step
            await mouse.wheel(0, sign * step)
            await self.pause(0.014, 0.036)

        if settle:
            await self.pause(0.38, 1.05)
        await self._after_action()
        return sign * dispatched

    @staticmethod
    async def resolve_awaitable(value: bool | Awaitable[bool]) -> bool:
        if inspect.isawaitable(value):
            value = await value
        return bool(value)


DEFAULT_HUMAN_ENGINE = HumanInteractionEngine()


async def human_wait(page: Any | None, base_ms: float, *, jitter: float = 0.32) -> float:
    """便捷函数：拟人随机等待（无需调用固定 page.wait_for_timeout）。"""
    _ = page
    return await DEFAULT_HUMAN_ENGINE.wait_ms(base_ms, jitter=jitter)


async def human_click_first(
    page: Any,
    selectors: Sequence[str],
    *,
    timeout: int = 3000,
) -> bool:
    """在候选选择器列表中找到首个可见元素并执行拟人轨迹点击（严禁 JS el.click）。"""
    per_sel_timeout = max(600, min(timeout, 2500))
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(await loc.count(), 8)
            for idx in range(count):
                cand = loc.nth(idx)
                try:
                    if await cand.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(
                            cand, page=page, timeout=per_sel_timeout
                        )
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


async def human_fill_first(
    page: Any,
    selectors: Sequence[str],
    text: str,
    *,
    clear: bool = True,
    timeout: int = 3500,
) -> str:
    """在候选输入框中找到首个可见元素并执行拟人键入，返回命中的选择器（未命中返回空串）。"""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(await loc.count(), 6)
            for idx in range(count):
                cand = loc.nth(idx)
                try:
                    if await cand.is_visible():
                        await DEFAULT_HUMAN_ENGINE.type_text(
                            cand, text, page=page, clear=clear, timeout=timeout
                        )
                        return sel
                except Exception:
                    continue
        except Exception:
            continue
    return ""


async def human_scroll(
    page: Any,
    *,
    distance: int | None = None,
    direction: int = 1,
    settle: bool = True,
) -> int:
    """便捷函数：多帧惯性滚轮滚动。"""
    return await DEFAULT_HUMAN_ENGINE.scroll_wheel(
        page, distance=distance, direction=direction, settle=settle
    )
