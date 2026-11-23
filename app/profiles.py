"""账号画像、代理池唯一性防关联校验与生命周期同步管理。

- assign_proxy_from_pool: 从代理池按「一号一代理 sticky」分配空闲且地域匹配的代理。
- find_same_platform_proxy_conflict: 校验同平台下是否有其他账号已占用同一代理或同一真实出口 IP。
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


def find_same_platform_proxy_conflict(
    session,
    platform: str,
    proxy_url: str,
    *,
    exclude_account_id: int | None = None,
    candidate_exit_ip: str = "",
) -> Optional[DouyinAccount]:
    """检查同一平台下是否已有其他账号占用相同的代理 URL 或相同的真实出口 IP。"""
    pf = str(platform or "").strip().lower()
    norm = normalize_proxy(str(proxy_url or "").strip())
    if not pf or not norm:
        return None
    exit_ip = str(candidate_exit_ip or "").strip()
    if not exit_ip:
        pool_row = resolve_pool_proxy_row(session, norm)
        if pool_row and pool_row.exit_ip:
            exit_ip = str(pool_row.exit_ip).strip()

    accounts = session.exec(
        select(DouyinAccount).where(DouyinAccount.platform == pf)
    ).all()
    for other in accounts:
        if exclude_account_id is not None and other.id == exclude_account_id:
            continue
        other_norm = normalize_proxy(str(other.proxy or "").strip())
        if other_norm and other_norm == norm:
            return other
        other_exit = str(other.exit_ip or "").strip()
        if exit_ip and other_exit and other_exit == exit_ip:
            return other
    return None


def account_requires_dedicated_proxy(
    session,
    cfg,
    *,
    platform: str,
    account: Optional[DouyinAccount] = None,
) -> tuple[bool, str]:
    """判定指定账号或拟登录的新账号是否必须绑定独立代理。

    规则：
    1. 全局关闭 `strict_proxy_required` 或单号关闭 `require_proxy` -> 不强制。
    2. 海外平台（tiktok/rednote/instagram/twitter） -> 必须绑定独立境外代理。
    3. 国内平台且同平台已存在其他账号（即同平台 >=2 个账号） -> 必须各自绑定独立代理，严禁共享本机 IP。
    4. 国内平台且全库仅有这 1 个账号 -> 允许安全使用本机真实宽带环境。
    """
    if not bool(getattr(cfg.engine, "strict_proxy_required", True)):
        return False, ""
    if account is not None and not bool(getattr(account, "require_proxy", True)):
        return False, ""
    pf = str(platform or (account.platform if account else "") or "").strip().lower()
    if pf in OVERSEAS_PLATFORMS:
        return True, "海外平台账号必须绑定独立境外代理，已拦截本机直连以免网络不通或暴露境内环境"
    exclude_id = account.id if account is not None else None
    others = [
        row for row in session.exec(
            select(DouyinAccount).where(DouyinAccount.platform == pf)
        ).all()
        if (exclude_id is None or row.id != exclude_id)
    ]
    if len(others) >= 1:
        return True, "同平台存在多个账号，防关联策略要求每个账号必须绑定独立代理，禁止共享本机真实 IP"
    return False, ""


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


def _pool_urls(session, cfg, *, platform: str = "") -> list:
    """Return normalized, unique proxies eligible for automatic assignment."""
    rows = session.exec(select(ProxyPool)).all()
    if rows:
        pf = str(platform or "").strip().lower()
        domestic = pf in DOMESTIC_PLATFORMS
        overseas = pf in OVERSEAS_PLATFORMS
        sources = []
        seen_exit_ips: set[str] = set()
        for p in rows:
            if not p.enabled or p.status in {"bad", "auth_error", "blocked", "disabled"}:
                continue
            geo_known = bool((p.exit_ip or "").strip() or (p.country or "").strip())
            if domestic and geo_known and not is_mainland_geo(
                    p.country, p.region, p.city):
                continue
            if overseas and geo_known and is_mainland_geo(
                    p.country, p.region, p.city):
                continue
            exit_ip = (p.exit_ip or "").strip()
            if exit_ip and exit_ip in seen_exit_ips:
                continue
            if exit_ip:
                seen_exit_ips.add(exit_ip)
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


def assign_proxy_from_pool(session, cfg, *, platform: str = "") -> str:
    """Return one unoccupied usable proxy whose exit IP is also unshared on the platform."""
    with _proxy_reservation_lock:
        pool = _pool_urls(session, cfg, platform=platform)
        if not pool:
            return ""
        all_accs = session.exec(select(DouyinAccount)).all()
        occupied = {
            normalize_proxy(a.proxy)
            for a in all_accs
            if a.proxy
        }
        occupied.update(_proxy_reservations.values())
        pf = str(platform or "").strip().lower()
        same_pf_exit_ips = {
            str(a.exit_ip or "").strip()
            for a in all_accs
            if pf and str(a.platform or "").lower() == pf and str(a.exit_ip or "").strip()
        }
        pool_exit_by_url = {
            normalize_proxy(r.url): str(r.exit_ip or "").strip()
            for r in session.exec(select(ProxyPool)).all()
            if r.url
        }
        for url in pool:
            if url in occupied:
                continue
            cand_exit = pool_exit_by_url.get(url, "")
            if cand_exit and cand_exit in same_pf_exit_ips:
                continue
            return url
        return ""


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

    free = assign_proxy_from_pool(session, cfg, platform=platform)
    if free:
        return free

    pool = _pool_urls(session, cfg, platform=platform)
    if not pool:
        return ""

    pf = str(platform or "").strip().lower()
    publish_proxies = {
        normalize_proxy(a.proxy)
        for a in session.exec(select(DouyinAccount)).all()
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
