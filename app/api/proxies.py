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
from app.profiles import assign_proxy_from_pool, is_mainland_geo
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
    """手动设置/清空账号专属代理。改后会关掉该账号常驻 context,下次用新代理重开。"""
    p = _proxy_input_url(body.proxy, body, allow_empty=True)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        acc.proxy = p
        acc.proxy_status = "unknown"
        _reset_account_exit_baseline(acc)
        s.add(acc); s.commit()
    if app_runtime.browser:
        await app_runtime.browser.close_context(account_id)
    return {"ok": True, "proxy": proxies_service._mask_proxy(p)}


@router.post("/api/accounts/{account_id}/clear-write-pause")
async def clear_account_write_pause(account_id: int, body: metadata_service.RiskClearIn,
                                    request: Request):
    """Compatibility alias for the audited risk-center clear action."""
    return await risk_control_api.clear_account_risk(account_id, body, request)


@router.post("/api/accounts/{account_id}/assign-proxy")
async def assign_account_proxy(account_id: int):
    """从代理池(config.proxies)给该账号分配一条占用最少的代理。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        p = assign_proxy_from_pool(s, app_runtime.cfg, platform=acc.platform)
        if not p:
            raise HTTPException(400, "代理池无适合该平台的可用空闲代理(可在「代理池」检查节点地区或状态)")
        acc.proxy = p
        acc.proxy_status = "unknown"
        _reset_account_exit_baseline(acc)
        s.add(acc); s.commit()
    if app_runtime.browser:
        await app_runtime.browser.close_context(account_id)
    return {"ok": True, "proxy": proxies_service._mask_proxy(p)}


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
    browser_ok = not (is_socks and auth_mode == "required")  # Patchright 不支持带密 SOCKS5
    geo = await proxies_service._proxy_geo(url)              # 经该代理查出口 IP 归属地
    return {
        "ok": True, "scheme": sch, "auth": auth_mode,
        "recommend": url, "browser_ok": browser_ok, "tried": tried,
        "geo": geo, "geo_text": _geo_text(geo),
        "note": ("HTTP 代理,浏览器与直连都支持" if not is_socks
                 else ("免密 SOCKS5,浏览器与直连都支持" if browser_ok
                       else "带密 SOCKS5:小红书直连/下载可用,但浏览器抓取/登录不支持(建议改用该节点的 HTTP 端口)")),
    }


class ProxyDetectIn(ProxyInputOptions):
    url: str


@router.post("/api/proxies/detect")
async def detect_proxy(body: ProxyDetectIn):
    return await _detect_proxy(body.url, body)


@router.post("/api/accounts/assign-proxies-all")
async def assign_proxies_all():
    """给所有「尚未配置代理」的账号从池里批量分配(占用最少优先,均衡)。
    池里代理不够时,分到没有为止,返回还差多少。"""
    assigned, names, pool_empty = 0, [], False
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
            acc.proxy = p
            acc.proxy_status = "unknown"
            _reset_account_exit_baseline(acc)
            s.add(acc); s.commit()
            assigned += 1
        if app_runtime.browser:
            await app_runtime.browser.close_context(aid)
    if pool_empty and assigned == 0:
        raise HTTPException(400, "代理池为空,请先在「代理池」添加代理")
    return {"ok": True, "assigned": assigned, "unassigned": len(remaining)}


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


def _is_mainland(country: str, region: str, city: str) -> bool:
    return is_mainland_geo(country, region, city)


def _geo_loc(p) -> str:
    return " · ".join([x for x in (p.country, p.region, p.city) if x])


def _pool_dict(p: ProxyPool, used: int = 0) -> dict:
    return {
        "id": p.id, "label": p.label, "url": proxies_service._mask_proxy(p.url),
        "url_full": p.url, "enabled": p.enabled, "status": p.status,
        "note": p.note, "used_by": used,
        "exit_ip": p.exit_ip, "country": p.country, "region": p.region,
        "city": p.city, "isp": p.isp,
        "geo_loc": _geo_loc(p), "is_mainland": _is_mainland(p.country, p.region, p.city),
        "geo_checked": bool(p.exit_ip),
        "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
    }


@router.get("/api/proxies")
async def list_proxies():
    with get_session() as s:
        rows = s.exec(select(ProxyPool).order_by(ProxyPool.id)).all()
        used = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                used[a.proxy] = used.get(a.proxy, 0) + 1
        return [_pool_dict(p, used.get(p.url, 0)) for p in rows]


@router.post("/api/proxies")
async def add_proxy(body: PoolProxyIn):
    url = _proxy_input_url(body.url, body)
    with get_session() as s:
        if s.exec(select(ProxyPool).where(ProxyPool.url == url)).first():
            raise HTTPException(409, "该代理已在池中")
        g = body.geo or {}
        p = ProxyPool(url=url, label=body.label.strip(), note=body.note.strip(),
                      enabled=body.enabled,
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
            s.add(ProxyPool(url=url, label=label))
            seen.add(url)
            added += 1
        if added:
            s.commit()
    return {"ok": True, "added": added, "skipped": skipped, "invalid": invalid,
            "invalid_samples": invalid_lines}


@router.put("/api/proxies/{pid}")
async def update_proxy(pid: int, body: PoolProxyUpdate):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        if body.url is not None:
            p.url = _proxy_input_url(body.url, body)
        if body.label is not None:
            p.label = body.label.strip()
        if body.note is not None:
            p.note = body.note.strip()
        if body.enabled is not None:
            p.enabled = body.enabled
        s.add(p); s.commit(); s.refresh(p)
        return _pool_dict(p)


@router.delete("/api/proxies/{pid}")
async def del_proxy(pid: int):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            return {"ok": True}
        used = len(s.exec(select(DouyinAccount.id)
                          .where(DouyinAccount.proxy == p.url)).all())
        s.delete(p); s.commit()
    return {"ok": True, "still_used_by": used}


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
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if p:
            p.status = _proxy_status_from_detail(ok, detail)
            p.last_checked_at = datetime.utcnow()
            if geo:                            # 归属地写入结构化字段(供「地区」列展示)
                p.exit_ip = geo.get("ip", "") or p.exit_ip
                p.country = geo.get("country", "") or p.country
                p.region = geo.get("region", "") or p.region
                p.city = geo.get("city", "") or p.city
                p.isp = geo.get("isp", "") or p.isp
            s.add(p); s.commit()
    return {"ok": ok, "detail": detail, "geo": geo, "geo_text": geo_text}


@router.get("/api/proxies/options")
async def proxy_options():
    """供账号/登录「选代理」下拉用:返回全部代理(启用优先,含停用并标记)。
    手动选可选任意一条;auto/批量分配仍只用启用的(见 assign_proxy_from_pool)。"""
    with get_session() as s:
        rows = s.exec(select(ProxyPool)
                      .order_by(ProxyPool.enabled.desc(), ProxyPool.id)).all()
        used = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                used[a.proxy] = used.get(a.proxy, 0) + 1
        return [{"id": p.id, "label": p.label or proxies_service._mask_proxy(p.url),
                 "url": p.url, "masked": proxies_service._mask_proxy(p.url),
                 "status": p.status, "enabled": p.enabled,
                 "used_by": used.get(p.url, 0),
                 "country": p.country, "geo_loc": _geo_loc(p),
                 "is_mainland": _is_mainland(p.country, p.region, p.city),
                 "geo_checked": bool(p.exit_ip or p.country)} for p in rows]
