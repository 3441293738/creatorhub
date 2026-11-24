"""Facebook 数据提取与归一化模块：统一处理 Comet GraphQL NDJSON、SSR HTML 与 DOM 提取结果。

支持归一化：
- 帖子/视频/Reels (`Aweme`)：图文（单图/多图相册）、视频/Reels（HD/SD MP4 直链）与纯文本帖
- 本账号作品 (`AccountWork` 字典)
- 帖子评论与嵌套回复 (`CommentRecord` / `KeywordCollectionComment`)
- 个人资料、公共主页、群组与好友/关注列表结构归一化
- Messenger 私信会话与历史消息 (`DmConversation` / `DmMessage`)
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import html
import json
import re
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .protocol import parse_members_count
from .resolve import normalize_facebook_url, resolve_facebook_post_ref
from .urls import FACEBOOK_BASE, post_url

if TYPE_CHECKING:
    from ..douyin.extract import Aweme, MediaItem

_IS_PROFILE_PLUS_RE = re.compile(
    r'\{"is_profile_plus"\s*:\s*true\s*,\s*"id"\s*:\s*"(\d{4,25})"\s*,\s*"name"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)


def _to_int(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return max(0, int(val))
    return parse_members_count(str(val))


def parse_facebook_timestamp(val: Any) -> int:
    """将 Facebook 秒/毫秒时间戳或 ISO8601 字符串统一转换为秒级 Unix 时间戳。"""
    if val is None or val == "":
        return 0
    if isinstance(val, (int, float)):
        n = int(val)
        return n // 1000 if n > 100_000_000_000 else max(0, n)
    s = str(val).strip()
    if not s:
        return 0
    if s.isdigit():
        n = int(s)
        return n // 1000 if n > 100_000_000_000 else max(0, n)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return max(0, int(dt.timestamp()))
    except Exception:
        return 0


def parse_ndjson_payloads(text: str) -> List[Any]:
    """解析 Facebook `/api/graphql` 常见的多行 NDJSON 或 `for (;;);` 前缀 JSON 响应。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    if raw.startswith("for (;;);"):
        raw = raw[len("for (;;);"):].strip()
    try:
        return [json.loads(raw)]
    except Exception:
        pass
    out: List[Any] = []
    for line in raw.splitlines():
        chunk = line.strip()
        if not chunk:
            continue
        if chunk.startswith("for (;;);"):
            chunk = chunk[len("for (;;);"):].strip()
        if not chunk.startswith(("{", "[")):
            continue
        try:
            out.append(json.loads(chunk))
        except Exception:
            continue
    return out


def _clean_fb_url(u: Any) -> str:
    s = str(u or "").strip()
    if not s:
        return ""
    s = s.replace("\\/", "/")
    if s.startswith("//"):
        s = "https:" + s
    if s.startswith("/"):
        s = FACEBOOK_BASE + s
    return html.unescape(s)


