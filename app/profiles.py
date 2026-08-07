"""账号画像与代理池管理。

- assign_proxy_from_pool: 从 config.proxies 里按「最少占用」挑一条,一号一代理 sticky 绑定。
- ensure_identity: 给缺画像的账号补齐 profile_dir / UA / 视口 / 指纹种子(+ 可选分配代理)。
- migrate_identities: 启动时为存量账号批量补画像。
"""
from __future__ import annotations

import threading
from pathlib import Path

from sqlmodel import select

from .browser.identity import generate_identity_fields, seed_from_id
from .browser.manager import normalize_proxy
from .db import get_session
from .models import DouyinAccount, ProxyPool


_proxy_reservation_lock = threading.RLock()
_proxy_reservations: dict[str, str] = {}


def _pool_urls(session, cfg) -> list:
    """Return normalized, unique proxies eligible for automatic assignment."""
    rows = session.exec(select(ProxyPool)).all()
    if rows:
        sources = [
            p.url for p in rows
            if p.enabled and p.status not in {"bad", "auth_error", "blocked"}
        ]
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


def assign_proxy_from_pool(session, cfg) -> str:
    """Return one unoccupied usable proxy, or an empty string."""
    with _proxy_reservation_lock:
        pool = _pool_urls(session, cfg)
        if not pool:
            return ""
        occupied = {
            normalize_proxy(a.proxy)
            for a in session.exec(select(DouyinAccount)).all()
            if a.proxy
        }
        occupied.update(_proxy_reservations.values())
        return next((url for url in pool if url not in occupied), "")


def reserve_proxy_from_pool(session, cfg, reservation_key: str) -> str:
    """Atomically reserve a free proxy while a new login is in progress."""
    key = str(reservation_key or "").strip()
    if not key:
        return assign_proxy_from_pool(session, cfg)
    with _proxy_reservation_lock:
        if key in _proxy_reservations:
            return _proxy_reservations[key]
        proxy = assign_proxy_from_pool(session, cfg)
        if proxy:
            _proxy_reservations[key] = proxy
        return proxy


def release_proxy_reservation(reservation_key: str) -> None:
    """Release a temporary login reservation after persistence or failure."""
    with _proxy_reservation_lock:
        _proxy_reservations.pop(str(reservation_key or "").strip(), None)


def seed_proxy_pool(cfg) -> int:
    """启动时把 config.yaml 里 proxies 列表导入数据库代理池(仅导入尚不存在的 url)。
    返回新增条数。这样老用户在 yaml 配的代理会进池,之后统一在页面管理。"""
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
    """补齐账号缺失的画像字段。返回是否有改动(调用方负责 commit)。
    代理仅在 session 给定且池非空时分配。"""
    changed = False
    if not acc.fp_seed:
        f = generate_identity_fields()
        if getattr(acc, "identity_mode", "legacy") != "native":
            acc.ua = acc.ua or f["ua"]
        acc.viewport_w = acc.viewport_w or f["viewport_w"]
        acc.viewport_h = acc.viewport_h or f["viewport_h"]
        acc.timezone_id = acc.timezone_id or f["timezone_id"]
        acc.locale = acc.locale or f["locale"]
        acc.fp_seed = f["fp_seed"]
        changed = True
    if not acc.fp_seed:                       # 兜底(理论不会到这)
        acc.fp_seed = seed_from_id(acc.id); changed = True
    if not acc.profile_dir and acc.id is not None:
        acc.profile_dir = str(Path(cfg.engine.profiles_dir) / f"acc_{acc.id}")
        changed = True
    if assign_proxy and not acc.proxy and session is not None:
        p = assign_proxy_from_pool(session, cfg)
        if p:
            acc.proxy = p
            changed = True
    return changed


def migrate_identities(cfg) -> int:
    """启动时给所有缺画像的存量账号补齐 profile/UA/指纹。返回处理条数。
    ⚠️ 不分配代理:代理绑定只由用户显式操作(设置/auto/批量分配),
    解绑后应保持解绑 —— 否则一重启就被重新绑上。"""
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
