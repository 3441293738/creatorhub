"""待发评论队列执行、多平台直连/浏览器发送及回退。"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.account_lifecycle import AccountUnavailableError
from app.browser import douyin_secure_post_api, post_bili_comment, post_channels_comment, post_comment_browser, post_ig_comment, post_ks_comment, post_twitter_comment
from app.browser.tiktok_actions import post_tiktok_comment
from app.db import get_session
from app.engine.monitor_helpers import log
from app.models import CommentTask, DouyinAccount
from app.platforms.bilibili import BilibiliClient
from app.platforms.douyin import DouyinClient, cookie_from_state as dy_cookie_from_state
from app.platforms.instagram import InstagramClient
from app.platforms.xhs import comment_xhs_browser
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.transport_matrix import douyin_client_environment


class CommentTaskRunnerMixin:
    async def execute_comment_task(self, task_id: int) -> dict:
        if task_id in self._commenting:
            return {"ok": False, "error": "正在执行中"}
        self._commenting.add(task_id)
        try:
            with get_session() as s:
                t = s.get(CommentTask, task_id)
                account_id = t.account_id if t else None
            async with self._operation_guard(
                    account_id, OperationKind.COMMENT,
                    fallback_key=f"cmt:{task_id}"):
                return await self._execute_comment_task_locked(task_id)
        except AccountUnavailableError:
            return self._fail_missing_account_task(CommentTask, task_id)
        finally:
            self._commenting.discard(task_id)

    async def _execute_comment_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status not in ("pending",):
                return {"ok": False, "error": f"任务状态为 {t.status}"}
            deferred = self._task_deferral(t)
            if deferred:
                return deferred
            account_id = t.account_id
            # 执行前再查一次每日上限(生成到执行之间可能已超额)
            cap = self.cfg.engine.comment_daily_cap_per_account
            if cap > 0 and self._acct_today_count(s, t.account_id) >= cap:
                self._defer_row(
                    t, "已达账号每日评论上限",
                    datetime.utcnow() + timedelta(days=1))
                s.add(t); s.commit()
                return {"ok": False, "error": "已达每日上限"}
            platform = t.platform
            aweme_id, xsec_token = t.aweme_id, t.xsec_token
            target_url = getattr(t, "target_url", "") or ""
            target_cid, target_nick = t.target_comment_id, t.target_nick
            target_text = getattr(t, "target_text", "") or ""
            content = t.content
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            # 写操作必须有登录账号:绑定账号不存在(被删/重登成新号)时直接失败,
            # 绝不退回匿名 profile(那会开一个未登录窗口,看着像"发了"其实没登录)
            if not acc:
                t.status = "failed"
                t.error = "绑定的账号不存在(可能已删除或重登成了新账号),请编辑规则重新选择账号"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
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
                or (platform == "bilibili" and bili_write_mode == "api")
                or (platform == "instagram" and ig_write_mode == "api")
            )
            environment_error = (
                ""
                if (
                    api_only
                    or (platform == "bilibili" and bili_write_mode == "hybrid")
                    or (platform == "instagram" and ig_write_mode == "hybrid")
                )
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
            gate_error = self._comment_gate_error(t.account_id)
            if gate_error:
                decision = self.risk.preflight(t.account_id, OperationKind.COMMENT)
                self._defer_row(t, gate_error, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": gate_error}
            state = acc.storage_state or acc.creator_storage_state or ""
            proxy = acc.proxy or ""
            ua = acc.ua or self.cfg.engine.user_agent
            direct_environment = douyin_client_environment(acc)
            native_mode = acc.identity_mode == "native"
            # API-only 评论不启动账号浏览器；hybrid/browser 保留页面回退所需身份。
            identity = (
                None if api_only
                else (self.browser.identity_for(acc)
                      if callable(getattr(self.browser, "identity_for", None))
                      else None)
            )
            t.status = "doing"; t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()

        ok, result, err, method = False, "", "", ""
        uncertain = False
        xhs_mode = self._xhs_comment_write_mode()
        if native_mode and xhs_mode == "api":
            xhs_mode = "browser"
        manual_only = platform == "xhs" and xhs_mode == "manual"
        try:
            if platform == "xhs":
                method = xhs_mode
                if manual_only:
                    err = "小红书评论默认转人工发布草稿;未调用评论发布接口"
                elif xhs_mode == "api":
                    client = self._xhs_client(identity, state, proxy)
                    if client is None:
                        err = "账号登录态缺少 a1,请重新扫码登录"
                    else:
                        d = await client.post_comment(aweme_id, content, xsec_token=xsec_token,
                                                      target_comment_id=target_cid)
                        cid = (d.get("comment") or {}).get("id") if isinstance(d, dict) else ""
                        ok, result = True, (cid or "ok")
                else:
                    outcome = await comment_xhs_browser(
                        self.browser, identity, aweme_id, xsec_token, content,
                        target_comment_id=target_cid,
                        target_text=target_text,
                        on_submit=lambda: self._mark_write_submit(
                            CommentTask, task_id))
                    ok = outcome.status == "success"
                    uncertain = outcome.status == "uncertain"
                    result, err, method = (
                        outcome.result, outcome.error, outcome.method)
            elif platform == "kuaishou":
                method = "browser"
                ok, err = await post_ks_comment(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_text if target_cid else "",
                    headed=(True if native_mode
                            else self.cfg.engine.comment_browser_headed))
                result = "ok" if ok else ""
            elif platform == "shipinhao":
                # 视频号只能回复自己作品的评论(助手端无法主动去别人作品下评论)
                method = "browser"
                ok, err = await post_channels_comment(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_nick if target_cid else "",
                    headed=(True if native_mode
                            else self.cfg.engine.comment_browser_headed))
                result = "ok" if ok else ""
            elif platform == "tiktok":
                method = "browser"
                ok, result, err = await post_tiktok_comment(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_text if target_cid else "",
                    require_reply=bool(target_cid),
                    on_submit=lambda: self._mark_write_submit(CommentTask, task_id),
                    headed=True, target_url=target_url)
            elif platform == "twitter":
                method = "browser"
                ok, result, err = await post_twitter_comment(
                    self.browser,
                    identity,
                    aweme_id,
                    content,
                    target_url=target_url,
                    reply_to_comment_id=target_cid,
                    headed=(True if native_mode else self.cfg.engine.comment_browser_headed),
                    on_submit=lambda: self._mark_write_submit(CommentTask, task_id, channel="browser"),
                )
            elif platform == "rednote":
                method = "unavailable"
                err = "unavailable:海外小红书暂未开放评论写入与自动计划"
            elif platform == "bilibili":
                if bili_write_mode in {"api", "hybrid"}:
                    method = "api"
                    bcli = BilibiliClient(
                        state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
                    )
                    async with bcli.session_scope():
                        ok, err, cmeta = await bcli.post_comment(
                            aweme_id,
                            content,
                            reply_to=target_cid,
                            on_submit=lambda: self._mark_write_submit(
                                CommentTask, task_id, channel="api"
                            ),
                        )
                        result = str(cmeta.get("rpid") or "ok") if ok else ""
                    if (
                        not ok
                        and bili_write_mode == "hybrid"
                        and identity is not None
                        and not target_cid
                        and not str(err or "").startswith("write_uncertain:")
                    ):
                        category, _ = classify_platform_error(err)
                        if category in {RiskCategory.BUSINESS, RiskCategory.RISK}:
                            ok, err = await post_bili_comment(
                                self.browser,
                                identity,
                                aweme_id,
                                content,
                                on_submit=lambda: self._mark_write_submit(
                                    CommentTask, task_id, channel="browser"
                                ),
                            )
                            method = "browser_fallback"
                            result = "ok" if ok else ""
                else:
                    method = "browser"
                    ok, err = await post_bili_comment(
                        self.browser,
                        identity,
                        aweme_id,
                        content,
                        on_submit=lambda: self._mark_write_submit(
                            CommentTask, task_id, channel="browser"
                        ),
                    )
                    result = "ok" if ok else ""
            elif platform == "instagram":
                if ig_write_mode in {"api", "hybrid"}:
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
                        ok, err, cmeta = await icli.post_comment(
                            aweme_id,
                            content,
                            reply_to=target_cid,
                            on_submit=lambda: self._mark_write_submit(
                                CommentTask, task_id, channel="api"
                            ),
                        )
                        result = str(cmeta.get("comment_id") or "ok") if ok else ""
                    if (
                        not ok
                        and ig_write_mode == "hybrid"
                        and identity is not None
                        and not target_cid
                        and not str(err or "").startswith((
                            "write_uncertain:",
                            "logged_out:",
                            "verification_required:",
                        ))
                    ):
                        category, _ = classify_platform_error(err)
                        if category in {RiskCategory.BUSINESS, RiskCategory.RISK}:
                            ok, err = await post_ig_comment(
                                self.browser,
                                identity,
                                aweme_id,
                                content,
                                on_submit=lambda: self._mark_write_submit(
                                    CommentTask, task_id, channel="browser"
                                ),
                            )
                            method = "browser_fallback"
                            result = "ok" if ok else ""
                else:
                    method = "browser"
                    ok, err = await post_ig_comment(
                        self.browser,
                        identity,
                        aweme_id,
                        content,
                        on_submit=lambda: self._mark_write_submit(
                            CommentTask, task_id, channel="browser"
                        ),
                    )
                    result = "ok" if ok else ""
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
                        ok, result, err = await client.post_comment(
                            aweme_id, content, reply_comment_id=target_cid)
                if (not ok and dy_write_mode == "hybrid"
                        and str(err or "").startswith(
                            "api_requires_browser_security:")):
                    secure_data, secure_error, secure_uncertain = (
                        await douyin_secure_post_api(
                            self.browser, identity,
                            "/aweme/v1/web/comment/publish/",
                            {"device_platform": "webapp", "aid": "6383",
                             "channel": "channel_pc_web",
                             "aweme_id": aweme_id, "item_type": 0},
                            {"aweme_id": aweme_id, "text": content,
                             "item_type": 0, "reply_id": target_cid,
                             "reply_comment_id": target_cid,
                             "at_users": "[]"},
                            timeout_ms=(
                                self.cfg.engine.request_timeout_seconds * 1000)))
                    method = "api_browser_context"
                    if secure_uncertain:
                        ok, result, err = False, "", secure_error
                    elif secure_data is None:
                        ok, result, err = False, "", secure_error
                    else:
                        client.last_response = {
                            "browser_security_context_required": True,
                            "dtrait_header_attached": True,
                        }
                        ok, err = client._write_result(
                            secure_data, secure_error or "secure_api_no_response")
                        comment = secure_data.get("comment") or {}
                        result = (str(comment.get("cid")
                                      or comment.get("comment_id")
                                      or secure_data.get("comment_id") or "ok")
                                  if ok else "")
                # Only a response that clearly rejected the POST may enter the
                # browser fallback. Unknown request outcome remains uncertain.
                if (not ok and dy_write_mode == "hybrid"
                        and not str(err or "").startswith("write_uncertain:")):
                    category, _signal = classify_platform_error(err)
                    if category in {RiskCategory.BUSINESS, RiskCategory.RISK}:
                        ok, err = await post_comment_browser(
                            self.browser, identity, aweme_id, content,
                            reply_to_text=target_text if target_cid else "",
                            require_reply=bool(target_cid),
                            headed=(True if native_mode
                                    else self.cfg.engine.comment_browser_headed))
                        method = "browser_fallback"
                        result = "ok" if ok else ""
            else:
                method = "browser"
                ok, err = await post_comment_browser(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_text if target_cid else "",
                    require_reply=bool(target_cid),
                    headed=(True if native_mode
                            else self.cfg.engine.comment_browser_headed))
                result = "ok" if ok else ""
        except Exception as e:
            ok, err = False, (str(e) or repr(e))

        uncertain = uncertain or (
            not ok and str(err or "").startswith("write_uncertain:"))

        failure = None if ok or manual_only or uncertain else self.risk.record_failure(
            account_id, OperationKind.COMMENT, err)
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            account_id = t.account_id if t else None
            if t:
                # “立即发”在 manual 模式下也只能回到草稿,不能变成失败后重试循环。
                if ok:
                    t.status = "done"
                elif manual_only:
                    t.status = "draft"
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
                t.result = result
                t.error = "" if ok else err
                t.method = method
                t.done_at = datetime.utcnow() if ok else t.done_at
                s.add(t); s.commit()
        if ok:
            self.risk.record_success(account_id, OperationKind.COMMENT)
            log.info("评论任务 %s 已发送(%s,作品 %s)", task_id, method, aweme_id)
        else:
            log.info("评论任务 %s 失败: %s", task_id, err)
        return {"ok": ok, "error": err, "method": method}