def _decode_json_str(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw


def _pick_uri(node: Any) -> str:
    if isinstance(node, str):
        return _clean_fb_url(node)
    if isinstance(node, Mapping):
        for k in ("uri", "url", "src", "href", "image_uri"):
            val = node.get(k)
            if isinstance(val, str) and val.strip():
                return _clean_fb_url(val)
            if isinstance(val, Mapping) and isinstance(val.get("uri"), str):
                return _clean_fb_url(val["uri"])
    return ""


def _extract_actor(node: Mapping[str, Any]) -> Dict[str, str]:
    actors = node.get("actors")
    actor: Optional[Mapping[str, Any]] = None
    if isinstance(actors, list) and actors and isinstance(actors[0], Mapping):
        actor = actors[0]
    for k in ("author", "actor", "owner", "owning_profile", "user"):
        if actor is None and isinstance(node.get(k), Mapping):
            actor = node[k]
    if actor is None:
        cs = node.get("comet_sections")
        if isinstance(cs, Mapping):
            candidate_stories: List[Mapping[str, Any]] = []
            ctx_layout = cs.get("context_layout")
            if isinstance(ctx_layout, Mapping) and isinstance(ctx_layout.get("story"), Mapping):
                ctx_st = ctx_layout["story"]
                candidate_stories.append(ctx_st)
                ctx_cs = ctx_st.get("comet_sections")
                if isinstance(ctx_cs, Mapping):
                    for subk in ("actor_photo", "title"):
                        sub = ctx_cs.get(subk)
                        if isinstance(sub, Mapping) and isinstance(sub.get("story"), Mapping):
                            candidate_stories.append(sub["story"])
            content = cs.get("content")
            if isinstance(content, Mapping) and isinstance(content.get("story"), Mapping):
                candidate_stories.append(content["story"])
            for st in candidate_stories:
                acts = st.get("actors")
                if isinstance(acts, list) and acts and isinstance(acts[0], Mapping):
                    actor = acts[0]
                    break
    if not isinstance(actor, Mapping):
        return {"uid": "", "unique_id": "", "name": "", "avatar": "", "url": ""}
    uid = str(actor.get("id") or actor.get("uid") or actor.get("user_id") or "").strip()
    name = str(actor.get("name") or actor.get("nickname") or actor.get("short_name") or "").strip()
    url = _clean_fb_url(actor.get("url") or actor.get("profile_url") or "")
    avatar = _pick_uri(actor.get("profile_picture") or actor.get("avatar") or actor.get("profile_pic"))
    unique_id = str(actor.get("vanity") or actor.get("username") or "").strip()
    if not unique_id and url:
        m = re.search(r"facebook\.com/([A-Za-z0-9._-]+)(?:[/?#]|$)", url)
        if m and m.group(1).lower() not in {"profile.php", "people", "groups", "pages"}:
            unique_id = m.group(1)
    return {
        "uid": uid,
        "unique_id": unique_id or uid,
        "name": name,
        "avatar": avatar,
        "url": url,
    }


def _extract_story_message(node: Mapping[str, Any]) -> str:
    msg = node.get("message")
    if isinstance(msg, Mapping) and msg.get("text"):
        return str(msg["text"]).strip()
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    for k in ("desc", "text", "caption", "title", "body"):
        v = node.get(k)
        if isinstance(v, Mapping) and v.get("text"):
            return str(v["text"]).strip()
        if isinstance(v, str) and v.strip():
            return v.strip()

    def _msg_from_story(st: Mapping[str, Any]) -> str:
        m = st.get("message")
        if isinstance(m, Mapping) and m.get("text"):
            return str(m["text"]).strip()
        if isinstance(m, str) and m.strip():
            return m.strip()
        cm = st.get("comet_sections")
        if isinstance(cm, Mapping):
            for mk in ("message", "message_container", "attached_story"):
                mnode = cm.get(mk)
                if not isinstance(mnode, Mapping):
                    continue
                st2 = mnode.get("story")
                if isinstance(st2, Mapping):
                    txt = _msg_from_story(st2)
                    if txt:
                        return txt
        att_st = st.get("attached_story")
        if isinstance(att_st, Mapping):
            txt = _msg_from_story(att_st)
            if txt:
                return txt
        return ""

    cs = node.get("comet_sections")
    if isinstance(cs, Mapping):
        for sec_key in ("content", "message", "message_container"):
            sec = cs.get(sec_key)
            if isinstance(sec, Mapping) and isinstance(sec.get("story"), Mapping):
                txt = _msg_from_story(sec["story"])
                if txt:
                    return txt
    att_root = node.get("attached_story")
    if isinstance(att_root, Mapping):
        txt = _msg_from_story(att_root)
        if txt:
            return txt
    return ""


def _extract_permalink_and_time(node: Mapping[str, Any]) -> Tuple[str, int, str]:
    """从 Comet Story 多层 `comet_sections` 提取 `(source_url, create_time, post_id)`。"""
    source_url = _clean_fb_url(
        node.get("source_url")
        or node.get("permalink_url")
        or node.get("wwwURL")
        or node.get("url")
        or ""
    )
    create_time = parse_facebook_timestamp(
        node.get("create_time")
        or node.get("creation_time")
        or node.get("publish_time")
        or node.get("created_time")
        or 0
    )
    post_id = str(
        node.get("post_id")
        or node.get("aweme_id")
        or node.get("legacy_fbid")
        or node.get("story_fbid")
        or ""
    ).strip()

    cs = node.get("comet_sections")
    if isinstance(cs, Mapping):
        # 1) context_layout.story.comet_sections.metadata[*].story.{url, creation_time}
        ctx = cs.get("context_layout")
        if isinstance(ctx, Mapping) and isinstance(ctx.get("story"), Mapping):
            ctx_st = ctx["story"]
            if not source_url and ctx_st.get("url"):
                source_url = _clean_fb_url(ctx_st["url"])
            if not create_time and ctx_st.get("creation_time"):
                create_time = parse_facebook_timestamp(ctx_st["creation_time"])
            if not post_id and ctx_st.get("post_id"):
                post_id = str(ctx_st["post_id"]).strip()
            ctx_cs = ctx_st.get("comet_sections")
            if isinstance(ctx_cs, Mapping):
                meta_list = ctx_cs.get("metadata")
                if isinstance(meta_list, list):
                    for item in meta_list:
                        if not isinstance(item, Mapping):
                            continue
                        mst = item.get("story")
                        if not isinstance(mst, Mapping):
                            continue
                        if not source_url and mst.get("url"):
                            source_url = _clean_fb_url(mst["url"])
                        if not create_time and mst.get("creation_time"):
                            create_time = parse_facebook_timestamp(mst["creation_time"])
                        if not post_id and mst.get("post_id"):
                            post_id = str(mst["post_id"]).strip()

        # 2) timestamp.story / content.story / feedback.story
        for sec_k in ("timestamp", "content", "feedback"):
            sec = cs.get(sec_k)
            if not isinstance(sec, Mapping):
                continue
            st = sec.get("story")
            if not isinstance(st, Mapping):
                continue
            if not source_url:
                source_url = _clean_fb_url(st.get("permalink_url") or st.get("wwwURL") or st.get("url") or "")
            if not create_time:
                create_time = parse_facebook_timestamp(st.get("creation_time") or st.get("publish_time") or 0)
            if not post_id:
                post_id = str(st.get("post_id") or st.get("legacy_fbid") or "").strip()
            ufi_wrap = st.get("story_ufi_container")
            if isinstance(ufi_wrap, Mapping) and isinstance(ufi_wrap.get("story"), Mapping):
                ust = ufi_wrap["story"]
                if not source_url and ust.get("url"):
                    source_url = _clean_fb_url(ust["url"])
                if not post_id and ust.get("post_id"):
                    post_id = str(ust["post_id"]).strip()

    return source_url, create_time, post_id


def _extract_media_from_story(node: Mapping[str, Any]) -> Tuple[str, List[MediaItem], str, int, str]:
    """从 Facebook 帖子节点提取视频/图片列表，返回 `(media_type, medias, cover, duration, quality_label)`。"""
    from ..douyin.extract import MediaItem

    medias: List[MediaItem] = []
    cover = ""
    duration = 0
    quality_label = ""
    seen_urls: set[str] = set()

    def _add_video(vobj: Mapping[str, Any]) -> None:
        nonlocal cover, duration, quality_label
        hd = _clean_fb_url(
            vobj.get("browser_native_hd_url")
            or vobj.get("playable_url_quality_hd")
            or vobj.get("hd_src")
            or vobj.get("hd_src_no_ratelimit")
            or ""
        )
        sd = _clean_fb_url(
            vobj.get("browser_native_sd_url")
            or vobj.get("playable_url")
            or vobj.get("sd_src")
            or vobj.get("sd_src_no_ratelimit")
            or vobj.get("video_url")
            or ""
        )
        # 兼容 progressive_urls / videoDeliveryResponseFragment
        if not hd and not sd:
            deliv = vobj.get("videoDeliveryResponseFragment")
            if isinstance(deliv, Mapping):
                res = deliv.get("videoDeliveryResponseResult")
                if isinstance(res, Mapping):
                    prog = res.get("progressive_urls")
                    if isinstance(prog, list):
                        for p in prog:
                            if not isinstance(p, Mapping):
                                continue
                            pu = _clean_fb_url(p.get("progressive_url") or "")
                            meta = p.get("metadata") if isinstance(p.get("metadata"), Mapping) else {}
                            qual = str(meta.get("quality") or "").upper()
                            if pu:
                                if "HD" in qual and not hd:
                                    hd = pu
                                elif not sd:
                                    sd = pu
        primary = hd or sd
        backups = [u for u in (sd,) if u and u != primary]
        if not cover:
            cover = _pick_uri(
                vobj.get("first_frame_thumbnail")
                or vobj.get("preferred_thumbnail")
                or vobj.get("thumbnailImage")
                or vobj.get("previewImage")
                or vobj.get("image")
                or vobj.get("poster")
            )
            if isinstance(vobj.get("preferred_thumbnail"), Mapping):
                cover = _pick_uri(vobj["preferred_thumbnail"].get("image")) or cover
        dur = vobj.get("playable_duration_in_ms") or vobj.get("length_in_second") or vobj.get("duration") or 0
        if isinstance(dur, (int, float)) and dur > 0:
            duration = int(dur / 1000) if dur > 10_000 else int(dur)
        if primary and primary not in seen_urls:
            seen_urls.add(primary)
            quality_label = "HD" if hd else "SD"
            medias.append(
                MediaItem(
                    url=primary,
                    kind="video",
                    ext="mp4",
                    index=len(medias),
                    backup_urls=backups,
                )
            )

    def _add_image(iobj: Any) -> None:
        nonlocal cover
        u = ""
        if isinstance(iobj, Mapping):
            for key in ("viewer_image", "photo_image", "image", "large_share_image", "thumbnailImage"):
                cand = _pick_uri(iobj.get(key))
                if cand:
                    u = cand
                    break
            if not u:
                u = _pick_uri(iobj)
        else:
            u = _pick_uri(iobj)
        if not u or u in seen_urls or u.startswith("data:"):
            return
        seen_urls.add(u)
        if not cover:
            cover = u
        ext = "png" if ".png" in u.lower() else ("webp" if ".webp" in u.lower() else "jpg")
        medias.append(MediaItem(url=u, kind="image", ext=ext, index=len(medias)))

    # 1) 已预归一化的 DOM/SSR 字典字段
    if isinstance(node.get("video_url"), str) and node["video_url"].strip():
        _add_video(node)
    if isinstance(node.get("images"), list):
        for im in node["images"]:
            _add_image(im)
    if isinstance(node.get("cover"), str) and node["cover"].strip() and not cover:
        cover = _clean_fb_url(node["cover"])

    # 2) 遍历 GraphQL attachments / subattachments
    att_lists: List[Any] = []
    if isinstance(node.get("attachments"), list):
        att_lists.extend(node["attachments"])
    cs = node.get("comet_sections")
    if isinstance(cs, Mapping):
        c_st = (cs.get("content") or {}).get("story") if isinstance(cs.get("content"), Mapping) else None
        if isinstance(c_st, Mapping):
            if isinstance(c_st.get("attachments"), list):
                att_lists.extend(c_st["attachments"])
            att_st = c_st.get("attached_story")
            if isinstance(att_st, Mapping) and isinstance(att_st.get("attachments"), list):
                att_lists.extend(att_st["attachments"])

    def _walk_attachment(att: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(att, Mapping):
            return
        media = att.get("media")
        if isinstance(media, Mapping):
            mtype = str(media.get("__typename") or "").lower()
            is_vid = (
                "video" in mtype
                or "reel" in mtype
                or bool(media.get("playable_url") or media.get("browser_native_hd_url") or media.get("browser_native_sd_url") or media.get("videoDeliveryResponseFragment"))
            )
            if is_vid:
                _add_video(media)
            else:
                _add_image(media)
        styles = att.get("styles")
        if isinstance(styles, Mapping):
            nested = styles.get("attachment")
            if isinstance(nested, Mapping):
                _walk_attachment(nested, depth + 1)
        all_subs = att.get("all_subattachments") or att.get("subattachments")
        if isinstance(all_subs, Mapping) and isinstance(all_subs.get("nodes"), list):
            for sub in all_subs["nodes"]:
                _walk_attachment(sub, depth + 1)
        elif isinstance(all_subs, list):
            for sub in all_subs:
                _walk_attachment(sub, depth + 1)

    for att in att_lists:
        _walk_attachment(att)

    has_video = any(m.kind == "video" for m in medias)
    explicit_type = str(node.get("media_type") or "").strip().lower()
    if has_video or explicit_type == "video":
        media_type = "video"
        # 视频帖保留视频流为主，过滤重复封面图
        vid_only = [m for m in medias if m.kind == "video"]
        if vid_only:
            medias = vid_only
    else:
        media_type = "images"
    return media_type, medias, cover, duration, quality_label


def _extract_counts(node: Mapping[str, Any]) -> Tuple[int, int, int, int]:
    """返回 `(like_count, comment_count, share_count, play_count)`。"""
    likes = _to_int(node.get("like_count"))
    comments = _to_int(node.get("comment_count"))
    shares = _to_int(node.get("share_count"))
    plays = _to_int(
        node.get("play_count")
        or node.get("view_count")
        or node.get("video_view_count")
    )

    # BFS 探测 feedback / comet_sections.feedback 下嵌套的 UFI 统计容器（兼容 2024-2026 Comet 包装）
    # 严禁进入 top_reactions（单个表情拆分项）和评论列表子树，防止子项计数覆盖主帖汇总计数。
    from collections import deque

    roots: List[Any] = []
    if isinstance(node.get("feedback"), Mapping):
        roots.append(node["feedback"])
    if isinstance(node.get("fb_reel_react_button"), Mapping):
        roots.append(node["fb_reel_react_button"])
    cs = node.get("comet_sections")
    if isinstance(cs, Mapping) and isinstance(cs.get("feedback"), Mapping):
        roots.append(cs["feedback"])

    skip_keys = {
        "comments",
        "top_level_comments",
        "relevant_comments",
        "display_comments",
        "comment_list_renderer",
        "top_reactions",
        "actors",
        "attachments",
        "plugins",
    }
    top_reactions_sum = 0
    queue: deque = deque((r, 0) for r in roots)
    while queue:
        cur, depth = queue.popleft()
        if depth > 14 or not isinstance(cur, Mapping):
            continue

        rc = cur.get("reaction_count")
        if isinstance(rc, Mapping) and rc.get("count") is not None:
            likes = max(likes, _to_int(rc.get("count")))
        elif isinstance(cur.get("reactors"), Mapping) and cur["reactors"].get("count") is not None:
            likes = max(likes, _to_int(cur["reactors"].get("count")))
        elif isinstance(cur.get("unified_reactors"), Mapping) and cur["unified_reactors"].get("count") is not None:
            likes = max(likes, _to_int(cur["unified_reactors"].get("count")))
        elif cur.get("aggregated_reaction_count") not in (None, "", 0):
            likes = max(likes, _to_int(cur.get("aggregated_reaction_count")))
        elif isinstance(cur.get("i18n_reaction_count"), str) and cur["i18n_reaction_count"].strip():
            likes = max(likes, _to_int(cur["i18n_reaction_count"]))

        # 记录 top_reactions 汇总作为最后兜底（仅当主帖未直接给 reaction_count 时使用）
        tr = cur.get("top_reactions")
        if isinstance(tr, Mapping) and isinstance(tr.get("edges"), list):
            s_tr = sum(
                _to_int(e.get("reaction_count"))
                for e in tr["edges"]
                if isinstance(e, Mapping)
            )
            if s_tr > top_reactions_sum:
                top_reactions_sum = s_tr

        for ck in ("comment_rendering_instance", "comments_count_summary_renderer", "total_comment_count", "comments"):
            cv = cur.get(ck)
            if isinstance(cv, Mapping):
                if isinstance(cv.get("comments"), Mapping) and cv["comments"].get("total_count") is not None:
                    comments = max(comments, _to_int(cv["comments"]["total_count"]))
                elif cv.get("total_count") is not None:
                    comments = max(comments, _to_int(cv["total_count"]))
                elif cv.get("count") is not None:
                    comments = max(comments, _to_int(cv["count"]))
            elif isinstance(cv, (int, float)) and not isinstance(cv, bool) and cv > 0:
                comments = max(comments, _to_int(cv))

        sc = cur.get("share_count")
        if isinstance(sc, Mapping) and sc.get("count") is not None:
            shares = max(shares, _to_int(sc.get("count")))
        elif isinstance(cur.get("i18n_share_count"), str) and cur["i18n_share_count"].strip():
            shares = max(shares, _to_int(cur["i18n_share_count"]))
        elif cur.get("share_count_reduced") not in (None, "", 0):
            shares = max(shares, _to_int(cur["share_count_reduced"]))

        if cur.get("video_view_count"):
            plays = max(plays, _to_int(cur.get("video_view_count")))

        for k, v in cur.items():
            if k in skip_keys:
                continue
            if isinstance(v, Mapping):
                queue.append((v, depth + 1))
            elif isinstance(v, list):
                # Comet 2026 UFI 将 reaction_count / comment_rendering_instance / share_count
                # 包装在 adaptive_ufi_action_renderers 等数组中，必须递归进入列表元素。
                for elem in v[:20]:
                    if isinstance(elem, Mapping):
                        queue.append((elem, depth + 1))

    if not likes and top_reactions_sum:
        likes = top_reactions_sum
    return likes, comments, shares, plays


def parse_facebook_aweme(raw: Any) -> Optional[Aweme]:
    """将 Facebook GraphQL Story、SSR 节点或 DOM 采集字典归一为 `Aweme`。"""
    from ..douyin.extract import Aweme

    if not isinstance(raw, Mapping):
        return None
    node = raw.get("node") if isinstance(raw.get("node"), Mapping) else raw

    source_url, create_time, post_id = _extract_permalink_and_time(node)
    if source_url:
        source_url = normalize_facebook_url(source_url)

    if not post_id and source_url:
        ref = resolve_facebook_post_ref(source_url, allow_numeric=False)
        if ref is not None:
            post_id = ref.post_id
    if not post_id:
        vid_obj = node.get("video")
        if isinstance(vid_obj, Mapping) and str(vid_obj.get("id") or "").isdigit():
            post_id = str(vid_obj["id"]).strip()
    if not post_id and isinstance(node.get("attachments"), list):
        for att in node["attachments"]:
            if isinstance(att, Mapping) and isinstance(att.get("media"), Mapping):
                mid = str(att["media"].get("id") or "").strip()
                if mid.isdigit():
                    post_id = mid
                    break
    if not post_id and isinstance(node.get("tracking"), str) and "{" in node["tracking"]:
        try:
            tr = json.loads(node["tracking"])
            if isinstance(tr, Mapping):
                for tk in ("top_level_post_id", "video_id", "mf_story_key"):
                    tv = str(tr.get(tk) or "").strip()
                    if tv.isdigit():
                        post_id = tv
                        break
                if not create_time:
                    pc = tr.get("page_insights")
                    if isinstance(pc, Mapping):
                        for pv in pc.values():
                            if isinstance(pv, Mapping) and isinstance(pv.get("post_context"), Mapping):
                                pt = parse_facebook_timestamp(pv["post_context"].get("publish_time"))
                                if pt:
                                    create_time = pt
                                    break
        except Exception:
            pass
    if not post_id:
        raw_id = str(node.get("id") or "").strip()
        if raw_id:
            # 防止超长 Base64 加密 Relay ID（如 UzpfSUZTOjE6...）污染 aweme_id、标题和 Windows 文件名
            if len(raw_id) > 48 and not raw_id.startswith("pfbid"):
                post_id = "fb_" + hashlib.md5(raw_id.encode("utf-8")).hexdigest()[:16]
            else:
                post_id = raw_id
    if not post_id and source_url:
        post_id = hashlib.md5(source_url.encode("utf-8")).hexdigest()[:16]
    if not post_id:
        return None
    if len(post_id) > 64 and not post_id.startswith("pfbid"):
        post_id = "fb_" + hashlib.md5(post_id.encode("utf-8")).hexdigest()[:16]

    actor = _extract_actor(node)
    author_name = str(node.get("author_name") or actor["name"] or actor["unique_id"] or "").strip()
    author_uid = str(node.get("author_unique_id") or actor["unique_id"] or actor["uid"] or "").strip()
    avatar = str(node.get("avatar") or actor["avatar"] or "").strip()

    desc = _extract_story_message(node)
    media_type, medias, cover, duration, quality_label = _extract_media_from_story(node)
    likes, comments, _shares, _plays = _extract_counts(node)

    if not source_url:
        kind = "video" if media_type == "video" else "post"
        source_url = post_url(post_id, author_uid, kind=kind)

    return Aweme(
        aweme_id=post_id,
        desc=desc,
        create_time=create_time,
        author_name=author_name,
        media_type=media_type,
        medias=medias,
        cover=cover or None,
        quality_label=quality_label,
        like_count=likes,
        comment_count=comments,
        duration=duration,
        avatar=avatar,
        platform="facebook",
        source_url=source_url,
        author_unique_id=author_uid,
    )


def parse_facebook_creator_work(raw: Any) -> Optional[Dict[str, Any]]:
    """转换为本账号作品 (`AccountWork`) 扁平字典。"""
    aw = parse_facebook_aweme(raw)
    if aw is None:
        return None
    node = raw.get("node") if isinstance(raw, Mapping) and isinstance(raw.get("node"), Mapping) else (raw if isinstance(raw, Mapping) else {})
    likes, comments, shares, plays = _extract_counts(node)
    return {
        "item_id": aw.aweme_id,
        "desc": aw.desc,
        "media_type": aw.media_type,
        "cover_url": aw.cover or "",
        "create_time": aw.create_time,
        "like_count": likes or aw.like_count,
        "comment_count": comments or aw.comment_count,
        "collect_count": 0,
        "share_count": shares,
        "play_count": plays,
        "status": str(node.get("status") or "公开"),
    }


def parse_facebook_comment(raw: Any, *, reply_to: str = "") -> Optional[Dict[str, Any]]:
    """将 Facebook GraphQL Comment 或 DOM 评论字典归一为 `CommentRecord` 兼容格式。"""
    if not isinstance(raw, Mapping):
        return None
    node = raw.get("node") if isinstance(raw.get("node"), Mapping) else raw
    cid = str(
        node.get("comment_id")
        or node.get("legacy_fbid")
        or node.get("id")
        or ""
    ).strip()
    body = node.get("preferred_body") if isinstance(node.get("preferred_body"), Mapping) else node.get("body")
    text = (
        str(body.get("text") or "").strip()
        if isinstance(body, Mapping)
        else str(node.get("text") or body or "").strip()
    )
    if not text and not cid:
        return None
    if not cid:
        cid = hashlib.md5(f"{node.get('user_nickname', '')}:{text}".encode("utf-8")).hexdigest()[:16]

    author = node.get("author") if isinstance(node.get("author"), Mapping) else {}
    nickname = str(
        node.get("user_nickname")
        or author.get("name")
        or node.get("author_name")
        or ""
    ).strip()
    sec_uid = str(
        node.get("user_sec_uid")
        or author.get("id")
        or author.get("url")
        or ""
    ).strip()
    if sec_uid.startswith("http"):
        m = re.search(r"(?:profile\.php\?id=(\d+)|facebook\.com/([A-Za-z0-9._-]+))", sec_uid)
        if m:
            sec_uid = m.group(1) or m.group(2) or sec_uid

    fb = node.get("feedback") if isinstance(node.get("feedback"), Mapping) else {}
    likes = _to_int(node.get("like_count"))
    if not likes and isinstance(fb.get("reactors"), Mapping):
        likes = _to_int(fb["reactors"].get("count"))
    if not likes and isinstance(fb.get("reaction_count"), Mapping):
        likes = _to_int(fb["reaction_count"].get("count"))

    create_time = parse_facebook_timestamp(
        node.get("create_time")
        or node.get("created_time")
        or 0
    )
    parent = str(
        reply_to
        or node.get("reply_to")
        or node.get("parent_comment_id")
        or ""
    ).strip()

    return {
        "comment_id": cid,
        "text": html.unescape(text),
        "user_nickname": nickname or sec_uid or cid,
        "user_sec_uid": sec_uid,
        "ip_label": str(node.get("ip_label") or "")[:40],
        "like_count": likes,
        "create_time": create_time,
        "reply_to": parent,
    }


def parse_facebook_user(raw: Any) -> Dict[str, Any]:
    """将 Facebook 用户 / 公共主页字典归一为 CreatorHub 账号/创作者资料字典。"""
    if not isinstance(raw, Mapping):
        return {}
    u = raw.get("user") if isinstance(raw.get("user"), Mapping) and not raw.get("name") else raw
    uid = str(u.get("uid") or u.get("c_user") or u.get("user_id") or u.get("id") or "").strip()
    handle = str(u.get("unique_id") or u.get("vanity") or u.get("username") or u.get("douyin_id") or "").strip().lstrip("@")
    name = str(u.get("nickname") or u.get("name") or u.get("full_name") or handle or uid).strip()
    avatar = _pick_uri(u.get("avatar") or u.get("profile_picture") or u.get("profile_pic") or "")
    bio = str(u.get("signature") or u.get("bio") or u.get("intro") or "").strip()
    return {
        "uid": uid,
        "sec_uid": handle or uid,
        "unique_id": handle,
        "douyin_id": handle or uid,
        "nickname": name,
        "avatar": avatar,
        "signature": bio,
        "follower_count": _to_int(u.get("follower_count") or u.get("followers_count") or u.get("friends_count")),
        "following_count": _to_int(u.get("following_count") or u.get("friends_count")),
        "friends_count": _to_int(u.get("friends_count")),
        "follower_count_is_friends_fallback": bool(u.get("follower_count_is_friends_fallback", False)),
        "following_count_is_friends_fallback": bool(u.get("following_count_is_friends_fallback", False)),
        "aweme_count": _to_int(u.get("aweme_count") or u.get("post_count")),
        "total_favorited": _to_int(u.get("total_favorited") or u.get("likes_count")),
        "is_following": bool(u.get("is_following", False)),
        "is_mutual": bool(u.get("is_mutual", False)),
        "pages": list(u.get("pages") or []) if isinstance(u.get("pages"), list) else [],
    }


parse_facebook_self_user = parse_facebook_user


def extract_pages_from_html(html_text: str) -> List[Dict[str, Any]]:
    """从 `/pages/?category=your_pages&ref=bookmarks` HTML 中解析 Profile+ 公共主页列表。

    提取 `{"is_profile_plus":true,"id":"...","name":"..."}` 结构并归一化为统一公共主页字段。
    """
    text = str(html_text or "")
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for m in _IS_PROFILE_PLUS_RE.finditer(text):
        pid = m.group(1).strip()
        pname = _decode_json_str(m.group(2)).strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append({
            "page_id": pid,
            "name": pname or pid,
            "link": f"{FACEBOOK_BASE}/profile.php?id={pid}",
            "is_profile_plus": True,
            "members_text": "",
            "members_count": 0,
        })
    return out


def _post_richness(aw: Any) -> int:
    return (
        (10 if aw.medias else 0)
        + (4 if aw.desc else 0)
        + (3 if aw.author_name else 0)
        + (3 if aw.create_time else 0)
        + (2 if aw.like_count else 0)
        + (2 if aw.comment_count else 0)
        + (2 if aw.source_url and "/photo" not in aw.source_url else 0)
    )


def extract_graphql_posts(payload: Any) -> List[Dict[str, Any]]:
    """递归遍历 Facebook `/api/graphql` 或页面内联 `<script data-sjs>` JSON，提取完整帖子（Story）节点。"""
    found_by_id: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, Mapping):
            continue
        typename = str(cur.get("__typename") or "")
        has_comet_story = isinstance(cur.get("comet_sections"), Mapping)
        looks_story = (
            typename in {"Story", "Video", "Reel", "Photo"}
            or (
                bool(cur.get("post_id") or cur.get("legacy_fbid") or cur.get("permalink_url") or cur.get("wwwURL"))
                and (
                    has_comet_story
                    or isinstance(cur.get("message"), Mapping)
                    or isinstance(cur.get("attachments"), list)
                    or isinstance(cur.get("feedback"), Mapping)
                )
            )
            or (
                has_comet_story
                and isinstance(cur["comet_sections"].get("content"), Mapping)
            )
        )
        if looks_story:
            aw = parse_facebook_aweme(cur)
            if aw is not None and aw.aweme_id and (aw.desc or aw.medias or aw.cover):
                score = _post_richness(aw)
                prev = found_by_id.get(aw.aweme_id)
                if prev is None or score > prev[0]:
                    found_by_id[aw.aweme_id] = (score, dict(cur))
                # 若外层 Story 已含完整的 comet_sections，不再下钻重复提取内层残缺子 Story
                if has_comet_story:
                    continue
        for v in cur.values():
            if isinstance(v, (Mapping, list)):
                stack.append(v)
    return [row for _, row in found_by_id.values()]


def extract_graphql_comments(payload: Any) -> List[Dict[str, Any]]:
    """递归遍历 Facebook `/api/graphql` JSON，提取评论节点。"""
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, Mapping):
            continue
        typename = str(cur.get("__typename") or "")
        if typename == "Comment" or (
            (isinstance(cur.get("body"), Mapping) or isinstance(cur.get("preferred_body"), Mapping))
            and isinstance(cur.get("author"), Mapping)
            and (cur.get("legacy_fbid") or cur.get("id"))
        ):
            parsed = parse_facebook_comment(cur)
            if parsed and parsed["comment_id"] not in seen and parsed["text"]:
                seen.add(parsed["comment_id"])
                found.append(parsed)
        for v in cur.values():
            if isinstance(v, (Mapping, list)):
                stack.append(v)
    return found


