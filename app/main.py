"""FastAPI 入口。对应逆向 cmd/server/main.go + internal/router。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import traceback
import uuid
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

from datetime import datetime
import uuid as _uuid

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import select

from .browser import (BrowserManager, cookie_string_to_state,
                      interactive_login, interactive_creator_login,
                      interactive_xhs_login, interactive_xhs_creator_login,
                      interactive_ks_login, interactive_ks_creator_login,
                      interactive_channels_login, interactive_channels_creator_login,
                      fetch_self_profile, fetch_xhs_self_profile, fetch_ks_self_profile,
                      fetch_channels_self_profile,
                      fetch_account_works, fetch_follows, fetch_dm_conversations,
                      fetch_dm_history)
from .platforms.douyin import parse_self_user
from .config import load_config
from .db import get_session, init_db
from .platforms.douyin import resolve_sec_uid, resolve_aweme_id, looks_like_video
from .platforms.xhs import (resolve_note as xhs_resolve_note,
                  resolve_user as xhs_resolve_user,
                  looks_like_note as xhs_looks_like_note,
                  parse_self_user as parse_xhs_self_user,
                  XhsApiClient, XhsApiError, cookie_str_from_state, has_a1,
                  has_creator_cookies)
from .platforms.kuaishou import (resolve_ks_user_id, resolve_ks_photo_id,
                  looks_like_photo as ks_looks_like_photo,
                  parse_self_user as parse_ks_self_user)
from .platforms.channels import parse_self_user as parse_channels_self_user
from .engine import MonitorEngine
from .models import (ContentRecord, CommentRecord, CommentRule, CommentTask,
                     CommentWatch, DouyinAccount, MonitorTarget,
                     NotificationChannel, ProxyPool, PublishTask,
                     AccountWork, FollowEdge, DmConversation, DmMessage,
                     AccountActionTask, AccountStatSnapshot)
from .notifier import CHANNEL_TYPES, send_one
from .profiles import (ensure_identity, migrate_identities, assign_proxy_from_pool,
                       seed_proxy_pool)
from .settings import get_setting, set_setting
from .windowing import (EXPLORER_WINDOW_CLASSES, bring_window_to_front,
                        capture_window_snapshot)

import json

cfg = load_config()
browser: BrowserManager | None = None
engine: MonitorEngine | None = None
im_receiver = None      # ImReceiverManager(私信实时接收)
login_tasks: Dict[str, dict] = {}
# 用户手动打开的账号浏览器窗口(account_id -> BrowserContext),留引用防 GC、便于复用/清理
open_browsers: Dict[int, Any] = {}
_file_manager_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser, engine, im_receiver
    init_db(cfg.db_path)
    # config.yaml 里配的 proxies 导入数据库代理池(之后统一在页面管理)
    try:
        seeded = seed_proxy_pool(cfg)
        if seeded:
            print(f"[startup] 已从 config.yaml 导入 {seeded} 条代理到代理池")
    except Exception as e:
        print(f"[startup] 代理池导入失败(不影响启动): {e!r}")
    # 存量账号补齐设备/网络画像(profile_dir / UA / 指纹 / 代理),防多账号关联
    try:
        n = migrate_identities(cfg)
        if n:
            print(f"[startup] 已为 {n} 个存量账号补齐画像(profile/UA/指纹/代理)")
    except Exception as e:
        print(f"[startup] 账号画像迁移失败(不影响启动): {e!r}")
    browser = BrowserManager(cfg.engine.user_agent, cfg.engine.profiles_dir,
                             cfg.engine.max_live_contexts)
    await browser.start()
    engine = MonitorEngine(cfg, browser)
    engine.start()
    from .engine.im_receiver import ImReceiverManager
    im_receiver = ImReceiverManager(browser)
    yield
    if im_receiver:
        await im_receiver.stop_all()
    if engine:
        await engine.stop()
    if browser:
        await browser.stop()


app = FastAPI(title="CreatorHub", lifespan=lifespan)
WEB_DIR = Path(__file__).parent / "web"


# ─────────── 扫码登录(真实浏览器) ───────────
async def _xhs_profile(state: str, proxy: str = ""):
    """用签名直连 API 拿小红书账号资料(me 身份 + otherinfo 昵称/头像/粉丝)。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。"""
    cookie_str = cookie_str_from_state(state)
    if not has_a1(cookie_str):
        return {}, "logged_out"
    client = XhsApiClient(cookie_str, cfg.engine.user_agent,
                          timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
    try:
        me = await client.self_info()
    except XhsApiError:
        return {}, "logged_out"
    except Exception as e:
        print(f"[xhs_profile] self_info 失败: {e!r}")
        return {}, "error"
    if not me or me.get("guest") is True or not me.get("user_id"):
        return {}, "logged_out"
    merged = dict(me)
    try:
        other = await client.user_info(me["user_id"])
        if other:
            merged = {**other, **me}      # me 提供身份,otherinfo 提供 basic_info/粉丝
    except Exception:
        pass
    return merged, ""


async def _enrich_account_profile(account_id: int, state: str) -> str:
    """用登录态拉取账号资料并顺带判断登录态。返回 ok | invalid | error。"""
    if browser is None or not state:
        return "error"
    with get_session() as s:
        a0 = s.get(DouyinAccount, account_id)
        platform = a0.platform if a0 else "douyin"
        creator_state = a0.creator_storage_state if a0 else ""
        proxy = (a0.proxy or "") if a0 else ""
        identity = browser.identity_for(a0) if a0 else browser.anon_identity()

    # XHS 创作者号:用创作平台「我的信息」拿资料 + 判活(www 接口对创作态拿不到)
    if platform == "xhs" and creator_state:
        from .platforms.xhs import creator_profile, creator_check
        prof = await creator_profile(creator_state, proxy=proxy)
        if prof and (prof.get("nickname") or prof.get("douyin_id")):
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    if prof.get("nickname"):
                        acc.nickname = prof["nickname"]
                    acc.sec_uid = prof.get("sec_uid") or acc.sec_uid
                    acc.douyin_id = prof.get("douyin_id") or acc.douyin_id
                    acc.avatar = prof.get("avatar") or acc.avatar
                    acc.follower_count = prof.get("follower_count") or acc.follower_count
                    acc.aweme_count = prof.get("aweme_count") or acc.aweme_count
                    acc.status = "active"
                    s.add(acc); s.commit()
            return "ok"
        chk = await creator_check(creator_state, proxy=proxy)
        if chk is True:
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    acc.status = "active"
                    s.add(acc); s.commit()
            return "ok"
        if chk is None:
            return "error"
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if acc:
                acc.status = "invalid"
                s.add(acc); s.commit()
        return "invalid"

    try:
        if platform == "xhs":
            u, err = await _xhs_profile(state, proxy)
        elif platform == "kuaishou":
            u, err = await fetch_ks_self_profile(browser, identity)
        elif platform == "shipinhao":
            u, err = await fetch_channels_self_profile(browser, identity)
        else:
            u, err = await fetch_self_profile(browser, identity)
    except Exception:
        return "error"
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            return "error"
        if u:
            if platform == "xhs":
                p = parse_xhs_self_user(u)
            elif platform == "kuaishou":
                p = parse_ks_self_user(u)
            elif platform == "shipinhao":
                p = parse_channels_self_user(u)
            else:
                p = parse_self_user(u)
            if p.get("nickname"):
                acc.nickname = p["nickname"]
            acc.sec_uid = p.get("sec_uid") or acc.sec_uid
            acc.douyin_id = p.get("douyin_id") or acc.douyin_id
            acc.avatar = p.get("avatar") or acc.avatar
            acc.follower_count = p.get("follower_count") or acc.follower_count
            acc.aweme_count = p.get("aweme_count") or acc.aweme_count
            acc.status = "active"
            s.add(acc); s.commit()
            return "ok"
        if err == "logged_out":
            acc.status = "invalid"
            s.add(acc); s.commit()
            return "invalid"
    return "error"


async def _run_login(task_id: str, creator: bool = False, account_id: int | None = None,
                     platform: str = "douyin", proxy_choice: str = "auto"):
    """扫码登录。多账号隔离模型:一账号=一持久 profile。
    - 传 account_id:登录进该账号自己的 profile(重新登录/补创作者登录)。
    - 不传:用「临时 profile」登录,**只有登录成功才建账号**;关窗/超时/取消都不留残号。"""
    import os
    import shutil
    from .browser import Identity, generate_identity_fields
    login_tasks[task_id] = {"status": "waiting"}
    fresh_account = account_id is None
    tmp_profile = ""
    new_fields = None
    nm = ("小红书账号" if platform == "xhs"
          else "快手账号" if platform == "kuaishou"
          else "视频号账号" if platform == "shipinhao"
          else "创作者账号" if creator else "扫码账号")
    try:
        # 1) 准备画像 + identity(新建账号此时不写库,只用临时 profile)
        if account_id:
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if not acc:
                    login_tasks[task_id] = {"status": "error", "error": "账号不存在"}
                    return
                ensure_identity(acc, cfg, session=s, assign_proxy=False)
                s.add(acc); s.commit(); s.refresh(acc)
                identity = browser.identity_for(acc)
                acc_id = acc.id
        else:
            acc_id = None
            new_fields = generate_identity_fields()
            tmp_profile = os.path.join(cfg.engine.profiles_dir, "new_" + uuid.uuid4().hex)
            # 登录前选定代理:具体地址 / auto(占用最少) / 空(不用代理)
            choice = (proxy_choice or "").strip()
            if choice.lower() in ("", "none"):
                proxy = ""
            elif choice.lower() == "auto":
                with get_session() as s:
                    proxy = assign_proxy_from_pool(s, cfg)
            else:
                from .browser.manager import normalize_proxy
                proxy = normalize_proxy(choice)
            identity = Identity(
                account_id=None, profile_dir=tmp_profile, proxy=proxy,
                ua=new_fields["ua"], viewport_w=new_fields["viewport_w"],
                viewport_h=new_fields["viewport_h"], timezone_id=new_fields["timezone_id"],
                locale=new_fields["locale"], fp_seed=new_fields["fp_seed"])

        # 2) 在 profile 里有头扫码
        if platform == "xhs":
            if creator:
                ok, state_json, nickname = await interactive_xhs_creator_login(browser, identity)
            else:
                ok, state_json, nickname = await interactive_xhs_login(browser, identity)
        elif platform == "kuaishou":
            if creator:
                ok, state_json, nickname = await interactive_ks_creator_login(browser, identity)
            else:
                ok, state_json, nickname = await interactive_ks_login(browser, identity)
        elif platform == "shipinhao":
            # 视频号只有一套登录态(助手即创作平台),读取/发布共用
            ok, state_json, nickname = await interactive_channels_login(browser, identity)
        elif creator:
            ok, state_json, nickname = await interactive_creator_login(browser, identity)
        else:
            ok, state_json, nickname = await interactive_login(browser, identity)

        # 3) 仅在成功时落库
        if ok and state_json:
            is_xhs = platform == "xhs"
            with get_session() as s:
                if account_id:
                    acc = s.get(DouyinAccount, acc_id)
                else:
                    acc = DouyinAccount(
                        platform=platform, nickname=nickname or nm, status="active",
                        profile_dir=tmp_profile, proxy=identity.proxy,
                        ua=new_fields["ua"], viewport_w=new_fields["viewport_w"],
                        viewport_h=new_fields["viewport_h"],
                        timezone_id=new_fields["timezone_id"], locale=new_fields["locale"],
                        fp_seed=new_fields["fp_seed"])
                    s.add(acc); s.commit(); s.refresh(acc); acc_id = acc.id
                if creator:
                    acc.creator_storage_state = state_json
                    if not is_xhs or not acc.storage_state:   # xhs 创作登录不覆盖读取态
                        acc.storage_state = state_json
                elif platform == "shipinhao":
                    # 视频号一套登录态即读取又发布,两处都写
                    acc.storage_state = state_json
                    acc.creator_storage_state = state_json
                else:
                    acc.storage_state = state_json
                if nickname:
                    acc.nickname = nickname
                acc.status = "active"
                s.add(acc); s.commit()
            await _enrich_account_profile(acc_id, state_json)   # best-effort
            with get_session() as s:
                acc = s.get(DouyinAccount, acc_id)
                login_tasks[task_id] = {"status": "confirmed", "account_id": acc_id,
                                        "nickname": acc.nickname}
        else:
            if fresh_account and tmp_profile:   # 没建账号,清理临时 profile
                try:
                    await browser.close_context(identity.key)
                except Exception:
                    pass
                shutil.rmtree(tmp_profile, ignore_errors=True)
            login_tasks[task_id] = {"status": "expired"}
    except Exception as e:
        traceback.print_exc()
        if fresh_account and tmp_profile:
            try:
                await browser.close_context(identity.key)
            except Exception:
                pass
            shutil.rmtree(tmp_profile, ignore_errors=True)
        login_tasks[task_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/api/login/browser/start")
async def login_browser_start(proxy: str = "auto"):
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开浏览器窗口,请在其中点击“登录”并用抖音 App 扫码"}


@app.post("/api/login/creator/start")
async def login_creator_start(proxy: str = "auto"):
    """创作中心登录(用于自有账号评论模式;其登录态同样可用于公开抓取)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=True, proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开创作中心窗口,请在其中扫码登录你的抖音号"}


