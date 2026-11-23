"""关键词与创作者采集输入校验、目标源解析及媒体结果序列化。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func
from sqlmodel import select

from app import runtime as app_runtime
from app.models import KeywordCollectionComment, KeywordCollectionContent, KeywordCollectionJob
from app.platforms.bilibili import resolve_bili_mid
from app.platforms.channels import resolve_channels_creator
from app.platforms.instagram import looks_like_ig_work, resolve_ig_user_target
from app.platforms.kuaishou import resolve_ks_user_id
from app.platforms.rednote import build_rednote_note_url, resolve_rednote_user
from app.platforms.twitter import looks_like_twitter_tweet, resolve_twitter_short_url, resolve_twitter_user_target
from app.risk import RiskController
from app.services import metadata as metadata_service
from app.services import thumb_cache


# ─────────── 抖音 / 小红书 / 快手 / 视频号批量采集 ───────────
class KeywordCollectionIn(BaseModel):
    platform: str = "douyin"
    account_id: int | None = None
    source_type: str = "keyword"
    source_ref: str = ""
    source_name: str = ""
    keywords: list[str] = PydanticField(default_factory=list)
    max_contents_per_keyword: int = 20
    max_pages_per_keyword: int = 12
    stagnant_pages: int = 3
    search_sort: str = "general"
    publish_time: str = "all"
    content_type: str = "all"
    min_likes: int = 0
    min_comments: int = 0
    max_comments_per_content: int = 20
    include_replies: bool = False
    download_media: bool = False
    video_quality: str = "highest"
    download_dir: str = ""


def _validated_collection_input(body: KeywordCollectionIn) \
        -> tuple[str, list[str], str, str, dict]:
    """校验创建/编辑共用的任务配置并返回规范化值。"""
    platform = body.platform.strip().lower()
    if platform not in {"douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"}:
        raise HTTPException(400, "批量采集仅支持抖音、小红书、海外小红书、快手、视频号、TikTok、哔哩哔哩、Instagram 或 Twitter")
    source_type = body.source_type.strip().lower() or "keyword"
    if source_type not in {
            "keyword", "favorite_all", "favorite_board", "creator",
            "account_works"}:
        raise HTTPException(400, "采集来源须为关键词、创作者主页、全部收藏、指定收藏夹或本账号作品")
    if platform not in {"shipinhao", "bilibili", "instagram", "twitter"} and source_type == "account_works":
        raise HTTPException(400, "本账号作品来源仅支持视频号、哔哩哔哩、Instagram 或 Twitter")
    if platform == "tiktok" and source_type != "keyword":
        raise HTTPException(400, "TikTok 首版批量采集仅支持关键词视频搜索")
    if source_type in {"favorite_all", "favorite_board"} and platform not in {"xhs", "rednote"}:
        raise HTTPException(400, "收藏来源仅支持小红书或海外小红书")
    source_ref = body.source_ref.strip()
    source_name = body.source_name.strip()
    if source_type == "account_works":
        source_ref = ""
        source_name = "本账号作品"
    if source_type == "favorite_board" and not source_ref:
        raise HTTPException(400, "指定收藏夹采集必须提供收藏夹 ID")
    if source_type == "creator" and not source_ref:
        raise HTTPException(400, "创作者主页采集必须提供用户 ID")
    if len(source_ref) > 200 or len(source_name) > 120:
        raise HTTPException(400, "采集来源标识或名称过长")
    keywords = _collection_keywords(body.keywords)
    if source_type == "keyword":
        if not keywords:
            raise HTTPException(400, "请至少填写一个关键词")
        if len(keywords) > 20:
            raise HTTPException(400, "单个任务最多包含 20 个关键词")
        if any(len(value) > 80 for value in keywords):
            raise HTTPException(400, "单个关键词不能超过 80 个字符")
    else:
        keywords = []
        source_name = source_name or ("全部收藏" if source_type == "favorite_all" else
                                      "收藏夹" if source_type == "favorite_board" else
                                      "本账号作品" if source_type == "account_works" else
                                      f"创作者 {source_ref}")
    if not 1 <= body.max_contents_per_keyword <= 100:
        raise HTTPException(400, "每个关键词作品数须为 1~100")
    if not 1 <= body.max_pages_per_keyword <= 40:
        raise HTTPException(400, "每个关键词采集深度须为 1~40 页")
    if not 1 <= body.stagnant_pages <= 8:
        raise HTTPException(400, "连续无新结果停止阈值须为 1~8 页")
    search_sort = body.search_sort.strip().lower()
    if search_sort not in {"general", "latest", "most_liked"}:
        raise HTTPException(400, "搜索排序须为综合、最新发布或最多点赞")
    publish_time = body.publish_time.strip().lower()
    if publish_time not in {"all", "day", "week", "half_year"}:
        raise HTTPException(400, "发布时间筛选值无效")
    content_type = body.content_type.strip().lower()
    if content_type not in {"all", "video", "images"}:
        raise HTTPException(400, "内容类型筛选值无效")
    if not 0 <= body.min_likes <= 2_000_000_000:
        raise HTTPException(400, "最低点赞数须为非负整数")
    if not 0 <= body.min_comments <= 2_000_000_000:
        raise HTTPException(400, "最低评论数须为非负整数")
    if not 0 <= body.max_comments_per_content <= 200:
        raise HTTPException(400, "每个作品评论数须为 0~200")
    quality = body.video_quality.strip() or "highest"
    if quality not in metadata_service.QUALITY_CHOICES:
        raise HTTPException(400, f"画质取值无效: {quality}")
    download_dir = body.download_dir.strip()
    if download_dir:
        try:
            Path(download_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(400, f"下载目录不可用: {exc}")
    options = {
        "source_type": source_type,
        "source_ref": source_ref,
        "source_name": source_name,
        "max_pages_per_keyword": body.max_pages_per_keyword,
        "stagnant_pages": body.stagnant_pages,
        "search_sort": search_sort,
        "publish_time": publish_time,
        "content_type": content_type,
        "min_likes": body.min_likes,
        "min_comments": body.min_comments,
    }
    return platform, keywords, quality, download_dir, options


async def _resolve_ks_creator_source(body: KeywordCollectionIn) -> None:
    """把快手创作者采集的 source_ref 解析为可直开主页的 3x user_id。"""
    if (body.platform.strip().lower() == "kuaishou"
            and body.source_type.strip().lower() == "creator"):
        original_ref = body.source_ref.strip()
        eff_proxy = metadata_service._resolve_ephemeral_proxy("kuaishou", body.account_id)
        resolved = await metadata_service._call_resolver(
            resolve_ks_user_id, original_ref, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
        )
        if not resolved:
            raise HTTPException(
                400, "无法解析快手创作者主页，请粘贴完整分享文案、"
                     "创作者主页链接 / v.kuaishou.com 短链 / user_id")
        body.source_ref = resolved
        if not body.source_name or body.source_name == f"创作者 {original_ref}":
            body.source_name = f"创作者 {resolved}"


async def _resolve_channels_creator_target(raw_input: str):
    """把视频号主页链接、finderUsername 或单条作品分享链接解析为创作者信息。"""
    cleaned = metadata_service._clean_platform_target_input(raw_input, "shipinhao")
    res = await resolve_channels_creator(cleaned, app_runtime.cfg.engine.user_agent, cfg=app_runtime.cfg)
    if not res.username:
        raise HTTPException(
            400,
            res.error or (
                "无法解析视频号创作者标识，请粘贴创作者主页链接、"
                "作品分享链接(weixin.qq.com/sph/...)、finderUsername(v2_...@finder) 或视频号 sph 标识"
            ),
        )
    return res


async def _resolve_channels_creator_source(body: KeywordCollectionIn) -> None:
    """把视频号创作者采集的 source_ref 规范化为 finderUsername(v2_...@finder) 或视频号标识。"""
    if (body.platform.strip().lower() == "shipinhao"
            and body.source_type.strip().lower() == "creator"):
        original_ref = body.source_ref.strip()
        res = await _resolve_channels_creator_target(original_ref)
        body.source_ref = res.username
        if not body.source_name or body.source_name == f"创作者 {original_ref}":
            body.source_name = (
                f"创作者 {res.nickname} ({res.username})"
                if res.nickname
                else f"创作者 {res.username}"
            )


async def _resolve_bili_creator_source(body: KeywordCollectionIn) -> None:
    """把哔哩哔哩创作者采集的 source_ref 解析为数字 mid。"""
    if (body.platform.strip().lower() == "bilibili"
            and body.source_type.strip().lower() == "creator"):
        original_ref = body.source_ref.strip()
        cleaned = metadata_service._clean_platform_target_input(original_ref, "bilibili")
        eff_proxy = metadata_service._resolve_ephemeral_proxy("bilibili", body.account_id)
        mid = await metadata_service._call_resolver(
            resolve_bili_mid, cleaned, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
        )
        if not mid:
            raise HTTPException(
                400,
                "无法解析哔哩哔哩 UP 主标识，请填写数字 mid、space.bilibili.com 主页链接或视频分享链接",
            )
        body.source_ref = mid
        if not body.source_name or body.source_name == f"创作者 {original_ref}":
            body.source_name = f"创作者 {mid}"


async def _resolve_rednote_creator_source(body: KeywordCollectionIn) -> None:
    """把海外小红书创作者采集的 source_ref 解析为 24 位 user_id。"""
    if (body.platform.strip().lower() == "rednote"
            and body.source_type.strip().lower() == "creator"):
        original_ref = body.source_ref.strip()
        cleaned = metadata_service._clean_platform_target_input(original_ref, "rednote")
        eff_proxy = metadata_service._resolve_ephemeral_proxy("rednote", body.account_id)
        ref = await metadata_service._call_resolver(
            resolve_rednote_user, cleaned, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
        )
        if not ref or not ref.user_id:
            raise HTTPException(
                400,
                "无法解析海外小红书创作者 user_id，请填写 www.rednote.com/zh/user/profile/... 主页链接或 24 位 user_id",
            )
        body.source_ref = ref.user_id
        if not body.source_name or body.source_name == f"创作者 {original_ref}":
            body.source_name = f"创作者 {ref.user_id}"


async def _resolve_ig_creator_source(body: KeywordCollectionIn) -> None:
    """把 Instagram 创作者采集的 source_ref 规范化为 @username 或数字 pk。"""
    if (body.platform.strip().lower() == "instagram"
            and body.source_type.strip().lower() == "creator"):
        original_ref = body.source_ref.strip()
        cleaned = metadata_service._clean_platform_target_input(original_ref, "instagram")
        if looks_like_ig_work(cleaned):
            raise HTTPException(
                400,
                "检测到 Instagram 单帖/Reels 链接；创作者采集请填写 @username、主页链接或数字 UID",
            )
        target = resolve_ig_user_target(cleaned)
        if not target:
            raise HTTPException(
                400,
                "无法解析 Instagram 创作者标识，请填写 @username、instagram.com/<username> 主页链接或数字 UID",
            )
        body.source_ref = target
        if not body.source_name or body.source_name == f"创作者 {original_ref}":
            body.source_name = f"创作者 @{target}" if not target.isdigit() else f"创作者 {target}"


async def _resolve_twitter_creator_source(body: KeywordCollectionIn) -> None:
    """把 Twitter 创作者采集的 source_ref 规范化为 @screen_name 或数字 rest_id。"""
    if (
        body.platform.strip().lower() == "twitter"
        and body.source_type.strip().lower() == "creator"
    ):
        original_ref = body.source_ref.strip()
        cleaned = metadata_service._clean_platform_target_input(original_ref, "twitter")
        eff_proxy = metadata_service._resolve_ephemeral_proxy("twitter", body.account_id)
        expanded = await metadata_service._call_resolver(
            resolve_twitter_short_url,
            cleaned,
            user_agent=app_runtime.cfg.engine.user_agent,
            proxy=eff_proxy,
        )
        if looks_like_twitter_tweet(expanded):
            raise HTTPException(
                400,
                "检测到 X (Twitter) 单条推文链接；创作者采集请填写 @username、x.com/<username> 主页链接或数字 UID",
            )
        target = resolve_twitter_user_target(expanded)
        if not target:
            raise HTTPException(
                400,
                "无法解析 X (Twitter) 创作者标识，请填写 @username、x.com/<username> 主页链接或数字 UID",
            )
        body.source_ref = target
        if not body.source_name or body.source_name == f"创作者 {original_ref}":
            body.source_name = f"创作者 @{target}" if not target.isdigit() else f"创作者 {target}"


def _collection_keywords(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        # API 调用者也可把多行或逗号分隔内容放进一个数组项。
        expanded = str(raw or "").replace("，", ",").replace("\r", "\n")
        for line in expanded.replace("\n", ",").split(","):
            value = line.strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                out.append(value)
    return out


def _collection_error_for_display(value: str) -> str:
    """把采集异常压缩成适合页面展示的短文案，原始值仍保留供诊断/导出。"""
    output = []
    for raw_line in str(value or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        lowered = line.lower()
        if "targetclosederror" in lowered or "has been closed" in lowered:
            keyword = line.split(":", 1)[0].strip()
            prefix = f"{keyword}：" if keyword else ""
            line = f"{prefix}采集窗口已关闭，请点击“续跑”并保持窗口开启"
        elif len(line) > 220:
            line = line[:219].rstrip() + "…"
        output.append(line)
    return "\n".join(output[-8:])


def _collection_job_dict(job: KeywordCollectionJob) -> dict:
    try:
        keywords = json.loads(job.keywords or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        keywords = []
    source_type = str(job.source_type or "keyword")
    total_keywords = len(keywords) if source_type == "keyword" else 1
    planned = total_keywords * max(0, job.max_contents_per_keyword)
    processed_keywords = min(
        total_keywords, max(0, int(job.processed_keyword_count or 0)))
    current_item_total = max(0, int(job.current_item_total or 0))
    current_item_index = min(
        current_item_total, max(0, int(job.current_item_index or 0)))
    if job.status == "done":
        progress_percent = 100
    elif total_keywords:
        current_fraction = (
            current_item_index / current_item_total
            if current_item_total and processed_keywords < total_keywords
            else 0
        )
        progress_percent = min(
            100, round((processed_keywords + current_fraction)
                       * 100 / total_keywords))
    else:
        progress_percent = 0
    return {
        "id": job.id, "platform": job.platform, "account_id": job.account_id,
        "source_type": source_type, "source_ref": job.source_ref,
        "source_name": job.source_name,
        "keywords": keywords,
        "max_contents_per_keyword": job.max_contents_per_keyword,
        "max_pages_per_keyword": job.max_pages_per_keyword,
        "stagnant_pages": job.stagnant_pages,
        "search_sort": job.search_sort,
        "publish_time": job.publish_time,
        "content_type": job.content_type,
        "min_likes": job.min_likes,
        "min_comments": job.min_comments,
        "max_comments_per_content": job.max_comments_per_content,
        "include_replies": job.include_replies,
        "download_media": job.download_media,
        "video_quality": job.video_quality,
        "download_dir": job.download_dir,
        "status": job.status, "current_keyword": job.current_keyword,
        "current_step": job.current_step,
        "processed_keyword_count": processed_keywords,
        "total_keyword_count": total_keywords,
        "current_item_index": current_item_index,
        "current_item_total": current_item_total,
        "progress_percent": progress_percent,
        "content_count": job.content_count, "comment_count": job.comment_count,
        "planned_content_count": planned, "error_count": job.error_count,
        "error": _collection_error_for_display(job.error),
        "error_detail": job.error,
        "blocked_signal": job.blocked_signal or "",
        "manual_review_required": bool(
            job.status == "pending"
            and (
                job.blocked_signal == "manual_review_required"
                or "风控中心" in str(job.current_step or "")
                or (
                    job.blocked_reason
                    and RiskController._needs_manual_review(
                        job.blocked_reason, job.blocked_signal or ""
                    )
                )
            )
        ),
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


_COLLECTION_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}


_COLLECTION_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


_COLLECTION_MEDIA_EXTS = _COLLECTION_IMAGE_EXTS | _COLLECTION_VIDEO_EXTS


def _collection_remote_medias(row: KeywordCollectionContent) -> list[dict]:
    """Return normalized remote media records saved by the collector."""
    try:
        raw_items = json.loads(row.media_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_items = []
    if not isinstance(raw_items, list):
        return []
    medias = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        ext = str(item.get("ext") or "").strip().lower().lstrip(".")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"image", "video"}:
            kind = "image" if f".{ext}" in _COLLECTION_IMAGE_EXTS else "video"
        entry = {"url": url, "kind": kind, "ext": ext}
        if str(item.get("decode_key") or "").strip():
            entry["encrypted"] = True
        medias.append(entry)
    return medias


def _collection_local_path(row: KeywordCollectionContent) -> Path | None:
    if not row.local_path:
        return None
    try:
        path = Path(row.local_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() or path.is_dir() else None


def _collection_media_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"_(\d+)\.[^.]+$", path.name)
    return (int(match.group(1)) if match else -1, path.name.casefold())


def _collection_local_media_paths(row: KeywordCollectionContent) -> list[Path]:
    """Resolve downloaded media without mixing files from another work."""
    path = _collection_local_path(row)
    if not path:
        return []
    if path.is_file():
        try:
            return [path] if path.suffix.lower() in _COLLECTION_MEDIA_EXTS and path.stat().st_size > 0 else []
        except OSError:
            return []
    prefix = f"{row.aweme_id}_"
    try:
        candidates = [
            child for child in path.iterdir()
            if child.is_file() and child.name.startswith(prefix)
            and ".cover." not in child.name.lower()
            and child.suffix.lower() in _COLLECTION_MEDIA_EXTS
            and child.stat().st_size > 0
        ]
    except OSError:
        return []
    return sorted(candidates, key=_collection_media_sort_key)


def _collection_content_dict(row: KeywordCollectionContent) -> dict:
    if row.platform == "xhs":
        query = urlencode({
            "xsec_token": row.xsec_token,
            "xsec_source": row.xsec_source or "pc_search",
        }) if row.xsec_token else ""
        url = f"https://www.xiaohongshu.com/explore/{row.aweme_id}"
        if query:
            url += "?" + query
    elif row.platform == "rednote":
        url = row.source_url or build_rednote_note_url(
            row.aweme_id,
            xsec_token=row.xsec_token or "",
            xsec_source=row.xsec_source or "pc_search",
        )
    elif row.platform == "kuaishou":
        url = f"https://www.kuaishou.com/short-video/{row.aweme_id}"
    elif row.platform == "shipinhao":
        url = row.source_url or "https://channels.weixin.qq.com/platform/post/list"
    elif row.platform == "tiktok":
        url = row.source_url or ""
    elif row.platform == "bilibili":
        aid_str = str(row.aweme_id or "")
        url = row.source_url or (
            f"https://www.bilibili.com/opus/{aid_str}"
            if aid_str.isdigit() and len(aid_str) >= 15
            else f"https://www.bilibili.com/video/{aid_str}"
        )
    elif row.platform == "instagram":
        url = row.source_url or f"https://www.instagram.com/p/{row.aweme_id}/"
    elif row.platform == "twitter":
        url = row.source_url or f"https://x.com/i/web/status/{row.aweme_id}"
    else:
        url = f"https://www.douyin.com/video/{row.aweme_id}"
    local_files = _collection_local_media_paths(row)
    local_cover = thumb_cache.find_work_local_cover(row.local_path, row.aweme_id)
    remote_medias = _collection_remote_medias(row)
    try:
        file_size = sum(path.stat().st_size for path in local_files)
    except OSError:
        file_size = 0
    has_cover = bool(local_cover or row.cover_url)
    cover_api_url = (
        f"/api/collections/{row.job_id}/contents/{row.id}/cover"
        if (has_cover and row.id)
        else thumb_cache.local_thumb_api_url(row.cover_url, platform=row.platform)
    )
    return {
        "id": row.id, "job_id": row.job_id, "platform": row.platform,
        "keyword": row.keyword, "aweme_id": row.aweme_id, "desc": row.desc,
        "author_name": row.author_name, "author_id": row.author_id,
        "media_type": row.media_type, "create_time": row.create_time,
        "cover_url": cover_api_url,
        "remote_cover_url": row.cover_url or "",
        "like_count": row.like_count,
        "comment_count": row.comment_count,
        "collected_comment_count": row.collected_comment_count,
        "download_status": row.download_status, "local_path": row.local_path,
        "xsec_token": row.xsec_token, "xsec_source": row.xsec_source,
        "error": row.error, "url": url,
        "local_exists": bool(local_files),
        "media_count": len(local_files) or len(remote_medias),
        "file_size": file_size,
        "preview_available": bool(local_files or remote_medias or has_cover),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _collection_comment_dict(row: KeywordCollectionComment, content=None) -> dict:
    payload = {
        "id": row.id, "job_id": row.job_id, "content_id": row.content_id,
        "aweme_id": row.aweme_id, "comment_id": row.comment_id,
        "text": row.text, "user_nickname": row.user_nickname,
        "user_sec_uid": row.user_sec_uid, "ip_label": row.ip_label,
        "like_count": row.like_count, "create_time": row.create_time,
        "reply_to": row.reply_to,
    }
    if content is not None:
        payload["source_keyword"] = content.keyword or ""
        payload["source_desc"] = content.desc or ""
    else:
        payload["source_keyword"] = ""
        payload["source_desc"] = ""
    return payload


def _collection_comment_query(session, job_id: int, *, content_id: int | None = None,
                              q: str = "", ip_label: str = "",
                              user_sec_uid: str = "", dedupe_users: bool = False):
    """Build the shared comment query used by the modal and Excel export."""
    job = session.get(KeywordCollectionJob, job_id)
    if not job:
        raise HTTPException(404, "采集任务不存在")
    conditions = [KeywordCollectionComment.job_id == job_id]
    if content_id is not None:
        content = session.get(KeywordCollectionContent, content_id)
        if not content or content.job_id != job_id:
            raise HTTPException(404, "采集作品不存在")
        conditions.append(KeywordCollectionComment.content_id == content_id)
    text_query = (q or "").strip()[:200]
    region_query = (ip_label or "").strip()[:64]
    sec_uid_query = (user_sec_uid or "").strip()[:128]
    if text_query:
        conditions.append(KeywordCollectionComment.text.contains(text_query))
    if region_query:
        conditions.append(KeywordCollectionComment.ip_label.contains(region_query))
    if sec_uid_query:
        conditions.append(KeywordCollectionComment.user_sec_uid.contains(sec_uid_query))
    if dedupe_users:
        conditions.append(func.coalesce(KeywordCollectionComment.user_sec_uid, "") != "")
        ranked = select(
            KeywordCollectionComment.id.label("comment_id"),
            func.row_number().over(
                partition_by=KeywordCollectionComment.user_sec_uid,
                order_by=(KeywordCollectionComment.create_time.desc(),
                          KeywordCollectionComment.id.desc()),
            ).label("user_rank"),
        ).where(*conditions).subquery()
        statement = (select(KeywordCollectionComment)
                     .join(ranked, KeywordCollectionComment.id == ranked.c.comment_id)
                     .where(ranked.c.user_rank == 1))
    else:
        statement = select(KeywordCollectionComment).where(*conditions)
    return job, statement


def _collection_comment_payloads(session, rows):
    content_ids = {row.content_id for row in rows if row.content_id is not None}
    content_map = {}
    if content_ids:
        content_map = {
            row.id: row for row in session.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.id.in_(content_ids))
            ).all()
        }
    return [_collection_comment_dict(row, content_map.get(row.content_id)) for row in rows]