def extract_graphql_groups(payload: Any) -> List[Dict[str, Any]]:
    """递归遍历 Facebook GraphQL 响应提取群组条目。"""
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, Mapping):
            continue
        typename = str(cur.get("__typename") or "")
        url = _clean_fb_url(cur.get("url") or "")
        if typename == "Group" or ("/groups/" in url and cur.get("name")):
            gid = str(cur.get("id") or "").strip()
            name = str(cur.get("name") or "").strip()
            link = url or (f"{FACEBOOK_BASE}/groups/{gid}" if gid else "")
            key = gid or link
            if key and name and key not in seen:
                seen.add(key)
                m_text = ""
                if isinstance(cur.get("group_member_profiles"), Mapping):
                    m_text = str(cur["group_member_profiles"].get("formatted_count_text") or cur["group_member_profiles"].get("count") or "")
                elif isinstance(cur.get("members_text"), str):
                    m_text = cur["members_text"]
                found.append({
                    "group_id": gid,
                    "name": name,
                    "link": link,
                    "members_text": m_text,
                    "members_count": parse_members_count(m_text),
                    "is_admin": bool(cur.get("viewer_admin_type") or cur.get("is_admin")),
                    "privacy": str(cur.get("privacy_info", {}).get("title", {}).get("text", "") if isinstance(cur.get("privacy_info"), Mapping) else ""),
                })
        for v in cur.values():
            if isinstance(v, (Mapping, list)):
                stack.append(v)
    return found


