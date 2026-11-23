"""代理池管理、批量导入、连通性检测与账号代理绑定接口。"""
from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from typing import Any, Dict
from urllib.parse import urlsplit
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.api import risk_control as risk_control_api
from app.browser.proxy import ProxyConfigError, ProxyInputFormat, ProxyPlan, ProxyScheme
from app.db import get_session
from app.models import DouyinAccount, ProxyPool
from app.profiles import (
    assign_proxy_from_pool,
    explain_empty_auto_proxy,
    inspect_account_proxy_risk,
    is_mainland_geo,
    resolve_pool_proxy_row,
    sync_proxy_pool_to_accounts,
)
from app.services import metadata as metadata_service
from app.services import proxies as proxies_service

router = APIRouter()


# ─────────── 账号代理(风控隔离)───────────
class ProxyInputOptions(BaseModel):
    default_scheme: ProxyScheme = "http"
    input_format: ProxyInputFormat = "auto"


def _parse_proxy_input(raw: str, options: ProxyInputOptions) -> ProxyPlan | None:
    return ProxyPlan.parse(raw, default_scheme=options.default_scheme,
                           input_format=options.input_format)


def _proxy_input_url(raw: str, options: ProxyInputOptions, *, allow_empty: bool = False) -> str:
    try:
        plan = _parse_proxy_input(raw, options)
    except ProxyConfigError as exc:
        raise HTTPException(400, str(exc)) from None
    if plan is None and not allow_empty:
        raise HTTPException(400, "请填写代理地址")
    return plan.normalized if plan else ""


class ProxyIn(ProxyInputOptions):
    proxy: str = ""


def _reset_account_exit_baseline(acc) -> None:
    """A proxy assignment starts a new browser-egress baseline generation."""
    acc.exit_ip = ""
    acc.exit_country = ""
    acc.exit_asn = ""
    acc.exit_timezone = ""
    acc.exit_proxy_signature = ""
    acc.exit_checked_at = None


@router.put("/api/accounts/{account_id}/proxy")
async def set_account_proxy(account_id: int, body: ProxyIn):
    """手动设置/清空账号代理。允许本机直连或多号复用同一代理，并返回防关联风险提醒，改后关掉常驻 context。"""
    p = _proxy_input_url(body.proxy, body, allow_empty=True)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        warnings = inspect_account_proxy_risk(
            s,
            platform=acc.platform,
            proxy_url=p,
            exclude_account_id=account_id,
        )
        pool_row = resolve_pool_proxy_row(s, p) if p else None
        acc.proxy = p
        acc.proxy_id = pool_row.id if pool_row else None
        acc.proxy_status = (
            "disabled" if (pool_row and not pool_row.enabled) else "unknown"
        )
        _reset_account_exit_baseline(acc)
        s.add(acc); s.commit()
    if app_runtime.im_receiver:
        await app_runtime.im_receiver.stop_account(account_id)
    if app_runtime.browser:
        await app_runtime.browser.close_context(account_id)
    return {
        "ok": True,
        "proxy": proxies_service._mask_proxy(p),
        "warnings": warnings,
        "warning": "；".join(warnings),
    }


@router.post("/api/accounts/{account_id}/clear-write-pause")
async def clear_account_write_pause(account_id: int, body: metadata_service.RiskClearIn,
                                    request: Request):
    """Compatibility alias for the audited risk-center clear action."""
    return await risk_control_api.clear_account_risk(account_id, body, request)


