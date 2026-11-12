import io
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest
from PIL import Image

from app import media_tools
from app.runtime_tools import find_ffmpeg
from app.platforms.xhs import creator_api, media


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "中文 video.mp4"
    subprocess.run([find_ffmpeg(), "-nostdin", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=160x90:r=25:d=1", "-c:v", "libx264", str(path)],
                   check=True, capture_output=True, timeout=20)
    return path


def test_real_video_cover_dimensions_duration_and_no_numpy(video):
    cover, meta = media_tools.video_cover_and_meta(video)
    with Image.open(io.BytesIO(cover)) as image:
        assert image.format == "JPEG"
        assert image.size == (160, 90)
    assert meta["video"]["duration"] == 1000
    assert meta["video"]["frame_rate"] == 25
    assert video.is_file()


def test_bytes_and_disk_media_both_work(video):
    blob = video.read_bytes()
    for source in (blob, media.MediaFile(video, len(blob))):
        cover, meta = creator_api._video_cover_and_meta(source)
        assert cover.startswith(b"\xff\xd8")
        assert meta["video"]["width"] == 160
    assert video.read_bytes() == blob


def test_pixel_budget_checked_before_cover_decode(video):
    with pytest.raises(media_tools.MediaToolError, match="像素"):
        media_tools.video_cover_and_meta(video, max_pixels=100)


def test_invalid_media_has_actionable_error(tmp_path):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(media_tools.MediaToolError, match="元信息"):
        media_tools.video_cover_and_meta(path)


def test_timeout_is_bounded_and_reported(video, monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired("ffmpeg", 1))
    monkeypatch.setattr(media_tools.subprocess, "run", run)
    with pytest.raises(media_tools.MediaToolError, match="超时"):
        media_tools.video_cover_and_meta(video)


def test_missing_runtime_does_not_trigger_network(video, monkeypatch):
    monkeypatch.setattr(media_tools, "find_ffmpeg", lambda: "")
    with pytest.raises(media_tools.MediaToolError, match="组件缺失"):
        media_tools.video_cover_and_meta(video)