def extract_graphql_pages(payload: Any) -> List[Dict[str, Any]]:
    """递归遍历 Facebook GraphQL 响应提取公共主页条目。"""
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, Mapping):
            continue
        typename = str(cur.get("__typename") or "")
        if typename in {"Page", "AdditionalProfilePlus"} or cur.get("is_profile_plus") is True:
            pid = str(cur.get("id") or "").strip()
            name = str(cur.get("name") or "").strip()
            link = _clean_fb_url(cur.get("url") or (f"{FACEBOOK_BASE}/profile.php?id={pid}" if pid else ""))
            key = pid or link
            if key and name and key not in seen:
                seen.add(key)
                found.append({
                    "page_id": pid,
                    "name": name,
                    "link": link,
                    "avatar": _pick_uri(cur.get("profile_picture") or cur.get("avatar")),
                    "is_profile_plus": bool(cur.get("is_profile_plus", typename == "AdditionalProfilePlus")),
                    "members_text": str(cur.get("followers_count") or cur.get("members_text") or ""),
                    "members_count": _to_int(cur.get("followers_count") or cur.get("members_count")),
                })
        for v in cur.values():
            if isinstance(v, (Mapping, list)):
                stack.append(v)
    return found


def parse_facebook_dm_conversations(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """将 Messenger 会话原始项归一为 CreatorHub `DmConversation` 字典列表。"""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, Mapping):
            continue
        conv_id = str(it.get("conv_id") or it.get("thread_id") or it.get("peer_uid") or "").strip()
        if not conv_id or conv_id in seen:
            continue
        seen.add(conv_id)
        out.append({
            "conv_id": conv_id,
            "peer_uid": str(it.get("peer_uid") or conv_id).strip(),
            "peer_sec_uid": str(it.get("peer_sec_uid") or it.get("peer_uid") or conv_id).strip(),
            "peer_nickname": str(it.get("peer_nickname") or it.get("name") or conv_id).strip()[:80],
            "peer_avatar": _clean_fb_url(it.get("peer_avatar") or it.get("avatar") or ""),
            "last_text": str(it.get("last_text") or "").strip()[:300],
            "last_time": parse_facebook_timestamp(it.get("last_time") or 0),
            "unread_count": _to_int(it.get("unread_count") or 0),
        })
    return out