@app.post("/api/login/xhs/start")
async def login_xhs_start(proxy: str = "auto"):
    """小红书扫码登录(用于监控/读取)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, platform="xhs", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开小红书窗口,请在其中用小红书 App 扫码登录"}


@app.post("/api/login/xhs-creator/start")
async def login_xhs_creator_start(proxy: str = "auto"):
    """小红书「创作服务平台」登录(用于发布/已发布列表)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=True, platform="xhs", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开小红书创作平台窗口,请扫码登录(发布用)"}


@app.post("/api/login/kuaishou/start")
async def login_ks_start(proxy: str = "auto"):
    """快手扫码登录(用于监控/读取)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, platform="kuaishou", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开快手窗口,请在其中用快手 App 扫码登录"}


@app.post("/api/login/kuaishou-creator/start")
async def login_ks_creator_start(proxy: str = "auto"):
    """快手「创作者服务平台」登录(用于发布)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=True, platform="kuaishou",
                                   proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开快手创作平台窗口,请扫码登录(发布用)"}


@app.post("/api/login/shipinhao/start")
async def login_channels_start(proxy: str = "auto"):
    """视频号扫码登录(读取/发布共用,微信扫码)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, platform="shipinhao", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开视频号助手窗口,请用微信扫码登录"}


@app.get("/api/login/browser/poll")
async def login_browser_poll(task_id: str):
    info = login_tasks.get(task_id)
    if not info:
        raise HTTPException(404, "task 不存在")
    if info.get("status") in ("confirmed", "error", "expired"):
        # 终态:取走后清理
        login_tasks.pop(task_id, None)
    return info


class CookieIn(BaseModel):
    cookie: str
    nickname: str = ""
    platform: str = "douyin"            # douyin | xhs


@app.post("/api/login/cookie")
async def login_cookie(body: CookieIn):
    """Cookie 粘贴兜底登录:转成浏览器登录态。"""
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou") else "douyin"
    state = cookie_string_to_state(body.cookie, platform)
    with get_session() as s:
        acc = DouyinAccount(nickname=body.nickname or "Cookie账号", platform=platform,
                            cookie=body.cookie.strip(), storage_state=state)
        s.add(acc); s.commit(); s.refresh(acc)
        # 分配画像(profile/UA/指纹/代理):Cookie 会在首次开持久 profile 时桥接注入
        ensure_identity(acc, cfg, session=s, assign_proxy=True)
        s.add(acc); s.commit(); s.refresh(acc)
        return {"account_id": acc.id, "nickname": acc.nickname}


@app.get("/api/accounts")
async def list_accounts(platform: str | None = None):
    with get_session() as s:
        q = select(DouyinAccount)
        if platform:
            q = q.where(DouyinAccount.platform == platform)
        accs = s.exec(q).all()
        out = []
        for a in accs:
            used = len(s.exec(select(MonitorTarget.id)
                              .where(MonitorTarget.account_id == a.id)).all())
            out.append({
                "id": a.id, "platform": a.platform, "nickname": a.nickname, "status": a.status,
                "sec_uid": a.sec_uid, "douyin_id": a.douyin_id, "avatar": a.avatar,
                "follower_count": a.follower_count, "aweme_count": a.aweme_count,
                "has_creator": bool(a.creator_storage_state) or has_creator_cookies(a.storage_state),
                "kind": "creator" if (a.creator_storage_state or has_creator_cookies(a.storage_state)) else "fetch",
                "has_storage": bool(a.storage_state),
                "login_type": "cookie" if a.cookie else "scan",
                "monitor_count": used,
                # 风控隔离画像
                "proxy": _mask_proxy(a.proxy),
                "proxy_status": a.proxy_status,
                "has_proxy": bool(a.proxy),
                "ua": a.ua,
                "profile_dir": a.profile_dir,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
        return out


def _mask_proxy(proxy: str) -> str:
    """脱敏展示代理(隐藏账号密码)。"""
    if not proxy:
        return ""
    try:
        from urllib.parse import urlparse
        u = urlparse(proxy if "://" in proxy else "http://" + proxy)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        auth = "***@" if u.username else ""
        return f"{u.scheme}://{auth}{host}{port}"
    except Exception:
        return "***"


@app.delete("/api/accounts/{account_id}")
async def del_account(account_id: int):
    import shutil
    pdir = ""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if acc:
            pdir = acc.profile_dir or ""
            s.delete(acc); s.commit()
    # 删号同时清理其持久 profile(释放磁盘);代理回到池里(占用计数自然下降)
    if pdir:
        try:
            await browser.close_context(account_id)
        except Exception:
            pass
        try:
            shutil.rmtree(pdir, ignore_errors=True)
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/accounts/{account_id}/refresh-profile")
async def refresh_account_profile(account_id: int):
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        state = acc.storage_state or acc.creator_storage_state
        platform = acc.platform
    if not state:
        raise HTTPException(400, "该账号无浏览器登录态(Cookie 粘贴账号可能不含完整态),无法拉取资料")
    res = await _enrich_account_profile(account_id, state)
    if res == "invalid":
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    if res != "ok":
        tag = ("[xhs_self_profile]" if platform == "xhs"
               else "[ks_self_profile]" if platform == "kuaishou"
               else "[self_profile]")
        raise HTTPException(400, f"未能获取账号资料:请看服务端控制台 {tag} 那行日志"
                                 "(含它实际看到的请求),把它发我即可定位")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        return {"ok": True, "nickname": acc.nickname, "platform": acc.platform,
                "douyin_id": acc.douyin_id, "sec_uid": acc.sec_uid}


@app.post("/api/accounts/{account_id}/relogin/start")
async def relogin_start(account_id: int):
    """重新登录:更新原账号的登录态(账号是创作者号则走创作中心)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        is_creator = bool(acc.creator_storage_state)
        platform = acc.platform
    task_id = uuid.uuid4().hex
    login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=is_creator, account_id=account_id,
                                   platform=platform))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开浏览器窗口,请扫码重新登录该账号"}


# ─────────── 本账号管理:作品 ───────────
def _work_dict(w: AccountWork) -> dict:
    return {
        "id": w.id, "platform": w.platform, "account_id": w.account_id,
        "item_id": w.item_id, "desc": w.desc, "media_type": w.media_type,
        "cover_url": w.cover_url, "create_time": w.create_time,
        "like_count": w.like_count, "comment_count": w.comment_count,
        "collect_count": w.collect_count, "share_count": w.share_count,
        "play_count": w.play_count, "status": w.status,
        "fetched_at": w.fetched_at.isoformat() if w.fetched_at else None,
    }


@app.get("/api/account-works")
async def list_account_works(account_id: int, limit: int = 200):
    with get_session() as s:
        q = (select(AccountWork).where(AccountWork.account_id == account_id)
             .order_by(AccountWork.create_time.desc()).limit(limit))
        return [_work_dict(w) for w in s.exec(q).all()]