@router.post("/api/accounts/{account_id}/assign-proxy")
async def assign_account_proxy(account_id: int):
    """从代理池给该账号分配代理（优先空闲节点，池余量不足时按最少占用复用并返回提醒）。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        p = assign_proxy_from_pool(s, app_runtime.cfg, platform=acc.platform)
        if not p:
            raise HTTPException(400, explain_empty_auto_proxy(s, app_runtime.cfg, platform=acc.platform))
        warnings = inspect_account_proxy_risk(
            s,
            platform=acc.platform,
            proxy_url=p,
            exclude_account_id=account_id,
        )
        pool_row = resolve_pool_proxy_row(s, p)
        acc.proxy = p
        acc.proxy_id = pool_row.id if pool_row else None
        acc.proxy_status = "unknown"
        _reset_account_exit_baseline(acc)
        s.add(acc); s.commit()
    if app_runtime.browser:
        await app_runtime.browser.close_context(account_id)
    return {
        "ok": True,
        "proxy": proxies_service._mask_proxy(p),
        "warnings": warnings,
        "warning": "；".join(warnings),
    }


def _proxy_probe_status(status_code: int) -> str:
    """Map a proxy probe response to a persisted health state."""
    if 200 <= status_code < 400:
        return "ok"
    if status_code == 407:
        return "auth_error"
    if status_code in {403, 429}:
        return "blocked"
    return "bad"


def _proxy_status_ok(status_code: int) -> bool:
    return _proxy_probe_status(status_code) == "ok"


def _proxy_status_from_detail(ok: bool, detail: str) -> str:
    if ok:
        return "ok"
    text = str(detail or "")
    for code in (407, 403, 429):
        if f"HTTP {code}" in text:
            return _proxy_probe_status(code)
    return "bad"


def _proxy_probe_error(exc: Exception, raw: str, plan: ProxyPlan) -> str:
    """Keep useful transport diagnostics without returning proxy credentials."""
    import httpx
    from urllib.parse import quote
    text = str(exc)
    auth_error = isinstance(exc, httpx.ProxyError) and (
        re.search(r"\b407\b", text) or "username/password" in text.lower())
    for secret in (raw, plan.normalized, quote(plan.username, safe=""),
                   quote(plan.password, safe=""), plan.username, plan.password):
        if secret:
            text = text.replace(secret, "***")
    prefix = "HTTP 407 · " if auth_error else ""
    return f"{prefix}{type(exc).__name__}: {text[:300]}"


async def _probe_proxy(url: str, platform: str | None = None, timeout: float = 15):
    """Pool/detection: neutral IP services; account: only its requested platform.

    A target-specific failure must never disable an otherwise working pool
    entry. Fallback stays on the exact same proxy, never on a direct connection.
    """
    import httpx
    if not url:
        return False, "未配置代理"
    try:
        plan = ProxyPlan.parse(url)
    except ProxyConfigError as exc:
        return False, str(exc)
    if plan is None:
        return False, "未配置代理"
    platform_urls = {
        "douyin": "https://www.douyin.com/", "xhs": "https://www.xiaohongshu.com/",
        "rednote": "https://www.rednote.com/zh",
        "kuaishou": "https://www.kuaishou.com/", "tiktok": "https://www.tiktok.com/",
        "shipinhao": "https://channels.weixin.qq.com/",
        "bilibili": "https://www.bilibili.com/",
        "instagram": "https://www.instagram.com/",
        "twitter": "https://x.com/",
    }
    if platform is not None and platform not in platform_urls:
        return False, "未配置该平台的检测地址"
    targets = ([platform_urls[platform]] if platform is not None else
               ["https://ipinfo.io/json", "https://api.ipify.org?format=json"])
    failures = []
    try:
        async with httpx.AsyncClient(proxy=plan.normalized, timeout=timeout,
                                     follow_redirects=True, trust_env=False) as cli:
            for target in targets:
                host = urlsplit(target).hostname
                try:
                    response = await cli.get(target)
                    detail = f"{host}: HTTP {response.status_code}"
                    if response.status_code == 407:
                        return False, detail
                    if _proxy_status_ok(response.status_code):
                        if platform is not None:
                            return True, detail
                        try:
                            payload = response.json()
                            value = payload.get("ip") if isinstance(payload, dict) else None
                            if not isinstance(value, str):
                                raise ValueError("missing IP")
                            ip_address(value)
                        except (TypeError, ValueError):
                            detail += "（未返回有效出口 IP）"
                        else:
                            return True, f"通用连通性正常（{detail}）；不代表所有平台均可访问"
                except Exception as exc:
                    detail = f"{host}: {_proxy_probe_error(exc, url, plan)}"
                    if _proxy_status_from_detail(False, detail) == "auth_error":
                        return False, detail
                failures.append(detail)
    except Exception as exc:
        failures.append(_proxy_probe_error(exc, url, plan))
    scope = "平台访问失败" if platform is not None else "通用连通性检测失败"
    return False, scope + "：" + "；".join(failures)


def _geo_text(g: dict | None) -> str:
    if not g:
        return ""
    loc = " · ".join([x for x in (g.get("country"), g.get("region"), g.get("city")) if x])
    parts = [p for p in (g.get("ip"), loc, g.get("isp")) if p]
    return "  ".join(parts)


async def _detect_proxy(raw: str, options: ProxyInputOptions | None = None) -> dict:
    """自动判别代理类型(HTTP / SOCKS5)与是否需要认证。
    保留输入中的账密/会话参数，只切换协议，不擅自改成免密出口。
    返回判别结果 + 推荐的规范化地址 + 浏览器兼容性。"""
    from dataclasses import replace
    raw = (raw or "").strip()
    if not raw:
        return {"ok": False, "error": "请先填代理地址"}
    has_scheme = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw))
    try:
        plan = _parse_proxy_input(raw, options or ProxyInputOptions())
    except ProxyConfigError as exc:
        return {"ok": False, "error": str(exc)}
    # 候选协议:输入已带则只测它,否则 http 与 socks5 都试
    if has_scheme:
        schemes = [plan.scheme]
    else:
        schemes = list(dict.fromkeys([plan.scheme, "http", "socks5"]))

    tried = []
    found = None   # (scheme, auth_mode, url)
    for sch in schemes:
        candidate = replace(plan, scheme=sch)
        url = candidate.normalized
        auth_mode = "required" if candidate.authenticated else "none"
        ok, detail = await _probe_proxy(url, timeout=8)
        tried.append({"scheme": sch, "auth": auth_mode, "ok": ok, "detail": detail})
        if ok:
            found = (sch, auth_mode, url)
            break

    if not found:
        return {"ok": False, "error": "通用连通性检测未通过，请检查协议、账密及服务商连接限制",
                "tried": tried, "need_auth_hint": not plan.authenticated}

    sch, auth_mode, url = found
    is_socks = sch.startswith("socks")
    browser_ok = True  # 内置 Socks5AuthRelay 与 HttpProxyAuthRelay 原生支持带账密 SOCKS5 / HTTP 代理
    geo = await proxies_service._proxy_geo(url)              # 经该代理查出口 IP 归属地
    return {
        "ok": True, "scheme": sch, "auth": auth_mode,
        "recommend": url, "browser_ok": browser_ok, "tried": tried,
        "geo": geo, "geo_text": _geo_text(geo),
        "note": ("HTTP 代理，浏览器与直连全链路支持（含本地无感认证中继）" if not is_socks
                 else ("免密 SOCKS5，浏览器与直连全链路支持" if auth_mode != "required"
                       else "带密 SOCKS5，已由内置本地 SOCKS5 认证中继提供浏览器与直连全链路支持（远端 DNS 防泄露）")),
    }


class ProxyDetectIn(ProxyInputOptions):
    url: str


@router.post("/api/proxies/detect")
async def detect_proxy(body: ProxyDetectIn):
    return await _detect_proxy(body.url, body)


@router.post("/api/accounts/assign-proxies-all")
async def assign_proxies_all():
    """给所有「尚未配置代理」的账号从池里批量分配(优先空闲节点,池余量不足时按最少占用均衡复用并提醒)。"""
    assigned, shared_assigned, pool_empty = 0, 0, False
    with get_session() as s:
        accs = [a.id for a in s.exec(select(DouyinAccount)).all() if not a.proxy]
    remaining = []
    for aid in accs:
        with get_session() as s:
            acc = s.get(DouyinAccount, aid)
            if not acc or acc.proxy:
                continue
            p = assign_proxy_from_pool(
                s, app_runtime.cfg, platform=acc.platform)   # 每次重算占用,保持均衡
            if not p:
                pool_empty = True
                remaining.append(aid)
                continue
            risk_warns = inspect_account_proxy_risk(
                s, platform=acc.platform, proxy_url=p, exclude_account_id=aid
            )
            if any("共用" in w or "复用" in w for w in risk_warns):
                shared_assigned += 1
            pool_row = resolve_pool_proxy_row(s, p)
            acc.proxy = p
            acc.proxy_id = pool_row.id if pool_row else None
            acc.proxy_status = "unknown"
            _reset_account_exit_baseline(acc)
            s.add(acc); s.commit()
            assigned += 1
        if app_runtime.browser:
            await app_runtime.browser.close_context(aid)
    if pool_empty and assigned == 0:
        with get_session() as s:
            sample_pf = ""
            if remaining:
                first_acc = s.get(DouyinAccount, remaining[0])
                sample_pf = first_acc.platform if first_acc else ""
            raise HTTPException(400, explain_empty_auto_proxy(s, app_runtime.cfg, platform=sample_pf))
    warning = (
        f"代理池节点少于账号数，其中 {shared_assigned} 个账号复用了已有代理（存在多号共用同一出口 IP 关联风险，建议补充独立节点）"
        if shared_assigned > 0
        else ""
    )
    return {
        "ok": True,
        "assigned": assigned,
        "unassigned": len(remaining),
        "shared_assigned": shared_assigned,
        "warning": warning,
    }


@router.post("/api/accounts/{account_id}/test-proxy")
async def test_account_proxy(account_id: int):
    """用 native 账号的真实 BrowserContext 验证代理出口并建立基线。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        proxy = acc.proxy or ""
        platform = acc.platform
        identity_mode = acc.identity_mode
        identity = app_runtime.browser.identity_for(acc) if app_runtime.browser else None
    if not proxy:
        return {"ok": False, "detail": "该账号未配置代理(将走宿主真实 IP)"}
    browser_exit = None
    if identity_mode == "native" and app_runtime.browser is not None and identity is not None:
        try:
            # 强制下次 context 使用数据库中最新的代理配置。
            await app_runtime.browser.close_context(account_id)
            browser_exit = await app_runtime.browser.probe_browser_exit(identity)
            ok = True
            detail = f"Browser IP {browser_exit['ip']}"
        except Exception as exc:
            ok = False
            detail = f"Browser probe failed: {exc}"
    else:
        ok, detail = await _probe_proxy(proxy, platform)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if acc:
            if ok and browser_exit:
                signature = app_runtime.browser.proxy_signature(proxy)
                same_generation = bool(acc.exit_proxy_signature) \
                    and acc.exit_proxy_signature == signature
                drift = same_generation and any((
                    bool(acc.exit_ip and acc.exit_ip != browser_exit["ip"]),
                    bool(acc.exit_country and browser_exit["country"]
                         and acc.exit_country != browser_exit["country"]),
                    bool(acc.exit_asn and browser_exit["asn"]
                         and acc.exit_asn != browser_exit["asn"]),
                ))
                if drift:
                    acc.proxy_status = "rotating"
                    detail = (
                        f"代理可用（轮换出口）: 上次 {acc.exit_ip or '-'} / "
                        f"{acc.exit_asn or '-'}, 当前 {browser_exit['ip']} / "
                        f"{browser_exit['asn'] or '-'}")
                else:
                    acc.proxy_status = "ok"
                # A successful browser probe is authoritative for reachability.
                # Rotating residential/mobile proxies legitimately change IP;
                # persist the new exit but do not block reads or writes.
                acc.exit_ip = browser_exit["ip"]
                acc.exit_country = browser_exit["country"]
                acc.exit_asn = browser_exit["asn"]
                acc.exit_timezone = browser_exit["timezone"]
                acc.exit_proxy_signature = signature
                acc.exit_checked_at = datetime.utcnow()
            else:
                acc.proxy_status = _proxy_status_from_detail(ok, detail)
            s.add(acc); s.commit()
    return {"ok": ok, "detail": detail, "proxy": proxies_service._mask_proxy(proxy),
            "browser_exit": browser_exit}