def parse_facebook_dm_messages(items: Iterable[Mapping[str, Any]], *, conv_id: str = "") -> List[Dict[str, Any]]:
    """将 Messenger 会话消息原始项归一为 CreatorHub `DmMessage` 字典列表。"""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for idx, it in enumerate(items or []):
        if not isinstance(it, Mapping):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        mid = str(it.get("msg_id") or "").strip()
        if not mid:
            mid = hashlib.md5(f"{conv_id}:{idx}:{it.get('direction', '')}:{text}".encode("utf-8")).hexdigest()[:16]
        if mid in seen:
            continue
        seen.add(mid)
        direction = str(it.get("direction") or "in").strip().lower()
        if direction not in {"in", "out"}:
            direction = "in"
        out.append({
            "msg_id": mid,
            "direction": direction,
            "sender_uid": str(it.get("sender_uid") or "").strip(),
            "sender_name": str(it.get("sender_name") or "").strip()[:80],
            "text": text,
            "created_at": parse_facebook_timestamp(it.get("created_at") or it.get("create_time") or 0),
        })
    return out


_SJS_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_sjs_json_from_html(
    html_text: str,
    *,
    markers: Tuple[str, ...] = ("comet_sections", "serpResponse", "permalink_url", '"Story"', '"Comment"', "timeline_list_feed_units", "group_feed"),
) -> List[Any]:
    """从 Facebook SSR HTML 的 `<script type="application/json">` 块中纯 Python 提取内联 Relay JSON。"""
    text = str(html_text or "")
    if not text:
        return []
    out: List[Any] = []
    for m in _SJS_SCRIPT_RE.finditer(text):
        chunk = m.group(1).strip()
        if len(chunk) < 80:
            continue
        if markers and not any(tok in chunk for tok in markers):
            continue
        try:
            out.append(json.loads(chunk))
        except Exception:
            continue
    return out