@app.post("/api/accounts/{account_id}/works/sync")
async def sync_account_works(account_id: int):
    """打开账号自己的主页,拦截抓取本账号已发布作品,落库(upsert)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        uid = acc.sec_uid or ""
        identity = browser.identity_for(acc)
    items, err = await fetch_account_works(browser, identity, platform, uid)
    if not items:
        if err and err.startswith("missing_uid"):
            raise HTTPException(400, err.split(":", 1)[-1])
        raise HTTPException(400, f"未抓到作品:{err or '可能登录态失效/无公开作品'}"
                                 "(详情见服务端控制台日志)")
    now = datetime.utcnow()
    added = 0
    with get_session() as s:
        for w in items:
            existing = s.exec(select(AccountWork).where(
                AccountWork.account_id == account_id,
                AccountWork.item_id == w["item_id"])).first()
            if existing:
                for k, v in w.items():
                    setattr(existing, k, v)
                existing.fetched_at = now
                s.add(existing)
            else:
                s.add(AccountWork(platform=platform, account_id=account_id,
                                  fetched_at=now, **w))
                added += 1
        s.commit()
    return {"ok": True, "fetched": len(items), "added": added}


# ─────────── 本账号数据分析(B4:粉丝/作品/互动趋势 + 单篇作品表)───────────
@app.get("/api/account-stats/{account_id}")
async def account_stats(account_id: int, days: int = 30):
    """返回该账号近 days 天的每日快照趋势 + 当前本账号作品的单篇互动明细。
    快照由引擎在账号体检/作品健康时写入(见 EngineConfig.work_health_*)。"""
    days = max(1, min(days, 180))
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        snaps = s.exec(select(AccountStatSnapshot)
                       .where(AccountStatSnapshot.account_id == account_id)
                       .order_by(AccountStatSnapshot.date.desc()).limit(days)).all()
        works = s.exec(select(AccountWork)
                       .where(AccountWork.account_id == account_id)
                       .order_by(AccountWork.create_time.desc()).limit(50)).all()
    trend = [{"date": x.date, "follower_count": x.follower_count,
              "aweme_count": x.aweme_count, "total_like": x.total_like,
              "total_comment": x.total_comment, "total_play": x.total_play}
             for x in reversed(snaps)]
    latest = trend[-1] if trend else {}
    prev = trend[-2] if len(trend) >= 2 else {}
    fans_delta = (latest.get("follower_count", 0) - prev.get("follower_count", 0)
                  if prev else 0)
    return {
        "account": {"id": acc.id, "platform": acc.platform, "nickname": acc.nickname,
                    "follower_count": acc.follower_count, "aweme_count": acc.aweme_count},
        "fans_delta": fans_delta,
        "trend": trend,
        "works": [_work_dict(w) for w in works],
    }


# ─────────── 本账号管理:作品评论(抖音直连分页 / 小红书客户端 / 快手拦截)───────────
@app.get("/api/account-works/{work_id}/comments")
async def list_work_comments(work_id: int, limit: int = 300):
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        item_id = w.item_id
        rows = s.exec(select(CommentRecord).where(
            CommentRecord.watch_id == 0,
            CommentRecord.aweme_id == item_id)
            .order_by(CommentRecord.id.desc()).limit(limit)).all()
        return [_comment_dict(c) for c in rows]


@app.post("/api/account-works/{work_id}/comments/sync")
async def sync_work_comments(work_id: int):
    if engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        platform, item_id = w.platform, w.item_id
        account_id, xsec_token = w.account_id, w.xsec_token
    res = await engine.sync_work_comments(account_id, platform, item_id, xsec_token)
    if not res.get("ok") and not res.get("added"):
        raise HTTPException(400, f"抓评论失败:{res.get('error') or '未知'}"
                                 "(详情见服务端控制台日志)")
    return res


# ─────────── 本账号管理:关注 / 粉丝 ───────────
def _follow_dict(f: FollowEdge) -> dict:
    return {
        "id": f.id, "platform": f.platform, "account_id": f.account_id,
        "direction": f.direction, "uid": f.uid, "sec_uid": f.sec_uid,
        "nickname": f.nickname, "avatar": f.avatar, "signature": f.signature,
        "is_mutual": f.is_mutual, "is_following": f.is_following,
        "fetched_at": f.fetched_at.isoformat() if f.fetched_at else None,
    }


@app.get("/api/follows")
async def list_follows(account_id: int, direction: str = "following", limit: int = 500):
    with get_session() as s:
        q = (select(FollowEdge).where(FollowEdge.account_id == account_id,
                                      FollowEdge.direction == direction)
             .order_by(FollowEdge.id.desc()).limit(limit))
        return [_follow_dict(f) for f in s.exec(q).all()]


@app.post("/api/accounts/{account_id}/follows/sync")
async def sync_follows(account_id: int, direction: str = "following"):
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    if direction not in ("following", "fan"):
        raise HTTPException(400, "direction 仅支持 following | fan")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        uid = acc.sec_uid or ""
        identity = browser.identity_for(acc)
        known = {f.uid for f in s.exec(select(FollowEdge).where(
            FollowEdge.account_id == account_id,
            FollowEdge.direction == direction)).all()}
    # 抖音优先直连(following/follower list 分页,比弹窗滚动抓得全);失败再回退浏览器拦截
    users, err = [], ""
    if platform == "douyin" and engine is not None:
        try:
            users, derr = await engine.fetch_douyin_follows_direct(account_id, direction)
        except Exception as e:
            users, derr = [], repr(e)
        if not users:
            print(f"[follow] douyin direct 空({derr}),回退浏览器拦截")
    if not users:
        users, err = await fetch_follows(browser, identity, platform, uid, direction, known)
    # 仅在登录态/缺 id 这类硬错误时报错;抓到 0 条不报错(可能确实没有,或接口待标定)
    if err and err.startswith("missing_uid"):
        raise HTTPException(400, err.split(":", 1)[-1])
    if err and err.startswith("logged_out"):
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    now = datetime.utcnow()
    with get_session() as s:
        # 快照式替换:先清掉该账号该方向旧数据(含历史误抓的 JS 模块垃圾),再写入本次精确快照
        for old in s.exec(select(FollowEdge).where(
                FollowEdge.account_id == account_id,
                FollowEdge.direction == direction)).all():
            s.delete(old)
        for u in users:
            s.add(FollowEdge(platform=platform, account_id=account_id,
                             direction=direction, fetched_at=now, **u))
        s.commit()
    return {"ok": True, "fetched": len(users), "added": len(users)}


# ─────────── 本账号管理:私信 ───────────
def _conv_dict(c: DmConversation) -> dict:
    return {
        "id": c.id, "platform": c.platform, "account_id": c.account_id,
        "conv_id": c.conv_id, "peer_uid": c.peer_uid, "peer_sec_uid": c.peer_sec_uid,
        "peer_nickname": c.peer_nickname, "peer_avatar": c.peer_avatar,
        "last_text": c.last_text, "last_time": c.last_time,
        "unread_count": c.unread_count,
        "fetched_at": c.fetched_at.isoformat() if c.fetched_at else None,
    }


@app.get("/api/dm/conversations")
async def list_dm_conversations(account_id: int, limit: int = 200):
    with get_session() as s:
        q = (select(DmConversation).where(DmConversation.account_id == account_id)
             .order_by(DmConversation.last_time.desc()).limit(limit))
        return [_conv_dict(c) for c in s.exec(q).all()]


@app.get("/api/dm/messages")
async def list_dm_messages(account_id: int, conv_id: str, limit: int = 200):
    with get_session() as s:
        q = (select(DmMessage).where(DmMessage.account_id == account_id,
                                     DmMessage.conv_id == conv_id)
             .order_by(DmMessage.create_time.asc()).limit(limit))

        def _card(m):
            if not m.raw_json:
                return None
            try:
                return json.loads(m.raw_json)
            except Exception:
                return None
        return [{"id": m.id, "direction": m.direction, "text": m.text,
                 "msg_type": m.msg_type, "create_time": m.create_time,
                 "card": _card(m)}
                for m in s.exec(q).all()]


@app.post("/api/accounts/{account_id}/dm/sync")
async def sync_dm(account_id: int):
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = browser.identity_for(acc)
    convs, err = await fetch_dm_conversations(browser, identity, platform)
    if err and err.startswith("logged_out"):
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    # 小红书网页端私信未开放(entry visible=false)等硬限制:直接把原因回给前端
    if not convs and err:
        raise HTTPException(400, err)
    now = datetime.utcnow()
    with get_session() as s:
        # 快照式替换:清掉旧会话(含历史误抓的 JS 模块垃圾),写入本次抓到的
        for old in s.exec(select(DmConversation).where(
                DmConversation.account_id == account_id)).all():
            s.delete(old)
        # 会话最后一条消息也快照式重写(仅 last:<conv> 这条,历史记录由按需抓取补)
        for old in s.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.msg_id.like("last:%"))).all():
            s.delete(old)
        msgs = 0
        for c in convs:
            s.add(DmConversation(platform=platform, account_id=account_id,
                                 fetched_at=now, **c))
            # get_message_by_init 已带每会话最后一条消息:落成 thread 里的一条,
            # 让「点开会话」不再空。方向由 last_sender_uid==self_uid 判定。
            meta = {}
            try:
                meta = json.loads(c.get("raw_json") or "{}")
            except Exception:
                meta = {}
            if c.get("last_text"):
                direction = ("out" if meta.get("last_sender_uid")
                             and meta.get("last_sender_uid") == meta.get("self_uid")
                             else "in")
                s.add(DmMessage(
                    platform=platform, account_id=account_id, conv_id=c["conv_id"],
                    msg_id="last:" + c["conv_id"], direction=direction,
                    msg_type="text", text=c["last_text"],
                    create_time=c.get("last_time") or 0))
                msgs += 1
        # 顺带存账号自身 uid(= IM device_id,实时接收 WS 要用);从任一会话的 self_uid 取
        self_uid = ""
        for c in convs:
            try:
                self_uid = (json.loads(c.get("raw_json") or "{}")).get("self_uid", "")
            except Exception:
                self_uid = ""
            if self_uid:
                break
        if self_uid:
            acc2 = s.get(DouyinAccount, account_id)
            if acc2 and acc2.uid != self_uid:
                acc2.uid = self_uid
                s.add(acc2)
        s.commit()
    return {"ok": True, "fetched": len(convs), "added": len(convs), "messages": msgs}


@app.get("/api/dm/stream")
async def dm_stream(account_id: int):
    """私信实时事件流(SSE)。前端打开 DM 面板时订阅;订阅即为该账号拉起 frontier-im
    WS 长连接,新消息实时推来(也已入库)。最后一个订阅断开时自动停连。"""
    if im_receiver is None:
        raise HTTPException(503, "实时接收未就绪")
    q = await im_receiver.subscribe(account_id)

    async def gen():
        try:
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"     # 心跳,防代理断流
        except asyncio.CancelledError:
            pass
        finally:
            im_receiver.unsubscribe(account_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/accounts/{account_id}/dm/conversations/{conv_id:path}/mark-read")
async def mark_dm_read(account_id: int, conv_id: str):
    """标记会话已读:清本地未读计数(红点)。"""
    with get_session() as s:
        conv = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if conv and conv.unread_count:
            conv.unread_count = 0
            s.add(conv); s.commit()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/dm/conversations/{conv_id:path}/fetch-history")
async def fetch_dm_conversation_history(account_id: int, conv_id: str,
                                        cursor: int = 0, debug: bool = False):
    """无头抓单个会话历史消息(imapi get_by_conversation,纯 cookie),落库 DmMessage。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = browser.identity_for(acc)
        conv = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if not conv:
            raise HTTPException(404, "会话不存在(先同步会话列表)")
        short_id, self_uid = conv.conv_short_id, ""
        try:
            self_uid = (json.loads(conv.raw_json or "{}")).get("self_uid", "")
        except Exception:
            pass
    if not short_id:
        raise HTTPException(400, "该会话缺 conversation_short_id,请重新同步会话列表")
    parsed, err = await fetch_dm_history(browser, identity, platform, conv_id,
                                         short_id, conv_type=1, cursor=cursor,
                                         debug=debug)
    if err:
        raise HTTPException(400, err)
    msgs = parsed.get("messages", [])
    added = 0
    with get_session() as s:
        # 按会话快照重写:拉到消息就清掉该会话旧消息(含 last:<conv> 占位、旧错时间戳),
        # 再插本次窗口。get_by_conversation 每次返回最近一窗,快照式最简单且能纠正旧数据。
        if msgs:
            for old in s.exec(select(DmMessage).where(
                    DmMessage.account_id == account_id,
                    DmMessage.conv_id == conv_id)).all():
                s.delete(old)
        seen = set()
        for m in msgs:
            mid = m.get("server_msg_id") or ""
            if not mid or mid in seen:
                continue
            seen.add(mid)
            direction = ("out" if self_uid and m.get("sender_uid") == self_uid
                         else "in")
            card = m.get("card")
            s.add(DmMessage(
                platform=platform, account_id=account_id, conv_id=conv_id,
                msg_id=mid, direction=direction,
                msg_type=("video" if card else
                          "text" if m.get("text") else str(m.get("msg_type") or "")),
                text=m.get("text") or "", create_time=int(m.get("create_time") or 0),
                raw_json=json.dumps(card, ensure_ascii=False) if card else ""))
            added += 1
        s.commit()
    out = {"ok": True, "fetched": len(msgs), "added": added,
           "next_cursor": parsed.get("next_cursor"), "has_more": parsed.get("has_more")}
    if debug:   # 非文本消息(分享视频=8/图片=27/语音=17...)回原始 content,标定字段用
        out["media_samples"] = [
            {"msg_type": m.get("msg_type"), "text": m.get("text"),
             "content": m.get("content")}
            for m in msgs if m.get("msg_type") not in (7, 0)]
    return out


# ─────────── 本账号管理:计数汇总(账号管理面板徽章,纯查库不触发抓取)───────────
@app.get("/api/hub/summary")
async def hub_summary(account_id: int):
    with get_session() as s:
        def _n(q):
            return len(s.exec(q).all())
        return {
            "works": _n(select(AccountWork.id)
                        .where(AccountWork.account_id == account_id)),
            "following": _n(select(FollowEdge.id)
                            .where(FollowEdge.account_id == account_id,
                                   FollowEdge.direction == "following")),
            "fans": _n(select(FollowEdge.id)
                       .where(FollowEdge.account_id == account_id,
                              FollowEdge.direction == "fan")),
            "dm": _n(select(DmConversation.id)
                     .where(DmConversation.account_id == account_id)),
        }


# ─────────── 本账号管理:写操作队列(取关/回关/发私信)───────────
class ActionIn(BaseModel):
    account_id: int
    action: str                 # follow | unfollow | send_dm
    target_uid: str = ""
    target_sec_uid: str = ""
    target_nick: str = ""
    conv_id: str = ""
    content: str = ""
    run_now: bool = False        # True=立即执行;False=入队(引擎节流后执行)


def _action_dict(t: AccountActionTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id,
        "action": t.action, "target_uid": t.target_uid, "target_nick": t.target_nick,
        "content": t.content, "status": t.status, "result": t.result,
        "error": t.error, "created_at": t.created_at.isoformat() if t.created_at else None,
        "done_at": t.done_at.isoformat() if t.done_at else None,
    }


async def _exec_action(task_id: int) -> tuple[bool, str]:
    """立即执行一条写操作:委托引擎(带每账号串行锁,避免同号并发开窗)。"""
    if engine is None:
        raise HTTPException(503, "引擎未就绪")
    res = await engine.execute_action_task(task_id)
    return bool(res.get("ok")), (res.get("error") or "")


@app.get("/api/account-actions")
async def list_account_actions(account_id: int | None = None, limit: int = 100):
    with get_session() as s:
        q = select(AccountActionTask)
        if account_id:
            q = q.where(AccountActionTask.account_id == account_id)
        q = q.order_by(AccountActionTask.id.desc()).limit(limit)
        return [_action_dict(t) for t in s.exec(q).all()]


@app.post("/api/account-actions")
async def create_account_action(body: ActionIn):
    if body.action not in ("follow", "unfollow", "send_dm"):
        raise HTTPException(400, "action 仅支持 follow | unfollow | send_dm")
    if body.action == "send_dm" and not body.content.strip():
        raise HTTPException(400, "发私信需填写内容")
    if not (body.target_uid or body.target_sec_uid):
        raise HTTPException(400, "缺目标用户")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        t = AccountActionTask(
            platform=acc.platform, account_id=body.account_id, action=body.action,
            target_uid=body.target_uid, target_sec_uid=body.target_sec_uid,
            target_nick=body.target_nick, conv_id=body.conv_id,
            content=body.content.strip(), status="pending")
        s.add(t); s.commit(); s.refresh(t)
        task_id = t.id
    if body.run_now:
        ok, detail = await _exec_action(task_id)
        if not ok:
            raise HTTPException(400, f"执行失败:{detail}")
        return {"ok": True, "id": task_id, "ran": True}
    return {"ok": True, "id": task_id, "ran": False}


@app.post("/api/account-actions/{task_id}/run-now")
async def run_account_action(task_id: int):
    ok, detail = await _exec_action(task_id)
    if not ok:
        raise HTTPException(400, f"执行失败:{detail}")
    return {"ok": True}


@app.post("/api/account-actions/{task_id}/cancel")
async def cancel_account_action(task_id: int):
    with get_session() as s:
        t = s.get(AccountActionTask, task_id)
        if not t:
            raise HTTPException(404, "任务不存在")
        if t.status in ("done", "doing"):
            raise HTTPException(400, "该任务已执行,无法取消")
        t.status = "canceled"; s.add(t); s.commit()
    return {"ok": True}


_PLATFORM_HOST = {"douyin": "douyin.com", "xhs": "xiaohongshu.com",
                  "kuaishou": "kuaishou.com", "shipinhao": "weixin.qq.com"}


@app.post("/api/accounts/{account_id}/open-browser")
async def open_account_browser(account_id: int, url: str = ""):
    """用该账号登录态弹出一个真实浏览器窗口。默认停在平台首页;传 url 则停在该地址
    (仅允许本平台域名,用于「查看」视频号作品/管理页等需登录态才能打开的页面)。
    留给用户手动操作(查看/收发私信、F12 抓接口、手动维护等)。关闭窗口即落盘 Cookie。
    注意:窗口开着期间该账号的后台抓取/写操作会因 profile 占用而暂时失败,用完关掉即可。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = browser.identity_for(acc)
        states = [acc.storage_state or "", acc.creator_storage_state or ""]
    # 该账号已开着窗口就先关旧的(同一 profile 不能并存)
    old = open_browsers.pop(account_id, None)
    if old is not None:
        try:
            await old.close()
        except Exception:
            pass
    home = {"xhs": "https://www.xiaohongshu.com/",
            "kuaishou": "https://www.kuaishou.com/",
            "shipinhao": "https://channels.weixin.qq.com/platform"}.get(
                platform, "https://www.douyin.com/")
    # 传了 url 且属于本平台域名 -> 停在该地址(否则回首页,防被当跳转开任意站)
    tgt = (url or "").strip()
    if tgt.startswith("http") and _PLATFORM_HOST.get(platform, "") in tgt:
        home = tgt
    # 持久 profile 只在"首次空目录"才注入登录态;为防 profile 里 Cookie 缺失/过期导致
    # 打开后未登录,这里用 DB 里已知的登录态 Cookie 再注入一次(覆盖刷新)。
    from .browser.manager import _sanitize_cookies
    cookies = []
    for st in states:
        if st:
            try:
                cookies.extend(json.loads(st).get("cookies") or [])
            except Exception:
                pass
    try:
        ctx = await browser.open_headed(identity)
        if cookies:
            try:
                await ctx.add_cookies(_sanitize_cookies(cookies))
            except Exception as e:
                print(f"[open-browser] 注入 Cookie 失败: {e!r}")
        page = await ctx.new_page()
        await page.goto(home, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        raise HTTPException(500, f"打开浏览器失败: {e!r}")
    open_browsers[account_id] = ctx
    try:                       # 用户手动关窗后,从登记表移除
        ctx.on("close", lambda *_: open_browsers.pop(account_id, None))
    except Exception:
        pass
    return {"ok": True}


# ─────────── 账号代理(风控隔离)───────────
class ProxyIn(BaseModel):
    proxy: str = ""


@app.put("/api/accounts/{account_id}/proxy")
async def set_account_proxy(account_id: int, body: ProxyIn):
    """手动设置/清空账号专属代理。改后会关掉该账号常驻 context,下次用新代理重开。"""
    from .browser.manager import _parse_proxy, normalize_proxy
    p = (body.proxy or "").strip()
    if p and not _parse_proxy(p):
        raise HTTPException(400, "代理格式无法解析,示例:http://user:pass@host:port 或 socks5://host:port")
    p = normalize_proxy(p)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        acc.proxy = p
        acc.proxy_status = "unknown"
        s.add(acc); s.commit()
    if browser:
        await browser.close_context(account_id)
    return {"ok": True, "proxy": _mask_proxy(p)}


@app.post("/api/accounts/{account_id}/assign-proxy")
async def assign_account_proxy(account_id: int):
    """从代理池(config.proxies)给该账号分配一条占用最少的代理。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        p = assign_proxy_from_pool(s, cfg)
        if not p:
            raise HTTPException(400, "代理池为空(请在 config.yaml 的 proxies 里配置)")
        acc.proxy = p
        acc.proxy_status = "unknown"
        s.add(acc); s.commit()
    if browser:
        await browser.close_context(account_id)
    return {"ok": True, "proxy": _mask_proxy(p)}


