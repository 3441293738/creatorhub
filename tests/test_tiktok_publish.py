import asyncio
import inspect
from pathlib import Path

from app.platforms.tiktok.publish import (
    _dismiss_upload_dialogs,
    _first_attached,
    _notify_submit,
    _page_state,
    _privacy_button,
    _video_file,
    build_caption,
    publish_tiktok,
)


def test_tiktok_caption_keeps_order_deduplicates_topics_and_obeys_limit():
    caption = build_caption(
        "Product update", "A concise description", "Tools, #News，tools, 中文"
    )
    assert caption == "Product update\nA concise description\n#Tools #News #中文"
    assert build_caption("same", "same", "", limit=4) == "same"


def test_tiktok_video_validation_is_single_media_type(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fixture")
    path, error = _video_file("video", [str(video)])
    assert path == str(video.resolve())
    assert error == ""
    assert "仅支持单个视频" in _video_file("images", [str(video)])[1]
    assert "路径不存在" in _video_file("video", [str(tmp_path / "missing.mp4")])[1]


def test_tiktok_submit_marker_supports_sync_and_async_callbacks():
    events = []

    async def async_marker():
        events.append("async")

    asyncio.run(_notify_submit(lambda: events.append("sync")))
    asyncio.run(_notify_submit(async_marker))
    assert events == ["sync", "async"]


def test_tiktok_upload_accepts_hidden_attached_file_input():
    class Input:
        def __init__(self):
            self.waited_for = ""

        async def wait_for(self, state, timeout):
            self.waited_for = state

        async def get_attribute(self, name):
            return None

    class Candidates:
        def __init__(self, control):
            self.control = control

        async def count(self):
            return 1

        def nth(self, index):
            return self.control

    class Page:
        def __init__(self, control):
            self.control = control

        def locator(self, selector):
            return Candidates(self.control)

    control = Input()
    found = asyncio.run(_first_attached(Page(control), ('input[type="file"]',)))
    assert found is control
    assert control.waited_for == "attached"


def test_tiktok_upload_page_state_separates_loading_login_and_verification():
    assert _page_state("https://www.tiktok.com/creator-center/upload", "") == (
        "loading", "")
    assert _page_state("https://www.tiktok.com/login", "") == (
        "logged_out", "")
    assert _page_state("https://www.tiktok.com/creator-center/upload",
                       "Security verification") == (
        "verification", "Security verification")


def test_tiktok_upload_dismisses_optional_dialogs_in_safe_order():
    source = inspect.getsource(_dismiss_upload_dialogs)
    assert "get_by_text(marker" in source
    assert "container.get_by_role" in source
    assert "page.get_by_role" not in source


def test_tiktok_privacy_control_is_scoped_from_its_label_before_fallback():
    source = inspect.getsource(_privacy_button)
    assert '"谁可以看"' in source
    assert "ancestor::*[.//*[@role='combobox']]" in source
    assert source.index("Who can watch this video") < source.index("selectors =")


def test_tiktok_publish_marks_no_retry_boundary_before_click():
    source = inspect.getsource(publish_tiktok)
    marker = source.index("await _notify_submit(on_submit)")
    click = source.index("await button.click(timeout=5000)", marker)
    assert marker < click
    assert "write_uncertain:" in source
