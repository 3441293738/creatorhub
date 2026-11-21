"""跨平台账号互动动作（关注、私信、点赞、收藏等）配额检查与执行。"""
from __future__ import annotations

from datetime import datetime
import json

from sqlmodel import select

from app.account_lifecycle import AccountUnavailableError
from app.browser import do_follow, do_twitter_follow, douyin_secure_post_api, send_dm, send_dm_api, send_twitter_dm
from app.browser.tiktok_actions import do_tiktok_follow, send_tiktok_dm
from app.db import get_session
from app.engine.monitor_helpers import _loads, log
from app.models import AccountActionTask, DmConversation, DouyinAccount, FollowEdge
from app.platforms.bilibili import BilibiliClient
from app.platforms.douyin import DouyinClient, cookie_from_state as dy_cookie_from_state
from app.platforms.instagram import InstagramClient
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.transport_matrix import douyin_client_environment


class ActionJobMixin:
    # ── 本账号写操作队列(取关/回关/发私信)──
    def _action_gap_ok(self, account_id, gap: int) -> bool:
        """距该账号上一次成功写操作是否已超过最小间隔(防同账号连发)。
        实际间隔取「任务级 min_gap」与「全局 action_min_gap_seconds」的较大者。"""
        if not account_id:
            return True
        gap = max(gap, self.cfg.engine.action_min_gap_seconds)
        if gap <= 0:
            return True
        with get_session() as s:
            rows = s.exec(select(AccountActionTask.done_at)
                          .where(AccountActionTask.account_id == account_id)
                          .where(AccountActionTask.status == "done")).all()
        last = max([d for d in rows if d] or [None])
        return last is None or (datetime.utcnow() - last).total_seconds() >= gap

    def _action_count_since(self, account_id, since: datetime) -> int:
        """该账号自 since 起已成功执行的写操作数(用于每日/每小时上限)。"""
        if not account_id:
            return 0
        with get_session() as s:
            return len(s.exec(select(AccountActionTask.id)
                              .where(AccountActionTask.account_id == account_id)
                              .where(AccountActionTask.status == "done")
                              .where(AccountActionTask.done_at >= since)).all())

    def _action_cap_ok(self, account_id) -> bool:
        """写操作是否还在每日 / 每小时配额内(关注取关是封号重灾区,双重限流)。"""
        dcap = self.cfg.engine.action_daily_cap_per_account
        hcap = self.cfg.engine.action_hourly_cap_per_account
        if dcap > 0 and self._action_count_since(
                account_id, self._today_start(account_id)) >= dcap:
            return False
        if hcap > 0 and self._action_count_since(account_id, self._hour_ago()) >= hcap:
            return False
        return True

    def _action_gate_error(self, account_id, gap: int, action: str = "follow") -> str:
        """Apply the same write gate to queued and API-triggered actions."""
        pause_error = self._write_pause_error(account_id)
        if pause_error:
            return pause_error
        if not self._in_active_window(account_id):
            return "当前处于非活跃时段，写操作已保留在队列"
        if not self._action_cap_ok(account_id):
            return "已达到账号写操作额度"
        if not self._action_gap_ok(account_id, gap):
            return "尚未达到账号写操作最小间隔"
        kind = OperationKind.DM if action == "send_dm" else OperationKind.SOCIAL
        decision = self.risk.preflight(account_id, kind)
        if not decision.allowed:
            return decision.reason
        return ""

    async def _process_action_tasks(self):
        now = datetime.utcnow()
        due = []
        with get_session() as s:
            tasks = s.exec(select(AccountActionTask).where(
                AccountActionTask.status == "pending")).all()
            for t in tasks:
                if self._task_due(t, now):
                    due.append((t.id, t.account_id, t.min_gap_seconds))
        seen_acct = set()
        for tid, aid, gap in due:
            # 同账号每轮最多执行一条,尊重最小间隔 + 每日/每小时配额(其余下轮再发)
            if aid in seen_acct or not self._action_gap_ok(aid, gap):
                continue
            if not self._action_cap_ok(aid):
                continue
            seen_acct.add(aid)
            try:
                await self.execute_action_task(tid)
            except Exception as e:
                log.warning("写操作任务 %s 执行异常: %s", tid, e)

    async def execute_action_task(self, task_id: int) -> dict:
        if task_id in self._actioning:
            return {"ok": False, "error": "正在执行中"}
        self._actioning.add(task_id)
        try:
            with get_session() as s:
                t = s.get(AccountActionTask, task_id)
                account_id = t.account_id if t else None
                kind = (OperationKind.DM if t and t.action == "send_dm"
                        else OperationKind.SOCIAL)
            async with self._operation_guard(
                    account_id, kind, fallback_key=f"act:{task_id}"):
                return await self._execute_action_task_locked(task_id)
        except AccountUnavailableError:
            return self._fail_missing_account_task(AccountActionTask, task_id)
        finally:
            self._actioning.discard(task_id)

    async def _execute_action_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(AccountActionTask, task_id)
            if not t or t.status != "pending":
                return {"ok": False, "error": "任务不可执行"}
            deferred = self._task_deferral(t)
            if deferred:
                return deferred
            account_id = t.account_id
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            if not acc:
                t.status = "failed"; t.error = "绑定账号不存在(可能已删除/重登成新号)"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            platform = t.platform
            dy_write_mode = (self._douyin_write_mode()
                             if platform == "douyin" else "browser")
            bili_write_mode = (
                str(getattr(self.cfg.engine, "bilibili_write_mode", "hybrid") or "hybrid").lower()
                if platform == "bilibili" else "browser"
            )
            ig_write_mode = (
                str(getattr(self.cfg.engine, "instagram_write_mode", "hybrid") or "hybrid").lower()
                if platform == "instagram" else "browser"
            )
            api_only = (
                (platform == "douyin" and dy_write_mode == "api")
                or (platform == "bilibili" and bili_write_mode in {"api", "hybrid"})
                or (platform == "instagram" and ig_write_mode == "api")
            )
            environment_error = (
                ""
                if (api_only or (platform == "instagram" and ig_write_mode == "hybrid"))
                else self._native_write_environment_error(acc, headed=True, browser_mode=True)
            )
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            gate_error = self._action_gate_error(
                t.account_id, t.min_gap_seconds, t.action)
            if gate_error:
                kind = (OperationKind.DM if t.action == "send_dm"
                        else OperationKind.SOCIAL)
                decision = self.risk.preflight(t.account_id, kind)
                self._defer_row(t, gate_error, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": gate_error}
            action = t.action
            target_uid, target_sec_uid, content = t.target_uid, t.target_sec_uid, t.content
            state = acc.storage_state or acc.creator_storage_state or ""
            ua = acc.ua or self.cfg.engine.user_agent
            proxy = acc.proxy or ""
            direct_environment = douyin_client_environment(acc)
            # 先复用本地会话；没有会话时，API 分支会解析双方数字 uid 后建会。
            dm_conv_id, dm_short_id, dm_ticket = t.conv_id, "", ""
            dm_self_uid, dm_self_sec_uid = acc.uid or "", acc.sec_uid or ""
            dm_target_nick = t.target_nick or ""
            if action == "send_dm" and platform == "douyin":
                _convs = s.exec(select(DmConversation).where(
                    DmConversation.account_id == t.account_id,
                    DmConversation.platform == "douyin")).all()
                _conv = next((c for c in _convs if t.conv_id
                              and c.conv_id == t.conv_id), None)
                if _conv is None and target_uid:
                    _conv = next((c for c in _convs
                                  if c.peer_uid == target_uid), None)
                if _conv is None and target_sec_uid:
                    _conv = next((c for c in _convs
                                  if c.peer_sec_uid == target_sec_uid), None)
                if _conv:
                    dm_conv_id = _conv.conv_id
                    dm_short_id, dm_ticket = _conv.conv_short_id, _conv.ticket
                    target_uid = target_uid or _conv.peer_uid
                    target_sec_uid = target_sec_uid or _conv.peer_sec_uid
                    dm_target_nick = dm_target_nick or _conv.peer_nickname
            # commit 会 expire 本 session 内的实例,先把所需原语取出来再 commit
            native_mode = acc.identity_mode == "native"
            # 纯 API 模式不需要构造浏览器身份；hybrid/browser 只有在
            # 页面写入或明确拒绝后的回退路径才建立账号浏览器上下文。
            identity = None if api_only else self.browser.identity_for(acc)
            t.status = "doing"; t.method = ("api" if api_only else "browser"); t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()

        async def _browser_action() -> tuple[bool, str, str]:
            """Original page route; kept as the only hybrid fallback."""
            if action == "follow":
                if platform == "tiktok":
                    ok, err = await do_tiktok_follow(
                        self.browser, identity, target_sec_uid or target_uid,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id))
                elif platform == "twitter":
                    ok, err = await do_twitter_follow(
                        self.browser, identity, target_sec_uid or target_uid,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id))
                else:
                    ok, err = await do_follow(self.browser, identity, platform,
                                              target_uid, target_sec_uid)
                return ok, err, "browser"
            if action == "unfollow":
                if platform == "tiktok":
                    ok, err = await do_tiktok_follow(
                        self.browser, identity, target_sec_uid or target_uid,
                        unfollow=True,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id))
                elif platform == "twitter":
                    ok, err = await do_twitter_follow(
                        self.browser, identity, target_sec_uid or target_uid,
                        unfollow=True,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id))
                else:
                    ok, err = await do_follow(self.browser, identity, platform,
                                              target_uid, target_sec_uid, unfollow=True)
                return ok, err, "browser"
            if action == "send_dm":
                # Preserve the pre-existing browser-context imapi route when
                # browser mode is selected, then fall back to the visible page.
                if (platform == "douyin" and not native_mode
                        and dm_conv_id and dm_short_id and dm_ticket):
                    ok, err = await send_dm_api(
                        self.browser, identity, dm_conv_id, dm_short_id,
                        dm_ticket, content)
                    if ok:
                        return True, "", "api_browser_context"
                    category, _signal = classify_platform_error(err)
                    if category != RiskCategory.BUSINESS:
                        return False, err, "api_browser_context"
                if platform == "tiktok":
                    ok, err = await send_tiktok_dm(
                        self.browser, identity, target_sec_uid or target_uid, content,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id))
                elif platform == "twitter":
                    ok, err = await send_twitter_dm(
                        self.browser, identity, target_sec_uid or target_uid, content,
                        conv_id=dm_conv_id,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id))
                elif platform == "xhs":
                    ok, err = await send_dm(
                        self.browser, identity, platform,
                        target_uid, target_sec_uid, content,
                        on_submit=lambda: self._mark_write_submit(
                            AccountActionTask, task_id),
                    )
                else:
                    ok, err = await send_dm(
                        self.browser, identity, platform,
                        target_uid, target_sec_uid, content)
                return ok, err, "browser"
            return False, f"未知动作 {action}", "browser"

        def _store_dm_conversation(row: dict, peer_uid: str,
                                   peer_sec_uid: str, peer_nickname: str,
                                   self_uid: str) -> None:
            """建会成功后先落库；后续发送结果不影响会话标识的保存。"""
            conv_id = str(row.get("conv_id") or "")
            if not conv_id:
                return
            now = datetime.utcnow()
            with get_session() as session:
                conv = session.exec(select(DmConversation).where(
                    DmConversation.account_id == account_id,
                    DmConversation.conv_id == conv_id)).first()
                if conv is None:
                    conv = DmConversation(
                        platform="douyin", account_id=account_id,
                        conv_id=conv_id)
                conv.peer_uid = str(peer_uid or conv.peer_uid or "")
                conv.peer_sec_uid = str(peer_sec_uid or conv.peer_sec_uid or "")
                conv.peer_nickname = str(
                    peer_nickname or conv.peer_nickname or "")
                conv.conv_short_id = str(row.get("conv_short_id") or "")
                conv.ticket = str(row.get("ticket") or "")
                if row.get("last_text") is not None:
                    conv.last_text = str(row.get("last_text") or "")
                if row.get("last_time"):
                    conv.last_time = int(row["last_time"])
                raw = _loads(conv.raw_json)
                raw["self_uid"] = str(self_uid or raw.get("self_uid") or "")
                raw["conversation_type"] = int(row.get("conv_type") or 1)
                conv.raw_json = json.dumps(raw, ensure_ascii=False)
                conv.fetched_at = now
                session.add(conv)
                task = session.get(AccountActionTask, task_id)
                if task:
                    task.conv_id = conv_id
                    task.target_uid = str(peer_uid or task.target_uid or "")
                    task.target_sec_uid = str(
                        peer_sec_uid or task.target_sec_uid or "")
                    task.target_nick = str(
                        peer_nickname or task.target_nick or "")
                    session.add(task)
                account = session.get(DouyinAccount, account_id)
                if account and self_uid and not account.uid:
                    account.uid = str(self_uid)
                    session.add(account)
                session.commit()

        def _conversation_match(rows: list[dict], *, conv_id: str,
                                peer_uid: str, peer_sec_uid: str):
            if conv_id:
                found = next((row for row in rows
                              if str(row.get("conv_id") or "") == conv_id), None)
                if found:
                    return found
            if peer_uid:
                found = next((row for row in rows
                              if str(row.get("peer_uid") or "") == peer_uid), None)
                if found:
                    return found
            if peer_sec_uid:
                return next((row for row in rows
                             if str(row.get("peer_sec_uid") or "") == peer_sec_uid), None)
            return None

        async def _send_new_or_existing_dm(client) -> tuple[bool, str]:
            nonlocal target_uid, target_sec_uid, dm_self_uid, dm_self_sec_uid
            nonlocal dm_conv_id, dm_short_id, dm_ticket, dm_target_nick

            if dm_conv_id and dm_short_id and dm_ticket:
                _store_dm_conversation({
                    "conv_id": dm_conv_id,
                    "conv_short_id": dm_short_id,
                    "ticket": dm_ticket,
                    "conv_type": 1,
                }, target_uid, target_sec_uid, dm_target_nick, dm_self_uid)
                return await client.send_dm(
                    dm_conv_id, dm_short_id, dm_ticket, content)

            if dm_conv_id:
                # An existing conversation does not need either participant UID.
                # Recover missing send credentials from the current snapshot first.
                rows = await client.fetch_dm_conversations()
                conversation = _conversation_match(
                    rows, conv_id=dm_conv_id, peer_uid=target_uid,
                    peer_sec_uid=target_sec_uid)
                if conversation is None:
                    return False, (client.last_error
                                   or "已有会话缺 short_id/ticket，同步后仍未找到")
                dm_conv_id = str(conversation.get("conv_id") or "")
                dm_short_id = str(conversation.get("conv_short_id") or "")
                dm_ticket = str(conversation.get("ticket") or "")
                target_uid = str(conversation.get("peer_uid") or target_uid)
                target_sec_uid = str(
                    conversation.get("peer_sec_uid") or target_sec_uid)
                _store_dm_conversation(
                    conversation, target_uid, target_sec_uid,
                    str(conversation.get("peer_nickname") or dm_target_nick),
                    dm_self_uid)
                return await client.send_dm(
                    dm_conv_id, dm_short_id, dm_ticket, content)

            if target_uid and not target_sec_uid:
                profile, resolve_error = await client.resolve_user_identifier(
                    target_uid)
                if profile is None:
                    return False, f"target_resolution_failed:{resolve_error}"
                target_uid = str(profile.get("uid") or "")
                target_sec_uid = str(profile.get("sec_uid") or "")
                dm_target_nick = str(profile.get("nickname")
                                     or dm_target_nick)
            if not target_uid.isdigit() and target_sec_uid:
                profile = await client.fetch_profile(target_sec_uid)
                target_uid = str((profile or {}).get("uid")
                                 or (profile or {}).get("user_id") or "")
                dm_target_nick = str((profile or {}).get("nickname")
                                     or dm_target_nick)
            if not target_uid.isdigit():
                return False, "缺少目标数字 uid，且无法由 sec_uid 解析"

            if not dm_self_uid.isdigit():
                profile = (await client.fetch_profile(dm_self_sec_uid)
                           if dm_self_sec_uid else await client.fetch_self_profile())
                dm_self_uid = str((profile or {}).get("uid")
                                  or (profile or {}).get("user_id") or "")
                dm_self_sec_uid = str((profile or {}).get("sec_uid")
                                      or dm_self_sec_uid)
            if not dm_self_uid.isdigit():
                return False, "缺少当前账号数字 uid，且无法从账号资料解析"

            conversation, create_error = await client.create_dm_conversation(
                target_uid, dm_self_uid, target_sec_uid=target_sec_uid)
            if conversation is None and str(create_error).startswith(
                    "write_uncertain:"):
                # 建会请求可能已落库：先同步确认，绝不直接重放 cmd=609。
                rows = await client.fetch_dm_conversations()
                conversation = _conversation_match(
                    rows, conv_id="", peer_uid=target_uid,
                    peer_sec_uid=target_sec_uid)
                if conversation is None:
                    return False, create_error
            elif conversation is None:
                return False, create_error

            if conversation is not None:
                dm_conv_id = str(conversation.get("conv_id") or "")
                dm_short_id = str(conversation.get("conv_short_id") or "")
                dm_ticket = str(conversation.get("ticket") or "")
                _store_dm_conversation(
                    conversation, target_uid, target_sec_uid,
                    str(conversation.get("peer_nickname") or dm_target_nick),
                    dm_self_uid)

            return await client.send_dm(
                dm_conv_id, dm_short_id, dm_ticket, content)

        method = "browser"
        try:
            if platform == "bilibili" and bili_write_mode in {"api", "hybrid"}:
                method = "api"
                bcli = BilibiliClient(
                    state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
                )
                async with bcli.session_scope():
                    if action == "follow":
                        ok, err = await bcli.modify_follow(
                            target_uid or target_sec_uid,
                            follow=True,
                            on_submit=lambda: self._mark_write_submit(
                                AccountActionTask, task_id, channel="api"
                            ),
                        )
                    elif action == "unfollow":
                        ok, err = await bcli.modify_follow(
                            target_uid or target_sec_uid,
                            follow=False,
                            on_submit=lambda: self._mark_write_submit(
                                AccountActionTask, task_id, channel="api"
                            ),
                        )
                    elif action == "send_dm":
                        ok, err, _meta = await bcli.send_dm(
                            target_uid or target_sec_uid or dm_conv_id,
                            content,
                            sender_uid=dm_self_uid,
                            on_submit=lambda: self._mark_write_submit(
                                AccountActionTask, task_id, channel="api"
                            ),
                        )
                    else:
                        ok, err = False, f"未知动作 {action}"
            elif platform == "instagram" and ig_write_mode in {"api", "hybrid"}:
                method = "api"
                icli = InstagramClient(
                    state,
                    ua,
                    proxy=proxy,
                    timeout=self.cfg.engine.request_timeout_seconds,
                    account_seed=(
                        (getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""))
                        if identity is not None
                        else ""
                    ),
                    timezone_id=(
                        getattr(identity, "timezone_id", "Asia/Shanghai")
                        if identity is not None
                        else "Asia/Shanghai"
                    ),
                )
                async with icli.session_scope():
                    if action == "follow":
                        ok, err = await icli.modify_follow(
                            target_uid or target_sec_uid,
                            follow=True,
                            on_submit=lambda: self._mark_write_submit(
                                AccountActionTask, task_id, channel="api"
                            ),
                        )
                    elif action == "unfollow":
                        ok, err = await icli.modify_follow(
                            target_uid or target_sec_uid,
                            follow=False,
                            on_submit=lambda: self._mark_write_submit(
                                AccountActionTask, task_id, channel="api"
                            ),
                        )
                    elif action == "send_dm":
                        dm_target = dm_conv_id or target_uid or target_sec_uid
                        ok, err, _meta = await icli.send_dm(
                            dm_target,
                            content,
                            is_thread=bool(dm_conv_id and dm_target == dm_conv_id),
                            on_submit=lambda: self._mark_write_submit(
                                AccountActionTask, task_id, channel="api"
                            ),
                        )
                    else:
                        ok, err = False, f"未知动作 {action}"
                if (
                    not ok
                    and ig_write_mode == "hybrid"
                    and identity is not None
                    and not str(err or "").startswith((
                        "write_uncertain:",
                        "target_resolution_failed:",
                        "logged_out:",
                        "verification_required:",
                    ))
                ):
                    category, _signal = classify_platform_error(err)
                    if category in {RiskCategory.BUSINESS, RiskCategory.RISK}:
                        ok, err, method = await _browser_action()
                        method = "browser_fallback" if method == "browser" else method
            elif platform == "douyin" and dy_write_mode in {"api", "hybrid"}:
                method = "api"
                cookie = dy_cookie_from_state(state)
                if not cookie:
                    ok, err = False, "api_missing_cookie"
                else:
                    client = DouyinClient(
                        cookie, ua,
                        timeout=self.cfg.engine.request_timeout_seconds,
                        proxy=proxy,
                        **direct_environment)
                    async with client.session_scope():
                        if action == "follow":
                            ok, err = await client.set_follow_state(
                                target_uid, target_sec_uid)
                        elif action == "unfollow":
                            ok, err = await client.set_follow_state(
                                target_uid, target_sec_uid, unfollow=True)
                        elif action == "send_dm":
                            ok, err = await _send_new_or_existing_dm(client)
                        else:
                            ok, err = False, f"未知动作 {action}"
                if (not ok and dy_write_mode == "hybrid"
                        and action in {"follow", "unfollow"}
                        and str(err or "").startswith(
                            "api_requires_browser_security:")):
                    secure_data, secure_error, secure_uncertain = (
                        await douyin_secure_post_api(
                            self.browser, identity,
                            "/aweme/v1/web/commit/follow/user/",
                            {"device_platform": "webapp", "aid": "6383",
                             "channel": "channel_pc_web"},
                            {"user_id": target_uid,
                             "type": 2 if action == "unfollow" else 1},
                            timeout_ms=(
                                self.cfg.engine.request_timeout_seconds * 1000)))
                    method = "api_browser_context"
                    if secure_uncertain:
                        ok, err = False, secure_error
                    elif secure_data is None:
                        ok, err = False, secure_error
                    else:
                        client.last_response = {
                            "browser_security_context_required": True,
                            "dtrait_header_attached": True,
                        }
                        ok, err = client._write_result(
                            secure_data, secure_error or "secure_api_no_response")
                # A known rejection has a response and can fall back; an
                # ambiguous request is deliberately not submitted twice.
                if (not ok and dy_write_mode == "hybrid"
                        and not str(err or "").startswith((
                            "write_uncertain:", "target_resolution_failed:"))):
                    category, _signal = classify_platform_error(err)
                    if category in {RiskCategory.BUSINESS, RiskCategory.RISK}:
                        ok, err, method = await _browser_action()
                        method = ("browser_fallback"
                                  if method == "browser" else method)
            else:
                ok, err, method = await _browser_action()
        except Exception as e:
            ok, err = False, (str(e) or f"{e!r}")

        kind = OperationKind.DM if action == "send_dm" else OperationKind.SOCIAL
        uncertain = not ok and str(err or "").startswith("write_uncertain:")
        failure = None if ok or uncertain else self.risk.record_failure(
            account_id, kind, err)
        with get_session() as s:
            t = s.get(AccountActionTask, task_id)
            account_id = t.account_id if t else None
            if t:
                if ok:
                    t.status = "done"
                elif uncertain:
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at,
                                    signal=failure.signal)
                else:
                    t.status = "failed"
                t.error = "" if ok else err
                t.result = "ok" if ok else ""
                t.method = method
                t.done_at = datetime.utcnow() if ok else t.done_at
                s.add(t); s.commit()
                if ok and action in ("follow", "unfollow"):
                    # 同一个人可能同时有两行:关注列表(following)+ 粉丝列表(fan)。
                    # 两行都要维护 —— 回关是在粉丝列表点的,只动 following 行的话
                    # 粉丝列表那行 is_following 还是 0,界面继续显示「未关注」。
                    def _edge(direction: str):
                        return s.exec(select(FollowEdge).where(
                            FollowEdge.account_id == t.account_id,
                            FollowEdge.platform == t.platform,
                            FollowEdge.direction == direction,
                            FollowEdge.uid == target_uid)).first()

                    edge, fan = _edge("following"), _edge("fan")
                    if action == "unfollow":
                        # 关注列表按 direction 取行,不看 is_following,
                        # 只翻标记的话取关成功后这人还挂在列表里。
                        if edge:
                            s.delete(edge)
                        if fan:      # ta 还关注我,但已不再互关
                            fan.is_following = False
                            fan.is_mutual = False
                            s.add(fan)
                    else:
                        if edge:
                            edge.is_following = True
                            s.add(edge)
                        else:        # 回关的人本来不在关注列表里,补一行
                            s.add(FollowEdge(
                                platform=t.platform, account_id=t.account_id,
                                direction="following", uid=target_uid,
                                sec_uid=target_sec_uid, nickname=t.target_nick,
                                avatar=fan.avatar if fan else "",
                                signature=fan.signature if fan else "",
                                is_following=True, is_mutual=bool(fan),
                                fetched_at=datetime.utcnow()))
                        if fan:      # 回关 ta = 互关
                            fan.is_following = True
                            fan.is_mutual = True
                            s.add(fan)
                    s.commit()
        if ok and account_id:
            self.risk.record_success(account_id, kind)
        return {"ok": ok, "error": "" if ok else err, "method": method}