async def _probe_proxy(url: str, platform: str = "douyin", timeout: float = 15):
    """经代理实连一次目标站,返回 (ok, detail)。"""
    import httpx
    if not url:
        return False, "未配置代理"
    test_url = ("https://www.xiaohongshu.com/" if platform == "xhs"
                else "https://www.kuaishou.com/" if platform == "kuaishou"
                else "https://channels.weixin.qq.com/" if platform == "shipinhao"
                else "https://www.douyin.com/")
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout, follow_redirects=True) as cli:
            r = await cli.get(test_url)
        return r.status_code < 500, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _parse_ipinfo(j: dict) -> dict:
    return {"ip": j.get("ip", ""), "country": j.get("country", ""),
            "region": j.get("region", ""), "city": j.get("city", ""),
            "isp": j.get("org", "")}


def _parse_ipapi(j: dict) -> dict:
    if j.get("status") != "success":
        return {}
    return {"ip": j.get("query", ""), "country": j.get("country", ""),
            "region": j.get("regionName", ""), "city": j.get("city", ""),
            "isp": j.get("isp", "")}


async def _proxy_geo(proxy_url: str, timeout: float = 8) -> dict | None:
    """经代理查出口 IP 及归属地(多源兜底)。返回 {ip,country,region,city,isp} 或 None。"""
    import httpx
    sources = [
        ("http://ip-api.com/json/?lang=zh-CN&fields=status,country,regionName,city,isp,query",
         _parse_ipapi),
        ("https://ipinfo.io/json", _parse_ipinfo),
    ]
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout,
                                     follow_redirects=True) as cli:
            for url, parser in sources:
                try:
                    g = parser((await cli.get(url)).json())
                    if g and g.get("ip"):
                        return g
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _geo_text(g: dict | None) -> str:
    if not g:
        return ""
    loc = " · ".join([x for x in (g.get("country"), g.get("region"), g.get("city")) if x])
    parts = [p for p in (g.get("ip"), loc, g.get("isp")) if p]
    return "  ".join(parts)


async def _detect_proxy(raw: str) -> dict:
    """自动判别代理类型(HTTP / SOCKS5)与是否需要认证。
    对同一 host:port 依次试 [按输入协议 或 http+socks5] × [免密 / 带密(若输入含账密)],
    取第一个连通的组合。返回判别结果 + 推荐的规范化地址 + 浏览器兼容性。"""
    from urllib.parse import urlparse
    raw = (raw or "").strip()
    if not raw:
        return {"ok": False, "error": "请先填代理地址"}
    has_scheme = "://" in raw
    u = urlparse(raw if has_scheme else "http://" + raw)
    host, port = u.hostname, u.port
    if not host or not port:
        return {"ok": False, "error": "地址需含 host:port"}
    user, pwd = u.username, u.password
    cred = f"{user}:{pwd}@" if user else ""
    # 候选协议:输入已带则只测它,否则 http 与 socks5 都试
    if has_scheme and u.scheme in ("http", "https", "socks5", "socks5h"):
        schemes = [u.scheme]
    else:
        schemes = ["http", "socks5"]

    tried = []
    found = None   # (scheme, auth_mode, url)
    for sch in schemes:
        # 先试免密
        url0 = f"{sch}://{host}:{port}"
        ok, detail = await _probe_proxy(url0, timeout=8)
        tried.append({"scheme": sch, "auth": "none", "ok": ok, "detail": detail})
        if ok:
            found = (sch, "none", url0)
            break
        # 免密不通且输入带账密 -> 再试带密
        if cred:
            url1 = f"{sch}://{cred}{host}:{port}"
            ok1, detail1 = await _probe_proxy(url1, timeout=8)
            tried.append({"scheme": sch, "auth": "required", "ok": ok1, "detail": detail1})
            if ok1:
                found = (sch, "required", url1)
                break

    if not found:
        return {"ok": False, "error": "所有组合都连不通(可能是 IP 未加白名单/需账号密码/代理已失效)",
                "tried": tried, "need_auth_hint": not cred}

    sch, auth_mode, url = found
    is_socks = sch.startswith("socks")
    browser_ok = not (is_socks and auth_mode == "required")  # Playwright 不支持带密 SOCKS5
    geo = await _proxy_geo(url)              # 经该代理查出口 IP 归属地
    return {
        "ok": True, "scheme": sch, "auth": auth_mode,
        "recommend": url, "browser_ok": browser_ok, "tried": tried,
        "geo": geo, "geo_text": _geo_text(geo),
        "note": ("HTTP 代理,浏览器与直连都支持" if not is_socks
                 else ("免密 SOCKS5,浏览器与直连都支持" if browser_ok
                       else "带密 SOCKS5:小红书直连/下载可用,但浏览器抓取/登录不支持(建议改用该节点的 HTTP 端口)")),
    }


class ProxyDetectIn(BaseModel):
    url: str


@app.post("/api/proxies/detect")
async def detect_proxy(body: ProxyDetectIn):
    return await _detect_proxy(body.url)


@app.post("/api/accounts/assign-proxies-all")
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
            p = assign_proxy_from_pool(s, cfg)   # 每次重算占用,保持均衡
            if not p:
                pool_empty = True
                remaining.append(aid)
                continue
            acc.proxy = p
            acc.proxy_status = "unknown"
            s.add(acc); s.commit()
            assigned += 1
        if browser:
            await browser.close_context(aid)
    if pool_empty and assigned == 0:
        raise HTTPException(400, "代理池为空,请先在「代理池」添加代理")
    return {"ok": True, "assigned": assigned, "unassigned": len(remaining)}


