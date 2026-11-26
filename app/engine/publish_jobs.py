"""多平台定时/立即发布任务调度、转发入队与浏览器/协议发布执行。"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Optional
import asyncio
import json

from sqlmodel import select
import inspect

from app.account_lifecycle import AccountUnavailableError
from app.db import get_session
from app.engine.monitor_helpers import (
    _AccountCheckBusy,
    _WRITE_SUBMIT_PREFIX,
    _loads,
    _loads_list,
    _round_robin_by_account,
    log,
)
from app.models import ContentRecord, DouyinAccount, NotificationChannel, PublishTask
from app.notifier import notify_all
from app.services.login_sessions import (
    close_open_browser_for_account,
    has_active_open_browser,
)
from app.platforms.bilibili import publish_bilibili
from app.platforms.channels import publish_channels
from app.platforms.douyin import publish_douyin
from app.platforms.facebook import MAX_FACEBOOK_IMAGES, publish_facebook
from app.platforms.youtube import MAX_YOUTUBE_TITLE_LEN, publish_youtube
from app.platforms.instagram import publish_instagram
from app.platforms.kuaishou import publish_kuaishou
from app.platforms.rednote import publish_rednote, weighted_truncate
from app.platforms.tiktok import publish_tiktok
from app.platforms.twitter import publish_twitter, trim_to_twitter_limit
from app.platforms.xhs import publish_xhs
from app.progress_hub import progress_hub
from app.risk import OperationKind, RiskCategory
from app.transport_matrix import resolve_transport


class PublishJobMixin:
    # ── 发布(小红书创作平台)+ 跨平台转发 ──
    def _content_files(self, rec: ContentRecord) -> list:
        """收集一条作品记录在本地的媒体文件路径。"""
        if not rec.local_path:
            return []
        p = Path(rec.local_path)
        if p.is_file():
            return [str(p)]
        folder = p if p.is_dir() else p.parent
        if not folder.exists():
            return []
        # 文件名形如 {aweme_id}_{title}_{index}.{ext};按末尾数字序号排(而非字典序,
        # 否则 10 张以上会 _10 排到 _2 前面 —— 图集顺序错乱、封面选错)。
        def _idx_key(f: Path):
            tail = f.stem.rsplit("_", 1)[-1]
            return (0, int(tail)) if tail.isdigit() else (1, f.name)
        cands = [f for f in folder.glob(f"{rec.aweme_id}_*")
                 if f.is_file() and not f.name.endswith(".part")
                 and ".cover." not in f.name.lower()]
        return [str(f) for f in sorted(cands, key=_idx_key)]

    def create_relay_publish(self, content_id: int, account_id: int,
                             target_platform: str = "xhs",
                             title: Optional[str] = None, desc: Optional[str] = None,
                             topics: Optional[str] = None,
                             visibility: str = "public", allow_save: bool = True,
                             media_order: Optional[list] = None,
                             scheduled_at: Optional[datetime] = None,
                             session=None
                             ) -> Optional[int]:
        """从已下载作品创建发往目标平台(小红书/抖音/视频号)的发布任务。返回任务 id。

        只接收作品 id,内部自开会话取记录,避免跨会话传入已绑定的 ORM 对象。
        target_platform: xhs / douyin / shipinhao。
        title/desc/topics 为 None 时沿用作品原始内容;传了则用编辑后的值(发布前可改)。
        """
        with (nullcontext(session) if session is not None else get_session()) as s:
            acc_row = s.get(DouyinAccount, account_id) if account_id else None
            if acc_row and str(acc_row.account_role or "hybrid").strip().lower() == "crawler":
                raise ValueError("所选账号角色为「仅采集号 (crawler)」，禁止用于跨平台转发发布")
            rec = s.get(ContentRecord, content_id)
            if not rec:
                return None
            files = self._content_files(rec)
            if not files:
                return None
            # 转发前若在弹窗里剔除/调序了图片,media_order 是保留下来的原始序号(按新顺序)。
            # None 才表示全部原序；无效选择应整单报错，不能回退发布其他媒体。
            if media_order is not None:
                if (not isinstance(media_order, list) or not media_order
                        or any(type(i) is not int or not 0 <= i < len(files)
                               for i in media_order)
                        or len(set(media_order)) != len(media_order)):
                    raise ValueError("转发媒体选择须为非空、不重复且未越界的整数序号列表")
                files = [files[i] for i in media_order]
            if target_platform == "tiktok" and rec.media_type != "video":
                raise ValueError("TikTok 网页发布当前仅支持视频")
            if target_platform == "instagram" and len(files) > 10:
                raise ValueError("Instagram 轮播发布最多支持 10 个媒体文件")
            if target_platform == "twitter":
                if rec.media_type == "video" and len(files) > 1:
                    files = files[:1]
                elif rec.media_type != "video" and len(files) > 4:
                    raise ValueError("Twitter 图集发布单次最多支持 4 张图片，请先勾选最多 4 张图片")
            if target_platform == "facebook":
                if rec.media_type == "video" and len(files) > 1:
                    files = files[:1]
                elif rec.media_type != "video" and len(files) > MAX_FACEBOOK_IMAGES:
                    raise ValueError(f"Facebook 图集发布单次最多支持 {MAX_FACEBOOK_IMAGES} 张图片，请先勾选最多 {MAX_FACEBOOK_IMAGES} 张图片")
            if target_platform == "youtube":
                if rec.media_type != "video":
                    raise ValueError("YouTube 仅支持视频或 Shorts 发布，暂不支持纯图集作品")
                if len(files) > 1:
                    files = files[:1]
            if target_platform in {"xhs", "rednote"}:
                from app.platforms.xhs.media import validate_publish_files
                files = validate_publish_files(
                    "video" if rec.media_type == "video" else "images", files)
            raw_title = title if title is not None else (rec.desc or "")
            if target_platform == "rednote":
                t_title = weighted_truncate(raw_title.strip(), 20)
            elif target_platform == "twitter":
                t_title = trim_to_twitter_limit(raw_title.strip(), limit=280)
            else:
                title_cap = {"douyin": 30, "shipinhao": 16, "bilibili": 80, "instagram": 2200, "facebook": 5000, "youtube": MAX_YOUTUBE_TITLE_LEN}.get(target_platform, 20)
                t_title = raw_title[:title_cap]
            t_desc = desc if desc is not None else (rec.desc or "")
            t_topics = topics if topics is not None else ""
            task = PublishTask(
                platform=target_platform, account_id=account_id,
                media_type="video" if rec.media_type == "video" else "images",
                title=t_title, desc=t_desc, topics=t_topics,
                visibility=visibility, allow_save=allow_save,
                media_json=json.dumps(files),
                scheduled_at=scheduled_at,
                source_platform=rec.platform, source_content_id=rec.id,
            )
            s.add(task); s.flush()
            if session is None:
                s.commit(); s.refresh(task)
            return task.id

    def _publish_is_api_only(self, platform: str, acc) -> bool:
        """判断当前发布任务是否走纯 API 无窗通道，避免阻塞有头浏览器发布串行锁。"""
        if acc is None:
            return False
        pf = str(platform or "").lower()
        if pf == "douyin":
            t = resolve_transport(self.cfg, "douyin", "publish", acc)
            return bool(t and t.get("configured_mode") == "api")
        if pf == "instagram":
            t = resolve_transport(self.cfg, "instagram", "publish", acc)
            return bool(t and t.get("effective_mode") == "api")
        if pf == "rednote":
            return self._rednote_publish_mode() == "api"
        if pf == "xhs":
            return acc.identity_mode != "native" and self._xhs_publish_mode() == "api"
        return False

    @staticmethod
    def _build_publish_steps(stage: str, *, status: str = "running") -> tuple[list[dict], int]:
        labels = ["准备环境", "上传媒体", "填写文案", "校验/验证", "提交确认"]
        text = str(stage or "")
        if status == "completed":
            return [{"label": lb, "state": "done"} for lb in labels], 100
        idx = 0
        if any(k in text for k in ("提交", "点击发布", "确认发布", "完成发布", "发布成功", "等待结果")):
            idx = 4
        elif any(k in text for k in ("验证", "滑块", "扫码", "验证码", "校验", "风控", "检查")):
            idx = 3
        elif any(k in text for k in ("标题", "正文", "文案", "话题", "分区", "标签", "填写", "选项")):
            idx = 2
        elif any(k in text for k in ("上传", "素材", "视频", "图片", "封面", "转码")):
            idx = 1
        waiting = any(k in text for k in ("安全验证", "验证码", "滑块", "扫码"))
        steps = []
        for i, lb in enumerate(labels):
            if i < idx:
                st = "done"
            elif i == idx:
                st = "warn" if waiting else ("failed" if status == "failed" else "active")
            else:
                st = "pending"
            steps.append({"label": lb, "state": st})
        pct = min(95, 12 + idx * 20)
        return steps, pct

    def _emit_publish_progress(
        self,
        task_id: int,
        *,
        status: str = "running",
        stage: str = "",
        title: str = "",
        platform: str = "",
        account_id: int | None = None,
        error: str = "",
        action_required: dict | None = None,
        immediate: bool = False,
    ) -> None:
        if not title or not platform:
            try:
                with get_session() as s:
                    row = s.get(PublishTask, task_id)
                    if row:
                        title = title or row.title or (row.desc or "")[:40] or f"发布任务 #{task_id}"
                        platform = platform or row.platform or ""
                        account_id = account_id if account_id is not None else row.account_id
            except Exception:
                pass
        steps, pct = self._build_publish_steps(stage, status=status)
        waiting = any(k in str(stage or "") for k in ("安全验证", "验证码", "滑块", "扫码"))
        eff_status = "waiting_user" if (status == "running" and waiting) else status
        eff_action = action_required
        if eff_action is None and waiting:
            eff_action = {"kind": "focus_tab", "label": "查看发布状态", "task_id": task_id}
        progress_hub.upsert(
            f"publish:{task_id}",
            immediate=immediate or waiting,
            category="publish",
            title=str(title or f"发布任务 #{task_id}")[:120],
            platform=str(platform or ""),
            account_id=account_id,
            source_tab="publish",
            status=eff_status,
            phase=eff_status,
            phase_label=str(stage or ("发布完成" if status == "completed" else "发布中")),
            error=str(error or ""),
            determinate=status in {"running", "waiting_user", "completed"},
            percent=100.0 if status == "completed" else (float(pct) if status in {"running", "waiting_user"} else None),
            steps=steps,
            action_required=eff_action,
            cancelable=False,
            extra={"task_id": task_id, "live_stage": str(stage or "")},
        )

    def _mark_publish_open_browser_blocked(
        self,
        task_id: int,
        account_id: int | None,
        platform: str,
        *,
        log_once: bool = True,
    ) -> None:
        reason = "该账号手动浏览器窗口仍开着，关闭窗口后将自动发布"
        prev_signal = ""
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if t and t.status == "pending":
                prev_signal = str(t.blocked_signal or "")
                t.blocked_reason = reason
                t.blocked_signal = "open_browser"
                t.blocked_operation = OperationKind.PUBLISH.value
                t.blocked_at = datetime.utcnow()
                t.next_allowed_at = None
                t.error = reason
                s.add(t)
                s.commit()
        self._emit_publish_progress(
            task_id,
            status="waiting_user",
            stage=reason,
            platform=platform,
            account_id=account_id,
            action_required={"kind": "close_and_publish", "label": "关闭窗口并发布", "task_id": task_id},
            immediate=True,
        )
        if not log_once or prev_signal != "open_browser":
            log.warning(
                "发布任务 #%s(%s, 账号#%s) 暂缓：该账号手动浏览器窗口仍开着，请关闭窗口后再发布",
                task_id,
                platform or "-",
                account_id or "-",
            )

    def nudge_publish_queue(self) -> None:
        """账号手动窗口释放后尽快唤醒待发队列，避免干等下一轮轮询。"""
        if not getattr(self, "_running", False):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if not loop.is_closed():
            loop.create_task(self._process_publish())

    async def _process_publish(self):
        worker = getattr(self, "_publish_worker_task", None)
        if worker is not None and not worker.done():
            return
        due: list[tuple[int, int | None]] = []
        now = datetime.utcnow()
        with get_session() as s:
            tasks = s.exec(
                select(PublishTask)
                .where(PublishTask.status == "pending")
                .order_by(PublishTask.id.asc())
            ).all()
            for t in tasks:
                if t.scheduled_at and not t.scheduled_at_is_utc:
                    t.status = "draft"
                    t.error = "旧预约未记录时区，请编辑并确认发布时间后再入队"
                    s.add(t)
                    continue
                if self._task_due(t, now):
                    due.append((t.id, t.account_id))
            s.commit()
        if not due:
            return
        ordered_ids = [tid for tid, _ in _round_robin_by_account(due)]
        task = asyncio.create_task(self._drain_due_publishes(ordered_ids))
        self._publish_worker_task = task

        def _done(completed: asyncio.Task) -> None:
            if getattr(self, "_publish_worker_task", None) is completed:
                self._publish_worker_task = None
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_done)

    async def _drain_due_publishes(self, due_ids: list[int]) -> None:
        for tid in due_ids:
            if not getattr(self, "_running", True):
                break
            try:
                await self.publish_task(tid, scheduled=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("后台调度发布任务 #%s 异常", tid)

    async def publish_task(
        self,
        task_id: int,
        *,
        scheduled: bool = False,
        close_open_browser: bool = False,
    ) -> dict:
        if task_id in self._publishing:
            return {"ok": False, "error": "正在发布中"}
        self._publishing.add(task_id)
        try:
            with get_session() as s:
                t = s.get(PublishTask, task_id)
                if not t:
                    return {"ok": False, "error": "任务不存在"}
                if t.status not in ("pending", "failed"):
                    return {"ok": False, "error": f"任务状态为 {t.status}"}
                account_id = t.account_id
                platform = t.platform
                acc_probe = s.get(DouyinAccount, account_id) if account_id else None
                api_only_publish = self._publish_is_api_only(platform, acc_probe)
                pub_sem = getattr(self, "_api_publish_sem", self._publish_sem) if api_only_publish else self._publish_sem

            # 1) 优先检测手动打开的浏览器窗口（纯 API 发布不占用浏览器 Profile，无需等待窗口关闭）。
            if not api_only_publish and await has_active_open_browser(account_id):
                if close_open_browser:
                    await close_open_browser_for_account(account_id, nudge=False)
                    log.info(
                        "发布任务 #%s(%s, 账号#%s) 已按确认保存登录态并关闭手动浏览器窗口，继续执行发布",
                        task_id,
                        platform,
                        account_id or "-",
                    )
                else:
                    self._mark_publish_open_browser_blocked(
                        task_id, account_id, platform, log_once=scheduled
                    )
                    return {
                        "ok": False,
                        "deferred": True,
                        "blocked_by": "open_browser",
                        "account_id": account_id,
                        "error": "该账号手动浏览器窗口仍开着，请先关闭窗口后再发布",
                    }

            # 2) 先判目标发布通道是否繁忙（纯 API 与有头浏览器通道相互独立）。
            if pub_sem.locked():
                return {
                    "ok": False,
                    "deferred": True,
                    "blocked_by": "publish_busy",
                    "account_id": account_id,
                    "error": "当前已有另一条发布任务正在执行，请等待完成后再试",
                }

            # 3) 先非阻塞获取账号锁（手动点立即发布时最多短暂等待 3 秒让正在收尾的轻读结束），
            #    拿到账号锁后再进发布通道，彻底杜绝单账号占锁拖死全站发布。
            acc_key = f"acc:{account_id}" if account_id else f"pub:{task_id}"
            acc_lock = self.browser.lock_for(acc_key)
            if acc_lock.locked():
                if not scheduled:
                    deadline = asyncio.get_running_loop().time() + 3.0
                    while acc_lock.locked() and asyncio.get_running_loop().time() < deadline:
                        if not api_only_publish and await has_active_open_browser(account_id):
                            break
                        await asyncio.sleep(0.25)
                if not api_only_publish and await has_active_open_browser(account_id):
                    self._mark_publish_open_browser_blocked(
                        task_id, account_id, platform, log_once=scheduled
                    )
                    return {
                        "ok": False,
                        "deferred": True,
                        "blocked_by": "open_browser",
                        "account_id": account_id,
                        "error": "该账号手动浏览器窗口仍开着，请先关闭窗口后再发布",
                    }
                if acc_lock.locked():
                    if not scheduled:
                        log.warning(
                            "发布任务 #%s(%s, 账号#%s) 暂不可执行：账号锁正被其他任务占用",
                            task_id,
                            platform,
                            account_id or "-",
                        )
                    return {
                        "ok": False,
                        "deferred": True,
                        "blocked_by": "account_busy",
                        "account_id": account_id,
                        "error": "该账号当前正在执行其他浏览器操作（如巡检/采集/登录），请稍后再试",
                    }

            async with self._operation_guard(
                    account_id, OperationKind.PUBLISH,
                    fallback_key=f"pub:{task_id}",
                    skip_busy=True):
                if pub_sem.locked():
                    return {
                        "ok": False,
                        "deferred": True,
                        "blocked_by": "publish_busy",
                        "account_id": account_id,
                        "error": "当前已有另一条发布任务正在执行，请等待完成后再试",
                    }
                async with pub_sem:
                    return await self._publish_task_locked(task_id)
        except _AccountCheckBusy:
            if await has_active_open_browser(account_id):
                self._mark_publish_open_browser_blocked(
                    task_id, account_id, platform, log_once=scheduled
                )
                return {
                    "ok": False,
                    "deferred": True,
                    "blocked_by": "open_browser",
                    "account_id": account_id,
                    "error": "该账号手动浏览器窗口仍开着，请先关闭窗口后再发布",
                }
            return {
                "ok": False,
                "deferred": True,
                "blocked_by": "account_busy",
                "account_id": account_id,
                "error": "该账号当前正在执行其他浏览器操作，稍后将自动重试",
            }
        except AccountUnavailableError:
            return self._fail_missing_account_task(PublishTask, task_id)
        finally:
            self._publishing.discard(task_id)

    async def _publish_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status not in ("pending", "failed"):
                return {"ok": False, "error": f"任务状态为 {t.status}"}
            if t.scheduled_at and not t.scheduled_at_is_utc:
                return {"ok": False, "error": "请先编辑并确认预约时区，再执行发布"}
            deferred = self._task_deferral(t)
            if deferred:
                return deferred
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            if not acc:
                t.status = "failed"
                t.error = "绑定账号不存在(可能已删除/重登成新号)"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if str(acc.account_role or "hybrid").strip().lower() == "crawler":
                t.status = "failed"
                t.error = "账号角色设为「仅采集号 (crawler)」，已拦截作品发布"
                s.add(t); s.commit()
                return {"ok": False, "error": "role_forbidden_crawler"}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            platform = t.platform
            dy_publish_transport = (resolve_transport(
                self.cfg, "douyin", "publish", acc)
                if platform == "douyin" else None)
            ig_publish_transport = (resolve_transport(
                self.cfg, "instagram", "publish", acc)
                if platform == "instagram" else None)
            rn_api_publish = (
                platform == "rednote" and self._rednote_publish_mode() == "api"
            )
            api_only = bool(
                (dy_publish_transport and dy_publish_transport["configured_mode"] == "api")
                or (ig_publish_transport and ig_publish_transport["effective_mode"] == "api")
                or rn_api_publish
            )
            skip_env_check = (
                (api_only and not rn_api_publish)
                or bool(ig_publish_transport and ig_publish_transport["configured_mode"] == "hybrid")
            )
            environment_error = (
                "" if skip_env_check else self._native_write_environment_error(
                    acc, headed=not api_only, browser_mode=not api_only
                )
            )
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            pause_error = self._write_pause_error(t.account_id)
            if pause_error:
                decision = self.risk.preflight(t.account_id, OperationKind.PUBLISH)
                self._defer_row(t, pause_error, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": pause_error}
            if not self._in_active_window(t.account_id):
                self._defer_row(t, "当前处于非活跃时段，发布任务已保留在队列")
                s.add(t); s.commit()
                return {"ok": False, "error": t.error}
            decision = self.risk.preflight(t.account_id, OperationKind.PUBLISH)
            if not decision.allowed:
                self._defer_row(t, decision.reason, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": decision.reason}
            # 发布用创作平台态;一次扫码已把创作 cookie 并入 storage_state,故回退它
            state = acc.creator_storage_state or acc.storage_state or ""
            native_mode = acc.identity_mode == "native"
            identity = (None if dy_publish_transport and
                        dy_publish_transport["effective_mode"] == "unavailable"
                        else (self.browser.identity_for(acc)
                              if callable(getattr(self.browser, "identity_for", None))
                              else None))
            media_type, title, desc, topics = t.media_type, t.title, t.desc, t.topics
            visibility, allow_save = t.visibility, t.allow_save
            allow_comment = getattr(t, "allow_comment", True)
            allow_duet = getattr(t, "allow_duet", True)
            allow_stitch = getattr(t, "allow_stitch", True)
            bili_options = _loads(getattr(t, "bili_options_json", "{}") or "{}")
            ig_options = _loads(getattr(t, "ig_options_json", "{}") or "{}")
            tw_options = _loads(getattr(t, "tw_options_json", "{}") or "{}")
            fb_options = _loads(getattr(t, "fb_options_json", "{}") or "{}")
            yt_options = _loads(getattr(t, "yt_options_json", "{}") or "{}")
            location = getattr(t, "location", "") or ""
            platform = t.platform
            files = _loads_list(t.media_json)
            t.status = "publishing"; t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()
            pub_title = t.title or (t.desc or "")[:40] or f"发布任务 #{task_id}"
            pub_acc_id = t.account_id
        self._emit_publish_progress(
            task_id,
            status="running",
            stage="正在准备发布环境",
            title=pub_title,
            platform=platform,
            account_id=pub_acc_id,
            immediate=True,
        )

        if (dy_publish_transport is not None
                and dy_publish_transport["effective_mode"] == "unavailable"):
            return await self._finish_publish(
                task_id, False, "", dy_publish_transport["reason"],
                platform="douyin")

        if platform == "kuaishou":
            # 快手发布:登录态在该账号持久 profile 里(creator/storage 任一即可),走浏览器自动化
            if not state:
                return await self._finish_publish(
                    task_id, False, "",
                    "该账号未完成快手「创作者登录」,请先在账号页点「创作者登录」",
                    platform="kuaishou")
            return await self._run_browser_publish(
                task_id, "kuaishou", publish_kuaishou,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, headed=True, visibility=visibility,
                allow_save=allow_save)

        if platform == "shipinhao":
            # 视频号发布:登录态在该账号持久 profile 里,走浏览器自动化(wujie shadowRoot)
            if not state:
                return await self._finish_publish(
                    task_id, False, "",
                    "该账号未完成视频号登录,请先在账号页点「视频号登录」",
                    platform="shipinhao")
            return await self._run_browser_publish(
                task_id, "shipinhao", publish_channels,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, headed=True, location=location)

        if platform == "douyin":
            # 抖音发布:同快手走浏览器自动化,登录态在该账号持久 profile 里
            if not state:
                return await self._finish_publish(
                    task_id, False, "",
                    "该账号未完成抖音「创作者登录」,请先在账号页点「创作者登录」",
                    platform="douyin")
            return await self._run_browser_publish(
                task_id, "douyin", publish_douyin,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, visibility=visibility,
                allow_save=allow_save, headed=True)

        if platform == "tiktok":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成 TikTok 登录，请先在账号页登录",
                    platform="tiktok")
            return await self._run_browser_publish(
                task_id, "tiktok", publish_tiktok,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, visibility=visibility,
                allow_comment=allow_comment, allow_duet=allow_duet,
                allow_stitch=allow_stitch, headed=True)

        if platform == "bilibili":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成哔哩哔哩登录，请先在账号页扫码登录",
                    platform="bilibili")
            return await self._run_browser_publish(
                task_id, "bilibili", publish_bilibili,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, visibility=visibility,
                allow_comment=allow_comment, bili_options=bili_options,
                headed=True)

        if platform == "rednote":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成海外小红书登录，请先在账号页扫码或创作者登录",
                    platform="rednote")
            rn_mode = self._rednote_publish_mode()
            return await self._run_browser_publish(
                task_id, "rednote", publish_rednote,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, headed=(rn_mode != "api"), mode=rn_mode,
                visibility=visibility,
                preserve_error=True, channel=rn_mode,
                cancel_message="write_uncertain:海外小红书发布已中断，结果需到平台核对")

        if platform == "instagram":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成 Instagram 登录，请先在账号页登录或导入 Cookie",
                    platform="instagram")
            ig_write_mode = str(
                getattr(self.cfg.engine, "instagram_write_mode", "hybrid") or "hybrid"
            ).strip().lower()
            if ig_write_mode not in {"hybrid", "api", "browser"}:
                ig_write_mode = "hybrid"
            ig_channel = "api" if ig_write_mode in {"api", "hybrid"} else "browser"
            return await self._run_browser_publish(
                task_id, "instagram", publish_instagram,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, visibility=visibility,
                allow_comment=allow_comment, ig_options=ig_options,
                mode=ig_write_mode, headed=True, channel=ig_channel)

        if platform == "twitter":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成 Twitter 登录，请先在账号页登录或导入 Cookie",
                    platform="twitter")
            return await self._run_browser_publish(
                task_id, "twitter", publish_twitter,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, visibility=visibility,
                allow_comment=allow_comment, tw_options=tw_options,
                headed=True, channel="browser")

        if platform == "facebook":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成 Facebook 登录，请先在账号页登录或导入 Cookie",
                    platform="facebook")
            return await self._run_browser_publish(
                task_id, "facebook", publish_facebook,
                self.browser, identity, state, media_type, title, desc, files,
                topics=topics, visibility=visibility,
                allow_comment=allow_comment, fb_options=fb_options,
                headed=True, channel="browser")

        if platform == "youtube":
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成 YouTube 登录，请先在账号页登录或导入 Cookie",
                    platform="youtube")
            return await self._run_browser_publish(
                task_id, "youtube", publish_youtube,
                self.browser, identity,
                media_type=media_type, files=files, title=title, desc=desc,
                topics=topics, visibility=visibility,
                yt_options=yt_options, headed=True, channel="browser")

        if not state:
            return await self._finish_publish(
                task_id, False, "", "该账号未完成小红书「创作者登录」,请先在账号页点「创作者登录」",
                platform="xhs")

        xhs_mode = ("browser" if native_mode
                    else self._xhs_publish_mode())
        return await self._run_browser_publish(
            task_id, "xhs", publish_xhs,
            self.browser, identity, state, media_type, title, desc, files,
            topics=topics, headed=True, mode=xhs_mode,
            visibility=visibility, preserve_error=True, channel=xhs_mode,
            cancel_message="write_uncertain:发布已中断，结果需到平台核对")

    def get_publish_stage(self, task_id: int) -> str:
        """Return transient browser publish progress/verification hint for UI polling."""
        return str((getattr(self, "_publish_stages", None) or {}).get(task_id) or "")

    def _set_publish_stage(self, task_id: int, stage: str) -> None:
        stages = getattr(self, "_publish_stages", None)
        if stages is None:
            stages = {}
            self._publish_stages = stages
        text = str(stage or "").strip()
        if text:
            stages[task_id] = text
            self._emit_publish_progress(task_id, status="running", stage=text)
        else:
            stages.pop(task_id, None)

    async def _run_browser_publish(self, task_id: int, platform: str, publish_fn,
                                     *args, channel: str = "browser",
                                     cancel_message: str = "", **kwargs) -> dict:
        """Execute one platform publisher with uniform submit-boundary & cancel protection."""
        pname = {"kuaishou": "快手", "douyin": "抖音",
                 "shipinhao": "视频号", "tiktok": "TikTok",
                 "bilibili": "哔哩哔哩", "rednote": "海外小红书", "instagram": "Instagram",
                 "twitter": "Twitter", "facebook": "Facebook", "youtube": "YouTube",
                 "xhs": "小红书"}.get(platform, platform)
        call_kwargs = dict(kwargs)
        try:
            params = inspect.signature(publish_fn).parameters
            if "on_stage" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                call_kwargs["on_stage"] = lambda msg: self._set_publish_stage(task_id, msg)
        except Exception:
            pass
        default_channel = "api" if channel in {"api", "hybrid"} else "browser"
        try:
            ok, url, err = await publish_fn(
                *args,
                on_submit=(lambda ch=default_channel: self._mark_write_submit(
                    PublishTask,
                    task_id,
                    channel=(ch if ch in {"api", "browser"} else default_channel),
                )),
                **call_kwargs,
            )
        except asyncio.CancelledError:
            msg = cancel_message or f"write_uncertain:{pname} 发布已中断，结果需到平台核对"
            await self._finish_publish(task_id, False, "", msg, platform=platform)
            raise
        except Exception as e:
            log.exception("发布任务 #%s(%s) 执行异常", task_id, platform)
            ok, url, err = False, "", f"发布异常: {e!r}"
        finally:
            self._set_publish_stage(task_id, "")
        return await self._finish_publish(task_id, ok, url, err, platform=platform)

    async def _finish_publish(self, task_id, ok, url, err, platform="xhs") -> dict:
        platform_error = err
        # Keep structured failures through risk classification, then persist
        # and return only text (never exception objects or raw response data).
        err = str(err or "")
        account_id = None
        failure = None
        uncertain = (not ok and isinstance(err, str)
                     and err.startswith("write_uncertain:"))
        if not ok:
            with get_session() as s:
                task = s.get(PublishTask, task_id)
                account_id = task.account_id if task else None
                submitted = bool(task and (task.error or "").startswith(_WRITE_SUBMIT_PREFIX))
            # A durable submit marker wins over any later adapter exception.
            # Losing the response is not evidence that the write was rejected.
            uncertain = uncertain or submitted
            if uncertain and not str(err or "").startswith("write_uncertain:"):
                err = f"write_uncertain:发布已提交，结果需到平台核对；{err}"
            if account_id and not uncertain:
                failure = self.risk.record_failure(
                    account_id, OperationKind.PUBLISH, platform_error)
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if t:
                account_id = t.account_id
                if ok:
                    t.status = "done"
                    t.done_at = datetime.utcnow()
                elif uncertain:
                    # Submission crossed the click/POST boundary but success evidence
                    # was lost.  Never enqueue it again automatically.
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at,
                                    signal=failure.signal)
                else:
                    t.status = "failed"
                t.result_url = url or t.result_url
                t.error = "" if ok else err
                s.add(t); s.commit()
        if ok and account_id:
            self.risk.record_success(account_id, OperationKind.PUBLISH)
        final_hub_status = (
            "completed" if ok
            else "waiting_user" if uncertain
            else "blocked" if (failure and failure.controlled and failure.category in {RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH})
            else "failed"
        )
        self._emit_publish_progress(
            task_id,
            status=final_hub_status,
            stage=("发布成功" if ok else "结果待人工核对" if uncertain else "风控延后，等待重试" if final_hub_status == "blocked" else "发布失败"),
            platform=platform,
            account_id=account_id,
            error="" if ok else err[:400],
            immediate=True,
        )
        if ok:
            log.info("发布任务 #%s(%s, 账号#%s) 已完成%s", task_id, platform, account_id or "-", f": {url}" if url else "")
        else:
            log.warning("发布任务 #%s(%s, 账号#%s) 未完成: %s", task_id, platform, account_id or "-", err[:300])
        if ok:
            try:
                with get_session() as s:
                    chans = s.exec(select(NotificationChannel)
                                   .where(NotificationChannel.enabled == True)).all()  # noqa: E712
                    channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
                if channels:
                    pname = {"kuaishou": "快手", "douyin": "抖音",
                             "shipinhao": "视频号", "tiktok": "TikTok",
                             "bilibili": "哔哩哔哩", "rednote": "海外小红书", "instagram": "Instagram",
                             "twitter": "Twitter", "facebook": "Facebook", "youtube": "YouTube"}.get(
                                 platform, "小红书")
                    await notify_all(channels, f"{pname}发布成功", url or "已发布一条作品")
            except Exception:
                pass
        return {"ok": ok, "url": url, "error": err}
