"""媒体下载器。对应逆向 engine.MediaDownloader。
优化:同一作品多个媒体并发下载、按 Content-Length 校验完整性、失败退避重试。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional, Tuple
from weakref import WeakValueDictionary

import httpx

from ..platforms.douyin.extract import Aweme, safe_title


class Downloader:
    def __init__(self, media_dir: str, user_agent: str, timeout: float = 120.0,
                 per_post_concurrency: int = 4):
        self.media_dir = Path(media_dir)
        self.ua = user_agent
        self.timeout = timeout
        self._media_sem = asyncio.Semaphore(per_post_concurrency)
        # 没有写入者或等待者后自动回收，避免长期监控积累文件锁。
        self._file_locks: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()

    def _target_dir(self, author: str, base_dir: str = "") -> Path:
        root = Path(base_dir).expanduser() if base_dir else self.media_dir
        d = root / safe_title(author or "unknown")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _filename(self, aweme: Aweme, m, title: str) -> str:
        if aweme.media_type == "images":
            return f"{aweme.aweme_id}_{title}_{m.index}.{m.ext}"
        return f"{aweme.aweme_id}_{title}.{m.ext}"

    async def download_aweme(self, aweme: Aweme, base_dir: str = "",
                             proxy: str = "") -> Tuple[bool, str, str]:
        """下载一条作品的全部媒体(并发)。返回 (ok, local_path, error)。
        proxy 非空时,媒体拉流走该账号专属代理,避免 CDN 拉流暴露宿主真实 IP。"""
        from ..browser.manager import normalize_proxy
        proxy = normalize_proxy(proxy)        # 裸 host:port 补 scheme(httpx 必须带)
        out_dir = self._target_dir(aweme.author_name, base_dir)
        title = safe_title(aweme.desc) or aweme.aweme_id
        _pf = getattr(aweme, "platform", "douyin")
        referer = ("https://www.xiaohongshu.com/" if _pf == "xhs"
                   else "https://www.kuaishou.com/" if _pf == "kuaishou"
                   else "https://www.douyin.com/")
        headers = {"User-Agent": self.ua, "Referer": referer}

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                         headers=headers, proxy=proxy or None) as cli:
                async def fetch(m):
                    fpath = out_dir / self._filename(aweme, m, title)
                    # 多账号监控可能下载同一作品；同一路径只留一个写入者，
                    # 等待者拿锁后复查成品，避免共享 .part 文件互相覆盖。
                    lock = self._file_locks.setdefault(fpath.resolve(), asyncio.Lock())
                    async with lock:
                        if fpath.exists() and fpath.stat().st_size > 0:
                            return str(fpath), ""        # 已存在,跳过
                        async with self._media_sem:
                            err = await self._download_one(cli, m.url, fpath)
                        return (str(fpath), "") if not err else ("", err)

                results = await asyncio.gather(*(fetch(m) for m in aweme.medias))
        except Exception as e:
            return False, "", str(e)

        saved = [p for p, err in results if p]
        errs = [err for p, err in results if err]
        if errs:
            return False, "", "; ".join(errs[:2])
        if not saved:
            return False, "", "无可下载媒体"
        return True, saved[0] if len(saved) == 1 else str(out_dir), ""

    async def _download_one(self, cli: httpx.AsyncClient, url: str, fpath: Path) -> str:
        """带断点续传的下载。成功返回空串,失败返回错误信息。
        .part 临时文件在多次尝试间保留,用 HTTP Range 续传。
        """
        tmp = fpath.with_suffix(fpath.suffix + ".part")
        last = "未知错误"
        for attempt in range(3):
            try:
                resume = tmp.stat().st_size if tmp.exists() else 0
                headers = {"Range": f"bytes={resume}-"} if resume else {}
                async with cli.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416:           # 已下完(范围越界)
                        if tmp.exists() and tmp.stat().st_size > 0:
                            tmp.replace(fpath)
                            return ""
                        tmp.unlink(missing_ok=True)
                        last = "HTTP 416"
                        continue
                    if r.status_code not in (200, 206):
                        last = f"HTTP {r.status_code}"
                        await asyncio.sleep(attempt + 1)
                        continue

                    resuming = (r.status_code == 206 and resume > 0)
                    mode = "ab" if resuming else "wb"
                    written = resume if resuming else 0
                    total = self._expected_total(r, resuming)
                    with open(tmp, mode) as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                    if total and written < total:      # 仍不完整,保留 .part 续传
                        last = f"不完整 {written}/{total}"
                        await asyncio.sleep(attempt + 1)
                        continue
                    tmp.replace(fpath)
                    return ""
            except Exception as e:
                last = repr(e)                          # 网络中断也保留 .part,下次续传
                await asyncio.sleep(attempt + 1)
        return f"下载失败({last}): {url[:80]}"

    @staticmethod
    def _expected_total(r, resuming: bool) -> int:
        if resuming:
            cr = r.headers.get("content-range", "")     # bytes start-end/total
            tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
            return int(tail) if tail.isdigit() else 0
        return int(r.headers.get("content-length") or 0)