@app.post("/api/accounts/{account_id}/test-proxy")
async def test_account_proxy(account_id: int):
    """通过该账号代理实际连一次目标站,验证代理可用并更新 proxy_status。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        proxy = acc.proxy or ""
        platform = acc.platform
    if not proxy:
        return {"ok": False, "detail": "该账号未配置代理(将走宿主真实 IP)"}
    ok, detail = await _probe_proxy(proxy, platform)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if acc:
            acc.proxy_status = "ok" if ok else "bad"
            s.add(acc); s.commit()
    return {"ok": ok, "detail": detail, "proxy": _mask_proxy(proxy)}


# ─────────── 代理池(提前配置,账号关联使用)───────────
class PoolProxyIn(BaseModel):
    url: str
    label: str = ""
    note: str = ""
    enabled: bool = True
    geo: Dict[str, Any] | None = None       # 判别得到的归属地(可选,建库时一并写入)


class PoolProxyUpdate(BaseModel):
    url: str | None = None
    label: str | None = None
    note: str | None = None
    enabled: bool | None = None


def _is_mainland(country: str, region: str, city: str) -> bool:
    if country not in ("中国", "China", "CN"):
        return False
    blob = (region or "") + (city or "")
    return not any(x in blob for x in ("香港", "澳门", "澳門", "台湾", "台灣",
                                       "Hong Kong", "Macau", "Taiwan"))


def _geo_loc(p) -> str:
    return " · ".join([x for x in (p.country, p.region, p.city) if x])


def _pool_dict(p: ProxyPool, used: int = 0) -> dict:
    return {
        "id": p.id, "label": p.label, "url": _mask_proxy(p.url),
        "url_full": p.url, "enabled": p.enabled, "status": p.status,
        "note": p.note, "used_by": used,
        "exit_ip": p.exit_ip, "country": p.country, "region": p.region,
        "city": p.city, "isp": p.isp,
        "geo_loc": _geo_loc(p), "is_mainland": _is_mainland(p.country, p.region, p.city),
        "geo_checked": bool(p.exit_ip),
        "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
    }


@app.get("/api/proxies")
async def list_proxies():
    with get_session() as s:
        rows = s.exec(select(ProxyPool).order_by(ProxyPool.id)).all()
        used = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                used[a.proxy] = used.get(a.proxy, 0) + 1
        return [_pool_dict(p, used.get(p.url, 0)) for p in rows]


@app.post("/api/proxies")
async def add_proxy(body: PoolProxyIn):
    from .browser.manager import _parse_proxy, normalize_proxy
    url = (body.url or "").strip()
    if not url or not _parse_proxy(url):
        raise HTTPException(400, "代理格式无法解析,示例:http://user:pass@host:port 或 socks5://host:port")
    url = normalize_proxy(url)
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


class ProxyImportIn(BaseModel):
    text: str = ""        # 多行,每行一个代理(可带 # 注释、空行)


@app.post("/api/proxies/import")
async def import_proxies(body: ProxyImportIn):
    """批量粘贴多行导入代理池。每行一个,支持 # 注释/空行,自动校验+去重。"""
    from .browser.manager import _parse_proxy, normalize_proxy
    added = skipped = invalid = 0
    invalid_lines = []
    with get_session() as s:
        existing = {p.url for p in s.exec(select(ProxyPool)).all()}
        seen = set(existing)
        for raw in (body.text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # 允许 "备注,地址" 或 "备注 地址" 形式;否则整行当地址
            label, url = "", line
            for sep in (",", "\t", " | ", " "):
                if sep in line:
                    a, b = line.split(sep, 1)
                    if _parse_proxy(b.strip()):
                        label, url = a.strip(), b.strip()
                    break
            url = normalize_proxy(url.strip())
            if not _parse_proxy(url):
                invalid += 1
                if len(invalid_lines) < 8:
                    invalid_lines.append(line[:60])
                continue
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


@app.put("/api/proxies/{pid}")
async def update_proxy(pid: int, body: PoolProxyUpdate):
    from .browser.manager import _parse_proxy, normalize_proxy
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        if body.url is not None:
            url = body.url.strip()
            if not url or not _parse_proxy(url):
                raise HTTPException(400, "代理格式无法解析")
            p.url = normalize_proxy(url)
        if body.label is not None:
            p.label = body.label.strip()
        if body.note is not None:
            p.note = body.note.strip()
        if body.enabled is not None:
            p.enabled = body.enabled
        s.add(p); s.commit(); s.refresh(p)
        return _pool_dict(p)


@app.delete("/api/proxies/{pid}")
async def del_proxy(pid: int):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            return {"ok": True}
        used = len(s.exec(select(DouyinAccount.id)
                          .where(DouyinAccount.proxy == p.url)).all())
        s.delete(p); s.commit()
    return {"ok": True, "still_used_by": used}


@app.post("/api/proxies/{pid}/test")
async def test_proxy_entry(pid: int):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        url = p.url
    ok, detail = await _probe_proxy(url)
    geo = await _proxy_geo(url) if ok else None
    geo_text = _geo_text(geo)
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if p:
            p.status = "ok" if ok else "bad"
            p.last_checked_at = datetime.utcnow()
            if geo:                            # 归属地写入结构化字段(供「地区」列展示)
                p.exit_ip = geo.get("ip", "") or p.exit_ip
                p.country = geo.get("country", "") or p.country
                p.region = geo.get("region", "") or p.region
                p.city = geo.get("city", "") or p.city
                p.isp = geo.get("isp", "") or p.isp
            s.add(p); s.commit()
    return {"ok": ok, "detail": detail, "geo": geo, "geo_text": geo_text}


@app.get("/api/proxies/options")
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
        return [{"id": p.id, "label": p.label or _mask_proxy(p.url),
                 "url": p.url, "masked": _mask_proxy(p.url),
                 "status": p.status, "enabled": p.enabled,
                 "used_by": used.get(p.url, 0)} for p in rows]


# ─────────── 全局设置 ───────────
QUALITY_CHOICES = {"highest", "1080", "720", "540", "lowest"}


class SettingsIn(BaseModel):
    download_dir: str | None = None
    video_quality: str | None = None
    # 大模型 API 文案生成(自动评论用;OpenAI 兼容接口)
    ai_enabled: bool | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None        # 留空=不改(避免误清空已存的 key)
    ai_model: str | None = None
    ai_prompt: str | None = None
    ai_temperature: str | None = None


def _settings_dict() -> dict:
    return {
        "download_dir": get_setting("download_dir", cfg.engine.media_dir),
        "video_quality": get_setting("video_quality", "highest"),
        "ai_enabled": get_setting("ai_enabled", "0") == "1",
        "ai_base_url": get_setting("ai_base_url", ""),
        "ai_model": get_setting("ai_model", ""),
        "ai_prompt": get_setting("ai_prompt", ""),
        "ai_temperature": get_setting("ai_temperature", "0.9"),
        # 不回传明文 key,只告知是否已配置
        "ai_api_key_set": bool(get_setting("ai_api_key", "")),
    }


@app.get("/api/settings")
async def get_settings():
    return _settings_dict()


@app.put("/api/settings")
async def put_settings(body: SettingsIn):
    if body.download_dir is not None:
        path = body.download_dir.strip()
        if path:
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise HTTPException(400, f"目录不可用: {e}")
        set_setting("download_dir", path)
    if body.video_quality is not None:
        q = body.video_quality.strip() or "highest"
        if q not in QUALITY_CHOICES:
            raise HTTPException(400, f"画质取值无效: {q}")
        set_setting("video_quality", q)
    if body.ai_enabled is not None:
        set_setting("ai_enabled", "1" if body.ai_enabled else "0")
    if body.ai_base_url is not None:
        set_setting("ai_base_url", body.ai_base_url.strip())
    if body.ai_model is not None:
        set_setting("ai_model", body.ai_model.strip())
    if body.ai_prompt is not None:
        set_setting("ai_prompt", body.ai_prompt)
    if body.ai_temperature is not None:
        set_setting("ai_temperature", (body.ai_temperature or "0.9").strip())
    if body.ai_api_key:    # 仅在传了非空值时更新,留空=保留原 key
        set_setting("ai_api_key", body.ai_api_key.strip())
    return _settings_dict()


class AiTestIn(BaseModel):
    # 可选覆盖(便于保存前先测);留空则用已保存设置。key 留空=用已存的
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    prompt: str | None = None
    temperature: str | None = None


@app.post("/api/settings/ai-test")
async def ai_test(body: AiTestIn):
    """用当前(或传入的)AI 配置做一次最小生成,验证连通性。返回 {ok, sample/error}。"""
    from .engine import compose
    ai = {
        "base_url": body.base_url if body.base_url is not None else get_setting("ai_base_url", ""),
        "api_key": body.api_key if body.api_key else get_setting("ai_api_key", ""),
        "model": body.model if body.model is not None else get_setting("ai_model", ""),
        "prompt": body.prompt if body.prompt is not None else get_setting("ai_prompt", ""),
        "temperature": body.temperature or get_setting("ai_temperature", "0.9"),
        "timeout": 25,
    }
    if not (ai["base_url"] and ai["api_key"] and ai["model"]):
        raise HTTPException(400, "请先填写 Base URL / 模型,并保存或填入 API Key")
    ctx = {"source_text": "这条视频拍得太治愈了,期待更新!", "nick": "测试用户",
           "kw": "", "platform": "douyin", "mode": "auto_reply"}
    try:
        text = await compose.generate(ctx, ai)
        return {"ok": True, "sample": text}
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        return {"ok": False, "error": f"{msg}(检查 Base URL / Key / 模型 / 网络/代理)"}


# ─────────── 监控目标 ───────────
class TargetIn(BaseModel):
    url_or_secuid: str                       # 抖音/小红书主页链接 或 小红书关键词
    platform: str = "douyin"                # douyin | xhs
    target_kind: str = "creator"            # creator | keyword(仅小红书)
    account_id: int | None = None
    interval_seconds: int = 300
    initial_backfill_count: int | None = None
    download_dir: str = ""
    video_quality: str = ""


class TargetUpdate(BaseModel):
    download_dir: str | None = None
    interval_seconds: int | None = None
    video_quality: str | None = None
    account_id: int | None = None
    relay_to_xhs_account_id: int | None = None   # -1 清除;>0 设为该小红书账号


@app.post("/api/monitors")
async def add_monitor(body: TargetIn):
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou") else "douyin"
    sec_uid = keyword = xsec_token = ""
    kind = "creator"

    if platform == "xhs" and body.target_kind == "keyword":
        kind = "keyword"
        keyword = body.url_or_secuid.strip()
        if not keyword:
            raise HTTPException(400, "请输入要监控的搜索关键词")
    elif platform == "xhs":
        ref = await xhs_resolve_user(body.url_or_secuid, cfg.engine.user_agent)
        if not ref:
            raise HTTPException(400, "无法解析小红书 user_id,请粘贴创作者主页链接 / xhslink 短链 / 24 位 user_id")
        sec_uid, xsec_token = ref.user_id, ref.xsec_token
    elif platform == "kuaishou":
        sec_uid = await resolve_ks_user_id(body.url_or_secuid, cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析快手 user_id,请粘贴创作者主页链接 / v.kuaishou.com 短链 / user_id")
    else:
        sec_uid = await resolve_sec_uid(body.url_or_secuid, cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析 sec_uid,请粘贴主页链接 / v.douyin.com 短链 / sec_uid")

    dl = body.download_dir.strip()
    if dl:
        try:
            Path(dl).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"下载目录不可用: {e}")
    with get_session() as s:
        if platform == "douyin":
            if not body.account_id:
                raise HTTPException(
                    400, "抖音作品监控必须选择已登录账号,匿名抓取可能返回陈旧或残缺作品")
            monitor_acc = s.get(DouyinAccount, body.account_id)
            if (not monitor_acc or monitor_acc.platform != "douyin"
                    or monitor_acc.status != "active"):
                raise HTTPException(400, "所选抖音账号不存在或登录态已失效")
        elif body.account_id:
            monitor_acc = s.get(DouyinAccount, body.account_id)
            if not monitor_acc or monitor_acc.platform != platform:
                raise HTTPException(400, "所选账号不存在或与监控平台不匹配")
        if kind == "keyword":
            dup = s.exec(select(MonitorTarget).where(MonitorTarget.platform == platform)
                         .where(MonitorTarget.keyword == keyword)).first()
        else:
            dup = s.exec(select(MonitorTarget).where(MonitorTarget.platform == platform)
                         .where(MonitorTarget.sec_uid == sec_uid)).first()
        if dup:
            raise HTTPException(409, "该监控目标已存在")
        q = body.video_quality.strip()
        if q and q not in QUALITY_CHOICES:
            raise HTTPException(400, f"画质取值无效: {q}")
        backfill_count = (cfg.engine.monitor_initial_backfill_count
                          if body.initial_backfill_count is None
                          else body.initial_backfill_count)
        if backfill_count < -1 or backfill_count > 1000:
            raise HTTPException(400, "首次回填数须为 -1(尽可能全量)或 0~1000")
        t = MonitorTarget(platform=platform, target_kind=kind, keyword=keyword,
                          sec_uid=sec_uid, xsec_token=xsec_token,
                          nickname=("#" + keyword) if kind == "keyword" else "",
                          account_id=body.account_id,
                          interval_seconds=body.interval_seconds, download_dir=dl,
                          initial_backfill_count=backfill_count, video_quality=q)
        s.add(t); s.commit(); s.refresh(t)
        return _target_dict(t)


@app.put("/api/monitors/{tid}")
async def update_monitor(tid: int, body: TargetUpdate):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if not t:
            raise HTTPException(404)
        if body.download_dir is not None:
            dl = body.download_dir.strip()
            if dl:
                try:
                    Path(dl).expanduser().mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    raise HTTPException(400, f"下载目录不可用: {e}")
            t.download_dir = dl
        if body.interval_seconds is not None:
            t.interval_seconds = body.interval_seconds
        if body.video_quality is not None:
            q = body.video_quality.strip()
            if q and q not in QUALITY_CHOICES:
                raise HTTPException(400, f"画质取值无效: {q}")
            t.video_quality = q
        if body.account_id is not None:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != t.platform or acc.status != "active":
                raise HTTPException(400, "账号不存在、登录态失效或与监控平台不匹配")
            t.account_id = body.account_id
        if body.relay_to_xhs_account_id is not None:
            if body.relay_to_xhs_account_id and body.relay_to_xhs_account_id > 0:
                acc = s.get(DouyinAccount, body.relay_to_xhs_account_id)
                if not acc or acc.platform != "xhs":
                    raise HTTPException(400, "转发目标须为已登录的小红书账号")
                if not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
                    raise HTTPException(400, "转发目标账号不可发布:请对该号完成「小红书扫码登录」或「创作者登录」")
                t.relay_to_xhs_account_id = body.relay_to_xhs_account_id
            else:
                t.relay_to_xhs_account_id = None
        s.add(t); s.commit(); s.refresh(t)
        return _target_dict(t)


@app.get("/api/monitors")
async def list_monitors(platform: str | None = None):
    with get_session() as s:
        q = select(MonitorTarget)
        if platform:
            q = q.where(MonitorTarget.platform == platform)
        ts = s.exec(q).all()
        out = []
        for t in ts:
            d = _target_dict(t)
            d["content_count"] = len(s.exec(
                select(ContentRecord).where(ContentRecord.target_id == t.id)).all())
            out.append(d)
        return out


@app.post("/api/monitors/{tid}/toggle")
async def toggle_monitor(tid: int):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if not t:
            raise HTTPException(404)
        t.enabled = not t.enabled
        s.add(t); s.commit()
        return {"enabled": t.enabled}


@app.post("/api/monitors/{tid}/run-now")
async def run_now(tid: int):
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    return await engine.scan_target(tid)


@app.delete("/api/monitors/{tid}")
async def del_monitor(tid: int):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


@app.get("/api/monitors/{tid}/contents")
async def target_contents(tid: int):
    with get_session() as s:
        rows = s.exec(select(ContentRecord)
                      .where(ContentRecord.target_id == tid)
                      .order_by(ContentRecord.create_time.desc())).all()
        return [_content_dict(r) for r in rows]


@app.get("/api/contents")
async def all_contents(limit: int = 100, platform: str | None = None,
                       target_id: int | None = None):
    with get_session() as s:
        q = select(ContentRecord)
        if platform:
            q = q.where(ContentRecord.platform == platform)
        if target_id is not None:
            q = q.where(ContentRecord.target_id == target_id)
        # 按作品发布时间倒序(回填时多条同批入库,用 id 排序会乱;create_time 才是真实时间序)
        rows = s.exec(q.order_by(ContentRecord.create_time.desc(),
                                 ContentRecord.id.desc()).limit(limit)).all()
        return [_content_dict(r) for r in rows]


@app.get("/api/stats/series")
async def stats_series(platform: str | None = None, days: int = 7):
    """近 N 天每天采集到的新作品 / 新评论计数(按入库时间 created_at 分桶),供总览图表用。"""
    from datetime import timedelta
    days = max(1, min(days, 31))
    today = datetime.utcnow().date()
    labels = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    index = {d: i for i, d in enumerate(labels)}

    def bucket(model) -> list[int]:
        counts = [0] * days
        with get_session() as s:
            q = select(model.created_at)
            if platform:
                q = q.where(model.platform == platform)
            for ts in s.exec(q).all():
                if not ts:
                    continue
                key = (ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10])
                i = index.get(key)
                if i is not None:
                    counts[i] += 1
        return counts

    return {"days": labels, "contents": bucket(ContentRecord),
            "comments": bucket(CommentRecord)}


def _target_dict(t: MonitorTarget) -> dict:
    return {
        "id": t.id, "platform": t.platform, "target_kind": t.target_kind,
        "keyword": t.keyword,
        "sec_uid": t.sec_uid, "nickname": t.nickname, "avatar": t.avatar,
        "enabled": t.enabled, "interval_seconds": t.interval_seconds,
        "initial_backfill_count": t.initial_backfill_count,
        "download_dir": t.download_dir, "video_quality": t.video_quality,
        "account_id": t.account_id,
        "relay_to_xhs_account_id": t.relay_to_xhs_account_id,
        "last_scan_at": t.last_scan_at.isoformat() if t.last_scan_at else None,
        "last_error": t.last_error,
    }


def _content_dict(r: ContentRecord) -> dict:
    return {
        "id": r.id, "platform": r.platform, "target_id": r.target_id,
        "aweme_id": r.aweme_id, "desc": r.desc, "media_type": r.media_type,
        "quality": r.quality, "create_time": r.create_time, "cover_url": r.cover_url,
        "like_count": r.like_count, "comment_count": r.comment_count,
        "duration": r.duration, "retry_count": r.retry_count,
        "download_status": r.download_status, "local_path": r.local_path, "error": r.error,
    }


def _content_local_path(rec: ContentRecord) -> Path | None:
    if not rec.local_path:
        return None
    try:
        path = Path(rec.local_path).expanduser().resolve(strict=True)
        return path if path.is_file() or path.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _content_local_media_path(rec: ContentRecord) -> Path | None:
    """Return the downloaded single-file media for a content record, if usable."""
    path = _content_local_path(rec)
    try:
        return path if path and path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _reveal_in_file_manager(path: Path):
    """Open a directory or select a file in the host OS file manager."""
    target = str(path)
    if sys.platform == "win32":
        with _file_manager_lock:
            folder = path if path.is_dir() else path.parent
            snapshot = capture_window_snapshot(EXPLORER_WINDOW_CLASSES)
            if path.is_dir():
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer.exe", "/select,", target])
            bring_window_to_front(snapshot, EXPLORER_WINDOW_CLASSES,
                                  title_hint=folder.name, timeout=2.5)
        return
    if sys.platform == "darwin":
        args = ["open", target] if path.is_dir() else ["open", "-R", target]
    else:
        args = ["xdg-open", target if path.is_dir() else str(path.parent)]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


@app.get("/api/contents/{cid}/media")
async def content_media(cid: int):
    """返回一条作品/笔记的媒体直链列表,供前端预览(图集/视频)。"""
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        try:
            medias = json.loads(rec.media_json or "[]")
        except Exception:
            medias = []
        local_media = _content_local_media_path(rec)
        return {
            "id": rec.id, "platform": rec.platform, "desc": rec.desc,
            "media_type": rec.media_type, "cover_url": rec.cover_url,
            "local_path": rec.local_path, "medias": medias,
            "local_url": f"/api/contents/{rec.id}/local-media" if local_media else "",
        }


@app.api_route("/api/contents/{cid}/local-media", methods=["GET", "HEAD"])
async def content_local_media(cid: int):
    """Stream downloaded media from the recorded path, including HTTP Range support."""
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        path = _content_local_media_path(rec)
    if not path:
        raise HTTPException(404, "本地媒体不存在")
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )


