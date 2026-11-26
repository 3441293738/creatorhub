"""轻量级异步/同步随机抖动节拍器，用于翻页抓取、多作品巡检与并发下载错峰。

独立于风控中心全局开关：即使单次任务内部执行多页拉取，也保持自然的人类节律，
避免固定毫秒间隔或 0ms 突刺请求触发平台反爬检测。
"""
from __future__ import annotations

import asyncio
import random
import time


def jitter_delay(
    base: float,
    *,
    ratio: float = 0.35,
    min_delay: float = 0.05,
    max_delay: float | None = None,
) -> float:
    """在 ``base * [1 - ratio, 1 + ratio]`` 区间采样一个自然抖动延迟（秒）。"""
    b = float(base or 0.0)
    if b <= 0.0:
        return 0.0
    r = max(0.0, min(0.9, float(ratio or 0.0)))
    lo = max(0.05, 1.0 - r)
    hi = 1.0 + r
    val = b * random.uniform(lo, hi)
    if min_delay > 0.0:
        val = max(min_delay, val)
    if max_delay is not None and max_delay > 0.0:
        val = min(max_delay, val)
    return val


async def jittered_sleep(
    base: float,
    *,
    ratio: float = 0.35,
    min_delay: float = 0.05,
    max_delay: float | None = None,
) -> float:
    """异步休眠带随机抖动的时长，返回实际休眠秒数。"""
    delay = jitter_delay(base, ratio=ratio, min_delay=min_delay, max_delay=max_delay)
    if delay > 0.0:
        await asyncio.sleep(delay)
    return delay


def jittered_sleep_sync(
    base: float,
    *,
    ratio: float = 0.35,
    min_delay: float = 0.05,
    max_delay: float | None = None,
) -> float:
    """同步休眠带随机抖动的时长，返回实际休眠秒数。"""
    delay = jitter_delay(base, ratio=ratio, min_delay=min_delay, max_delay=max_delay)
    if delay > 0.0:
        time.sleep(delay)
    return delay


async def download_stagger(min_s: float = 0.18, max_s: float = 0.75) -> float:
    """并发媒体下载启动前的轻度随机错峰，避免同一毫秒并发突刺。"""
    lo = max(0.0, float(min_s))
    hi = max(lo, float(max_s))
    if hi <= 0.0:
        return 0.0
    delay = random.uniform(lo, hi)
    await asyncio.sleep(delay)
    return delay


class PagePacer:
    """翻页/多作品循环节拍器：支持基础随机间隔 + 每隔 N 步的偶发长停顿。

    典型用法（在每次发起请求前调用 ``await pacer.step()``）：
    - 第 1 次调用（首页/首条）不等待，立即返回 ``0.0``；
    - 第 2 次起执行带抖动的基础停顿；
    - 每隔 ``long_pause_every`` 步触发一次模拟人类阅读的较长停顿。
    """

    def __init__(
        self,
        base: float = 0.6,
        *,
        ratio: float = 0.35,
        min_delay: float = 0.15,
        long_pause_every: tuple[int, int] | None = (4, 7),
        long_pause_range: tuple[float, float] = (1.6, 3.6),
    ) -> None:
        self.base = max(0.0, float(base))
        self.ratio = max(0.0, min(0.9, float(ratio)))
        self.min_delay = max(0.0, float(min_delay))
        self._steps = 0
        self._long_pause_every = long_pause_every
        self._long_pause_range = long_pause_range
        self._next_long_pause = (
            random.randint(long_pause_every[0], long_pause_every[1])
            if long_pause_every and long_pause_every[0] > 0
            else 0
        )

    async def step(self) -> float:
        self._steps += 1
        if self._steps <= 1 or self.base <= 0.0:
            return 0.0
        if self._next_long_pause and self._steps >= self._next_long_pause:
            lo, hi = self._long_pause_range
            delay = random.uniform(max(self.min_delay, lo), max(lo, hi))
            assert self._long_pause_every is not None
            self._next_long_pause += random.randint(
                self._long_pause_every[0], self._long_pause_every[1]
            )
            await asyncio.sleep(delay)
            return delay
        return await jittered_sleep(
            self.base, ratio=self.ratio, min_delay=self.min_delay
        )