# ─────────── 代理池(提前配置,账号关联使用)───────────
class PoolProxyIn(ProxyInputOptions):
    url: str
    label: str = ""
    note: str = ""
    enabled: bool = True
    geo: Dict[str, Any] | None = None       # 判别得到的归属地(可选,建库时一并写入)


class PoolProxyUpdate(ProxyInputOptions):
    url: str | None = None
    label: str | None = None
    note: str | None = None
    enabled: bool | None = None
    geo: Dict[str, Any] | None = None


def _is_mainland(country: str, region: str, city: str) -> bool:
    return is_mainland_geo(country, region, city)


def _geo_loc(p) -> str:
    return " · ".join([x for x in (p.country, p.region, p.city) if x])


def _safe_plan_meta(raw_url: str) -> dict:
    try:
        plan = ProxyPlan.parse(raw_url)
    except Exception:
        plan = None
    if not plan:
        return {"scheme": "http", "host_port": "", "user_hint": ""}
    u = str(plan.username or "").strip()
    if not u:
        hint = ""
    elif len(u) <= 4:
        hint = u[:1] + "***"
    elif len(u) <= 8:
        hint = u[:2] + "…" + u[-2:]
    else:
        hint = u[:3] + "…" + u[-4:]
    return {
        "scheme": plan.scheme,
        "host_port": f"{plan.host}:{plan.port}",
        "user_hint": hint,
    }