@app.post("/api/contents/{cid}/reveal")
async def reveal_content_file(cid: int, request: Request):
    """在服务所在电脑的文件管理器中打开本地目录或定位文件。"""
    def is_loopback(host: str) -> bool:
        host = host.split("%", 1)[0].casefold()
        if host == "localhost":
            return True
        try:
            return ip_address(host).is_loopback
        except ValueError:
            return False

    client_host = request.client.host if request.client else ""
    page_host = request.url.hostname or ""
    try:
        origin = urlsplit(request.headers.get("origin", ""))
        same_origin = (origin.scheme == request.url.scheme and
                       (origin.hostname or "").casefold() == page_host.casefold() and
                       origin.port == request.url.port)
    except ValueError:
        same_origin = False
    local_action = request.headers.get("x-creatorhub-local-action") == "reveal"
    if not is_loopback(client_host) or not is_loopback(page_host) or \
            not same_origin or not local_action:
        raise HTTPException(403, "仅允许从本机 CreatorHub 页面打开文件夹")
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        path = _content_local_path(rec)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(_reveal_in_file_manager, path)
    except OSError as e:
        raise HTTPException(500, f"打开文件夹失败:{e}") from e
    return {"ok": True}


@app.post("/api/contents/{cid}/retry-download")
async def retry_download(cid: int):
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    return await engine.retry_download(cid)


def _delete_content_files(rec: ContentRecord):
    """只删除该作品自己的文件(按 aweme_id 前缀),不动作者文件夹其它内容。"""
    if not rec.local_path:
        return 0
    p = Path(rec.local_path)
    folder = p if p.is_dir() else p.parent
    if not folder.exists():
        return 0
    n = 0
    for f in folder.glob(f"{rec.aweme_id}_*"):
        try:
            f.unlink(); n += 1
        except Exception:
            pass
    return n


@app.delete("/api/contents/{cid}")
async def del_content(cid: int, with_file: bool = True):
    removed = 0
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        if with_file:
            removed = _delete_content_files(rec)
        s.delete(rec); s.commit()
    return {"ok": True, "files_removed": removed}


class IdsIn(BaseModel):
    ids: list[int]
    with_file: bool = True


@app.post("/api/contents/batch-delete")
async def batch_del_contents(body: IdsIn):
    deleted = removed = 0
    with get_session() as s:
        for cid in body.ids:
            rec = s.get(ContentRecord, cid)
            if not rec:
                continue
            if body.with_file:
                removed += _delete_content_files(rec)
            s.delete(rec); deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted, "files_removed": removed}


# ─────────── 评论监控(独立实体)───────────
class WatchIn(BaseModel):
    url_or_id: str                       # 视频/笔记链接、主页链接、id
    platform: str = "douyin"            # douyin | xhs
    kind: str = "auto"                  # auto | video(单条视频/笔记) | user(账号/创作者)
    mode: str = "public"               # public | creator(仅抖音 user)
    account_id: int | None = None
    interval_seconds: int = 600


class WatchUpdate(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    mode: str | None = None


def _watch_dict(w: CommentWatch) -> dict:
    return {
        "id": w.id, "platform": w.platform,
        "kind": w.kind, "aweme_id": w.aweme_id, "sec_uid": w.sec_uid,
        "title": w.title, "avatar": w.avatar, "mode": w.mode,
        "account_id": w.account_id, "interval_seconds": w.interval_seconds,
        "enabled": w.enabled, "comment_count": w.comment_count,
        "last_scan_at": w.last_scan_at.isoformat() if w.last_scan_at else None,
        "last_error": w.last_error,
    }


@app.get("/api/comment-watches")
async def list_watches(platform: str | None = None):
    with get_session() as s:
        q = select(CommentWatch)
        if platform:
            q = q.where(CommentWatch.platform == platform)
        return [_watch_dict(w) for w in s.exec(q).all()]


@app.post("/api/comment-watches")
async def add_watch(body: WatchIn):
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou") else "douyin"
    aweme_id = sec_uid = xsec_token = ""
    title = ""

    if platform == "xhs":
        kind = body.kind
        if kind == "auto":
            kind = "video" if xhs_looks_like_note(body.url_or_id) else "user"
        if kind == "video":
            ref = await xhs_resolve_note(body.url_or_id, cfg.engine.user_agent)
            if not ref:
                raise HTTPException(400, "无法解析小红书笔记,请粘贴 explore 笔记链接 / xhslink 短链 / 24 位 note_id")
            aweme_id, xsec_token = ref.note_id, ref.xsec_token
            title = "笔记 " + aweme_id
        else:
            ref = await xhs_resolve_user(body.url_or_id, cfg.engine.user_agent)
            if not ref:
                raise HTTPException(400, "无法解析小红书创作者,请粘贴主页链接 / xhslink 短链 / 24 位 user_id")
            sec_uid, xsec_token = ref.user_id, ref.xsec_token
        mode = "public"
    elif platform == "kuaishou":
        kind = body.kind
        if kind == "auto":
            kind = "video" if ks_looks_like_photo(body.url_or_id) else "user"
        if kind == "video":
            aweme_id = await resolve_ks_photo_id(body.url_or_id, cfg.engine.user_agent)
            if not aweme_id:
                raise HTTPException(400, "无法解析快手作品 id,请粘贴作品链接 / v.kuaishou.com 短链 / photo_id")
            title = "作品 " + aweme_id
        else:
            sec_uid = await resolve_ks_user_id(body.url_or_id, cfg.engine.user_agent)
            if not sec_uid:
                raise HTTPException(400, "无法解析快手 user_id,请粘贴主页链接 / 短链 / user_id")
        mode = "public"
    else:
        kind = body.kind
        if kind == "auto":
            kind = "video" if looks_like_video(body.url_or_id) else "user"
        if kind == "video":
            aweme_id = await resolve_aweme_id(body.url_or_id, cfg.engine.user_agent)
            if not aweme_id:
                raise HTTPException(400, "无法解析视频 id,请粘贴作品链接 / 短链 / 数字 id")
            title = "视频 " + aweme_id
        else:
            sec_uid = await resolve_sec_uid(body.url_or_id, cfg.engine.user_agent)
            if not sec_uid:
                raise HTTPException(400, "无法解析 sec_uid,请粘贴主页链接 / 短链 / sec_uid")
        mode = body.mode if body.mode in ("public", "creator") else "public"
        if mode == "creator":
            if kind != "user":
                raise HTTPException(400, "创作中心模式只能用于「账号」类型")
            with get_session() as s:
                acc = s.get(DouyinAccount, body.account_id) if body.account_id else None
                has_creator = bool(acc and acc.creator_storage_state)
            if not has_creator:
                raise HTTPException(400, "创作中心模式需要选择一个已“创作者登录”的账号")

    with get_session() as s:
        if kind == "video":
            dup = s.exec(select(CommentWatch).where(CommentWatch.platform == platform)
                         .where(CommentWatch.aweme_id == aweme_id)).first()
        else:
            dup = s.exec(select(CommentWatch).where(CommentWatch.platform == platform)
                         .where(CommentWatch.sec_uid == sec_uid)
                         .where(CommentWatch.mode == mode)).first()
        if dup:
            raise HTTPException(409, "已存在相同的评论监控")
        w = CommentWatch(platform=platform, kind=kind, aweme_id=aweme_id, sec_uid=sec_uid,
                         xsec_token=xsec_token, mode=mode, account_id=body.account_id,
                         interval_seconds=body.interval_seconds, title=title)
        s.add(w); s.commit(); s.refresh(w)
        return _watch_dict(w)


@app.put("/api/comment-watches/{wid}")
async def update_watch(wid: int, body: WatchUpdate):
    with get_session() as s:
        w = s.get(CommentWatch, wid)
        if not w:
            raise HTTPException(404)
        if body.enabled is not None:
            w.enabled = body.enabled
        if body.interval_seconds is not None:
            w.interval_seconds = body.interval_seconds
        if body.mode is not None and body.mode in ("public", "creator"):
            w.mode = body.mode
        s.add(w); s.commit(); s.refresh(w)
        return _watch_dict(w)


@app.delete("/api/comment-watches/{wid}")
async def del_watch(wid: int, with_comments: bool = True):
    with get_session() as s:
        w = s.get(CommentWatch, wid)
        if not w:
            return {"ok": True}
        if with_comments:
            for c in s.exec(select(CommentRecord).where(CommentRecord.watch_id == wid)).all():
                s.delete(c)
        s.delete(w); s.commit()
    return {"ok": True}


@app.post("/api/comment-watches/{wid}/scan-now")
async def scan_watch_now(wid: int):
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    return await engine.scan_comment_watch(wid)


# ─────────── 评论数据 ───────────
def _comment_dict(c: CommentRecord) -> dict:
    return {
        "id": c.id, "watch_id": c.watch_id, "aweme_id": c.aweme_id,
        "comment_id": c.comment_id, "text": c.text, "user_nickname": c.user_nickname,
        "like_count": c.like_count, "create_time": c.create_time,
        "is_reply": bool(c.reply_to),
    }


@app.get("/api/comments")
async def list_comments(limit: int = 100, watch_id: int | None = None,
                        aweme_id: str | None = None, platform: str | None = None):
    with get_session() as s:
        q = select(CommentRecord)
        if platform is not None:
            q = q.where(CommentRecord.platform == platform)
        if watch_id is not None:
            q = q.where(CommentRecord.watch_id == watch_id)
        if aweme_id is not None:
            q = q.where(CommentRecord.aweme_id == aweme_id)
        rows = s.exec(q.order_by(CommentRecord.id.desc()).limit(limit)).all()
        return [_comment_dict(c) for c in rows]


@app.delete("/api/comments/{cmid}")
async def del_comment(cmid: int):
    with get_session() as s:
        c = s.get(CommentRecord, cmid)
        if c:
            s.delete(c); s.commit()
    return {"ok": True}


@app.post("/api/comments/batch-delete")
async def batch_del_comments(body: IdsIn):
    deleted = 0
    with get_session() as s:
        for cid in body.ids:
            c = s.get(CommentRecord, cid)
            if c:
                s.delete(c); deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted}


@app.delete("/api/comments")
async def clear_comments(watch_id: int | None = None):
    with get_session() as s:
        q = select(CommentRecord)
        if watch_id is not None:
            q = q.where(CommentRecord.watch_id == watch_id)
        rows = s.exec(q).all()
        for c in rows:
            s.delete(c)
        s.commit()
        return {"ok": True, "deleted": len(rows)}


# ─────────── 发布(创作平台)+ 跨平台转发 ───────────
UPLOAD_DIR = Path("./data/uploads")


