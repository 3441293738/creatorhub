"""平台账号列表、环境指纹配置、浏览器后端切换、资料刷新与删号生命周期接口。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import asyncio
import hashlib
import os
import shutil
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.account_lifecycle import active_account_task, retire_account_tasks
from app.browser.backends import ACCOUNT_BROWSER_BACKENDS, LOCAL_BACKEND, fingerprint_seed_u32
from app.browser.ip_fingerprint import derive_ip_fingerprint
from app.db import get_session
from app.models import AccountRiskState, DouyinAccount, MonitorTarget
from app.platforms.rednote import has_web_session as rednote_has_web_session
from app.platforms.xhs import has_creator_cookies
from app.risk import OperationKind, RiskController
from app.services import account_profiles as account_profiles_service
from app.services import browser_runtimes as browser_runtimes_service
from app.services import login_flows as login_flows_service
from app.services import login_sessions as login_sessions_service
from app.services import proxies as proxies_service
from app.transport_matrix import resolve_transport

router = APIRouter()


@router.get("/api/accounts")
async def list_accounts(platform: str | None = None):
    risk_controller = app_runtime.engine.risk if app_runtime.engine else RiskController(app_runtime.cfg)
    with get_session() as s:
        all_accounts = s.exec(select(DouyinAccount)).all()
        profile_keys: dict[int, str] = {}
        profile_counts: dict[str, int] = {}
        for account in all_accounts:
            if not account.id or not account.profile_dir:
                continue
            normalized = os.path.normcase(str(
                Path(account.profile_dir).expanduser().resolve()))
            profile_keys[account.id] = normalized
            profile_counts[normalized] = profile_counts.get(normalized, 0) + 1
        q = select(DouyinAccount)
        if platform:
            q = q.where(DouyinAccount.platform == platform)
        accs = s.exec(q).all()
        out = []
        for a in accs:
            profile_key = profile_keys.get(a.id or 0, "")
            risk_state = s.get(AccountRiskState, a.id) if a.id else None
            next_write_at = risk_controller.next_write_at(a.id) if a.id else None
            used = len(s.exec(select(MonitorTarget.id)
                              .where(MonitorTarget.account_id == a.id)).all())
            environment = None
            environment_check = None
            if app_runtime.browser is not None:
                try:
                    account_identity = app_runtime.browser.identity_for(a)
                    environment = app_runtime.browser.environment_snapshot(
                        account_identity, headless=False)
                    check_status = getattr(
                        app_runtime.browser, "environment_check_status", None)
                    if callable(check_status):
                        environment_check = check_status(account_identity)
                except Exception:
                    environment = None
                    environment_check = None
            has_creator = (
                bool(a.creator_storage_state)
                or (a.platform in ("kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "rednote", "instagram", "twitter")
                    and bool(a.storage_state))
                or (a.platform == "xhs" and has_creator_cookies(a.storage_state))
            )
            has_read_login = (
                account_profiles_service._xhs_has_read_login_state(a.storage_state)
                if a.platform == "xhs"
                else rednote_has_web_session(a.storage_state)
                if a.platform == "rednote"
                else bool(a.storage_state)
            )
            out.append({
                "id": a.id, "platform": a.platform, "nickname": a.nickname, "status": a.status,
                "sec_uid": a.sec_uid, "douyin_id": a.douyin_id, "avatar": a.avatar,
                "follower_count": a.follower_count,
                "following_count": a.following_count,
                "aweme_count": a.aweme_count,
                "total_favorited": a.total_favorited,
                "gender": a.gender,
                "has_creator": has_creator,
                "has_read_login": has_read_login,
                "kind": "creator" if has_creator else "fetch",
                "has_storage": has_read_login,
                "login_type": "cookie" if a.cookie else "scan",
                "monitor_count": used,
                # 风控隔离画像
                "proxy": proxies_service._mask_proxy(a.proxy),
                "proxy_status": a.proxy_status,
                "has_proxy": bool(a.proxy),
                "exit_ip": a.exit_ip,
                "exit_country": a.exit_country,
                "exit_asn": a.exit_asn,
                "exit_timezone": a.exit_timezone,
                "exit_checked_at": (a.exit_checked_at.isoformat()
                                    if a.exit_checked_at else None),
                "write_paused_until": (a.write_paused_until.isoformat()
                                        if a.write_paused_until else None),
                "write_pause_reason": a.write_pause_reason,
                "identity_mode": a.identity_mode,
                "browser_backend": a.browser_backend,
                "browser_runtime_id": a.browser_runtime_id,
                "fingerprint_id": (a.fp_seed or "")[:12],
                "fingerprint_ip": a.fp_source_ip,
                "fingerprint_country": a.fp_country,
                "fingerprint_region": a.fp_region,
                "fingerprint_city": a.fp_city,
                "fingerprint_timezone": a.timezone_id,
                "fingerprint_locale": a.locale,
                "fingerprint_generated_at": (
                    a.fp_generated_at.isoformat() if a.fp_generated_at else None),
                "fingerprint_ip_matches_exit": bool(
                    a.fp_source_ip and a.exit_ip and a.fp_source_ip == a.exit_ip),
                "risk_level": risk_state.risk_level if risk_state else 0,
                "risk_cooldown_until": (
                    risk_state.cooldown_until.isoformat()
                    if risk_state and risk_state.cooldown_until else None),
                "risk_signal": risk_state.last_risk_reason if risk_state else "",
                "next_write_at": (next_write_at.isoformat()
                                  if next_write_at else None),
                "ua": a.ua,
                "profile_dir": a.profile_dir,
                "profile_isolated": bool(
                    profile_key and profile_counts.get(profile_key) == 1),
                "profile_isolation_id": (
                    hashlib.sha256(profile_key.encode("utf-8")).hexdigest()[:10]
                    if profile_key else ""),
                "environment": environment,
                "environment_check": environment_check,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
        return out


@router.get("/api/accounts/{account_id}/environment")
async def account_browser_environment(account_id: int):
    """Return redacted browser-backend diagnostics for one account."""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        identity = app_runtime.browser.identity_for(account)
    return app_runtime.browser.environment_snapshot(identity, headless=False)


def _account_fingerprint_payload(account: DouyinAccount) -> dict:
    return {
        "account_id": account.id,
        "fingerprint_id": (account.fp_seed or "")[:12],
        "seed": account.fp_seed,
        "engine_seed": fingerprint_seed_u32(account.fp_seed),
        "source_ip": account.fp_source_ip,
        "country": account.fp_country,
        "region": account.fp_region,
        "city": account.fp_city,
        "timezone": account.timezone_id,
        "locale": account.locale,
        "accept_languages": account.fp_accept_languages,
        "viewport": f"{account.viewport_w}x{account.viewport_h}",
        "viewport_w": account.viewport_w,
        "viewport_h": account.viewport_h,
        "geo": ({"latitude": account.geo_lat, "longitude": account.geo_lon}
                if account.geo_lat or account.geo_lon else None),
        "geo_lat": account.geo_lat,
        "geo_lon": account.geo_lon,
        "platform": account.fp_platform,
        "platform_version": account.fp_platform_version,
        "brand": account.fp_brand,
        "brand_version": account.fp_brand_version,
        "hardware_concurrency": account.fp_hardware_concurrency,
        "gpu_vendor": account.fp_gpu_vendor,
        "gpu_renderer": account.fp_gpu_renderer,
        "disable_spoofing": [
            value for value in account.fp_disable_spoofing.split(",") if value],
        "language_mode": account.fp_language_mode,
        "timezone_mode": account.fp_timezone_mode,
        "viewport_mode": account.fp_viewport_mode,
        "location_mode": account.fp_location_mode,
        "geolocation_permission": account.fp_geolocation_permission,
        "webrtc_mode": account.fp_webrtc_mode,
        "extra_args": account.fp_extra_args,
        "actual_ua": account.ua,
        "generated_at": (account.fp_generated_at.isoformat()
                         if account.fp_generated_at else None),
        "browser_backend": account.browser_backend,
        "browser_runtime_id": account.browser_runtime_id,
        "exit_ip": account.exit_ip,
        "ip_matches_exit": bool(
            account.fp_source_ip and account.exit_ip
            and account.fp_source_ip == account.exit_ip),
    }


@router.get("/api/accounts/{account_id}/fingerprint")
async def account_fingerprint(account_id: int):
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        return _account_fingerprint_payload(account)


@router.put("/api/accounts/{account_id}/fingerprint")
async def update_account_fingerprint(
        account_id: int, body: proxies_service.AccountFingerprintUpdateIn):
    fields = proxies_service._validate_fingerprint_update(body)
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        for name, value in fields.items():
            setattr(account, name, value)
        account.fp_generated_at = datetime.utcnow()
        session.add(account)
        session.commit()
        session.refresh(account)
        payload = _account_fingerprint_payload(account)
    lease = app_runtime.open_browsers.pop(account_id, None)
    if lease is not None:
        try:
            await lease.close()
        except Exception:
            pass
    if app_runtime.browser is not None:
        await app_runtime.browser.close_context(account_id)
    return {"ok": True, "fingerprint": payload}


@router.post("/api/accounts/{account_id}/fingerprint/from-ip")
async def generate_account_fingerprint_from_ip(account_id: int):
    """Probe the current egress and persist a stable, locality-aligned device."""
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        proxy = account.proxy or ""
        fallback = {
            "ip": account.exit_ip,
            "country": account.exit_country or account.fp_country,
            "region": account.fp_region,
            "city": account.fp_city,
            "timezone": account.exit_timezone or account.timezone_id,
            "lat": account.geo_lat,
            "lon": account.geo_lon,
        }

    geo = await proxies_service._proxy_geo(proxy)
    if not geo or not geo.get("ip"):
        geo = fallback if fallback.get("ip") else None
    if not geo:
        raise HTTPException(400, "未取得当前出口 IP，请先确认代理可用后重试")

    try:
        fields = derive_ip_fingerprint(
            account_id,
            geo.get("ip", ""),
            country=geo.get("country", ""),
            region=geo.get("region", ""),
            city=geo.get("city", ""),
            timezone_id=geo.get("timezone", ""),
            latitude=geo.get("lat") or 0.0,
            longitude=geo.get("lon") or 0.0,
            fallback_timezone=fallback.get("timezone") or "Asia/Shanghai",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        account.fp_seed = fields["fp_seed"]
        account.fp_source_ip = fields["source_ip"]
        account.fp_country = fields["country"]
        account.fp_region = fields["region"]
        account.fp_city = fields["city"]
        account.fp_generated_at = datetime.utcnow()
        account.timezone_id = fields["timezone_id"]
        account.locale = fields["locale"]
        account.viewport_w = fields["viewport_w"]
        account.viewport_h = fields["viewport_h"]
        account.geo_lat = fields["geo_lat"]
        account.geo_lon = fields["geo_lon"]
        # “按 IP 自动生成”同时恢复内核的种子派生值；之后仍可在编辑器逐项覆盖。
        account.fp_platform = ""
        account.fp_platform_version = ""
        account.fp_brand = ""
        account.fp_brand_version = ""
        account.fp_hardware_concurrency = 0
        account.fp_gpu_vendor = ""
        account.fp_gpu_renderer = ""
        account.fp_accept_languages = ""
        account.fp_disable_spoofing = ""
        account.fp_language_mode = "auto"
        account.fp_timezone_mode = "auto"
        account.fp_viewport_mode = "auto"
        account.fp_location_mode = "auto"
        account.fp_geolocation_permission = "allow"
        account.fp_webrtc_mode = "conceal"
        account.fp_extra_args = ""
        session.add(account)
        session.commit()
        session.refresh(account)
        payload = _account_fingerprint_payload(account)

    # A running context was created with the old engine seed/timezone.  Release
    # both user-opened and background contexts before returning the new device.
    lease = app_runtime.open_browsers.pop(account_id, None)
    if lease is not None:
        try:
            await lease.close()
        except Exception:
            pass
    if app_runtime.browser is not None:
        await app_runtime.browser.close_context(account_id)
    return {"ok": True, "fingerprint": payload}


class AccountBrowserBackendIn(BaseModel):
    browser_backend: str
    browser_runtime_id: str = ""


@router.put("/api/accounts/{account_id}/browser-backend")
async def set_account_browser_backend(
        account_id: int, body: AccountBrowserBackendIn):
    """Select an account runtime and close any context using the old one."""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    requested = str(body.browser_backend or "").strip().lower()
    runtime_id = str(body.browser_runtime_id or "").strip()
    if requested not in ACCOUNT_BROWSER_BACKENDS:
        raise HTTPException(400, "浏览器后端取值无效")
    status = browser_runtimes_service._browser_backend_status(requested, runtime_id)
    if not status["available"]:
        raise HTTPException(
            400, f"浏览器后端不可用:{status['detail']}")

    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        if account.platform == "xhs" and status.get("name") != LOCAL_BACKEND:
            raise HTTPException(
                400,
                "小红书账号仅支持系统 Chrome/CDP 原生环境，不能切换到指纹内核",
            )

    # A user-held headed context owns the same profile and must be released
    # before switching its runtime executable.
    lease = app_runtime.open_browsers.pop(account_id, None)
    if lease is not None:
        try:
            await lease.close()
        except Exception:
            pass

    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:  # pragma: no cover - guarded above; handles races
            raise HTTPException(404, "账号不存在")
        account.browser_backend = requested
        account.browser_runtime_id = (
            runtime_id if requested == "fingerprint_chromium" else "")
        session.add(account)
        session.commit()
        session.refresh(account)
        identity = app_runtime.browser.identity_for(account)

    await app_runtime.browser.close_context(account_id)
    environment = app_runtime.browser.environment_snapshot(identity, headless=False)
    return {
        "ok": True,
        "browser_backend": requested,
        "browser_runtime_id": identity.browser_runtime_id,
        "effective_backend": status["name"],
        "environment": environment,
    }


@router.delete("/api/accounts/{account_id}")
async def del_account(account_id: int):
    pdir = ""
    shared_profile = False
    runtime_cleanup_error = ""
    counts = {"canceled_tasks": 0, "disabled_rules": 0, "disabled_monitors": 0}
    if any(state.get("account_id") == account_id
           and state.get("status") in login_sessions_service._ACTIVE_LOGIN_STATUSES
           for state in app_runtime.login_tasks.values()):
        raise HTTPException(409, "该账号正在登录，请先完成或关闭登录窗口")
    lock_for = getattr(app_runtime.browser, "lock_for", None)
    lock = lock_for(f"acc:{account_id}") if callable(lock_for) else asyncio.Lock()
    if lock.locked():
        raise HTTPException(409, "该账号有操作正在执行，请等待完成后删除")
    async with lock:
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if acc:
                active = active_account_task(s, account_id)
                if active:
                    raise HTTPException(409, active)
                pdir = acc.profile_dir or ""
                if pdir:
                    key = os.path.normcase(str(Path(pdir).expanduser().resolve()))
                    shared_profile = any(
                        other.profile_dir and os.path.normcase(str(
                            Path(other.profile_dir).expanduser().resolve())) == key
                        for other in s.exec(select(DouyinAccount).where(
                            DouyinAccount.id != account_id)).all())
                counts = retire_account_tasks(s, account_id)
                risk_state = s.get(AccountRiskState, account_id)
                if risk_state:
                    s.delete(risk_state)
                # Keep task/content history and RiskEvent audit records. The
                # independent ID reservation prevents reassignment to new users.
                s.delete(acc)
                s.commit()
        for manager in (getattr(app_runtime.engine, "dm_automation", None), app_runtime.im_receiver):
            stop_account = getattr(manager, "stop_account", None)
            if callable(stop_account):
                try:
                    await stop_account(account_id)
                except Exception:
                    runtime_cleanup_error = "账号已删除；后台监听清理失败，已保留 Profile，请重启服务后清理"
        await browser_runtimes_service._close_runtime_accounts([account_id])
    # 删号同时清理其持久 profile(释放磁盘);代理回到池里(占用计数自然下降)
    profile_removed = False
    profile_cleanup_error = runtime_cleanup_error
    if pdir:
        root = Path(app_runtime.cfg.engine.profiles_dir).expanduser().resolve()
        candidate = Path(pdir).expanduser().resolve()
        try:
            candidate.relative_to(root)
            managed = candidate != root
        except ValueError:
            managed = False
        if runtime_cleanup_error:
            pass
        elif shared_profile:
            profile_cleanup_error = "其他账号仍使用此 Profile，已保留目录"
        elif managed:
            try:
                if candidate.exists():
                    shutil.rmtree(candidate)
                profile_removed = True
            except OSError as exc:
                profile_cleanup_error = str(exc)[:240]
        else:
            profile_cleanup_error = "Profile 不在受管目录内，已跳过磁盘清理"
    return {
        "ok": True,
        "profile_removed": profile_removed,
        "profile_cleanup_error": profile_cleanup_error,
        **counts,
    }


@router.post("/api/accounts/{account_id}/refresh-profile")
async def refresh_account_profile(account_id: int):
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        state = acc.storage_state or acc.creator_storage_state
        platform = acc.platform
        creator_state = acc.creator_storage_state or ""
        profile_transport = (resolve_transport(
            app_runtime.cfg, "douyin", "account_profile", acc)
            if platform == "douyin" else None)
    manual_browser = app_runtime.open_browsers.get(account_id)
    if (manual_browser is not None
            and bool(getattr(manual_browser, "active", True))
            and (profile_transport is None or profile_transport["opens_browser"])):
        return {
            "ok": True,
            "skipped": True,
            "reason": "该账号浏览器窗口仍开着，请先关闭窗口再刷新资料",
            "blocked_by": "open_browser",
        }
    if not state:
        raise HTTPException(400, "该账号无浏览器登录态(Cookie 粘贴账号可能不含完整态),无法拉取资料")

    async def _refresh_profile():
        return await account_profiles_service._enrich_account_profile(
            account_id, state, detailed=True)

    res, outcome = await account_profiles_service._run_account_read(
        account_id, OperationKind.READ_LIGHT,
        f"refresh-profile:{account_id}", _refresh_profile,
        empty_result={"ok": True}, allow_invalid_probe=True)
    if isinstance(outcome, dict):
        return outcome
    if res == "invalid":
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    if res != "ok":
        tag = ("[xhs_self_profile]" if platform == "xhs"
               else "[rednote_self_profile]" if platform == "rednote"
               else "[ks_self_profile]" if platform == "kuaishou"
               else "[ig_self_profile]" if platform == "instagram"
               else "[tw_self_profile]" if platform == "twitter"
               else "[self_profile]")
        raise HTTPException(400, f"未能获取账号资料:请看服务端控制台 {tag} 那行日志"
                                 "(含它实际看到的请求),把它发我即可定位")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        has_read_login = (
            account_profiles_service._xhs_has_read_login_state(acc.storage_state)
            if acc and acc.platform == "xhs"
            else rednote_has_web_session(acc.storage_state)
            if acc and acc.platform == "rednote"
            else bool(acc and acc.storage_state)
        )
        has_creator_login = bool(acc and (
            acc.creator_storage_state
            or (acc.platform in ("kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "rednote", "instagram", "twitter")
                and acc.storage_state)
            or (acc.platform == "xhs" and has_creator_cookies(acc.storage_state))
        ))
        return {"ok": True, "nickname": acc.nickname, "platform": acc.platform,
                "douyin_id": acc.douyin_id, "sec_uid": acc.sec_uid,
                "status": acc.status,
                "login_scope": ("creator" if platform in ("xhs", "rednote") and creator_state and not has_read_login
                                  else "read"),
                "has_read_login": has_read_login,
                "has_creator": has_creator_login}


@router.post("/api/accounts/{account_id}/relogin/start")
async def relogin_start(account_id: int, scope: str = "auto"):
    """重新登录:更新原账号的登录态(账号是创作者号则走创作中心)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        requested_scope = str(scope or "auto").strip().lower()
        if requested_scope not in {"auto", "read", "creator"}:
            raise HTTPException(400, "登录范围取值无效")
        if platform in ("xhs", "rednote") and requested_scope != "auto":
            is_creator = requested_scope == "creator"
        else:
            is_creator = bool(acc.creator_storage_state) and not bool(acc.storage_state) if platform == "rednote" else bool(acc.creator_storage_state)

    reused = await login_sessions_service._reuse_or_reject_interactive_login(
        platform, is_creator, account_id)
    if reused is not None:
        reused["login_scope"] = "creator" if is_creator else "read"
        return reused

    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        # 重登会清空旧认证 Cookie；完成扫码校验前账号不应继续显示“正常”。
        other_scope_available = (
            platform == "xhs" and (
                (not is_creator and bool(acc.creator_storage_state))
                or (is_creator and account_profiles_service._xhs_has_read_login_state(acc.storage_state))
            )
        )
        if not other_scope_available:
            acc.status = "invalid"
        s.add(acc)
        s.commit()
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform=platform, creator=is_creator,
        account_id=account_id)
    asyncio.create_task(login_flows_service._run_login(task_id, creator=is_creator, account_id=account_id,
                                   platform=platform))
    return {"task_id": task_id, "status": "opening",
            "login_scope": "creator" if is_creator else "read",
            "hint": "已打开浏览器窗口,请扫码重新登录该账号"}
