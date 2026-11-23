"""账号画像、代理池均衡分配与防关联风险提醒管理。

- assign_proxy_from_pool: 优先分配空闲且地域匹配的代理；池余量不足时按最少占用均衡复用，不强制拦截。
- find_same_platform_proxy_conflict / inspect_account_proxy_risk: 检测同平台共用代理/出口 IP 与本机直连风险并生成提醒。
- sync_proxy_pool_to_accounts: 当代理池条目修改/停用/测速异常时，实时同步至已绑定的账号。
- ensure_identity / migrate_identities: 补齐 profile_dir / UA / 视口 / 指纹种子及 proxy_id 关联。
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Optional

from sqlmodel import select

from .browser.identity import generate_identity_fields, seed_from_id
from .browser.manager import normalize_proxy
from .db import get_session
from .models import DouyinAccount, ProxyPool


_proxy_reservation_lock = threading.RLock()
_proxy_reservations: dict[str, str] = {}
DOMESTIC_PLATFORMS = {"douyin", "xhs", "kuaishou", "shipinhao", "bilibili"}
OVERSEAS_PLATFORMS = {"tiktok", "rednote", "instagram", "twitter"}


def is_mainland_geo(country: str, region: str = "", city: str = "") -> bool:
    """Return True only when geo metadata resolves to mainland China."""
    if str(country or "").strip() not in ("中国", "China", "CN"):
        return False
    blob = (region or "") + (city or "")
    return not any(
        mark in blob
        for mark in ("香港", "澳门", "澳門", "台湾", "台灣", "Hong Kong", "Macau", "Taiwan")
    )


def allocate_profile_dir(profiles_root: str, prefix: str = "account") -> str:
    """Allocate a never-reused browser profile path."""
    root = Path(profiles_root)
    while True:
        candidate = root / f"{prefix}_{uuid.uuid4().hex}"
        if not candidate.exists():
            return str(candidate)


def resolve_pool_proxy_row(session, proxy_url: str) -> Optional[ProxyPool]:
    """根据规范化代理 URL 查找对应的 ProxyPool 行(若存在)。"""
    norm = normalize_proxy(str(proxy_url or "").strip())
    if not norm:
        return None
    for row in session.exec(select(ProxyPool)).all():
        if normalize_proxy(row.url) == norm:
            return row
    return None


def find_same_platform_proxy_conflicts(
    session,
    platform: str,
    proxy_url: str,
    *,
    exclude_account_id: int | None = None,
    candidate_exit_ip: str = "",
) -> list[DouyinAccount]:
    """返回同一平台下与指定代理 URL 或真实出口 IP 共用的全部其他账号（用于风险提醒，不阻断操作）。"""
    pf = str(platform or "").strip().lower()
    norm = normalize_proxy(str(proxy_url or "").strip())
    if not pf or not norm:
        return []
    exit_ip = str(candidate_exit_ip or "").strip()
    if not exit_ip:
        pool_row = resolve_pool_proxy_row(session, norm)
        if pool_row and pool_row.exit_ip:
            exit_ip = str(pool_row.exit_ip).strip()

    conflicts: list[DouyinAccount] = []
    accounts = session.exec(
        select(DouyinAccount).where(DouyinAccount.platform == pf)
    ).all()
    for other in accounts:
        if exclude_account_id is not None and other.id == exclude_account_id:
            continue
        other_norm = normalize_proxy(str(other.proxy or "").strip())
        if other_norm and other_norm == norm:
            conflicts.append(other)
            continue
        other_exit = str(other.exit_ip or "").strip()
        if exit_ip and other_exit and other_exit == exit_ip:
            conflicts.append(other)
    return conflicts


def find_same_platform_proxy_conflict(
    session,
    platform: str,
    proxy_url: str,
    *,
    exclude_account_id: int | None = None,
    candidate_exit_ip: str = "",
) -> Optional[DouyinAccount]:
    """检查同一平台下是否已有其他账号占用相同的代理 URL 或相同的真实出口 IP。"""
    conflicts = find_same_platform_proxy_conflicts(
        session,
        platform,
        proxy_url,
        exclude_account_id=exclude_account_id,
        candidate_exit_ip=candidate_exit_ip,
    )
    return conflicts[0] if conflicts else None


def inspect_account_proxy_risk(
    session,
    *,
    platform: str,
    proxy_url: str = "",
    exclude_account_id: int | None = None,
    candidate_exit_ip: str = "",
) -> list[str]:
    """评估账号代理配置风险并返回非阻断式提醒列表（本机直连、同平台共用代理、跨平台复用、地区不匹配）。"""
    pf = str(platform or "").strip().lower()
    norm = normalize_proxy(str(proxy_url or "").strip())
    warnings: list[str] = []

    all_accounts = session.exec(select(DouyinAccount)).all()
    others = [
        a for a in all_accounts
        if exclude_account_id is None or a.id != exclude_account_id
    ]
    same_pf_others = [a for a in others if str(a.platform or "").lower() == pf]

    if not norm:
        if pf in OVERSEAS_PLATFORMS:
            warnings.append(
                "本机直连提醒：当前未绑定代理，海外平台直连可能受网络环境限制或暴露真实 IP，建议配置境外住宅代理"
            )
        elif len(same_pf_others) >= 1:
            same_pf_direct = [a for a in same_pf_others if not str(a.proxy or "").strip()]
            if same_pf_direct:
                names = "、".join(str(a.nickname or f"#{a.id}") for a in same_pf_direct[:3])
                warnings.append(
                    f"多号直连提醒：当前与同平台账号（{names}）共用本机真实 IP，高频操作易触发同 IP 关联限流，建议尽量一号一代理"
                )
            else:
                warnings.append(
                    "本机直连提醒：同平台已有其他账号，当前账号走本机真实 IP，建议按需绑定独立代理以降低关联风险"
                )
        return warnings

    pool_row = resolve_pool_proxy_row(session, norm)
    exit_ip = str(candidate_exit_ip or (pool_row.exit_ip if pool_row else "") or "").strip()

    same_pf_shared: list[DouyinAccount] = []
    cross_pf_shared: list[DouyinAccount] = []
    for other in others:
        other_norm = normalize_proxy(str(other.proxy or "").strip())
        other_exit = str(other.exit_ip or "").strip()
        shared = bool(
            (other_norm and other_norm == norm)
            or (exit_ip and other_exit and other_exit == exit_ip)
        )
        if not shared:
            continue
        if str(other.platform or "").lower() == pf:
            same_pf_shared.append(other)
        else:
            cross_pf_shared.append(other)

    if same_pf_shared:
        names = "、".join(str(a.nickname or f"#{a.id}") for a in same_pf_shared[:3])
        suffix = f" 等 {len(same_pf_shared)} 个账号" if len(same_pf_shared) > 3 else ""
        warnings.append(
            f"一号多用提醒：该代理或出口 IP 正与同平台账号「{names}」{suffix}共用，存在同 IP 关联风控风险，建议尽量一号一独立代理"
        )
    elif cross_pf_shared:
        names = "、".join(str(a.nickname or f"#{a.id}") for a in cross_pf_shared[:3])
        warnings.append(
            f"跨平台复用提醒：该代理同时被其他平台 {len(cross_pf_shared)} 个账号（{names}）复用"
        )

    if pool_row is not None and (pool_row.exit_ip or pool_row.country):
        mainland = is_mainland_geo(pool_row.country, pool_row.region, pool_row.city)
        if pf in DOMESTIC_PLATFORMS and not mainland:
            warnings.append("代理地区提醒：国内平台账号绑定了非中国大陆节点，可能触发异地验证或限流")
        elif pf in OVERSEAS_PLATFORMS and mainland:
            warnings.append("代理地区提醒：海外平台账号绑定了中国大陆节点，可能导致无法连接或风控异常")

    return warnings


def account_requires_dedicated_proxy(
    session,
    cfg,
    *,
    platform: str,
    account: Optional[DouyinAccount] = None,
) -> tuple[bool, str]:
    """评估无代理直连风险并返回 (False, 提醒文案)。不再强制拦截本机直连或多号无代理。"""
    pf = str(platform or (account.platform if account else "") or "").strip().lower()
    exclude_id = account.id if account is not None else None
    warnings = inspect_account_proxy_risk(
        session,
        platform=pf,
        proxy_url="",
        exclude_account_id=exclude_id,
    )
    return False, ("；".join(warnings) if warnings else "")


def sync_proxy_pool_to_accounts(
    session,
    pool_row: ProxyPool,
    *,
    old_url: str = "",
) -> list[int]:
    """将 ProxyPool 的 URL 变更、启停状态或连通性状态同步至所有关联账号，返回受影响账号 ID。"""
    target_norm = normalize_proxy(pool_row.url)
    old_norm = normalize_proxy(old_url) if old_url else target_norm
    affected: list[int] = []
    for acc in session.exec(select(DouyinAccount)).all():
        acc_norm = normalize_proxy(acc.proxy)
        matched = (
            (pool_row.id is not None and acc.proxy_id == pool_row.id)
            or (acc_norm and acc_norm in {target_norm, old_norm})
        )
        if not matched:
            continue
        changed = False
        if acc.proxy_id != pool_row.id:
            acc.proxy_id = pool_row.id
            changed = True
        if target_norm and acc_norm != target_norm:
            acc.proxy = target_norm
            acc.exit_ip = ""
            acc.exit_country = ""
            acc.exit_asn = ""
            acc.exit_timezone = ""
            acc.exit_proxy_signature = ""
            acc.exit_checked_at = None
            acc.proxy_status = "unknown"
            changed = True
        if not pool_row.enabled:
            if acc.proxy_status != "disabled":
                acc.proxy_status = "disabled"
                changed = True
        elif pool_row.status in {"bad", "auth_error", "blocked"}:
            if acc.proxy_status != pool_row.status:
                acc.proxy_status = pool_row.status
                changed = True
        elif acc.proxy_status == "disabled" and pool_row.enabled:
            acc.proxy_status = pool_row.status if pool_row.status == "ok" else "unknown"
            changed = True
        if changed:
            session.add(acc)
            if acc.id is not None:
                affected.append(acc.id)
    return affected


def _pool_urls(session, cfg, *, platform: str = "", strict_geo: bool = True) -> list:
    """Return normalized, unique proxies eligible for automatic assignment."""
    rows = session.exec(select(ProxyPool)).all()
    if rows:
        pf = str(platform or "").strip().lower()
        domestic = pf in DOMESTIC_PLATFORMS
        overseas = pf in OVERSEAS_PLATFORMS
        sources = []
        for p in rows:
            if not p.enabled or p.status in {"bad", "auth_error", "blocked", "disabled"}:
                continue
            if strict_geo:
                geo_known = bool((p.exit_ip or "").strip() or (p.country or "").strip())
                if domestic and geo_known and not is_mainland_geo(
                        p.country, p.region, p.city):
                    continue
                if overseas and geo_known and is_mainland_geo(
                        p.country, p.region, p.city):
                    continue
            sources.append(p.url)
    else:
        sources = list(cfg.proxies or [])

    urls: list[str] = []
    seen: set[str] = set()
    for raw in sources:
        url = normalize_proxy(raw)
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def explain_empty_auto_proxy(session, cfg, *, platform: str = "") -> str:
    """Return a clear user-facing reason when automatic proxy assignment yields nothing."""
    any_healthy = _pool_urls(session, cfg, platform=platform, strict_geo=False)
    pf = str(platform or "").strip().lower()
    if any_healthy:
        if pf in DOMESTIC_PLATFORMS:
            return (
                "代理池暂无中国大陆（或未测地区）节点；池内现有节点均为境外出口，"
                "自动分配已跳过以避免海外代理拦截大陆站点（如需强行使用请手动选择节点，或改用本机直连）"
            )
        if pf in OVERSEAS_PLATFORMS:
            return (
                "代理池暂无境外（或未测地区）节点；池内现有节点均为中国大陆出口，"
                "无法直达海外平台，请先添加境外代理节点"
            )
    return "代理池暂无已启用且连通正常的代理节点，请先在「代理池」添加或启用节点"


def assign_proxy_from_pool(session, cfg, *, platform: str = "") -> str:
    """优先分配空闲且地域匹配的独立代理；绝不将已确认为反方向地区的节点自动错配。"""
    with _proxy_reservation_lock:
        pool = _pool_urls(session, cfg, platform=platform, strict_geo=True)
        if not pool:
            return ""
        all_accs = session.exec(select(DouyinAccount)).all()
        pf = str(platform or "").strip().lower()
        total_counts: dict[str, int] = {}
        same_pf_counts: dict[str, int] = {}
        same_pf_exit_ips: set[str] = set()
        for a in all_accs:
            ap = normalize_proxy(a.proxy) if a.proxy else ""
            if ap:
                total_counts[ap] = total_counts.get(ap, 0) + 1
                if pf and str(a.platform or "").lower() == pf:
                    same_pf_counts[ap] = same_pf_counts.get(ap, 0) + 1
            if pf and str(a.platform or "").lower() == pf and str(a.exit_ip or "").strip():
                same_pf_exit_ips.add(str(a.exit_ip or "").strip())
        for reserved_url in _proxy_reservations.values():
            rp = normalize_proxy(reserved_url)
            if rp:
                total_counts[rp] = total_counts.get(rp, 0) + 1
                if pf:
                    same_pf_counts[rp] = same_pf_counts.get(rp, 0) + 1
        pool_exit_by_url = {
            normalize_proxy(r.url): str(r.exit_ip or "").strip()
            for r in session.exec(select(ProxyPool)).all()
            if r.url
        }

        def _rank(item: tuple[int, str]) -> tuple[int, int, int, int]:
            idx, url = item
            sp_cnt = same_pf_counts.get(url, 0)
            tot_cnt = total_counts.get(url, 0)
            cand_exit = pool_exit_by_url.get(url, "")
            same_pf_shared = 1 if (sp_cnt > 0 or (cand_exit and cand_exit in same_pf_exit_ips)) else 0
            return (same_pf_shared, sp_cnt, tot_cnt, idx)

        _, chosen = min(enumerate(pool), key=_rank)
        return chosen


def reserve_proxy_from_pool(
        session, cfg, reservation_key: str, *, platform: str = "") -> str:
    """Atomically reserve a free proxy while a new login is in progress."""
    key = str(reservation_key or "").strip()
    if not key:
        return assign_proxy_from_pool(session, cfg, platform=platform)
    with _proxy_reservation_lock:
        if key in _proxy_reservations:
            return _proxy_reservations[key]
        proxy = assign_proxy_from_pool(session, cfg, platform=platform)
        if proxy:
            _proxy_reservations[key] = proxy
        return proxy


def release_proxy_reservation(reservation_key: str) -> None:
    """Release a temporary login reservation after persistence or failure."""
    with _proxy_reservation_lock:
        _proxy_reservations.pop(str(reservation_key or "").strip(), None)


def pick_ephemeral_proxy(
    session,
    cfg,
    *,
    platform: str = "",
    account_id: int | None = None,
) -> str:
    """为短链解析或匿名公开抓取挑选安全的代理出口（优先账号自身代理，其次空闲池代理，避开发布主号专属 IP）。"""
    import random

    if account_id:
        acc = session.get(DouyinAccount, int(account_id))
        if acc and acc.proxy and acc.proxy_status not in {"bad", "auth_error", "blocked", "disabled"}:
            return normalize_proxy(acc.proxy)

    pool = _pool_urls(session, cfg, platform=platform)
    if not pool:
        return ""

    all_accs = session.exec(select(DouyinAccount)).all()
    occupied = {normalize_proxy(a.proxy) for a in all_accs if a.proxy}
    idle = [u for u in pool if u not in occupied]
    if idle:
        return idle[0]

    pf = str(platform or "").strip().lower()
    publish_proxies = {
        normalize_proxy(a.proxy)
        for a in all_accs
        if a.proxy
        and str(a.account_role or "hybrid").lower() == "publish"
        and (not pf or str(a.platform or "").lower() == pf)
    }
    non_publish = [u for u in pool if u not in publish_proxies]
    candidates = non_publish or pool
    return random.choice(candidates)


def seed_proxy_pool(cfg) -> int:
    """启动时把 config.yaml 里 proxies 列表导入数据库代理池(仅导入尚不存在的 url)。"""
    pool = list(cfg.proxies or [])
    if not pool:
        return 0
    n = 0
    with get_session() as s:
        existing = {p.url for p in s.exec(select(ProxyPool)).all()}
        for i, url in enumerate(pool):
            url = normalize_proxy(url)
            if url and url not in existing:
                s.add(ProxyPool(label=f"config-{i + 1}", url=url))
                existing.add(url)
                n += 1
        if n:
            s.commit()
    return n


def ensure_identity(acc: DouyinAccount, cfg, session=None, assign_proxy: bool = True) -> bool:
    """补齐账号缺失的画像字段。返回是否有改动(调用方负责 commit)。"""
    changed = False
    pf = str(getattr(acc, "platform", "") or "")
    if not acc.fp_seed:
        f = generate_identity_fields(platform=pf)
        if getattr(acc, "identity_mode", "legacy") != "native":
            acc.ua = acc.ua or f["ua"]
        acc.viewport_w = acc.viewport_w or f["viewport_w"]
        acc.viewport_h = acc.viewport_h or f["viewport_h"]
        acc.timezone_id = acc.timezone_id or f["timezone_id"]
        acc.locale = acc.locale or f["locale"]
        acc.fp_seed = f["fp_seed"]
        changed = True
    if not acc.fp_seed:
        acc.fp_seed = seed_from_id(acc.id)
        changed = True
    if not acc.profile_dir and acc.id is not None:
        acc.profile_dir = allocate_profile_dir(cfg.engine.profiles_dir)
        changed = True
    if assign_proxy and not acc.proxy and session is not None:
        p = assign_proxy_from_pool(session, cfg, platform=pf)
        if p:
            acc.proxy = p
            changed = True
    if acc.proxy and acc.proxy_id is None and session is not None:
        pool_row = resolve_pool_proxy_row(session, acc.proxy)
        if pool_row is not None and pool_row.id is not None:
            acc.proxy_id = pool_row.id
            changed = True
    return changed


def migrate_identities(cfg) -> int:
    """启动时给所有缺画像的存量账号补齐 profile/UA/指纹及代理池外键关联。"""
    n = 0
    with get_session() as s:
        accs = s.exec(select(DouyinAccount)).all()
        for acc in accs:
            if ensure_identity(acc, cfg, session=s, assign_proxy=False):
                s.add(acc)
                n += 1
        if n:
            s.commit()
    return n