@app.post("/api/publish/upload")
async def publish_upload(files: list[UploadFile] = File(...)):
    """上传图集/视频文件,返回本地路径列表(供创建发布任务用)。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix or ".bin"
        name = f"{_uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / name
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        saved.append({"path": str(dest), "name": f.filename})
    return {"files": saved}


class PublishIn(BaseModel):
    account_id: int
    media_type: str = "images"            # images | video
    title: str = ""
    desc: str = ""
    topics: str = ""
    location: str = ""                    # 视频号:位置 POI(可选)
    media_paths: list[str] = []
    visibility: str = "public"            # 抖音:public | friends | private
    allow_save: bool = True               # 抖音:是否允许他人保存
    scheduled_at: str | None = None       # ISO 时间(本地),空=尽快发


def _publish_dict(t: PublishTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id,
        "media_type": t.media_type, "title": t.title, "desc": t.desc,
        "topics": t.topics, "status": t.status, "result_url": t.result_url,
        "visibility": t.visibility, "allow_save": t.allow_save,
        "error": t.error, "media_count": len(json.loads(t.media_json or "[]")),
        "source_platform": t.source_platform, "source_content_id": t.source_content_id,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _parse_when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


@app.get("/api/publish")
async def list_publish(platform: str | None = None):
    with get_session() as s:
        q = select(PublishTask)
        if platform:
            q = q.where(PublishTask.platform == platform)
        rows = s.exec(q.order_by(PublishTask.id.desc())).all()
        return [_publish_dict(t) for t in rows]


@app.post("/api/publish")
async def add_publish(body: PublishIn):
    if body.media_type not in ("images", "video"):
        raise HTTPException(400, "media_type 须为 images 或 video")
    paths = [p for p in body.media_paths if Path(p).exists()]
    if not paths:
        raise HTTPException(400, "没有可用的媒体文件,请先上传")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc or acc.platform not in ("xhs", "kuaishou", "douyin", "shipinhao"):
            raise HTTPException(400, "请选择一个已登录的抖音 / 小红书 / 快手 / 视频号账号")
        pname = {"kuaishou": "快手", "douyin": "抖音",
                 "shipinhao": "视频号"}.get(acc.platform, "小红书")
        if acc.platform in ("kuaishou", "douyin", "shipinhao"):
            # 抖音 / 快手 / 视频号发布走浏览器自动化,登录态在该账号持久 profile 里
            if not (acc.creator_storage_state or acc.storage_state):
                raise HTTPException(400, f"该{pname}账号不可发布:请先在账号页完成登录")
        elif not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
            raise HTTPException(400, "该账号不可发布:请对该号完成「小红书扫码登录」或「创作者登录」")
        vis = body.visibility if body.visibility in ("public", "friends", "private") else "public"
        t = PublishTask(
            platform=acc.platform, account_id=body.account_id, media_type=body.media_type,
            title=body.title.strip()[:20], desc=body.desc, topics=body.topics,
            location=(body.location or "").strip()[:60],
            visibility=vis, allow_save=bool(body.allow_save),
            media_json=json.dumps(paths), scheduled_at=_parse_when(body.scheduled_at),
        )
        s.add(t); s.commit(); s.refresh(t)
        return _publish_dict(t)


@app.post("/api/publish/{tid}/run-now")
async def run_publish(tid: int):
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    return await engine.publish_task(tid)


@app.delete("/api/publish/{tid}")
async def del_publish(tid: int):
    with get_session() as s:
        t = s.get(PublishTask, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


def _first_val(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, []):
            return v
    return default


async def _xhs_account_uid(state: str, proxy: str = "") -> str:
    """拿到该账号自己的 user_id(self_info → 创作平台资料兜底)。"""
    from .platforms.xhs import XhsApiClient, cookie_str_from_state, has_a1, creator_profile
    cookie = cookie_str_from_state(state)
    if has_a1(cookie):
        try:
            client = XhsApiClient(cookie, cfg.engine.user_agent,
                                  timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
            me = await client.self_info()
            uid = str((me or {}).get("user_id") or "")
            if uid:
                return uid
        except Exception:
            pass
    prof = await creator_profile(state, proxy=proxy)
    return (prof or {}).get("sec_uid") or ""


def _imgs_of(n: dict) -> list:
    out = []
    for it in (n.get("images_list") or n.get("imageList") or []):
        if isinstance(it, dict):
            u = it.get("url") or it.get("url_default") or it.get("urlDefault") or ""
            if u:
                out.append(u)
    return out


@app.get("/api/publish/published")
async def list_published_notes(account_id: int):
    """拉取「已发布作品列表」。
    优先用「读取登录态」打开自己的 www 主页(token 对预览/评论有效);
    没有读取态时回退创作平台「笔记管理」(能显示,但视频预览/评论可能不可用)。"""
    if browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform != "xhs":
            raise HTTPException(400, "请选择一个已登录的小红书账号")
        read_state = acc.storage_state or ""
        creator_state = acc.creator_storage_state or ""
        proxy = acc.proxy or ""
        identity = browser.identity_for(acc)
        if not (read_state or creator_state):
            raise HTTPException(400, "该账号未登录,请先在账号页扫码登录")
    from .browser import fetch_xhs_notes, fetch_creator_published
    from .platforms.xhs import parse_note_brief

    out, good = [], False
    if read_state:
        uid = await _xhs_account_uid(read_state, proxy)
        if uid:
            items, _a, _e = await fetch_xhs_notes(browser, identity, uid, set())
            for raw in items[:80]:
                b = parse_note_brief(raw)
                if not b:
                    continue
                card = raw.get("note_card") or raw
                interact = card.get("interact_info") or {}
                out.append({
                    "note_id": b["note_id"], "title": b.get("title") or "(无标题)",
                    "type": b.get("type") or "normal", "cover": b.get("cover") or "",
                    "images": [], "like": interact.get("liked_count") or 0,
                    "time": card.get("time") or 0,
                    "xsec_token": b.get("xsec_token") or "", "xsec_source": "pc_feed",
                })
            good = bool(out)
    if not out:   # 回退:创作平台笔记管理(显示用)
        notes, err = await fetch_creator_published(browser, identity)
        if "logged_out" in (err or ""):
            raise HTTPException(400, "登录态已失效,请对该账号点「重新登录」")
        for n in notes[:80]:
            imgs = _imgs_of(n)
            vi = n.get("video_info") or {}
            cover = imgs[0] if imgs else (vi.get("cover") if isinstance(vi, dict) else "")
            out.append({
                "note_id": str(_first_val(n, "id", "noteId", "note_id")),
                "title": _first_val(n, "display_title", "title", "desc", default="(无标题)"),
                "type": _first_val(n, "type", "noteType", default="normal"),
                "cover": cover or "", "images": imgs,
                "like": _first_val(n, "likes", "likeCount", default=0),
                "time": _first_val(n, "time", "postTime", default=0),
                "xsec_token": _first_val(n, "xsec_token", default=""),
                "xsec_source": _first_val(n, "xsec_source", default="pc_note_detail"),
            })
    return {"notes": out, "total": len(out), "good_tokens": good}


@app.get("/api/publish/note-media")
async def publish_note_media(account_id: int, note_id: str,
                             xsec_token: str = "", xsec_source: str = "pc_note_detail"):
    """取一条小红书笔记的完整媒体(图集/视频),供「已发布作品」预览。"""
    from .platforms.xhs import XhsApiClient, XhsApiError, cookie_str_from_state, has_a1, parse_note_detail
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform != "xhs":
            raise HTTPException(400, "账号无效")
        state = acc.storage_state or acc.creator_storage_state or ""
        proxy = acc.proxy or ""
    cookie = cookie_str_from_state(state)
    if not has_a1(cookie):
        raise HTTPException(400, "登录态缺少 a1")
    client = XhsApiClient(cookie, cfg.engine.user_agent,
                          timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
    try:
        card = await client.note_detail(note_id, xsec_token=xsec_token, xsec_source=xsec_source)
    except XhsApiError as e:
        raise HTTPException(400, f"取笔记失败:{e}")
    aw = parse_note_detail(card or {}, {"note_id": note_id})
    if not aw or not aw.medias:
        raise HTTPException(400, "拿不到该笔记的媒体(xsec_token 对 feed 接口无效)")
    return {
        "media_type": aw.media_type, "desc": aw.desc, "cover_url": aw.cover or "",
        "medias": [{"url": m.url, "kind": m.kind, "ext": m.ext, "index": m.index}
                   for m in aw.medias],
    }


@app.get("/api/publish/note-comments")
async def publish_note_comments(account_id: int, note_id: str,
                                xsec_token: str = "", xsec_source: str = "pc_note_detail"):
    """拉取一条小红书笔记的评论(一级 + 子评论拍平)。"""
    from .platforms.xhs import (XhsApiClient, XhsApiError, cookie_str_from_state, has_a1,
                      parse_comment as parse_xhs_comment, flatten_comments)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform != "xhs":
            raise HTTPException(400, "账号无效")
        state = acc.storage_state or acc.creator_storage_state or ""
        proxy = acc.proxy or ""
    cookie = cookie_str_from_state(state)
    if not has_a1(cookie):
        raise HTTPException(400, "登录态缺少 a1")
    client = XhsApiClient(cookie, cfg.engine.user_agent,
                          timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
    # 评论接口要 pc_feed 令牌;先调 feed 拿一个新鲜令牌(feed 接受 pc_creatormng 令牌)
    tok, src = xsec_token, xsec_source
    try:
        item = await client.note_detail_raw(note_id, xsec_token=xsec_token, xsec_source=xsec_source)
        fresh = item.get("xsec_token") or ((item.get("note_card") or {}).get("xsec_token"))
        if fresh:
            tok, src = fresh, "pc_feed"
    except Exception:
        pass
    try:
        d = await client.note_comments(note_id, xsec_token=tok, xsec_source=src)
    except XhsApiError as e:
        raise HTTPException(400, f"取评论失败:{e}")
    raw = d.get("comments") or []
    fresh = [c for c in (parse_xhs_comment(rc) for rc in flatten_comments(raw)) if c]
    fresh.sort(key=lambda c: c.get("create_time") or 0, reverse=True)
    return {"comments": fresh, "total": len(fresh), "has_more": bool(d.get("has_more"))}


class RepostIn(BaseModel):
    account_id: int
    scheduled_at: str | None = None
    # 转发前可编辑的笔记信息;为 None 时沿用作品原始内容
    title: str | None = None
    desc: str | None = None
    topics: str | None = None
    visibility: str = "public"           # 抖音:public | friends | private
    allow_save: bool = True              # 抖音:是否允许他人保存
    media_order: list[int] | None = None  # 剔除/调序后保留的图片原始序号(按新顺序);None=全部原序


async def _repost_content(cid: int, body: RepostIn, target_platform: str):
    """把一条已下载的作品转成目标平台(xhs / douyin)的发布任务。跨平台转发共用。"""
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    # 1) 只在会话内做校验,取出需要的值后退出会话,不把 ORM 对象带出去
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "作品不存在")
        if rec.download_status != "done":
            raise HTTPException(400, "该作品尚未下载完成,无法转发")
        acc = s.get(DouyinAccount, body.account_id)
        if not acc or acc.platform != target_platform:
            pname = "抖音" if target_platform == "douyin" else "小红书"
            raise HTTPException(400, f"请选择一个已登录的{pname}账号")
        if target_platform == "douyin":
            # 抖音发布走浏览器自动化,有任一登录态即可(需创作者/登录态)
            if not (acc.creator_storage_state or acc.storage_state):
                raise HTTPException(400, "该抖音账号不可发布:请先在账号页完成「创作者登录」")
        elif not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
            raise HTTPException(400, "该账号不可发布:请对该号完成「小红书扫码登录」或「创作者登录」")
    # 2) 退出会话后再创建发布任务(create_relay_publish 内部自开会话)
    #    若前端传了编辑后的标题/正文/话题,则用编辑值覆盖作品原始内容
    vis = body.visibility if body.visibility in ("public", "friends", "private") else "public"
    tid = engine.create_relay_publish(
        cid, body.account_id, target_platform=target_platform,
        title=body.title, desc=body.desc, topics=body.topics,
        visibility=vis, allow_save=bool(body.allow_save),
        media_order=body.media_order)
    if not tid:
        raise HTTPException(400, "未找到该作品的本地文件,无法转发")
    # 3) 定时时间另开一个会话更新
    if body.scheduled_at:
        with get_session() as s:
            t = s.get(PublishTask, tid)
            if t:
                t.scheduled_at = _parse_when(body.scheduled_at)
                s.add(t); s.commit()
    return {"ok": True, "task_id": tid}


@app.post("/api/contents/{cid}/repost-xhs")
async def repost_to_xhs(cid: int, body: RepostIn):
    """把一条已下载的抖音作品转成小红书发布任务。"""
    return await _repost_content(cid, body, "xhs")


@app.post("/api/contents/{cid}/repost-douyin")
async def repost_to_douyin(cid: int, body: RepostIn):
    """把一条已下载的小红书作品转成抖音发布任务(反向转发)。"""
    return await _repost_content(cid, body, "douyin")


# ─────────── 自动评论(规则 + 任务)───────────
class CommentRuleIn(BaseModel):
    platform: str = "douyin"
    name: str = ""
    mode: str = "auto_reply"            # auto_reply | auto_comment
    account_id: int
    target_kind: str = "self"          # reply: self|work ; comment: keyword|creator
    target: str = ""                   # 关键词,或 创作者/作品 的链接/id
    templates: list[str] = []
    use_ai: bool = False
    require_review: bool = False
    reply_filter: str = ""
    skip_keywords: str = ""
    daily_cap: int = 20
    min_gap_seconds: int = 90
    max_per_run: int = 5
    interval_seconds: int = 1800
    enabled: bool = False


class CommentRuleUpdate(BaseModel):
    name: str | None = None
    templates: list[str] | None = None
    use_ai: bool | None = None
    require_review: bool | None = None
    reply_filter: str | None = None
    skip_keywords: str | None = None
    daily_cap: int | None = None
    min_gap_seconds: int | None = None
    max_per_run: int | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    # 改目标(任一非空则重新解析)。account_id 可单独改。
    account_id: int | None = None
    mode: str | None = None
    target_kind: str | None = None
    target: str | None = None


def _rule_dict(r: CommentRule) -> dict:
    return {
        "id": r.id, "platform": r.platform, "name": r.name, "mode": r.mode,
        "account_id": r.account_id, "target_kind": r.target_kind,
        "keyword": r.keyword, "sec_uid": r.sec_uid, "aweme_id": r.aweme_id,
        "templates": json.loads(r.templates or "[]"), "use_ai": r.use_ai,
        "require_review": r.require_review,
        "reply_filter": r.reply_filter, "skip_keywords": r.skip_keywords,
        "daily_cap": r.daily_cap, "min_gap_seconds": r.min_gap_seconds,
        "max_per_run": r.max_per_run, "interval_seconds": r.interval_seconds,
        "enabled": r.enabled, "last_error": r.last_error,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
    }


async def _resolve_rule_target(platform: str, mode: str, target_kind: str, target: str):
    """把 mode/target_kind/target 解析成 (kind, sec_uid, aweme_id, keyword, xsec_token)。
    解析失败抛 HTTPException。POST 与 PUT(改目标)共用。"""
    sec_uid = aweme_id = keyword = xsec_token = ""
    if mode == "auto_comment":
        kind = target_kind if target_kind in ("keyword", "creator") else "keyword"
        if kind == "keyword":
            keyword = (target or "").strip()
            if not keyword:
                raise HTTPException(400, "请填写要评论的搜索关键词")
            if platform in ("douyin", "kuaishou"):
                pn = "快手" if platform == "kuaishou" else "抖音"
                raise HTTPException(400, f"{pn}暂不支持关键词发现,请用「创作者」模式")
        else:
            if platform == "xhs":
                ref = await xhs_resolve_user(target, cfg.engine.user_agent)
                if not ref:
                    raise HTTPException(400, "无法解析小红书创作者(主页链接 / xhslink / user_id)")
                sec_uid, xsec_token = ref.user_id, ref.xsec_token
            elif platform == "kuaishou":
                sec_uid = await resolve_ks_user_id(target, cfg.engine.user_agent)
                if not sec_uid:
                    raise HTTPException(400, "无法解析快手创作者(主页链接 / 短链 / user_id)")
            else:
                sec_uid = await resolve_sec_uid(target, cfg.engine.user_agent)
                if not sec_uid:
                    raise HTTPException(400, "无法解析 sec_uid(主页链接 / 短链 / sec_uid)")
    else:
        kind = target_kind if target_kind in ("self", "work") else "self"
        if kind == "work":
            if platform == "xhs":
                ref = await xhs_resolve_note(target, cfg.engine.user_agent)
                if not ref:
                    raise HTTPException(400, "无法解析小红书笔记(explore 链接 / xhslink / note_id)")
                aweme_id, xsec_token = ref.note_id, ref.xsec_token
            elif platform == "kuaishou":
                aweme_id = await resolve_ks_photo_id(target, cfg.engine.user_agent)
                if not aweme_id:
                    raise HTTPException(400, "无法解析快手作品 id(作品链接 / 短链 / photo_id)")
            else:
                aweme_id = await resolve_aweme_id(target, cfg.engine.user_agent)
                if not aweme_id:
                    raise HTTPException(400, "无法解析作品 id(作品链接 / 短链 / 数字 id)")
    return kind, sec_uid, aweme_id, keyword, xsec_token


def _task_dict(t: CommentTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "rule_id": t.rule_id,
        "account_id": t.account_id, "aweme_id": t.aweme_id,
        "target_comment_id": t.target_comment_id, "target_nick": t.target_nick,
        "content": t.content, "status": t.status, "result": t.result,
        "error": t.error, "method": t.method,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "done_at": t.done_at.isoformat() if t.done_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.get("/api/comment-rules")
async def list_comment_rules(platform: str | None = None):
    with get_session() as s:
        q = select(CommentRule)
        if platform:
            q = q.where(CommentRule.platform == platform)
        return [_rule_dict(r) for r in s.exec(q.order_by(CommentRule.id.desc())).all()]


@app.post("/api/comment-rules")
async def add_comment_rule(body: CommentRuleIn):
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou") else "douyin"
    mode = body.mode if body.mode in ("auto_reply", "auto_comment") else "auto_reply"
    templates = [t.strip() for t in body.templates if t.strip()]
    if not templates:
        raise HTTPException(400, "请至少配置一条文案模板(AI 生成失败时回退用)")
    _pn = {"xhs": "小红书", "kuaishou": "快手"}.get(platform, "抖音")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc or acc.platform != platform:
            raise HTTPException(400, f"请选择一个已登录的{_pn}账号")
        if not (acc.storage_state or acc.creator_storage_state):
            raise HTTPException(400, "该账号未登录,发评论需要登录态")

    kind, sec_uid, aweme_id, keyword, xsec_token = await _resolve_rule_target(
        platform, mode, body.target_kind, body.target)

    with get_session() as s:
        r = CommentRule(
            platform=platform, name=body.name or ("自动回复" if mode == "auto_reply" else "自动评论"),
            mode=mode, account_id=body.account_id, target_kind=kind,
            keyword=keyword, sec_uid=sec_uid, aweme_id=aweme_id, xsec_token=xsec_token,
            templates=json.dumps(templates, ensure_ascii=False), use_ai=body.use_ai,
            require_review=body.require_review,
            reply_filter=body.reply_filter.strip(), skip_keywords=body.skip_keywords.strip(),
            daily_cap=max(0, body.daily_cap), min_gap_seconds=max(1, body.min_gap_seconds),
            max_per_run=max(1, body.max_per_run),
            interval_seconds=max(60, body.interval_seconds), enabled=body.enabled)
        s.add(r); s.commit(); s.refresh(r)
        return _rule_dict(r)


@app.put("/api/comment-rules/{rid}")
async def update_comment_rule(rid: int, body: CommentRuleUpdate):
    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        platform = r.platform

    # 改账号:校验平台一致 + 已登录
    if body.account_id is not None:
        with get_session() as s:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != platform:
                raise HTTPException(400, "账号无效或与规则平台不一致")
            if not (acc.storage_state or acc.creator_storage_state):
                raise HTTPException(400, "该账号未登录,发评论需要登录态")

    # 改目标:mode/target_kind/target 任一传入则整体重解析
    new_target = None
    if body.mode is not None or body.target_kind is not None or body.target is not None:
        with get_session() as s:
            r = s.get(CommentRule, rid)
            mode = body.mode if body.mode in ("auto_reply", "auto_comment") else r.mode
            tk = body.target_kind if body.target_kind is not None else r.target_kind
            tgt = body.target if body.target is not None else ""
        new_target = (mode, *await _resolve_rule_target(platform, mode, tk, tgt))

    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        if body.account_id is not None:
            r.account_id = body.account_id
        if new_target is not None:
            r.mode, r.target_kind, r.sec_uid, r.aweme_id, r.keyword, r.xsec_token = new_target
        if body.name is not None:
            r.name = body.name
        if body.templates is not None:
            tps = [t.strip() for t in body.templates if t.strip()]
            if not tps:
                raise HTTPException(400, "文案模板不能为空")
            r.templates = json.dumps(tps, ensure_ascii=False)
        if body.use_ai is not None:
            r.use_ai = body.use_ai
        if body.require_review is not None:
            r.require_review = body.require_review
        if body.reply_filter is not None:
            r.reply_filter = body.reply_filter.strip()
        if body.skip_keywords is not None:
            r.skip_keywords = body.skip_keywords.strip()
        if body.daily_cap is not None:
            r.daily_cap = max(0, body.daily_cap)
        if body.min_gap_seconds is not None:
            r.min_gap_seconds = max(1, body.min_gap_seconds)
        if body.max_per_run is not None:
            r.max_per_run = max(1, body.max_per_run)
        if body.interval_seconds is not None:
            r.interval_seconds = max(60, body.interval_seconds)
        if body.enabled is not None:
            r.enabled = body.enabled
        s.add(r); s.commit(); s.refresh(r)
        return _rule_dict(r)


@app.delete("/api/comment-rules/{rid}")
async def del_comment_rule(rid: int, with_tasks: bool = True):
    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            return {"ok": True}
        if with_tasks:
            for t in s.exec(select(CommentTask).where(CommentTask.rule_id == rid)).all():
                s.delete(t)
        s.delete(r); s.commit()
    return {"ok": True}


@app.post("/api/comment-rules/{rid}/run-now")
async def run_comment_rule_now(rid: int):
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    return await engine.run_comment_rule(rid)


@app.get("/api/comment-tasks")
async def list_comment_tasks(platform: str | None = None, rule_id: int | None = None,
                             status: str | None = None, limit: int = 200):
    with get_session() as s:
        q = select(CommentTask)
        if platform:
            q = q.where(CommentTask.platform == platform)
        if rule_id is not None:
            q = q.where(CommentTask.rule_id == rule_id)
        if status:
            q = q.where(CommentTask.status == status)
        rows = s.exec(q.order_by(CommentTask.id.desc()).limit(limit)).all()
        return [_task_dict(t) for t in rows]


@app.post("/api/comment-tasks/{tid}/run-now")
async def run_comment_task_now(tid: int):
    if not engine:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status in ("done", "doing"):
            raise HTTPException(400, f"任务状态为 {t.status}")
        t.status = "pending"; t.scheduled_at = None; t.error = ""
        s.add(t); s.commit()
    return await engine.execute_comment_task(tid)


@app.post("/api/comment-tasks/{tid}/cancel")
async def cancel_comment_task(tid: int):
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status in ("draft", "pending", "failed"):
            t.status = "canceled"
            s.add(t); s.commit()
    return {"ok": True}


class IdsIn2(BaseModel):
    ids: list[int] = []


class TaskContentIn(BaseModel):
    content: str


@app.put("/api/comment-tasks/{tid}")
async def edit_comment_task(tid: int, body: TaskContentIn):
    """编辑草稿/待发任务的文案(草稿审核时人工微调用)。"""
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "文案不能为空")
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status not in ("draft", "pending", "failed"):
            raise HTTPException(400, f"任务状态为 {t.status},不可编辑")
        t.content = content[:200]
        s.add(t); s.commit(); s.refresh(t)
        return _task_dict(t)


def _approve_one(s, t) -> bool:
    """把 draft 任务转为 pending(通过审核)。返回是否改动。"""
    if t and t.status == "draft":
        t.status = "pending"; t.error = ""; t.scheduled_at = None
        s.add(t)
        return True
    return False


@app.post("/api/comment-tasks/{tid}/approve")
async def approve_comment_task(tid: int):
    """通过单条草稿:draft -> pending,引擎随后按节流自动发出。"""
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if not _approve_one(s, t):
            raise HTTPException(400, f"任务状态为 {t.status},非草稿")
        s.commit()
    return {"ok": True}


@app.post("/api/comment-tasks/batch-approve")
async def batch_approve_comment_tasks(body: IdsIn2):
    """批量通过草稿。ids 为空时通过该平台所有草稿(由前端传 platform 过滤的 ids 更精确)。"""
    n = 0
    with get_session() as s:
        if body.ids:
            for tid in body.ids:
                if _approve_one(s, s.get(CommentTask, tid)):
                    n += 1
        else:
            for t in s.exec(select(CommentTask).where(CommentTask.status == "draft")).all():
                if _approve_one(s, t):
                    n += 1
        s.commit()
    return {"ok": True, "approved": n}


@app.delete("/api/comment-tasks/{tid}")
async def del_comment_task(tid: int):
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


@app.post("/api/comment-tasks/batch-delete")
async def batch_del_comment_tasks(body: IdsIn2):
    n = 0
    with get_session() as s:
        for tid in body.ids:
            t = s.get(CommentTask, tid)
            if t:
                s.delete(t); n += 1
        s.commit()
    return {"ok": True, "deleted": n}


# ─────────── 通知渠道 ───────────
class ChannelIn(BaseModel):
    name: str = ""
    type: str
    config: Dict[str, Any] = {}
    enabled: bool = True


class ChannelUpdate(BaseModel):
    name: str | None = None
    config: Dict[str, Any] | None = None
    enabled: bool | None = None


def _channel_dict(c: NotificationChannel) -> dict:
    try:
        cfg = json.loads(c.config or "{}")
    except Exception:
        cfg = {}
    return {"id": c.id, "name": c.name, "type": c.type,
            "enabled": c.enabled, "config": cfg}


@app.get("/api/notifications")
async def list_channels():
    with get_session() as s:
        return [_channel_dict(c) for c in s.exec(select(NotificationChannel)).all()]


@app.post("/api/notifications")
async def add_channel(body: ChannelIn):
    if body.type not in CHANNEL_TYPES:
        raise HTTPException(400, f"渠道类型须为 {CHANNEL_TYPES}")
    with get_session() as s:
        c = NotificationChannel(name=body.name or body.type, type=body.type,
                                config=json.dumps(body.config), enabled=body.enabled)
        s.add(c); s.commit(); s.refresh(c)
        return _channel_dict(c)


@app.put("/api/notifications/{cid}")
async def update_channel(cid: int, body: ChannelUpdate):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if not c:
            raise HTTPException(404)
        if body.name is not None:
            c.name = body.name
        if body.config is not None:
            c.config = json.dumps(body.config)
        if body.enabled is not None:
            c.enabled = body.enabled
        s.add(c); s.commit(); s.refresh(c)
        return _channel_dict(c)


@app.delete("/api/notifications/{cid}")
async def del_channel(cid: int):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if c:
            s.delete(c); s.commit()
    return {"ok": True}


@app.post("/api/notifications/{cid}/test")
async def test_channel(cid: int):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if not c:
            raise HTTPException(404)
        ch_type, cfg = c.type, json.loads(c.config or "{}")
    ok, detail = await send_one(ch_type, cfg, "CreatorHub · 测试通知",
                                "这是一条测试消息,收到说明渠道配置正常 ✓")
    return {"ok": ok, "detail": detail}


# ─────────── 前端 ───────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # 给 app.js 带上基于 mtime 的版本号,前端改动后自动击穿浏览器缓存(免手动强刷)
    try:
        ver = int((WEB_DIR / "app.js").stat().st_mtime)
        html = html.replace("/static/app.js", f"/static/app.js?v={ver}")
    except Exception:
        pass
    # 首页(含内联 CSS)禁缓存:否则 webview 缓存旧 HTML,改了样式也不生效
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}
