"""Extract Rednote JSON payloads into normalized CreatorHub models (`Aweme`, etc.)."""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

from ..douyin.extract import Aweme
from ..xhs.creator_api import parse_creator_user as parse_rednote_creator_user
from ..xhs.extract import (
    _first,
    flatten_comments as flatten_rednote_comments,
    parse_comment as parse_rednote_comment,
    parse_note_brief as _parse_xhs_note_brief,
    parse_note_detail as _parse_xhs_note_detail,
    parse_self_user as parse_rednote_self_user,
)

_HTTPS_UPGRADE_SUFFIXES = (".rednotecdn.com", ".xhscdn.com")


def upgrade_rednote_media_https(url: str | None) -> str:
    """Upgrade plain ``http://`` Rednote/XHS CDN URLs to ``https://``.

    ``webapi.rednote.com/api/sns/web/v1/feed`` often emits ``http://sns-web-i10.rednotecdn.com/...``
    in ``info_list``. Residential SOCKS5/HTTP proxies frequently reject port 80
    with ``HTTP 502``, whereas ``https://`` (port 443) succeeds reliably.
    """
    raw = str(url or "").strip()
    if not raw.lower().startswith("http://"):
        return raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if any(host == suf.lstrip(".") or host.endswith(suf) for suf in _HTTPS_UPGRADE_SUFFIXES):
            netloc = host if parts.port in (None, 80) else f"{host}:{parts.port}"
            return urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return raw


def _collect_image_candidates(img: dict) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    def _add(val: object) -> None:
        u = upgrade_rednote_media_https(str(val or "").strip())
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)

    info = _first(img, "info_list", "infoList", default=[])
    info_list = [it for it in info if isinstance(it, dict)] if isinstance(info, list) else []
    for scene in ("WB_DFT", "WB_PRV"):
        for it in info_list:
            if _first(it, "image_scene", "imageScene") == scene:
                _add(it.get("url"))
    for it in info_list:
        _add(it.get("url"))
    for key in ("url_default", "urlDefault", "url_pre", "urlPre", "url"):
        _add(img.get(key))
    return out


def parse_rednote_brief(item: dict) -> Optional[dict]:
    brief = _parse_xhs_note_brief(item)
    if brief is not None and brief.get("cover"):
        brief["cover"] = upgrade_rednote_media_https(brief["cover"])
    return brief


def parse_rednote_detail(
    note_card: dict,
    brief: dict | None = None,
    *,
    require_media: bool = True,
) -> Optional[Aweme]:
    """Parse a Rednote ``note_card``, upgrade CDN URLs to HTTPS, and attach backup URLs."""
    aweme = _parse_xhs_note_detail(
        note_card, brief=brief, require_media=require_media
    )
    if aweme is None:
        return None
    aweme.platform = "rednote"
    if aweme.cover:
        aweme.cover = upgrade_rednote_media_https(aweme.cover)

    imgs = _first(note_card, "image_list", "imageList", default=[]) if isinstance(note_card, dict) else []
    img_dicts = [img for img in imgs if isinstance(img, dict)] if isinstance(imgs, list) else []

    for idx, media in enumerate(aweme.medias):
        media.url = upgrade_rednote_media_https(media.url)
        if media.kind == "image" and idx < len(img_dicts):
            cands = _collect_image_candidates(img_dicts[idx])
            if cands:
                media.url = cands[0]
                media.backup_urls = [u for u in cands[1:] if u != media.url]
    return aweme


__all__ = [
    "parse_rednote_brief",
    "parse_rednote_detail",
    "parse_rednote_comment",
    "flatten_rednote_comments",
    "parse_rednote_self_user",
    "parse_rednote_creator_user",
    "upgrade_rednote_media_https",
]
