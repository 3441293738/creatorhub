"""指纹浏览器内核扫描、数据库同步与运行管理器注册辅助。"""
from __future__ import annotations

from pathlib import Path
import os

from sqlmodel import select

from app import runtime as app_runtime
from app.browser.runtime_catalog import discover_chromium_runtimes, runtime_metadata
from app.db import get_session
from app.models import BrowserRuntime


def _browser_backend_status(requested: str, runtime_id: str = "") -> dict:
    """Call the runtime-aware manager while keeping lightweight test adapters."""
    checker = app_runtime.browser.backend_status
    try:
        return checker(requested, runtime_id)
    except TypeError:
        return checker(requested)


def _browser_runtime_dict(runtime: BrowserRuntime) -> dict:
    path = Path(runtime.executable_path)
    return {
        "id": runtime.id,
        "runtime_id": runtime.runtime_id,
        "name": runtime.name,
        "backend": runtime.backend,
        "version": runtime.version,
        "major": (runtime.version.split(".", 1)[0]
                  if runtime.version else ""),
        "executable_path": runtime.executable_path,
        "platform": runtime.platform,
        "allow_headless": runtime.allow_headless,
        "enabled": runtime.enabled,
        "is_default": runtime.is_default,
        "status": runtime.status,
        "last_error": runtime.last_error,
        "available": path.is_file(),
        "file_sha256": runtime.file_sha256,
        "last_checked_at": (runtime.last_checked_at.isoformat()
                            if runtime.last_checked_at else None),
        "created_at": (runtime.created_at.isoformat()
                       if runtime.created_at else None),
    }


def _browser_runtime_executable_candidates() -> list[str]:
    """Return explicitly configured executable paths without assuming a drive."""
    values = [
        str(app_runtime.cfg.engine.fingerprint_chromium_path or "").strip(),
        os.environ.get("CREATORHUB_FINGERPRINT_CHROMIUM_PATH", "").strip(),
    ]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = os.path.normcase(str(Path(value).expanduser().resolve()))
        if normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result


def _browser_runtime_scan_roots() -> list[str]:
    """Resolve configured, environment and portable project-local scan roots."""
    values: list[str] = []
    configured = str(app_runtime.cfg.engine.fingerprint_chromium_root or "").strip()
    if configured:
        values.append(configured)
    env_roots = os.environ.get(
        "CREATORHUB_FINGERPRINT_CHROMIUM_ROOTS", "").strip()
    if env_roots:
        values.extend(part.strip() for part in env_roots.split(os.pathsep))
    # Portable deployments can put browser archives beside or inside the
    # project. Only existing narrow directories are considered; no disk-wide
    # scan is performed.
    workspace = Path.cwd()
    for candidate in (
        workspace / "browsers",
        workspace / "browser",
        workspace / "data" / "browsers",
        workspace.parent / "browsers",
    ):
        if candidate.is_dir():
            values.append(str(candidate))
    for executable in _browser_runtime_executable_candidates():
        path = Path(executable).expanduser()
        if path.parent.is_dir():
            values.append(str(path.parent))

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        resolved = str(Path(value).expanduser().resolve())
        normalized = os.path.normcase(resolved)
        if normalized not in seen:
            seen.add(normalized)
            result.append(resolved)
    return result


def _seed_browser_runtimes() -> list[dict]:
    """Import configured/discovered runtimes and return manager specifications."""
    discovered: dict[str, dict] = {}
    configured_paths = _browser_runtime_executable_candidates()
    for configured_path in configured_paths:
        try:
            item = runtime_metadata(configured_path)
            discovered[item["runtime_id"]] = item
        except (OSError, ValueError) as exc:
            print(f"[startup] 指纹内核路径不可用: {exc}")
    for root in _browser_runtime_scan_roots():
        try:
            for item in discover_chromium_runtimes(root):
                discovered[item["runtime_id"]] = item
        except (OSError, ValueError) as exc:
            print(f"[startup] 指纹内核扫描失败 ({root}): {exc}")

    with get_session() as session:
        existing = {
            row.runtime_id: row
            for row in session.exec(select(BrowserRuntime)).all()
        }
        configured_runtime_id = ""
        for runtime_id, item in discovered.items():
            row = existing.get(runtime_id)
            if row is None:
                row = BrowserRuntime(
                    runtime_id=runtime_id,
                    name=item["name"],
                    version=item["version"],
                    executable_path=item["executable_path"],
                    platform=app_runtime.cfg.engine.fingerprint_chromium_platform,
                    allow_headless=app_runtime.cfg.engine.fingerprint_chromium_allow_headless,
                    file_sha256=item["file_sha256"],
                )
                session.add(row)
                existing[runtime_id] = row
            else:
                row.version = item["version"]
                row.executable_path = item["executable_path"]
                row.file_sha256 = item["file_sha256"]
                if not row.name:
                    row.name = item["name"]
                session.add(row)
            if any(os.path.normcase(item["executable_path"])
                   == os.path.normcase(str(Path(path).expanduser().resolve()))
                   for path in configured_paths):
                configured_runtime_id = runtime_id
        session.commit()

        rows = session.exec(select(BrowserRuntime).order_by(BrowserRuntime.id)).all()
        default = next(
            (row for row in rows if row.is_default and row.enabled), None)
        if default is None:
            default = next(
                (row for row in rows
                 if row.runtime_id == configured_runtime_id and row.enabled), None)
        if default is None:
            default = next((row for row in rows if row.enabled), None)
        if default is not None:
            for row in rows:
                wanted = row.id == default.id
                if row.is_default != wanted:
                    row.is_default = wanted
                    session.add(row)
            session.commit()
        return [_browser_runtime_dict(row) for row in rows]


async def _close_runtime_accounts(account_ids: list[int]) -> None:
    for account_id in set(account_ids):
        lease = app_runtime.open_browsers.pop(account_id, None)
        if lease is not None:
            try:
                await lease.close()
            except Exception:
                pass
        if app_runtime.browser is not None:
            try:
                await app_runtime.browser.close_context(account_id)
            except Exception:
                pass
