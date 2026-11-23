"""小红书与海外小红书已发布笔记列表、完整媒体预览与评论拉取接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import runtime as app_runtime
from app.browser import fetch_rednote_comments, fetch_rednote_creator_published, fetch_rednote_note_detail, fetch_rednote_notes, fetch_rednote_self_profile, fetch_xhs_comments, fetch_xhs_note_detail, fetch_xhs_self_profile
from app.db import get_session
from app.models import DouyinAccount
from app.platforms.rednote import RednoteApiClient, flatten_rednote_comments, parse_rednote_brief, parse_rednote_comment, parse_rednote_detail, parse_rednote_self_user, cookie_str_from_state as rednote_cookie_str_from_state, has_a1 as rednote_has_a1
from app.platforms.xhs import XhsApiClient, XhsApiError, has_a1, parse_self_user as parse_xhs_self_user, cookie_str_from_state as xhs_cookie_str_from_state
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.services import account_profiles as account_profiles_service
from app.services import thumb_cache

router = APIRouter()


def _first_val(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, []):
            return v
    return default


async def _xhs_account_uid(state: str, proxy: str = "", *,
                           detailed: bool = False, user_agent: str = ""):
    """拿到该账号自己的 user_id(self_info → 创作平台资料兜底)。"""
    from app.platforms.xhs import creator_profile
    cookie = xhs_cookie_str_from_state(state)
    if has_a1(cookie):
        try:
            client = XhsApiClient(
                cookie,
                user_agent or app_runtime.cfg.engine.user_agent,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
                proxy=proxy,
                storage_state=state,
            )
            me = await client.self_info()
            uid = str((me or {}).get("user_id") or "")
            if uid:
                return (uid, "") if detailed else uid
        except Exception as exc:
            category, _signal = classify_platform_error(exc)
            if detailed and category in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                return "", exc
    try:
        if detailed:
            prof, profile_error = await creator_profile(
                state, proxy=proxy, preserve_error=True, user_agent=user_agent)
        else:
            prof = await creator_profile(state, proxy=proxy, user_agent=user_agent)
            profile_error = ""
    except Exception as exc:
        if detailed:
            return "", exc
        raise
    if detailed and profile_error:
        return "", profile_error
    uid = (prof or {}).get("sec_uid") or ""
    return (uid, "") if detailed else uid


def _imgs_of(n: dict) -> list:
    out = []
    for it in (n.get("images_list") or n.get("imageList") or []):
        if isinstance(it, dict):
            u = it.get("url") or it.get("url_default") or it.get("urlDefault") or ""
            if u:
                out.append(u)
    return out


@router.get("/api/publish/published")
async def list_published_notes(account_id: int, platform: str | None = None):
    """拉取「已发布作品列表」（支持小红书与海外小红书 Rednote）。
    优先用「读取登录态」打开自己的 www 主页(token 对预览/评论有效);
    没有读取态时回退创作平台「笔记管理」(能显示,但视频预览/评论可能不可用)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform not in ("xhs", "rednote"):
            raise HTTPException(400, "请选择一个已登录的小红书或海外小红书账号")
        if platform and platform in ("xhs", "rednote") and acc.platform != platform:
            raise HTTPException(400, "所选账号与当前平台不匹配")
        acc_platform = acc.platform
        read_state = acc.storage_state or ""
        creator_state = acc.creator_storage_state or ""
        proxy = acc.proxy or ""
        if not (read_state or creator_state):
            raise HTTPException(400, "该账号未登录,请先在账号页扫码登录")
    from app.browser import fetch_xhs_notes, fetch_creator_published
    from app.platforms.xhs import parse_note_brief

    async def _fetch_published_notes():
        def _payload(items, good_tokens=False, error_category=""):
            raw_covers = [str(it.get("cover") or "") for it in items if isinstance(it, dict)]
            thumb_cache.schedule_thumb_prewarm(raw_covers, platform=acc_platform, proxy=proxy)
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("cover"):
                    it["remote_cover"] = it["cover"]
                    it["cover"] = thumb_cache.local_thumb_api_url(str(it["cover"]), platform=acc_platform)
                if isinstance(it.get("images"), list):
                    it["images"] = [
                        thumb_cache.local_thumb_api_url(str(u), platform=acc_platform) or u
                        for u in it["images"]
                    ]
            payload = {
                "notes": items,
                "total": len(items),
                "good_tokens": good_tokens,
            }
            if error_category:
                payload["_error_category"] = error_category
            return payload

        def _failure(error):
            category, _signal = classify_platform_error(error)
            return _payload([], error_category=category.value), error

        with get_session() as s:
            current = s.get(DouyinAccount, account_id)
            identity = app_runtime.browser.identity_for(current)
        out, good = [], False
        if acc_platform == "rednote":
            uid = str(current.sec_uid or "") if current else ""
            rmode = str(getattr(app_runtime.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
            rn_state = read_state or creator_state
            rn_cookie = rednote_cookie_str_from_state(rn_state)
            if rmode in {"api", "hybrid"} and rn_cookie and rednote_has_a1(rn_cookie):
                rcli = RednoteApiClient(
                    rn_cookie,
                    account_profiles_service._direct_request_ua(identity),
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    proxy=proxy,
                    storage_state=rn_state,
                )
                try:
                    async with rcli.session_scope():
                        notes_api = await rcli.published_notes(max_pages=5)
                    for n in (notes_api or [])[:80]:
                        imgs = _imgs_of(n)
                        vi = n.get("video_info") or {}
                        cover = imgs[0] if imgs else (vi.get("cover") if isinstance(vi, dict) else "")
                        out.append({
                            "note_id": str(_first_val(n, "id", "noteId", "note_id")),
                            "title": _first_val(n, "display_title", "title", "desc", default="(无标题)"),
                            "type": _first_val(n, "type", "noteType", default="normal"),
                            "cover": cover or "",
                            "images": imgs,
                            "like": _first_val(n, "likes", "likeCount", default=0),
                            "time": _first_val(n, "time", "postTime", default=0),
                            "xsec_token": _first_val(n, "xsec_token", default=""),
                            "xsec_source": _first_val(n, "xsec_source", default="pc_note_detail"),
                        })
                    if out:
                        return _payload(out, False), ""
                except Exception as exc:
                    if rmode == "api":
                        return _failure(exc)
            if read_state:
                if not uid:
                    profile, uid_error = await fetch_rednote_self_profile(app_runtime.browser, identity)
                    if uid_error and classify_platform_error(uid_error)[0] in {
                            RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                        return _failure(uid_error)
                    if profile:
                        uid = str((parse_rednote_self_user(profile) or {}).get("sec_uid") or "")
                if uid:
                    items, _a, read_error = await fetch_rednote_notes(
                        app_runtime.browser, identity, uid, set(), ssr_fallback=True
                    )
                    if read_error and classify_platform_error(read_error)[0] in {
                            RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                        return _failure(read_error)
                    for raw in items[:80]:
                        b = parse_rednote_brief(raw)
                        if not b:
                            continue
                        card = raw.get("note_card") or raw
                        interact = card.get("interact_info") or {}
                        out.append({
                            "note_id": b["note_id"],
                            "title": b.get("title") or "(无标题)",
                            "type": b.get("type") or "normal",
                            "cover": b.get("cover") or "",
                            "images": [],
                            "like": interact.get("liked_count") or 0,
                            "time": card.get("time") or 0,
                            "xsec_token": b.get("xsec_token") or "",
                            "xsec_source": "pc_feed",
                        })
                    good = bool(out)
            error = ""
            if not out:
                notes, error = await fetch_rednote_creator_published(app_runtime.browser, identity)
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
            if error:
                category, _signal = classify_platform_error(error)
                return _payload(out, good, error_category=category.value), error
            return _payload(out, good), ""

        if read_state:
            uid = str(current.sec_uid or "") if current else ""
            uid_error = ""
            if not uid and account_profiles_service._xhs_browser_reads_enabled():
                profile, uid_error = await fetch_xhs_self_profile(
                    app_runtime.browser, identity)
                if profile:
                    uid = str(
                        (parse_xhs_self_user(profile) or {}).get("sec_uid")
                        or "")
            elif not uid:
                uid, uid_error = await _xhs_account_uid(
                    read_state, proxy, detailed=True)
            if uid_error:
                category, _signal = classify_platform_error(uid_error)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return _failure(uid_error)
            if uid:
                try:
                    items, _a, read_error = await fetch_xhs_notes(
                        app_runtime.browser, identity, uid, set())
                except Exception as exc:
                    category, _signal = classify_platform_error(exc)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        return _failure(exc)
                    items, read_error = [], exc
                if read_error:
                    category, _signal = classify_platform_error(read_error)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        return _failure(read_error)
                for raw in items[:80]:
                    b = parse_note_brief(raw)
                    if not b:
                        continue
                    card = raw.get("note_card") or raw
                    interact = card.get("interact_info") or {}
                    out.append({
                        "note_id": b["note_id"],
                        "title": b.get("title") or "(无标题)",
                        "type": b.get("type") or "normal",
                        "cover": b.get("cover") or "",
                        "images": [],
                        "like": interact.get("liked_count") or 0,
                        "time": card.get("time") or 0,
                        "xsec_token": b.get("xsec_token") or "",
                        "xsec_source": "pc_feed",
                    })
                good = bool(out)
        error = ""
        if not out:   # 回退:创作平台笔记管理(显示用)
            try:
                notes, error = await fetch_creator_published(app_runtime.browser, identity)
            except Exception as exc:
                category, _signal = classify_platform_error(exc)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return _failure(exc)
                raise
            for n in notes[:80]:
                imgs = _imgs_of(n)
                vi = n.get("video_info") or {}
                cover = (imgs[0] if imgs else
                         (vi.get("cover") if isinstance(vi, dict) else ""))
                out.append({
                    "note_id": str(_first_val(n, "id", "noteId", "note_id")),
                    "title": _first_val(
                        n, "display_title", "title", "desc", default="(无标题)"),
                    "type": _first_val(n, "type", "noteType", default="normal"),
                    "cover": cover or "", "images": imgs,
                    "like": _first_val(n, "likes", "likeCount", default=0),
                    "time": _first_val(n, "time", "postTime", default=0),
                    "xsec_token": _first_val(n, "xsec_token", default=""),
                    "xsec_source": _first_val(
                        n, "xsec_source", default="pc_note_detail"),
                })
        if error:
            category, _signal = classify_platform_error(error)
            return _payload(
                out, good, error_category=category.value), error
        return _payload(out, good), ""

    payload, outcome = await account_profiles_service._run_account_read(
        account_id, OperationKind.READ_LIGHT, f"published:{account_id}",
        _fetch_published_notes,
        empty_result={"notes": [], "total": 0, "good_tokens": False},
        unexpected_detail="读取已发布作品失败")
    if isinstance(outcome, dict):
        return outcome
    error_category = payload.pop("_error_category", "")
    if error_category == RiskCategory.AUTH.value or "logged_out" in (outcome or ""):
        raise HTTPException(400, "登录态已失效,请对该账号点「重新登录」")
    if error_category in {
            RiskCategory.RISK.value, RiskCategory.NETWORK.value}:
        raise HTTPException(400, f"读取已发布作品失败:{outcome}")
    return payload


@router.get("/api/publish/note-media")
async def publish_note_media(account_id: int, note_id: str,
                             xsec_token: str = "", xsec_source: str = "pc_note_detail"):
    """取一条小红书或海外小红书笔记的完整媒体(图集/视频),供「已发布作品」预览。"""
    from app.platforms.xhs import parse_note_detail
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform not in ("xhs", "rednote"):
            raise HTTPException(400, "账号无效")
        acc_platform = acc.platform
        state = acc.storage_state or acc.creator_storage_state or ""
        proxy = acc.proxy or ""
        identity = app_runtime.browser.identity_for(acc) if app_runtime.browser is not None else None
    cookie = (
        rednote_cookie_str_from_state(state)
        if acc_platform == "rednote"
        else xhs_cookie_str_from_state(state)
    )
    rmode = str(getattr(app_runtime.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
    if acc_platform == "xhs" and not has_a1(cookie):
        raise HTTPException(400, "登录态缺少 a1")
    if acc_platform == "rednote" and rmode == "api" and not rednote_has_a1(cookie):
        raise HTTPException(400, "海外小红书登录态缺少 a1")

    no_media = "拿不到该笔记的媒体(xsec_token 对 feed 接口无效)"

    async def _fetch_note_media():
        if acc_platform == "rednote":
            card = None
            if rmode in {"api", "hybrid"} and cookie and rednote_has_a1(cookie):
                rcli = RednoteApiClient(
                    cookie,
                    (account_profiles_service._direct_request_ua(identity)
                     if app_runtime.browser is not None and identity is not None
                     else app_runtime.cfg.engine.user_agent),
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    proxy=proxy,
                    storage_state=state,
                )
                try:
                    async with rcli.session_scope():
                        card = await rcli.note_detail(
                            note_id, xsec_token=xsec_token, xsec_source=xsec_source
                        )
                except Exception as exc:
                    if rmode == "api":
                        return None, exc
            if not card and rmode in {"browser", "hybrid"} and app_runtime.browser is not None and identity is not None:
                card, read_error = await fetch_rednote_note_detail(
                    app_runtime.browser, identity, note_id,
                    xsec_token=xsec_token, xsec_source=xsec_source,
                    block_media=app_runtime.cfg.engine.block_media_resources)
                if read_error and not card:
                    return None, read_error
            aw = parse_rednote_detail(card or {}, {"note_id": note_id})
        elif account_profiles_service._xhs_browser_reads_enabled() and identity is not None:
            card, read_error = await fetch_xhs_note_detail(
                app_runtime.browser, identity, note_id,
                xsec_token=xsec_token, xsec_source=xsec_source,
                block_media=app_runtime.cfg.engine.block_media_resources)
            if read_error:
                category, _signal = classify_platform_error(read_error)
                if not card or category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return None, read_error
        else:
            client = XhsApiClient(
                cookie,
                (account_profiles_service._direct_request_ua(identity)
                 if app_runtime.browser is not None and identity is not None
                 else app_runtime.cfg.engine.user_agent),
                timeout=app_runtime.cfg.engine.request_timeout_seconds, proxy=proxy)
            try:
                card = await client.note_detail(
                    note_id, xsec_token=xsec_token, xsec_source=xsec_source)
            except XhsApiError as exc:
                return None, exc
            aw = parse_note_detail(card or {}, {"note_id": note_id})
        if acc_platform == "xhs" and account_profiles_service._xhs_browser_reads_enabled() and identity is not None:
            aw = parse_note_detail(card or {}, {"note_id": note_id})
        if not aw or not aw.medias:
            return None, no_media
        return {
            "media_type": aw.media_type,
            "desc": aw.desc,
            "cover_url": thumb_cache.local_thumb_api_url(aw.cover or "", platform=acc_platform),
            "medias": [{
                "url": (
                    thumb_cache.local_thumb_api_url(media.url, platform=acc_platform)
                    if media.kind == "image"
                    else media.url
                ),
                "kind": media.kind,
                "ext": media.ext,
                "index": media.index,
            } for media in aw.medias],
        }, ""

    payload, outcome = await account_profiles_service._run_account_read(
        account_id, OperationKind.READ_HEAVY,
        f"note-media:{account_id}:{note_id}", _fetch_note_media,
        empty_result={
            "media_type": "", "desc": "", "cover_url": "", "medias": []},
        unexpected_detail="取笔记失败")
    if isinstance(outcome, dict):
        return outcome
    if outcome == no_media:
        raise HTTPException(400, no_media)
    if outcome:
        raise HTTPException(400, f"取笔记失败:{outcome}")
    return payload


@router.get("/api/publish/note-comments")
async def publish_note_comments(account_id: int, note_id: str,
                                xsec_token: str = "", xsec_source: str = "pc_note_detail"):
    """拉取一条小红书或海外小红书笔记的评论(一级 + 子评论拍平)。"""
    from app.platforms.xhs import parse_comment as parse_xhs_comment, flatten_comments
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform not in ("xhs", "rednote"):
            raise HTTPException(400, "账号无效")
        acc_platform = acc.platform
        state = acc.storage_state or acc.creator_storage_state or ""
        proxy = acc.proxy or ""
        identity = app_runtime.browser.identity_for(acc) if app_runtime.browser is not None else None
    cookie = (
        rednote_cookie_str_from_state(state)
        if acc_platform == "rednote"
        else xhs_cookie_str_from_state(state)
    )
    rmode = str(getattr(app_runtime.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
    if acc_platform == "xhs" and not has_a1(cookie):
        raise HTTPException(400, "登录态缺少 a1")
    if acc_platform == "rednote" and rmode == "api" and not rednote_has_a1(cookie):
        raise HTTPException(400, "海外小红书登录态缺少 a1")

    async def _fetch_note_comments():
        if acc_platform == "rednote":
            if rmode in {"api", "hybrid"} and cookie and rednote_has_a1(cookie):
                rcli = RednoteApiClient(
                    cookie,
                    (account_profiles_service._direct_request_ua(identity)
                     if app_runtime.browser is not None and identity is not None
                     else app_runtime.cfg.engine.user_agent),
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    proxy=proxy,
                    storage_state=state,
                )
                try:
                    async with rcli.session_scope():
                        data = await rcli.collect_note_comments(
                            note_id,
                            xsec_token=xsec_token,
                            xsec_source=xsec_source,
                            max_comments=200,
                            max_requests=max(1, min(20, app_runtime.cfg.engine.comment_max_scrolls)),
                            include_replies=True,
                            request_interval=max(0.5, app_runtime.cfg.engine.xhs_item_gap_seconds),
                        )
                    raw_rn = data.get("comments") or []
                    comments_rn = [
                        c for c in (parse_rednote_comment(it) for it in flatten_rednote_comments(raw_rn))
                        if c
                    ]
                    comments_rn.sort(key=lambda c: c.get("create_time") or 0, reverse=True)
                    return {
                        "comments": comments_rn,
                        "total": len(comments_rn),
                        "has_more": bool(data.get("has_more")),
                    }, ""
                except Exception as exc:
                    if rmode == "api":
                        return None, exc
            if app_runtime.browser is not None and identity is not None:
                raw, read_error = await fetch_rednote_comments(
                    app_runtime.browser, identity, note_id, set(),
                    xsec_token=xsec_token, xsec_source=xsec_source,
                    max_scrolls=app_runtime.cfg.engine.comment_max_scrolls,
                    block_media=app_runtime.cfg.engine.block_media_resources)
                if read_error and not raw:
                    return None, read_error
                parsed = [
                    item for item in (parse_rednote_comment(v) for v in flatten_rednote_comments(raw))
                    if item
                ]
                parsed.sort(key=lambda c: c.get("create_time") or 0, reverse=True)
                return {"comments": parsed, "total": len(parsed), "has_more": False}, ""
            return None, "浏览器读取不可用"
        if account_profiles_service._xhs_browser_reads_enabled() and identity is not None:
            raw, read_error = await fetch_xhs_comments(
                app_runtime.browser, identity, note_id, set(),
                xsec_token=xsec_token, xsec_source=xsec_source,
                max_scrolls=app_runtime.cfg.engine.comment_max_scrolls,
                block_media=app_runtime.cfg.engine.block_media_resources)
            if read_error:
                category, _signal = classify_platform_error(read_error)
                if not raw or category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return None, read_error
            parsed = [item for item in
                      (parse_xhs_comment(value)
                       for value in flatten_comments(raw)) if item]
            parsed.sort(
                key=lambda comment: comment.get("create_time") or 0,
                reverse=True)
            return {
                "comments": parsed,
                "total": len(parsed),
                "has_more": False,
            }, ""
        client = XhsApiClient(
            cookie,
            (account_profiles_service._direct_request_ua(identity)
             if app_runtime.browser is not None and identity is not None
             else app_runtime.cfg.engine.user_agent),
            timeout=app_runtime.cfg.engine.request_timeout_seconds, proxy=proxy)
        async with client.session_scope():
            # Token refresh and both comment levels share this bounded session.
            tok, src = xsec_token, xsec_source
            try:
                item = await client.note_detail_raw(
                    note_id, xsec_token=xsec_token, xsec_source=xsec_source)
                fresh_token = (item.get("xsec_token") or
                               ((item.get("note_card") or {}).get("xsec_token")))
                if fresh_token:
                    tok, src = fresh_token, "pc_feed"
            except XhsApiError as exc:
                if exc.category in {"risk", "auth", "network"}:
                    return None, exc
            except Exception as exc:
                category, _signal = classify_platform_error(exc)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return None, exc
                raise
            try:
                data = await client.collect_note_comments(
                    note_id, xsec_token=tok, xsec_source=src,
                    max_comments=200,
                    max_requests=max(1, min(20, app_runtime.cfg.engine.comment_max_scrolls)),
                    include_replies=True,
                    request_interval=max(0.5, app_runtime.cfg.engine.xhs_item_gap_seconds))
            except XhsApiError as exc:
                return None, exc
        raw = data.get("comments") or []
        comments = [
            comment for comment in (
                parse_xhs_comment(item) for item in flatten_comments(raw))
            if comment
        ]
        comments.sort(
            key=lambda comment: comment.get("create_time") or 0, reverse=True)
        return {
            "comments": comments,
            "total": len(comments),
            "has_more": bool(data.get("has_more")),
        }, ""

    payload, outcome = await account_profiles_service._run_account_read(
        account_id, OperationKind.READ_HEAVY,
        f"note-comments:{account_id}:{note_id}", _fetch_note_comments,
        empty_result={"comments": [], "total": 0, "has_more": False},
        unexpected_detail="取评论失败")
    if isinstance(outcome, dict):
        return outcome
    if outcome:
        raise HTTPException(400, f"取评论失败:{outcome}")
    return payload
