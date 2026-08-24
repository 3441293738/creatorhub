"""Pluggable browser runtime launch plans.

The account/profile lifecycle remains owned by :mod:`app.browser.manager`.
Backends in this module only describe which Chromium executable to launch and
which engine-level identity arguments it needs.  This keeps platform code on
the existing Playwright-compatible BrowserContext/Page API.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .identity import Identity


DEFAULT_BACKEND = "default"
LOCAL_BACKEND = "local"
FINGERPRINT_CHROMIUM_BACKEND = "fingerprint_chromium"
ACCOUNT_BROWSER_BACKENDS = {
    DEFAULT_BACKEND,
    LOCAL_BACKEND,
    FINGERPRINT_CHROMIUM_BACKEND,
}


class BrowserBackendError(RuntimeError):
    """Base error raised by a configured browser runtime."""


class BrowserBackendUnavailableError(BrowserBackendError):
    """The selected runtime cannot be launched on this machine."""


@dataclass(frozen=True)
class BrowserLaunchPlan:
    name: str
    label: str
    executable_path: str
    args: tuple[str, ...]
    headless: bool
    engine_controlled_identity: bool = False
    runtime_id: str = ""
    version: str = ""


class BrowserRuntimeBackend(Protocol):
    name: str
    label: str

    @property
    def available(self) -> bool: ...

    @property
    def unavailable_reason(self) -> str: ...

    def launch_plan(
        self, identity: Identity, *, requested_headless: bool,
    ) -> BrowserLaunchPlan: ...


def fingerprint_seed_u32(seed: str) -> int:
    """Map an arbitrary persistent account seed to Chromium's uint32 seed."""
    raw = str(seed or "0").encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big")


def _host_fingerprint_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def _accept_language(locale: str) -> str:
    value = str(locale or "zh-CN").strip() or "zh-CN"
    base = value.split("-", 1)[0]
    return value if base == value else f"{value},{base}"


class FingerprintChromiumBackend:
    """Engine-level fingerprint Chromium launched by Patchright.

    The browser owns Canvas/WebGL/Audio/navigator identity.  CreatorHub must
    therefore skip its legacy JavaScript fingerprint injection for this plan.
    """

    name = FINGERPRINT_CHROMIUM_BACKEND
    label = "Fingerprint Chromium · 开源内核"

    def __init__(
        self,
        executable_path: str = "",
        *,
        allow_headless: bool = False,
        platform: str = "auto",
        runtime_id: str = "",
        version: str = "",
        label: str = "",
    ):
        self._raw_path = str(executable_path or "").strip()
        self.allow_headless = bool(allow_headless)
        self.runtime_id = str(runtime_id or "").strip()
        self.version = str(version or "").strip()
        self.label = str(label or "").strip() or self.__class__.label
        requested_platform = str(platform or "auto").strip().lower()
        self.platform = (
            _host_fingerprint_platform()
            if requested_platform == "auto"
            else requested_platform
        )
        if self.platform not in {"windows", "linux", "macos"}:
            self.platform = _host_fingerprint_platform()

    @property
    def executable_path(self) -> Path | None:
        if not self._raw_path:
            return None
        return Path(self._raw_path).expanduser().resolve()

    @property
    def available(self) -> bool:
        path = self.executable_path
        return bool(path and path.is_file())

    @property
    def unavailable_reason(self) -> str:
        if not self._raw_path:
            return "未配置 engine.fingerprint_chromium_path"
        if not self.available:
            return "fingerprint_chromium_path 指向的浏览器不存在"
        return ""

    def launch_plan(
        self, identity: Identity, *, requested_headless: bool,
    ) -> BrowserLaunchPlan:
        path = self.executable_path
        if path is None or not path.is_file():
            raise BrowserBackendUnavailableError(self.unavailable_reason)

        locale = str(identity.locale or "zh-CN").strip() or "zh-CN"
        timezone = (
            str(identity.timezone_id or "Asia/Shanghai").strip()
            or "Asia/Shanghai"
        )
        args = (
            f"--fingerprint={fingerprint_seed_u32(identity.fp_seed)}",
            f"--fingerprint-platform={self.platform}",
            "--fingerprint-brand=Chrome",
            f"--timezone={timezone}",
            f"--lang={locale}",
            f"--accept-lang={_accept_language(locale)}",
            "--disable-non-proxied-udp",
        )
        return BrowserLaunchPlan(
            name=self.name,
            label=self.label,
            executable_path=str(path),
            args=args,
            # The upstream runtime documents that headless only normalizes the
            # UA and still leaks other headless traits.  Keep it opt-in.
            headless=bool(requested_headless and self.allow_headless),
            engine_controlled_identity=True,
            runtime_id=self.runtime_id,
            version=self.version,
        )