def extract_pagination_info(payload: Any, *, kind: str = "posts") -> Dict[str, Any]:
    """从 Facebook GraphQL / SSR 响应中提取翻页游标、是否还有下一页及上下文目标 ID。

    返回字典包含：
    - `end_cursor`: 下一页游标字符串
    - `has_next_page`: 是否还有下一页
    - `feedback_id`: 帖子评论对应的 Relay `feedback_id`
    - `target_id`: 主页 `userID` / 群组 `groupID`
    - `bsid`: 搜索分页会话 `bsid`
    """
    info: Dict[str, Any] = {
        "end_cursor": "",
        "has_next_page": False,
        "feedback_id": "",
        "target_id": "",
        "bsid": "",
    }
    if not payload:
        return info

    # 不同业务优先匹配的容器键名
    preferred_containers = (
        ("comments", "top_level_comments", "display_comments")
        if kind == "comments"
        else ("results", "timeline_list_feed_units", "group_feed", "feed_units", "edges")
    )

    stack: List[Any] = [payload]
    fallback_cursor = ""
    fallback_has_next = False

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, Mapping):
            continue

        # 提取 bsid（搜索分页会话标识，兼容 bsid / serpResponse.session_id）
        if not info["bsid"]:
            for bk in ("bsid", "session_id"):
                bv = cur.get(bk)
                if isinstance(bv, str) and len(bv.strip()) >= 8:
                    info["bsid"] = bv.strip()
                    break

        # 提取 target_id（profile / group 数字 ID）
        if not info["target_id"]:
            for tk in ("profileID", "userID", "groupID", "group_id"):
                tv = cur.get(tk)
                if isinstance(tv, str) and tv.isdigit():
                    info["target_id"] = tv
                    break
            if not info["target_id"] and cur.get("__typename") in {"Group", "User", "Page"}:
                cid = str(cur.get("id") or "").strip()
                if cid.isdigit():
                    info["target_id"] = cid

        # 提取评论区 feedback_id（Relay base64 ID，通常以 ZmVlZGJhY2s6 开头）
        if not info["feedback_id"]:
            fb = cur.get("feedback")
            if isinstance(fb, Mapping) and isinstance(fb.get("id"), str) and fb["id"].strip():
                if kind == "comments" or fb.get("comment_rendering_instance") or fb.get("comments"):
                    info["feedback_id"] = fb["id"].strip()
            elif cur.get("__typename") == "Feedback" and isinstance(cur.get("id"), str) and cur["id"].strip():
                info["feedback_id"] = cur["id"].strip()

        # 优先在对应业务容器下读取 page_info
        for ck in preferred_containers:
            sub = cur.get(ck)
            if isinstance(sub, Mapping):
                pi = sub.get("page_info")
                if isinstance(pi, Mapping):
                    ec = str(pi.get("end_cursor") or "").strip()
                    hn = bool(pi.get("has_next_page"))
                    if ec and not info["end_cursor"]:
                        info["end_cursor"] = ec
                        info["has_next_page"] = hn
                    elif hn and not info["has_next_page"]:
                        info["has_next_page"] = hn

        # 兜底记录任意 page_info
        pi_any = cur.get("page_info")
        if isinstance(pi_any, Mapping):
            ec = str(pi_any.get("end_cursor") or "").strip()
            hn = bool(pi_any.get("has_next_page"))
            if ec and not fallback_cursor:
                fallback_cursor = ec
                fallback_has_next = hn

        for v in cur.values():
            if isinstance(v, (Mapping, list)):
                stack.append(v)

    if not info["end_cursor"] and fallback_cursor:
        info["end_cursor"] = fallback_cursor
        info["has_next_page"] = fallback_has_next
    return info


