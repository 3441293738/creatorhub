"""指纹浏览器内核管理、自动扫描、启动验证与后端列表接口。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.browser.runtime_catalog import discover_chromium_runtimes, runtime_metadata
from app.db import get_session
from app.models import BrowserRuntime, DouyinAccount
from app.services import browser_runtimes as browser_runtimes_service

router = APIRouter()


@router.get("/api/browser-backends")
async def list_browser_backends():
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    return app_runtime.browser.backend_catalog()


def _register_runtime_with_manager(runtime: BrowserRuntime | dict) -> None:
    if app_runtime.browser is None:
        return
    item = (browser_runtimes_service._browser_runtime_dict(runtime)
            if isinstance(runtime, BrowserRuntime) else runtime)
    app_runtime.browser.register_fingerprint_runtime(
        item["runtime_id"], item["executable_path"],
        allow_headless=bool(item.get("allow_headless", False)),
        platform=str(item.get("platform") or "auto"),
        version=str(item.get("version") or ""),
        label=str(item.get("name") or ""),
        enabled=bool(item.get("enabled", True)),
    )


def _upsert_browser_runtime(
        session, item: dict, *, name: str = "",
        platform: str = "auto", allow_headless: bool = False,
        enable_new: bool = True) -> tuple[BrowserRuntime, bool]:
    row = session.exec(select(BrowserRuntime).where(
        BrowserRuntime.runtime_id == item["runtime_id"])).first()
    created = row is None
    if row is None:
        row = BrowserRuntime(
            runtime_id=item["runtime_id"],
            enabled=enable_new,
            platform=platform,
            allow_headless=allow_headless,
        )
    row.name = str(name or row.name or item["name"]).strip()[:120]
    row.version = item["version"]
    row.executable_path = item["executable_path"]
    row.file_sha256 = item["file_sha256"]
    row.platform = str(row.platform or platform or "auto")
    row.last_error = ""
    if not row.status:
        row.status = "unknown"
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, created


def _runtime_account_ids(session, runtime_id: str, *, followers: bool = False) -> list[int]:
    rows = session.exec(select(DouyinAccount)).all()
    ids = []
    for account in rows:
        if account.id is None:
            continue
        selected = str(account.browser_runtime_id or "").strip()
        if selected == runtime_id or (followers and not selected):
            effective = (app_runtime.browser.effective_browser_backend(account)
                         if app_runtime.browser is not None else account.browser_backend)
            if effective == "fingerprint_chromium":
                ids.append(account.id)
    return ids


class BrowserRuntimeAddIn(BaseModel):
    executable_path: str
    name: str = ""
    platform: str = "auto"
    allow_headless: bool = False


class BrowserRuntimeScanIn(BaseModel):
    root: str = ""


class BrowserRuntimeUpdateIn(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    allow_headless: bool | None = None


@router.get("/api/browser-runtimes")
async def list_browser_runtimes():
    roots = browser_runtimes_service._browser_runtime_scan_roots()
    with get_session() as session:
        rows = session.exec(select(BrowserRuntime).order_by(
            BrowserRuntime.is_default.desc(), BrowserRuntime.id)).all()
        return {
            "root": roots[0] if roots else "",
            "scan_roots": roots,
            "default_runtime_id": next(
                (row.runtime_id for row in rows if row.is_default), ""),
            "runtimes": [browser_runtimes_service._browser_runtime_dict(row) for row in rows],
        }


@router.post("/api/browser-runtimes")
async def add_browser_runtime(body: BrowserRuntimeAddIn):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    platform = str(body.platform or "auto").strip().lower()
    if platform not in {"auto", "windows", "linux", "macos"}:
        raise HTTPException(400, "内核平台取值无效")
    try:
        item = runtime_metadata(body.executable_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    with get_session() as session:
        row, created = _upsert_browser_runtime(
            session, item, name=body.name, platform=platform,
            allow_headless=body.allow_headless)
        rows = session.exec(select(BrowserRuntime)).all()
        if not any(candidate.is_default for candidate in rows):
            row.is_default = True
            session.add(row)
            session.commit()
            session.refresh(row)
        payload = browser_runtimes_service._browser_runtime_dict(row)
    _register_runtime_with_manager(payload)
    if payload["is_default"]:
        app_runtime.browser.set_default_fingerprint_runtime(payload["runtime_id"])
    return {"ok": True, "created": created, "runtime": payload}


@router.post("/api/browser-runtimes/scan")
async def scan_browser_runtimes(body: BrowserRuntimeScanIn | None = None):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    requested_root = str(body.root if body else "").strip()
    roots = ([str(Path(requested_root).expanduser().resolve())]
             if requested_root else browser_runtimes_service._browser_runtime_scan_roots())
    if not roots:
        raise HTTPException(400, "请先配置内核目录或输入扫描目录")
    discovered_by_id: dict[str, dict] = {}
    errors = []
    for root in roots:
        try:
            for item in discover_chromium_runtimes(root):
                discovered_by_id[item["runtime_id"]] = item
        except (OSError, ValueError) as exc:
            errors.append(f"{root}: {exc}")
    if requested_root and errors:
        raise HTTPException(400, errors[0])
    discovered = list(discovered_by_id.values())
    payloads = []
    created_count = 0
    with get_session() as session:
        for item in discovered:
            row, created = _upsert_browser_runtime(
                session, item,
                platform=app_runtime.cfg.engine.fingerprint_chromium_platform,
                allow_headless=app_runtime.cfg.engine.fingerprint_chromium_allow_headless)
            payloads.append(browser_runtimes_service._browser_runtime_dict(row))
            created_count += int(created)
        rows = session.exec(select(BrowserRuntime).order_by(BrowserRuntime.id)).all()
        if rows and not any(row.is_default and row.enabled for row in rows):
            default = next((row for row in rows if row.enabled), None)
            if default is not None:
                for row in rows:
                    row.is_default = row.id == default.id
                    session.add(row)
                session.commit()
                payloads = [browser_runtimes_service._browser_runtime_dict(row) for row in session.exec(
                    select(BrowserRuntime).where(
                        BrowserRuntime.runtime_id.in_(
                            [item["runtime_id"] for item in discovered]))).all()]
        default_id = next(
            (row.runtime_id for row in session.exec(select(BrowserRuntime)).all()
             if row.is_default and row.enabled), "")
    for payload in payloads:
        _register_runtime_with_manager(payload)
    if default_id:
        app_runtime.browser.set_default_fingerprint_runtime(default_id)
    return {
        "ok": True, "root": roots[0], "roots": roots,
        "found": len(discovered), "created": created_count,
        "runtimes": payloads, "errors": errors,
    }


@router.put("/api/browser-runtimes/{runtime_id}")
async def update_browser_runtime(runtime_id: str, body: BrowserRuntimeUpdateIn):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    old_default = ""
    close_ids: list[int] = []
    with get_session() as session:
        row = session.exec(select(BrowserRuntime).where(
            BrowserRuntime.runtime_id == runtime_id)).first()
        if row is None:
            raise HTTPException(404, "内核运行时不存在")
        old_default = next((candidate.runtime_id for candidate in session.exec(
            select(BrowserRuntime)).all() if candidate.is_default), "")
        if body.name is not None:
            row.name = str(body.name or "").strip()[:120] or row.name
        if body.allow_headless is not None:
            row.allow_headless = bool(body.allow_headless)
        if body.enabled is not None:
            row.enabled = bool(body.enabled)
        if body.is_default:
            row.enabled = True
            for candidate in session.exec(select(BrowserRuntime)).all():
                candidate.is_default = candidate.runtime_id == runtime_id
                session.add(candidate)
        elif not row.enabled and row.is_default:
            replacement = session.exec(select(BrowserRuntime).where(
                BrowserRuntime.runtime_id != runtime_id,
                BrowserRuntime.enabled == True).order_by(  # noqa: E712
                    BrowserRuntime.id)).first()
            row.is_default = False
            if replacement is not None:
                replacement.is_default = True
                session.add(replacement)
        session.add(row)
        session.commit()
        rows = session.exec(select(BrowserRuntime)).all()
        payload = browser_runtimes_service._browser_runtime_dict(session.exec(select(BrowserRuntime).where(
            BrowserRuntime.runtime_id == runtime_id)).first())
        default_id = next(
            (candidate.runtime_id for candidate in rows
             if candidate.is_default and candidate.enabled), "")
        close_ids = _runtime_account_ids(
            session, runtime_id,
            followers=(old_default == runtime_id or default_id == runtime_id))
    _register_runtime_with_manager(payload)
    if default_id:
        app_runtime.browser.set_default_fingerprint_runtime(default_id)
    elif app_runtime.browser.default_fingerprint_runtime_id == runtime_id:
        app_runtime.browser.clear_default_fingerprint_runtime()
    await browser_runtimes_service._close_runtime_accounts(close_ids)
    return {"ok": True, "runtime": payload,
            "default_runtime_id": default_id}


@router.post("/api/browser-runtimes/{runtime_id}/test")
async def test_browser_runtime(runtime_id: str):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        row = session.exec(select(BrowserRuntime).where(
            BrowserRuntime.runtime_id == runtime_id)).first()
        if row is None:
            raise HTTPException(404, "内核运行时不存在")
        payload = browser_runtimes_service._browser_runtime_dict(row)
    _register_runtime_with_manager(payload)
    checked_at = datetime.utcnow()
    try:
        result = await app_runtime.browser.probe_fingerprint_runtime(
            runtime_id, Path(app_runtime.cfg.engine.profiles_dir) / "_runtime_probes")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:500]
        with get_session() as session:
            row = session.exec(select(BrowserRuntime).where(
                BrowserRuntime.runtime_id == runtime_id)).first()
            if row is not None:
                row.status = "bad"
                row.last_error = detail
                row.last_checked_at = checked_at
                session.add(row)
                session.commit()
        raise HTTPException(400, detail) from exc
    with get_session() as session:
        row = session.exec(select(BrowserRuntime).where(
            BrowserRuntime.runtime_id == runtime_id)).first()
        if row is not None:
            row.status = "ok"
            row.last_error = ""
            row.last_checked_at = checked_at
            session.add(row)
            session.commit()
    return result


@router.delete("/api/browser-runtimes/{runtime_id}")
async def delete_browser_runtime(runtime_id: str):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        row = session.exec(select(BrowserRuntime).where(
            BrowserRuntime.runtime_id == runtime_id)).first()
        if row is None:
            raise HTTPException(404, "内核运行时不存在")
        bound = session.exec(select(DouyinAccount).where(
            DouyinAccount.browser_runtime_id == runtime_id)).all()
        if bound:
            raise HTTPException(
                409, f"仍有 {len(bound)} 个账号绑定此内核，请先切换账号环境")
        was_default = row.is_default
        session.delete(row)
        session.commit()
        replacement = session.exec(select(BrowserRuntime).where(
            BrowserRuntime.enabled == True).order_by(  # noqa: E712
                BrowserRuntime.id)).first()
        if was_default and replacement is not None:
            replacement.is_default = True
            session.add(replacement)
            session.commit()
        default_id = replacement.runtime_id if was_default and replacement else next(
            (candidate.runtime_id for candidate in session.exec(
                select(BrowserRuntime)).all() if candidate.is_default), "")
        follower_ids = _runtime_account_ids(
            session, runtime_id, followers=was_default)
    app_runtime.browser.unregister_fingerprint_runtime(runtime_id)
    if default_id:
        app_runtime.browser.set_default_fingerprint_runtime(default_id)
    await browser_runtimes_service._close_runtime_accounts(follower_ids)
    return {"ok": True, "default_runtime_id": default_id}
