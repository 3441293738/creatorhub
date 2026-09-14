"""Bounded, content-addressed download cache with verified HTTP Range resume."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import time


class DownloadCancelled(Exception):
    pass


def prune_cache(cache, keep):
    """Only our digest-named files; never recurse into update attempts/user data."""
    files = sorted((p for p in cache.iterdir() if p.is_file() and not p.is_symlink()
                    and re.fullmatch(r"[a-f0-9]{64}\.(part|bin)", p.name)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for path in files:
        total += path.stat().st_size
        if path.stem != keep and (time.time() - path.stat().st_mtime > 14 * 86400 or total > 2 * 1024 ** 3):
            try:
                path.unlink()
            except OSError:
                pass


def download_file(info, cache, cancel, progress, opener):
    """Retry after cancellation/network loss reuses bytes even after app restart.

Expected SHA-256 identifies both the cache key and the immutable release content;
no partial/cached file is handed to the installer until its entire hash matches.
"""
    size, expected = info["size"], info["sha256"]
    if type(size) is not int or not 0 < size <= 2 * 1024 ** 3 or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Invalid update download identity")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink() or getattr(cache.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("更新缓存目录不支持链接。")
    partial, complete = cache / (expected + ".part"), cache / (expected + ".bin")
    for path in (partial, complete):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("更新缓存文件不支持链接。")
    prune_cache(cache, expected)

    def cancelled():
        if cancel.is_set():
            raise DownloadCancelled()

    def digest_of(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                cancelled()
                digest.update(chunk)
        return digest

    cancelled()
    if complete.is_file():
        if complete.stat().st_size == size and hmac.compare_digest(digest_of(complete).hexdigest(), expected):
            progress(size, size, size)
            return complete
        complete.unlink()
    if partial.exists() and partial.stat().st_size > size:
        partial.unlink()
    offset = partial.stat().st_size if partial.is_file() else 0
    if shutil.disk_usage(cache).free < size - offset + 64 * 1024 ** 2:
        raise ValueError("下载目录空间不足，请清理磁盘后重试。")
    digest = digest_of(partial) if offset else hashlib.sha256()
    progress(offset, size, offset)
    try:
        if offset < size:
            response = opener(info["download_url"], offset=offset) if offset else opener(info["download_url"])
            with response:
                status = getattr(response, "status", 200)
                if status == 200:
                    # A server without Range support safely starts from zero.
                    offset, digest = 0, hashlib.sha256()
                elif status == 206 and offset:
                    content_range = response.headers.get("Content-Range", "")
                    if content_range != f"bytes {offset}-{size - 1}/{size}":
                        raise ValueError("续传范围与发布文件不一致，请重试。")
                else:
                    raise ValueError("更新服务器返回了无效的下载响应。")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != size - offset:
                    raise ValueError("下载文件大小与发布记录不一致。")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("更新服务器返回了不支持的内容编码。")
                downloaded, resumed = offset, offset
                deadline = time.monotonic() + 3600
                with partial.open("ab" if offset else "wb") as target:
                    while True:
                        cancelled()
                        if time.monotonic() > deadline:
                            raise TimeoutError("Download deadline exceeded")
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > size:
                            raise ValueError("下载文件超出发布大小。")
                        target.write(chunk)
                        digest.update(chunk)
                        progress(downloaded, size, resumed)
                    target.flush()
                    os.fsync(target.fileno())
                if downloaded != size:
                    raise OSError("Incomplete response; retain verified-on-completion partial bytes")
        cancelled()
        if not hmac.compare_digest(digest.hexdigest(), expected):
            raise ValueError("更新文件完整性校验失败，已丢弃损坏文件，请重试。")
        partial.replace(complete)
        progress(size, size, offset)
        return complete
    except ValueError:
        partial.unlink(missing_ok=True)
        raise
    # Network errors and cancellation intentionally retain .part for a retry.
