"""媒体并发下载器。
优化:
1. 改用 curl_cffi 浏览器 BoringSSL TLS/HTTP2 握手指纹(impersonate_for_ua)替代纯 httpx OpenSSL 指纹；
2. 强制 trust_env=False 隔离宿主机 HTTP_PROXY/NO_PROXY 环境变量；
3. 同一作品多个媒体并发下载、按 Content-Length 校验完整性、失败退避重试。
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any, List, Mapping, Tuple
from weakref import WeakValueDictionary

from curl_cffi.requests import AsyncSession

from ..netfp import impersonate_for_ua
from ..platforms.douyin.extract import Aweme, safe_title


class Downloader:
    def __init__(self, media_dir: str, user_agent: str, timeout: float = 120.0,
                 per_post_concurrency: int = 4):
        self.media_dir = Path(media_dir)
        self.ua = user_agent
        self.timeout = timeout
        self._media_sem = asyncio.Semaphore(per_post_concurrency)
        self._file_locks: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()

    def _target_dir(self, author: str, base_dir: str = "") -> Path:
        root = Path(base_dir).expanduser() if base_dir else self.media_dir
        d = root / safe_title(author or "unknown")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _filename(self, aweme: Aweme, m, title: str) -> str:
        if aweme.media_type == "images" or len(getattr(aweme, "medias", ()) or ()) > 1:
            return f"{aweme.aweme_id}_{title}_{m.index}.{m.ext}"
        return f"{aweme.aweme_id}_{title}.{m.ext}"

    async def download_aweme(self, aweme: Aweme, base_dir: str = "",
                             proxy: str = "", *,
                             user_agent: str = "",
                             cookies: Mapping[str, str] | None = None,
                             referer: str = "") -> Tuple[bool, str, str]:
        """下载一条作品的全部媒体(并发)。返回 (ok, local_path, error)。"""
        from ..netfp import curl_proxy_url
        norm_proxy = curl_proxy_url(proxy) or None
        out_dir = self._target_dir(aweme.author_name, base_dir)
        title = (safe_title(aweme.desc)
                 if str(aweme.desc or "").strip() else aweme.aweme_id)
        _pf = getattr(aweme, "platform", "douyin")
        platform_referer = (
            "https://www.xiaohongshu.com/" if _pf == "xhs" else
            "https://www.rednote.com/" if _pf == "rednote" else
            "https://www.kuaishou.com/" if _pf == "kuaishou" else
            "https://www.tiktok.com/" if _pf == "tiktok" else
            "https://channels.weixin.qq.com/" if _pf == "shipinhao" else
            "https://www.bilibili.com/" if _pf == "bilibili" else
            "https://www.instagram.com/" if _pf == "instagram" else
            "https://x.com/" if _pf == "twitter" else
            "https://www.douyin.com/"
        )
        effective_ua = str(user_agent or self.ua)
        headers = {
            "User-Agent": effective_ua,
            "Referer": str(referer or platform_referer),
        }
        impersonate = impersonate_for_ua(effective_ua)

        try:
            async with AsyncSession(
                timeout=self.timeout,
                allow_redirects=True,
                headers=headers,
                proxy=norm_proxy,
                cookies=dict(cookies or {}),
                impersonate=impersonate,
                trust_env=False,
            ) as cli:
                async def fetch(m):
                    fpath = out_dir / self._filename(aweme, m, title)
                    lock = self._file_locks.setdefault(fpath.resolve(), asyncio.Lock())
                    async with lock:
                        if fpath.exists() and fpath.stat().st_size > 0:
                            return str(fpath), ""
                        decode_key = str(getattr(m, "decode_key", "") or "").strip()
                        if decode_key and decode_key != "0" and m.kind == "video":
                            from ..platforms.channels.decrypt import (
                                ChannelsDecryptError,
                                decrypt_channels_video_file,
                            )
                            enc_path = fpath.with_suffix(fpath.suffix + ".enc")
                            async with self._media_sem:
                                err = await self._download_one(cli, m.url, enc_path)
                            if err:
                                return "", err
                            try:
                                await asyncio.to_thread(
                                    decrypt_channels_video_file,
                                    enc_path,
                                    fpath,
                                    decode_key,
                                    overwrite=True,
                                    validate_mp4=True,
                                )
                            except (ChannelsDecryptError, Exception) as dec_exc:
                                fpath.unlink(missing_ok=True)
                                return "", f"视频号媒体解密失败({dec_exc})"
                            finally:
                                enc_path.unlink(missing_ok=True)
                            return str(fpath), ""
                        audio_url = str(getattr(m, "audio_url", "") or "").strip()
                        if audio_url and m.kind == "video":
                            v_tmp = fpath.with_suffix(".video.m4s")
                            a_tmp = fpath.with_suffix(".audio.m4s")
                            mux_tmp = fpath.with_suffix(".mux.mp4")
                            try:
                                async with self._media_sem:
                                    v_err, a_err = await asyncio.gather(
                                        self._download_one(cli, m.url, v_tmp),
                                        self._download_one(cli, audio_url, a_tmp),
                                    )
                                if v_err or a_err:
                                    return "", v_err or a_err
                                mux_err = await asyncio.to_thread(
                                    self._mux_dash_streams, v_tmp, a_tmp, mux_tmp
                                )
                                if mux_err:
                                    return "", mux_err
                                mux_tmp.replace(fpath)
                                return str(fpath), ""
                            finally:
                                v_tmp.unlink(missing_ok=True)
                                a_tmp.unlink(missing_ok=True)
                                mux_tmp.unlink(missing_ok=True)
                        async with self._media_sem:
                            backup_urls = list(getattr(m, "backup_urls", None) or ())
                            if backup_urls:
                                err = await self._download_one(
                                    cli, m.url, fpath, backup_urls=backup_urls,
                                )
                            else:
                                err = await self._download_one(cli, m.url, fpath)
                        return (str(fpath), "") if not err else ("", err)

                async def fetch_cover() -> None:
                    cover_u = str(getattr(aweme, "cover", "") or "").strip()
                    if not cover_u or any(getattr(m, "kind", "") == "image" for m in (aweme.medias or ())):
                        return
                    cover_dest = out_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
                    if cover_dest.exists() and cover_dest.stat().st_size > 0:
                        return
                    try:
                        async with self._media_sem:
                            await self._download_one(cli, cover_u, cover_dest)
                    except Exception:
                        pass

                results, _ = await asyncio.gather(
                    asyncio.gather(*(fetch(m) for m in aweme.medias)),
                    fetch_cover(),
                )
        except Exception as e:
            return False, "", str(e)

        saved = [p for p, err in results if p]
        errs = [err for p, err in results if err]
        if errs:
            return False, "", "; ".join(errs[:2])
        if not saved:
            return False, "", "无可下载媒体"
        return True, saved[0] if len(saved) == 1 else str(out_dir), ""

    @staticmethod
    def _upgrade_cdn_https(url: str) -> str:
        raw = str(url or "").strip()
        if not raw.lower().startswith("http://"):
            return raw
        from urllib.parse import urlsplit, urlunsplit

        try:
            parts = urlsplit(raw)
            host = (parts.hostname or "").lower()
            if host.endswith((".rednotecdn.com", ".xhscdn.com")):
                netloc = host if parts.port in (None, 80) else f"{host}:{parts.port}"
                return urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))
        except ValueError:
            pass
        return raw

    async def _download_one(
        self,
        cli: Any,
        url: str,
        fpath: Path,
        *,
        backup_urls: List[str] | None = None,
    ) -> str:
        """带断点续传与备用 CDN 回退的下载(同时兼容 curl_cffi stream 与 httpx stream)。"""
        candidates: List[str] = []
        from ..services.thumb_cache import xhs_unsigned_cdn_urls
        for raw_u in [url, *(backup_urls or [])]:
            upgraded = self._upgrade_cdn_https(raw_u)
            if not upgraded:
                continue
            mirrors, is_expired = xhs_unsigned_cdn_urls(upgraded)
            ordered = [*mirrors, upgraded] if (mirrors and is_expired) else [upgraded, *mirrors]
            for cand_u in ordered:
                if cand_u and cand_u not in candidates:
                    candidates.append(cand_u)
        if not candidates:
            return "下载失败(空链接)"

        tmp = fpath.with_suffix(fpath.suffix + ".part")
        last = "未知错误"
        force_fresh_conn = False
        for cand_idx, target_url in enumerate(candidates):
            if cand_idx > 0:
                tmp.unlink(missing_ok=True)
            for attempt in range(3):
                try:
                    resume = tmp.stat().st_size if tmp.exists() else 0
                    headers = {"Range": f"bytes={resume}-"} if resume else {}
                    if force_fresh_conn:
                        headers["Connection"] = "close"
                    async with cli.stream("GET", target_url, headers=headers) as r:
                        if r.status_code == 416:
                            if tmp.exists() and tmp.stat().st_size > 0:
                                tmp.replace(fpath)
                                return ""
                            tmp.unlink(missing_ok=True)
                            last = "HTTP 416"
                            continue
                        if r.status_code not in (200, 206):
                            last = f"HTTP {r.status_code}"
                            if r.status_code in (502, 503, 504):
                                force_fresh_conn = True
                            await asyncio.sleep((attempt + 1) * random.uniform(0.85, 1.35))
                            continue

                        force_fresh_conn = False
                        resuming = (r.status_code == 206 and resume > 0)
                        mode = "ab" if resuming else "wb"
                        written = resume if resuming else 0
                        total = self._expected_total(r, resuming)
                        aiter = (
                            r.aiter_content(chunk_size=64 * 1024)
                            if hasattr(r, "aiter_content")
                            else r.aiter_bytes(64 * 1024)
                        )
                        with open(tmp, mode) as f:
                            async for chunk in aiter:
                                if chunk:
                                    f.write(chunk)
                                    written += len(chunk)
                        if total and written < total:
                            last = f"不完整 {written}/{total}"
                            force_fresh_conn = True
                            await asyncio.sleep((attempt + 1) * random.uniform(0.85, 1.35))
                            continue
                        tmp.replace(fpath)
                        return ""
                except Exception as e:
                    last = repr(e)
                    force_fresh_conn = True
                    await asyncio.sleep((attempt + 1) * random.uniform(0.85, 1.35))
        return f"下载失败({last}): {candidates[0][:80]}"

    @staticmethod
    def _expected_total(r, resuming: bool) -> int:
        if resuming:
            cr = r.headers.get("content-range", "")
            tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
            return int(tail) if tail.isdigit() else 0
        return int(r.headers.get("content-length") or 0)

    @staticmethod
    def _mux_dash_streams(video_path: Path, audio_path: Path, out_path: Path) -> str:
        """用内置 FFmpeg 无损重封装 B 站 DASH 音视频分离流为单文件 MP4。"""
        import os
        import subprocess
        from ..runtime_tools import find_ffmpeg

        exe = find_ffmpeg()
        if not exe:
            return "缺少内置 FFmpeg 组件，无法合并 B 站 DASH 音视频流"
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        cmd = [
            exe, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c", "copy",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]
        try:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
                **flags,
            )
        except subprocess.TimeoutExpired:
            return "FFmpeg 合并 DASH 音视频流超时"
        except Exception as exc:
            return f"FFmpeg 合并异常({exc})"
        if res.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            detail = (res.stderr or b"").decode("utf-8", errors="replace").strip()[:160]
            return f"FFmpeg 合并失败({detail or res.returncode})"
        return ""
