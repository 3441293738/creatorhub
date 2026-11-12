"""Bounded local-video preparation using the already bundled FFmpeg runtime.

No NumPy/OpenCV, additional download, or second statically linked FFmpeg suite.
Header parsing is provided by the pinned imageio-ffmpeg dependency; both the
probe and extraction have deadlines and all temporary files are removed.
"""
import io
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from app.runtime_tools import find_ffmpeg


class MediaToolError(RuntimeError):
    pass


def video_cover_and_meta(source, *, timeout=30, max_pixels=50_000_000):
    from PIL import Image
    from imageio_ffmpeg._parsing import parse_ffmpeg_header

    executable = find_ffmpeg()
    if not executable:
        raise MediaToolError("视频处理组件缺失，请重新安装完整安装包。")
    source = Path(source).resolve(strict=True)
    deadline = time.monotonic() + timeout
    base = [executable, "-nostdin", "-hide_banner", "-protocol_whitelist", "file,pipe",
            "-i", str(source)]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with tempfile.TemporaryDirectory(prefix="creatorhub-video-") as directory:
        root = Path(directory)
        def run(args):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaToolError("视频处理超时，请检查文件后重试。")
            try:
                with (root / "ffmpeg.log").open("w+b") as log:
                    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=log, timeout=remaining, **flags)
                    log.seek(0)
                    header = log.read(256 * 1024).decode("utf-8", errors="replace")
                return result.returncode, header
            except subprocess.TimeoutExpired as exc:
                raise MediaToolError("视频处理超时，请检查文件后重试。") from exc

        # No output means FFmpeg exits nonzero after inspecting the input; no
        # frame is decoded before checking the original pixel dimensions.
        _, header = run(base)
        try:
            meta = parse_ffmpeg_header(header)
            width, height = meta["source_size"]
            fps, seconds = float(meta["fps"]), float(meta["duration"])
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise MediaToolError("视频像素数超过本项目资源上限。")
            if not all(math.isfinite(n) and n >= 0 for n in (fps, seconds)):
                raise ValueError("invalid media metadata")
        except (IndexError, KeyError, AttributeError, TypeError, ValueError) as exc:
            raise MediaToolError("视频元信息读取失败，请检查视频格式。") from exc

        cover = root / "cover.jpg"
        code, _ = run(base + ["-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                              "-threads", "1", "-q:v", "2", "-update", "1", "-y", str(cover)])
        if code or not cover.is_file() or not 0 < cover.stat().st_size <= 32 * 1024 * 1024:
            raise MediaToolError("视频封面提取失败，请检查视频文件。")
        blob = cover.read_bytes()
        try:
            with Image.open(io.BytesIO(blob), formats=("JPEG",)) as image:
                # FFmpeg applies display rotation; describe the oriented frame.
                width, height = image.size
                if width * height > max_pixels:
                    raise MediaToolError("视频像素数超过本项目资源上限。")
                image.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise MediaToolError("视频封面校验失败。") from exc
        duration = round(seconds * 1000)
        return blob, {
            "video": {"bitrate": None, "colour_primaries": "BT.709", "duration": duration,
                      "format": "AVC", "frame_rate": round(fps, 3), "height": height,
                      "matrix_coefficients": "BT.709", "rotation": 0,
                      "transfer_characteristics": "BT.709", "width": width},
            "audio": {"bitrate": None, "channels": 2, "duration": duration,
                      "format": "AAC", "sampling_rate": 48000},
        }