def _pool_dict(p: ProxyPool, used: int = 0, bound_accounts: list[dict] | None = None) -> dict:
    accounts = bound_accounts or []
    pf_counts: dict[str, int] = {}
    for acc in accounts:
        pf = str(acc.get("platform") or "").lower()
        if pf:
            pf_counts[pf] = pf_counts.get(pf, 0) + 1
    meta = _safe_plan_meta(p.url)
    return {
        "id": p.id, "label": p.label, "url": proxies_service._mask_proxy(p.url),
        "url_full": p.url, "enabled": p.enabled, "status": p.status,
        "scheme": meta["scheme"], "host_port": meta["host_port"], "user_hint": meta["user_hint"],
        "note": p.note, "used_by": used,
        "bound_accounts": accounts,
        "same_platform_shared": any(cnt > 1 for cnt in pf_counts.values()),
        "exit_ip": p.exit_ip, "country": p.country, "region": p.region,
        "city": p.city, "isp": p.isp,
        "geo_loc": _geo_loc(p), "is_mainland": _is_mainland(p.country, p.region, p.city),
        "geo_checked": bool(p.exit_ip or p.country),
        "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
    }


@router.get("/api/proxies")
async def list_proxies():
    from app.browser.manager import normalize_proxy
    with get_session() as s:
        rows = s.exec(select(ProxyPool).order_by(ProxyPool.id)).all()
        used: dict[str, int] = {}
        bound_map: dict[str, list[dict]] = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                np = normalize_proxy(a.proxy)
                used[np] = used.get(np, 0) + 1
                bound_map.setdefault(np, []).append({
                    "id": a.id,
                    "nickname": a.nickname or f"账号 #{a.id}",
                    "platform": a.platform or "douyin",
                })
        return [
            _pool_dict(
                p,
                used.get(normalize_proxy(p.url), 0),
                bound_map.get(normalize_proxy(p.url), []),
            )
            for p in rows
        ]