def extract_target_post_from_sjs(
    sjs_blocks: List[Any],
    *,
    target_url: str = "",
    target_id: str = "",
) -> Optional[Dict[str, Any]]:
    """合并 Facebook Comet 流式 `@defer` SSR 多块数据，精准还原目标 Reel / 视频 / 帖子。

    Facebook `/reel/<id>/` 页面会把同一条作品拆分到多个 `<script type="application/json">` 块中：
    - 块 A (`data.video.creation_story`)：视频直链 (`videoDeliveryResponseFragment`)、封面、时长；
    - 块 B (`data.fb_reel_react_button` / `feedback`)：点赞数、评论数、分享数、`post_id`；
    - 块 C (`data.actors` / `message` / `creation_time`)：作者、文案与发布时间；
    同时页面末尾还附带下一条推荐 Reel。按 `target_id` / 关联 `post_id` 合并即可避免错拿推荐视频与 Base64 乱码 ID。
    """
    if not sjs_blocks:
        return None

    clean_target = normalize_facebook_url(target_url) if target_url else ""
    tid = str(target_id or "").strip()
    if not tid and clean_target:
        ref = resolve_facebook_post_ref(clean_target, allow_numeric=False)
        if ref is not None:
            tid = ref.post_id

    data_chunks: List[Dict[str, Any]] = []

    def _collect_bbox_data(obj: Any) -> None:
        stack: List[Any] = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
                continue
            if not isinstance(cur, Mapping):
                continue
            res = cur.get("result")
            if isinstance(res, Mapping) and isinstance(res.get("data"), Mapping):
                d = dict(res["data"])
                data_chunks.append(d)
                vid = d.get("video")
                if isinstance(vid, Mapping) and isinstance(vid.get("creation_story"), Mapping):
                    cs_dict = dict(vid["creation_story"])
                    if vid.get("id") and not cs_dict.get("video"):
                        cs_dict["video"] = {"id": str(vid["id"])}
                    data_chunks.append(cs_dict)
            for v in cur.values():
                if isinstance(v, (Mapping, list)):
                    stack.append(v)

    for blk in sjs_blocks:
        _collect_bbox_data(blk)

    def _chunk_ids(d: Mapping[str, Any]) -> Tuple[set[str], List[str]]:
        ids: set[str] = set()
        urls: List[str] = []
        for k in ("post_id",):
            v = str(d.get(k) or "").strip()
            if v:
                ids.add(v)
        vid = d.get("video")
        if isinstance(vid, Mapping):
            v = str(vid.get("id") or "").strip()
            if v:
                ids.add(v)
        for uk in ("url", "permalink_url", "wwwURL"):
            uv = str(d.get(uk) or "").strip()
            if uv:
                urls.append(uv)
        atts = d.get("attachments")
        if isinstance(atts, list):
            for att in atts:
                if not isinstance(att, Mapping):
                    continue
                med = att.get("media")
                if isinstance(med, Mapping):
                    mid = str(med.get("id") or "").strip()
                    if mid:
                        ids.add(mid)
                    su = str(med.get("shareable_url") or "").strip()
                    if su:
                        urls.append(su)
        tr_raw = d.get("tracking")
        if isinstance(tr_raw, str) and "{" in tr_raw:
            try:
                tr = json.loads(tr_raw)
                if isinstance(tr, Mapping):
                    for tk in ("top_level_post_id", "video_id", "mf_story_key"):
                        tv = str(tr.get(tk) or "").strip()
                        if tv.isdigit():
                            ids.add(tv)
            except Exception:
                pass
        return ids, urls

    wanted_ids: set[str] = {tid} if tid else set()
    if wanted_ids:
        # 两轮关联扩展：由 reel/video id 发现对应的 post_id，从而合并同作品的 actors/message/feedback 块
        for _ in range(2):
            before = len(wanted_ids)
            for d in data_chunks:
                cids, curls = _chunk_ids(d)
                matched = bool(cids & wanted_ids) or any(w in u for w in wanted_ids for u in curls)
                if matched:
                    for cid in cids:
                        if cid.isdigit() or cid.startswith("pfbid"):
                            wanted_ids.add(cid)
            if len(wanted_ids) == before:
                break

        matched_chunks: List[Dict[str, Any]] = []
        for d in data_chunks:
            cids, curls = _chunk_ids(d)
            if bool(cids & wanted_ids) or any(w in u for w in wanted_ids for u in curls):
                matched_chunks.append(d)

        if matched_chunks:
            merged: Dict[str, Any] = {}
            rich_atts: List[Any] = []
            plain_atts: List[Any] = []
            merged_feedback: Dict[str, Any] = {}
            for d in matched_chunks:
                if d.get("post_id") and not merged.get("post_id"):
                    merged["post_id"] = str(d["post_id"]).strip()
                if isinstance(d.get("video"), Mapping) and not merged.get("video"):
                    merged["video"] = d["video"]
                if isinstance(d.get("actors"), list) and d["actors"] and not merged.get("actors"):
                    merged["actors"] = d["actors"]
                if d.get("message") and not merged.get("message"):
                    merged["message"] = d["message"]
                if d.get("creation_time") and not merged.get("creation_time"):
                    merged["creation_time"] = d["creation_time"]
                if isinstance(d.get("fb_reel_react_button"), Mapping) and not merged.get("fb_reel_react_button"):
                    merged["fb_reel_react_button"] = d["fb_reel_react_button"]
                if isinstance(d.get("comet_sections"), Mapping) and not merged.get("comet_sections"):
                    merged["comet_sections"] = d["comet_sections"]
                if isinstance(d.get("tracking"), str) and not merged.get("tracking"):
                    merged["tracking"] = d["tracking"]
                if isinstance(d.get("url"), str) and d["url"].startswith("http") and not merged.get("url"):
                    merged["url"] = d["url"]
                if isinstance(d.get("feedback"), Mapping):
                    for fk, fv in d["feedback"].items():
                        if fv not in (None, "", {}, []) and (fk not in merged_feedback or fk == "total_comment_count"):
                            merged_feedback[fk] = fv
                if isinstance(d.get("attachments"), list):
                    for att in d["attachments"]:
                        if not isinstance(att, Mapping):
                            continue
                        med = att.get("media") if isinstance(att.get("media"), Mapping) else {}
                        if med.get("videoDeliveryResponseFragment") or med.get("playable_url") or med.get("browser_native_hd_url") or med.get("viewer_image") or med.get("photo_image"):
                            rich_atts.append(att)
                        else:
                            plain_atts.append(att)
                        if not merged.get("actors") and isinstance(med.get("owner"), Mapping) and med["owner"].get("name"):
                            merged["actors"] = [med["owner"]]
            if merged_feedback:
                merged["feedback"] = merged_feedback
            if rich_atts or plain_atts:
                merged["attachments"] = rich_atts + plain_atts
            if clean_target:
                merged["source_url"] = clean_target
            if tid and not merged.get("post_id"):
                merged["post_id"] = tid
            aw_m = parse_facebook_aweme(merged)
            if aw_m is not None and (aw_m.medias or aw_m.desc or aw_m.author_name):
                return merged

    # 兜底：常规 Story 提取并按目标 ID 匹配度 + 信息完整度打分择优
    all_stories: List[Dict[str, Any]] = []
    for blk in sjs_blocks:
        all_stories.extend(extract_graphql_posts(blk))
    if not all_stories:
        return None

    best_story: Optional[Dict[str, Any]] = None
    best_score = -1
    for st in all_stories:
        aw = parse_facebook_aweme(st)
        if aw is None:
            continue
        score = (
            (100 if (tid and (tid in aw.aweme_id or tid in aw.source_url or tid in str(st.get("post_id") or ""))) else 0)
            + (20 if aw.medias else 0)
            + (10 if aw.desc else 0)
            + (8 if aw.author_name else 0)
            + (5 if aw.like_count else 0)
            + (5 if aw.comment_count else 0)
        )
        if score > best_score:
            best_score = score
            best_story = st
    return best_story
