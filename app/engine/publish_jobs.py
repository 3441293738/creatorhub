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
from app.engine.monitor_helpers import _WRITE_SUBMIT_PREFIX, _loads, _loads_list
from app.models import ContentRecord, DouyinAccount, NotificationChannel, PublishTask
from app.notifier import notify_all
from app.platforms.bilibili import publish_bilibili
from app.platforms.channels import publish_channels
from app.platforms.douyin import publish_douyin
from app.platforms.instagram import publish_instagram
from app.platforms.kuaishou import publish_kuaishou
from app.platforms.rednote import publish_rednote, weighted_truncate
from app.platforms.tiktok import publish_tiktok
from app.platforms.twitter import publish_twitter, trim_to_twitter_limit
from app.platforms.xhs import publish_xhs
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
                 if f.is_file() and not f.name.endswith(".part")]
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
                title_cap = {"douyin": 30, "shipinhao": 16, "bilibili": 80, "instagram": 2200}.get(target_platform, 20)
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

    async def _process_publish(self):
        due = []
        now = datetime.utcnow()
        with get_session() as s:
            tasks = s.exec(select(PublishTask)
                           .where(PublishTask.status == "pending")).all()
            for t in tasks:
                if t.scheduled_at and not t.scheduled_at_is_utc:
                    t.status = "draft"
                    t.error = "旧预约未记录时区，请编辑并确认发布时间后再入队"
                    s.add(t)
                    continue
                if self._task_due(t, now):
                    due.append(t.id)
            s.commit()
        for tid in due:
            await self.publish_task(tid)

    async def publish_task(self, task_id: int) -> dict:
        if task_id in self._publishing:
            return {"ok": False, "error": "正在发布中"}
        self._publishing.add(task_id)
        try:
            with get_session() as s:
                t = s.get(PublishTask, task_id)
                account_id = t.account_id if t else None
            # 发布串行 + 该账号串行(有头浏览器会接管该账号 profile,不能与抓取并发)
            async with self._publish_sem:
                async with self._operation_guard(
                        account_id, OperationKind.PUBLISH,
                        fallback_key=f"pub:{task_id}"):
                    return await self._publish_task_locked(task_id)
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
            location = getattr(t, "location", "") or ""
            platform = t.platform
            files = _loads_list(t.media_json)
            t.status = "publishing"; t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()

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
        else:
            stages.pop(task_id, None)

    async def _run_browser_publish(self, task_id: int, platform: str, publish_fn,
                                     *args, channel: str = "browser",
                                     cancel_message: str = "", **kwargs) -> dict:
        """Execute one platform publisher with uniform submit-boundary & cancel protection."""
        pname = {"kuaishou": "快手", "douyin": "抖音",
                 "shipinhao": "视频号", "tiktok": "TikTok",
                 "bilibili": "哔哩哔哩", "rednote": "海外小红书", "instagram": "Instagram",
                 "twitter": "Twitter",
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
                             "twitter": "Twitter"}.get(
                                 platform, "小红书")
                    await notify_all(channels, f"{pname}发布成功", url or "已发布一条作品")
            except Exception:
                pass
        return {"ok": ok, "url": url, "error": err}