@router.post("/api/proxies")
async def add_proxy(body: PoolProxyIn):
    url = _proxy_input_url(body.url, body)
    with get_session() as s:
        if s.exec(select(ProxyPool).where(ProxyPool.url == url)).first():
            raise HTTPException(409, "该代理已在池中")
        g = body.geo or {}
        has_geo = bool(g.get("ip") or g.get("country"))
        p = ProxyPool(url=url, label=body.label.strip(), note=body.note.strip(),
                      enabled=body.enabled,
                      status="ok" if has_geo else "unknown",
                      last_checked_at=datetime.utcnow() if has_geo else None,
                      exit_ip=g.get("ip", ""), country=g.get("country", ""),
                      region=g.get("region", ""), city=g.get("city", ""),
                      isp=g.get("isp", ""))
        s.add(p); s.commit(); s.refresh(p)
        return _pool_dict(p)


class ProxyImportIn(ProxyInputOptions):
    text: str = ""        # 多行,每行一个代理(可带 # 注释、空行)


@router.post("/api/proxies/import")
async def import_proxies(body: ProxyImportIn):
    """批量粘贴多行导入代理池。每行一个,支持 # 注释/空行,自动校验+去重。"""
    added = skipped = invalid = 0
    invalid_lines = []
    created_rows: list[ProxyPool] = []
    with get_session() as s:
        existing = {p.url for p in s.exec(select(ProxyPool)).all()}
        seen = set(existing)
        for line_number, raw in enumerate((body.text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Parse the whole line first: commas can belong to a provider export.
            label, plan, error = "", None, "代理配置无效"
            try:
                plan = _parse_proxy_input(line, body)
            except ProxyConfigError as exc:
                error = str(exc)
            if plan is None:
                for sep in (" | ", "\t", ",", " "):
                    if sep not in line:
                        continue
                    a, b = line.split(sep, 1)
                    try:
                        plan = _parse_proxy_input(b.strip(), body)
                    except ProxyConfigError:
                        continue
                    if plan:
                        label = a.strip()
                        break
            if plan is None:
                invalid += 1
                if len(invalid_lines) < 8:
                    invalid_lines.append(f"第 {line_number} 行：{error}")
                continue
            url = plan.normalized
            if url in seen:
                skipped += 1
                continue
            row = ProxyPool(url=url, label=label)
            s.add(row)
            created_rows.append(row)
            seen.add(url)
            added += 1
        if added:
            s.commit()
            for row in created_rows:
                s.refresh(row)
        added_ids = [row.id for row in created_rows if row.id is not None]
    return {"ok": True, "added": added, "added_ids": added_ids,
            "skipped": skipped, "invalid": invalid,
            "invalid_samples": invalid_lines}


@router.put("/api/proxies/{pid}")
async def update_proxy(pid: int, body: PoolProxyUpdate):
    affected_ids: list[int] = []
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        old_url = p.url
        if body.url is not None:
            p.url = _proxy_input_url(body.url, body)
        if body.label is not None:
            p.label = body.label.strip()
        if body.note is not None:
            p.note = body.note.strip()
        if body.enabled is not None:
            p.enabled = body.enabled
        if body.geo:
            g = body.geo
            p.exit_ip = g.get("ip", "") or p.exit_ip
            p.country = g.get("country", "") or p.country
            p.region = g.get("region", "") or p.region
            p.city = g.get("city", "") or p.city
            p.isp = g.get("isp", "") or p.isp
            p.status = "ok"
            p.last_checked_at = datetime.utcnow()
        elif p.url != old_url:
            p.status = "unknown"
            p.exit_ip = ""
            p.country = ""
            p.region = ""
            p.city = ""
            p.isp = ""
            p.last_checked_at = None
        s.add(p); s.flush()
        affected_ids = sync_proxy_pool_to_accounts(s, p, old_url=old_url)
        s.commit(); s.refresh(p)
        result = _pool_dict(p)
    for aid in affected_ids:
        if app_runtime.im_receiver:
            await app_runtime.im_receiver.stop_account(aid)
        if app_runtime.browser:
            await app_runtime.browser.close_context(aid)
    return result


@router.delete("/api/proxies/{pid}")
async def del_proxy(pid: int):
    from app.browser.manager import normalize_proxy
    warning = ""
    bound_count = 0
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            return {"ok": True}
        norm_p = normalize_proxy(p.url)
        bound = [
            a for a in s.exec(select(DouyinAccount)).all()
            if a.proxy_id == p.id or (a.proxy and normalize_proxy(a.proxy) == norm_p)
        ]
        bound_count = len(bound)
        if bound:
            names = "、".join(str(a.nickname or a.id) for a in bound[:4])
            for a in bound:
                if a.proxy_id == p.id:
                    a.proxy_id = None
                    s.add(a)
            warning = f"该代理此前被 {bound_count} 个账号（{names}）引用，已解除池关联（建议及时核对这些账号的代理设置）"
        s.delete(p); s.commit()
    return {"ok": True, "still_used_by": bound_count, "warning": warning}


@router.post("/api/proxies/{pid}/test")
async def test_proxy_entry(pid: int):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        url = p.url
    ok, detail = await _probe_proxy(url)
    geo = await proxies_service._proxy_geo(url) if ok else None
    geo_text = _geo_text(geo)
    affected_ids: list[int] = []
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if p:
            p.status = _proxy_status_from_detail(ok, detail)
            p.last_checked_at = datetime.utcnow()
            if geo:
                p.exit_ip = geo.get("ip", "") or p.exit_ip
                p.country = geo.get("country", "") or p.country
                p.region = geo.get("region", "") or p.region
                p.city = geo.get("city", "") or p.city
                p.isp = geo.get("isp", "") or p.isp
            s.add(p); s.flush()
            affected_ids = sync_proxy_pool_to_accounts(s, p)
            s.commit()
    if not ok:
        for aid in affected_ids:
            if app_runtime.im_receiver:
                await app_runtime.im_receiver.stop_account(aid)
            if app_runtime.browser:
                await app_runtime.browser.close_context(aid)
    return {"ok": ok, "detail": detail, "geo": geo, "geo_text": geo_text}


@router.get("/api/proxies/options")
async def proxy_options():
    """供账号/登录「选代理」下拉用:返回全部代理(启用优先,含停用并标记及同平台占用统计)。"""
    from app.browser.manager import normalize_proxy
    with get_session() as s:
        rows = s.exec(select(ProxyPool)
                      .order_by(ProxyPool.enabled.desc(), ProxyPool.id)).all()
        used: dict[str, int] = {}
        used_by_pf: dict[str, dict[str, int]] = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                np = normalize_proxy(a.proxy)
                used[np] = used.get(np, 0) + 1
                pf = str(a.platform or "").lower()
                if pf:
                    used_by_pf.setdefault(np, {})
                    used_by_pf[np][pf] = used_by_pf[np].get(pf, 0) + 1
        return [{
            "id": p.id,
            "label": p.label or proxies_service._mask_proxy(p.url),
            "raw_label": p.label or "",
            "url": p.url,
            "masked": proxies_service._mask_proxy(p.url),
            "status": p.status,
            "enabled": p.enabled,
            "note": p.note or "",
            "used_by": used.get(normalize_proxy(p.url), 0),
            "used_by_platform": used_by_pf.get(normalize_proxy(p.url), {}),
            "exit_ip": p.exit_ip or "",
            "country": p.country or "",
            "region": p.region or "",
            "city": p.city or "",
            "isp": p.isp or "",
            "geo_loc": _geo_loc(p),
            "is_mainland": _is_mainland(p.country, p.region, p.city),
            "geo_checked": bool(p.exit_ip or p.country),
        } for p in rows]
