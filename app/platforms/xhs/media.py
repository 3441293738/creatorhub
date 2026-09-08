"""Local media preflight shared by both XHS publishing channels."""
from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


MAX_IMAGES = 18
# Project resource budgets, not claims about the platform's upload limits.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_IMAGE_BATCH_BYTES = 256 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class MediaFile:
    """A private, disk-backed snapshot owned by one publishing operation."""
    path: Path
    size: int


def validate_media_size(media_type: str, size: int, total: int) -> None:
    single = MAX_VIDEO_BYTES if media_type == "video" else MAX_IMAGE_BYTES
    batch = MAX_VIDEO_BYTES if media_type == "video" else MAX_IMAGE_BATCH_BYTES
    if size <= 0:
        raise ValueError("媒体文件为空，本次未提交发布")
    if size > single or total > batch:
        raise ValueError(
            f"媒体超过本项目资源上限：单文件 {single // (1024 * 1024)} MiB，"
            f"整组 {batch // (1024 * 1024)} MiB；请缩小文件后重新提交")


def media_size(source: bytes | bytearray | MediaFile) -> int:
    if isinstance(source, MediaFile):
        info = source.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != source.size:
            raise ValueError("媒体快照已变化，本次未提交发布")
        return source.size
    if isinstance(source, (bytes, bytearray)):
        return len(source)
    raise ValueError("媒体内容为空或格式异常")


def validate_media_count(media_type: str, count: int) -> None:
    if not isinstance(media_type, str) or media_type not in {"image", "images", "video"}:
        raise ValueError("小红书发布媒体类型须为 image、images 或 video")
    if media_type == "video":
        if count != 1:
            raise ValueError("小红书视频发布须选择且仅选择 1 个视频文件")
    elif not 1 <= count <= MAX_IMAGES:
        raise ValueError(f"本项目小红书图文发布支持 1–{MAX_IMAGES} 张图片，请调整后重新提交")


def validate_publish_files(media_type: str, files: Sequence[str]) -> list[str]:
    """Validate the whole selection without dropping, sorting or truncating it."""
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise ValueError("媒体文件须为路径列表")
    validate_media_count(media_type, len(files))
    paths = []
    total = 0
    for index, raw in enumerate(files, 1):
        if not isinstance(raw, (str, os.PathLike)) or not raw:
            raise ValueError(f"第 {index} 个媒体文件路径为空或格式异常，请重新选择")
        try:
            path = Path(raw)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError("not a nonempty regular file")
            # Check readability without loading every image/video into memory.
            with path.open("rb") as stream:
                if not stream.read(1):
                    raise ValueError("empty file")
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(
                f"第 {index} 个媒体文件不存在、为空或不可读取，请重新选择；本次未提交发布") from exc
        total += info.st_size
        validate_media_size(media_type, info.st_size, total)
        paths.append(str(path))
    return paths


@contextmanager
def snapshot_publish_files(media_type: str, files: Sequence[str], *, check_active):
    """Freeze ALL files before network I/O, using bounded memory and disk space.

    Snapshots survive source edits/deletion after copying, and are removed on
    success, error or cancellation only after the publishing worker has exited.
    """
    check_active()
    paths = validate_publish_files(media_type, files)
    with tempfile.TemporaryDirectory(prefix="creatorhub-xhs-media-") as root:
        snapshots = []
        total = 0
        for index, raw in enumerate(paths):
            check_active()
            path = Path(raw)
            target = Path(root) / f"{index}{path.suffix}"
            size = 0
            with path.open("rb") as src, target.open("wb") as dst:
                before = os.fstat(src.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("媒体文件类型已变化，本次未提交发布")
                validate_media_size(media_type, before.st_size, total + before.st_size)
                while True:
                    check_active()
                    chunk = src.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    validate_media_size(media_type, size, total + size)
                    dst.write(chunk)
                after = os.fstat(src.fileno())
            # Detect a changing/truncated source instead of uploading a mixed copy.
            if (size != before.st_size or size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ValueError("媒体文件在读取期间发生变化，本次未提交发布")
            validate_media_size(media_type, size, total + size)
            total += size
            snapshots.append(MediaFile(target, size))
        check_active()
        yield snapshots
