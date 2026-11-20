"""Resolve Rednote note/profile URLs and construct canonical ``rednote.com`` links."""
from __future__ import annotations

import re
import urllib.parse as up
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from ...netfp import impersonate_for_ua
from .domains import WWW_URL, is_rednote_host

_ID_RE = r"[0-9a-fA-F]{24}"
_NOTE_PATH_RE = re.compile(rf"/(?:explore|discovery/item|item)/({_ID_RE})")
_USER_PATH_RE = re.compile(rf"/user/profile/({_ID_RE})")
_XSEC_TOKEN_RE = re.compile(r"xsec_token=([A-Za-z0-9_\-=+%]+)")
_XSEC_SOURCE_RE = re.compile(r"xsec_source=([A-Za-z0-9_\-]+)")
_URL_CANDIDATE_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SHORT_RE = re.compile(r"https?://(?:xhslink\.com|rednote\.link)/[\w/\-]+", re.IGNORECASE)
_BARE_ID_RE = re.compile(rf"^{_ID_RE}$")


@dataclass
class RednoteNoteRef:
    note_id: str
    xsec_token: str = ""
    xsec_source: str = ""


@dataclass
class RednoteUserRef:
    user_id: str
    xsec_token: str = ""
    xsec_source: str = ""


def _clean_url_token(url_text: str) -> str:
    return url_text.rstrip(".,，。!！?？)）]】>\"'")


def _extract_tokens(text: str) -> tuple[str, str]:
    tok = _XSEC_TOKEN_RE.search(text)
    src = _XSEC_SOURCE_RE.search(text)
    return (
        up.unquote(tok.group(1)) if tok else "",
        up.unquote(src.group(1)) if src else "",
    )


def build_rednote_note_url(note_id: str, xsec_token: str = "", xsec_source: str = "") -> str:
    """Build canonical ``https://www.rednote.com/zh/discovery/item/{note_id}`` URL.

    Uses ``/zh/discovery/item/`` so EdgeOne serves Rednote SSR directly (200 OK)
    instead of 302-redirecting unprefixed paths to ``www.xiaohongshu.com``.
    Guards against empty/None ``xsec_token`` so ``?xsec_token=undefined`` is
    never emitted.
    """
    nid = str(note_id or "").strip()
    if not nid:
        return ""
    base = f"{WWW_URL}/zh/discovery/item/{nid}"
    token = str(xsec_token or "").strip()
    if not token or token == "undefined":
        return base
    params: dict[str, str] = {"xsec_token": token}
    source = str(xsec_source or "").strip()
    if source and source != "undefined":
        params["xsec_source"] = source
    return f"{base}?{up.urlencode(params)}"


def build_rednote_profile_url(
    user_id: str, xsec_token: str = "", xsec_source: str = ""
) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    base = f"{WWW_URL}/zh/user/profile/{uid}"
    params: dict[str, str] = {}
    if xsec_token and xsec_token != "undefined":
        params["xsec_token"] = str(xsec_token).strip()
    if xsec_source and xsec_source != "undefined":
        params["xsec_source"] = str(xsec_source).strip()
    return f"{base}?{up.urlencode(params)}" if params else base


async def _follow_short(text: str, user_agent: str) -> Optional[str]:
    short = _SHORT_RE.search(text)
    if not short:
        return None
    try:
        async with AsyncSession(impersonate=impersonate_for_ua(user_agent)) as cli:
            resp = await cli.get(
                _clean_url_token(short.group(0)),
                headers={"User-Agent": user_agent},
                timeout=15,
            )
            return str(resp.url)
    except Exception:
        return None


def _match_rednote_url(candidate: str, pattern: re.Pattern[str]) -> Optional[re.Match[str]]:
    cleaned = _clean_url_token(candidate)
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.hostname and not is_rednote_host(parsed.hostname):
        return None
    return pattern.search(parsed.path or "")


async def resolve_rednote_note(text: str, user_agent: str = "") -> Optional[RednoteNoteRef]:
    """Resolve a Rednote explore/discovery URL, share text, short link, or bare 24-hex note ID."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if _BARE_ID_RE.match(raw):
        return RednoteNoteRef(note_id=raw)

    candidates = _URL_CANDIDATE_RE.findall(raw)
    for url_candidate in candidates:
        matched = _match_rednote_url(url_candidate, _NOTE_PATH_RE)
        if matched:
            tok, src = _extract_tokens(url_candidate)
            return RednoteNoteRef(
                note_id=matched.group(1),
                xsec_token=tok,
                xsec_source=src or "pc_feed",
            )

    if not candidates:
        matched = _NOTE_PATH_RE.search(raw)
        if matched and "xiaohongshu.com" not in raw.lower():
            tok, src = _extract_tokens(raw)
            return RednoteNoteRef(
                note_id=matched.group(1),
                xsec_token=tok,
                xsec_source=src or "pc_feed",
            )

    final = await _follow_short(raw, user_agent)
    if final:
        matched = _match_rednote_url(final, _NOTE_PATH_RE)
        if matched:
            tok, src = _extract_tokens(final)
            return RednoteNoteRef(
                note_id=matched.group(1),
                xsec_token=tok,
                xsec_source=src or "pc_feed",
            )
    return None


async def resolve_rednote_user(text: str, user_agent: str = "") -> Optional[RednoteUserRef]:
    """Resolve a Rednote profile URL, share text, short link, or bare 24-hex user ID."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if _BARE_ID_RE.match(raw):
        return RednoteUserRef(user_id=raw)

    candidates = _URL_CANDIDATE_RE.findall(raw)
    for url_candidate in candidates:
        matched = _match_rednote_url(url_candidate, _USER_PATH_RE)
        if matched:
            tok, src = _extract_tokens(url_candidate)
            return RednoteUserRef(
                user_id=matched.group(1),
                xsec_token=tok,
                xsec_source=src or "pc_search",
            )

    if not candidates:
        matched = _USER_PATH_RE.search(raw)
        if matched and "xiaohongshu.com" not in raw.lower():
            tok, src = _extract_tokens(raw)
            return RednoteUserRef(
                user_id=matched.group(1),
                xsec_token=tok,
                xsec_source=src or "pc_search",
            )

    final = await _follow_short(raw, user_agent)
    if final:
        matched = _match_rednote_url(final, _USER_PATH_RE)
        if matched:
            tok, src = _extract_tokens(final)
            return RednoteUserRef(
                user_id=matched.group(1),
                xsec_token=tok,
                xsec_source=src or "pc_search",
            )
    return None


def looks_like_rednote_note(text: str) -> bool:
    raw = str(text or "").strip()
    if "xiaohongshu.com" in raw.lower():
        return False
    return bool(_NOTE_PATH_RE.search(raw)) and not _USER_PATH_RE.search(raw)
