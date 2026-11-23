"""作品上传、多平台定时/立即发布任务创建与队列管理接口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import json
import uuid as _uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.models import DouyinAccount, PublishTask
from app.platforms.bilibili import VIDEO_MANAGE_URL as BILI_MANAGE_URL
from app.platforms.kuaishou import MANAGE_URL as KS_MANAGE_URL
from app.platforms.rednote import weighted_truncate
from app.platforms.twitter import validate_twitter_publish_files
from app.platforms.xhs import has_creator_cookies
from app.scheduling import utc_iso
from app.services import metadata as metadata_service
from app.submissions import submit_once

router = APIRouter()


# ─────────── 发布(创作平台)+ 跨平台转发 ───────────
UPLOAD_DIR = Path("./data/uploads")


@router.post("/api/publish/upload")
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
    allow_comment: bool = True             # TikTok / B站:允许评论
    allow_duet: bool = True                # TikTok:允许 Duet
    allow_stitch: bool = True              # TikTok:允许 Stitch
    bili_options: dict[str, Any] = PydanticField(default_factory=dict)  # B站投稿扩展参数
    ig_options: dict[str, Any] = PydanticField(default_factory=dict)    # Instagram 发布扩展参数(post_kind/share_to_feed 等)
    tw_options: dict[str, Any] = PydanticField(default_factory=dict)    # Twitter 发布扩展参数(reply_permission/first_comment/long_text 等)
    scheduled_at: str | None = None       # ISO 带偏移；无偏移按账号时区，空=尽快发


class PublishUpdate(BaseModel):
    account_id: int | None = None
    title: str | None = None
    desc: str | None = None
    topics: str | None = None
    location: str | None = None
    visibility: str | None = None
    allow_save: bool | None = None
    allow_comment: bool | None = None
    allow_duet: bool | None = None
    allow_stitch: bool | None = None
    bili_options: dict[str, Any] | None = None
    ig_options: dict[str, Any] | None = None
    tw_options: dict[str, Any] | None = None
    scheduled_at: str | None = None


def _publish_dict(t: PublishTask) -> dict:
    result_url = t.result_url or ""
    if t.platform == "kuaishou" and t.status == "done":
        try:
            result_path = urlsplit(result_url).path.lower()
        except (TypeError, ValueError):
            result_path = ""
        # 旧任务把发布表单地址存成结果地址；列表输出时即时修正，历史记录
        # 无需重新发布即可打开真正的作品管理页。
        if not result_url or "/article/publish/" in result_path:
            result_url = KS_MANAGE_URL
    elif t.platform == "bilibili" and t.status == "done" and not result_url:
        result_url = BILI_MANAGE_URL
    try:
        bili_opts = json.loads(getattr(t, "bili_options_json", "{}") or "{}")
    except Exception:
        bili_opts = {}
    try:
        ig_opts = json.loads(getattr(t, "ig_options_json", "{}") or "{}")
    except Exception:
        ig_opts = {}
    try:
        tw_opts = json.loads(getattr(t, "tw_options_json", "{}") or "{}")
    except Exception:
        tw_opts = {}
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id,
        "media_type": t.media_type, "title": t.title, "desc": t.desc,
        "topics": t.topics, "location": t.location,
        "status": t.status, "result_url": result_url,
        "visibility": t.visibility, "allow_save": t.allow_save,
        "allow_comment": t.allow_comment, "allow_duet": t.allow_duet,
        "allow_stitch": t.allow_stitch,
        "bili_options": bili_opts if isinstance(bili_opts, dict) else {},
        "ig_options": ig_opts if isinstance(ig_opts, dict) else {},
        "tw_options": tw_opts if isinstance(tw_opts, dict) else {},
        "error": t.error, "live_stage": app_runtime.engine.get_publish_stage(t.id) if (app_runtime.engine and hasattr(app_runtime.engine, "get_publish_stage")) else "",
        "media_count": len(json.loads(t.media_json or "[]")),
        "source_platform": t.source_platform, "source_content_id": t.source_content_id,
        "scheduled_at": (utc_iso(t.scheduled_at) if t.scheduled_at_is_utc
                         else t.scheduled_at.isoformat() if t.scheduled_at else None),
        "schedule_needs_confirmation": bool(t.scheduled_at and not t.scheduled_at_is_utc),
        "next_allowed_at": utc_iso(t.next_allowed_at),
        "blocked_reason": t.blocked_reason,
        "created_at": utc_iso(t.created_at),
    }


@router.get("/api/publish")
async def list_publish(platform: str | None = None):
    with get_session() as s:
        q = select(PublishTask)
        if platform:
            q = q.where(PublishTask.platform == platform)
        rows = s.exec(q.order_by(PublishTask.id.desc())).all()
        return [_publish_dict(t) for t in rows]


def _publish_title_cap(platform: str) -> int:
    return {"douyin": 30, "shipinhao": 16, "bilibili": 80, "instagram": 2200, "twitter": 4000}.get(str(platform or "").lower(), 20)


def _validate_non_xhs_publish_files(platform: str, media_type: str,
                                    media_paths: list[str]) -> list[str]:
    paths: list[str] = []
    for raw in media_paths:
        path = Path(raw)
        if path.is_file():
            try:
                if path.stat().st_size <= 0:
                    raise HTTPException(422, f"媒体文件为空，无法发布: {path.name}")
            except OSError as exc:
                raise HTTPException(400, f"无法读取媒体文件: {path.name}") from exc
            paths.append(raw)
    if platform == "twitter":
        checked, tw_err = validate_twitter_publish_files(media_type, paths)
        if tw_err:
            raise HTTPException(422, tw_err)
        return [str(p) for p in checked]
    if not paths:
        raise HTTPException(400, "没有可用的媒体文件,请先上传")
    if platform == "tiktok" and media_type != "video":
        raise HTTPException(422, "TikTok 网页发布当前仅支持视频")
    if platform == "instagram":
        if len(paths) > 10:
            raise HTTPException(422, "Instagram 单次发布最多支持 10 个媒体文件")
        return paths
    if media_type == "video" and not (
        len(paths) == 1 or (platform == "bilibili" and len(paths) == 2)
    ):
        label = "TikTok" if platform == "tiktok" else "视频发布"
        raise HTTPException(422, f"{label} 每个发布任务须选择一个视频")
    if media_type == "images" and len(paths) > 18:
        raise HTTPException(422, "单次图文发布最多支持 18 张图片")
    return paths


@router.post("/api/publish")
async def add_publish(body: PublishIn, request: Request = None):
    with get_session() as s:
        def create():
            if body.media_type not in ("images", "video"):
                raise HTTPException(400, "media_type 须为 images 或 video")
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform not in (
                    "xhs", "rednote", "kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"):
                raise HTTPException(
                    400, "请选择一个已登录的抖音 / 小红书 / 海外小红书 / 快手 / 视频号 / TikTok / 哔哩哔哩 / Instagram / Twitter 账号")
            if str(acc.account_role or "hybrid").strip().lower() == "crawler":
                raise HTTPException(
                    400,
                    "该账号角色设为「仅采集号 (crawler)」，已物理禁止执行作品发布以隔离主号风控；请改选「发布主号 (publish)」或「混合号 (hybrid)」",
                )
            if acc.platform in ("xhs", "rednote"):
                from app.platforms.xhs.media import validate_publish_files
                try:
                    paths = validate_publish_files(body.media_type, body.media_paths)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            else:
                paths = _validate_non_xhs_publish_files(
                    acc.platform, body.media_type, body.media_paths)
                if acc.platform == "twitter" and not paths and not (body.title.strip() or body.desc.strip()):
                    raise HTTPException(400, "Twitter 纯文本发布请至少填写推文内容，或上传 1~4 张图片 / 1 个视频")
            pname = {"kuaishou": "快手", "douyin": "抖音",
                     "shipinhao": "视频号", "tiktok": "TikTok",
                     "bilibili": "哔哩哔哩", "rednote": "海外小红书", "instagram": "Instagram",
                     "twitter": "Twitter"}.get(
                         acc.platform, "小红书")
            if acc.platform == "rednote":
                if not (acc.creator_storage_state or acc.storage_state):
                    raise HTTPException(400, f"该{pname}账号不可发布:请先在账号页完成登录")
            elif acc.platform in ("kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"):
                if not (acc.creator_storage_state or acc.storage_state or acc.cookie):
                    raise HTTPException(400, f"该{pname}账号不可发布:请先在账号页完成登录")
            elif not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
                raise HTTPException(400, "该账号不可发布:请对该号完成「小红书扫码登录」或「创作者登录」")
            vis = body.visibility if body.visibility in ("public", "friends", "private") else "public"
            title_cap = _publish_title_cap(acc.platform)
            norm_title = (
                weighted_truncate(body.title.strip(), 20)
                if acc.platform == "rednote"
                else body.title.strip()[:title_cap]
            )
            t = PublishTask(
                platform=acc.platform, account_id=body.account_id, media_type=body.media_type,
                title=norm_title, desc=body.desc, topics=body.topics,
                location=(body.location or "").strip()[:60],
                visibility=vis, allow_save=bool(body.allow_save),
                allow_comment=bool(body.allow_comment),
                allow_duet=bool(body.allow_duet),
                allow_stitch=bool(body.allow_stitch),
                bili_options_json=json.dumps(body.bili_options or {}, ensure_ascii=False),
                ig_options_json=json.dumps(body.ig_options or {}, ensure_ascii=False),
                tw_options_json=json.dumps(body.tw_options or {}, ensure_ascii=False),
                media_json=json.dumps(paths),
                scheduled_at=metadata_service._parse_when(body.scheduled_at, acc.timezone_id),
            )
            s.add(t); s.flush()
            return _publish_dict(t)
        payload, _ = submit_once(s, request=request, scope="publish", body=body, create=create)
        return payload


@router.put("/api/publish/{tid}")
async def update_publish(tid: int, body: PublishUpdate):
    with get_session() as s:
        t = s.get(PublishTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status not in ("draft", "pending", "failed", "canceled"):
            raise HTTPException(400, f"任务状态为 {t.status},不可编辑")
        if t.scheduled_at and not t.scheduled_at_is_utc and "scheduled_at" not in body.model_fields_set:
            raise HTTPException(422, "请确认预约时间，或明确清空时间选择尽快发布")
        if body.account_id is not None:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != t.platform or acc.status != "active":
                raise HTTPException(400, "发布账号不存在、登录态失效或与任务平台不匹配")
            if str(acc.account_role or "hybrid").strip().lower() == "crawler":
                raise HTTPException(
                    400,
                    "该账号角色设为「仅采集号 (crawler)」，禁止绑定发布任务",
                )
            t.account_id = body.account_id
        if body.title is not None:
            t.title = (
                weighted_truncate(body.title.strip(), 20)
                if t.platform == "rednote"
                else body.title.strip()[:_publish_title_cap(t.platform)]
            )
        if body.desc is not None:
            t.desc = body.desc
        if body.topics is not None:
            t.topics = body.topics.strip()
        if body.location is not None:
            t.location = body.location.strip()[:60]
        if body.visibility is not None:
            if body.visibility not in ("public", "friends", "private"):
                raise HTTPException(400, "可见范围须为 public、friends 或 private")
            t.visibility = body.visibility
        if body.allow_save is not None:
            t.allow_save = body.allow_save
        if body.allow_comment is not None:
            t.allow_comment = body.allow_comment
        if body.allow_duet is not None:
            t.allow_duet = body.allow_duet
        if body.allow_stitch is not None:
            t.allow_stitch = body.allow_stitch
        if body.bili_options is not None:
            t.bili_options_json = json.dumps(body.bili_options, ensure_ascii=False)
        if body.ig_options is not None:
            t.ig_options_json = json.dumps(body.ig_options, ensure_ascii=False)
        if body.tw_options is not None:
            t.tw_options_json = json.dumps(body.tw_options, ensure_ascii=False)
        if "scheduled_at" in body.model_fields_set:
            account = s.get(DouyinAccount, t.account_id) if t.account_id else None
            t.scheduled_at = metadata_service._parse_when(body.scheduled_at, account.timezone_id if account else "Asia/Shanghai")
            t.scheduled_at_is_utc = True
        if t.status in ("draft", "failed", "canceled"):
            t.status = "pending"
            t.error = ""
        s.add(t); s.commit(); s.refresh(t)
        return _publish_dict(t)


@router.post("/api/publish/{tid}/run-now")
async def run_publish(tid: int):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.publish_task(tid)


@router.delete("/api/publish/{tid}")
async def del_publish(tid: int):
    with get_session() as s:
        t = s.get(PublishTask, tid)
        if t:
            if t.status in ("publishing", "uncertain"):
                raise HTTPException(409, "任务正在执行或结果待核对，请先确认平台结果")
            s.delete(t); s.commit()
    return {"ok": True}
