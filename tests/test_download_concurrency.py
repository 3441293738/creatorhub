"""Shared monitor downloads use one writer per destination, without network."""
import asyncio
from pathlib import Path

import pytest

import app.engine.downloader as downloads
from app.platforms.douyin.extract import Aweme, MediaItem


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    class OfflineClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(downloads.httpx, "AsyncClient", OfflineClient)
    return downloads.Downloader(str(tmp_path / "media"), "fixture")


def work(identifier="shared-work", media_count=1):
    return Aweme(identifier, "作品", 0, "主页",
        "video" if media_count == 1 else "images", medias=[
            MediaItem(f"https://media.invalid/{identifier}/{index}",
                      "video" if media_count == 1 else "image",
                      "mp4" if media_count == 1 else "jpeg", index)
            for index in range(media_count)])


@pytest.mark.parametrize("media_count", [1, 3])
def test_shared_destinations_are_written_once_and_rechecked_after_waiting(downloader, media_count):
    writes = []
    active = set()

    async def write(_client, _url, path):
        resolved = path.resolve()
        assert resolved not in active, "concurrent writers share the same .part file"
        active.add(resolved)
        writes.append(resolved)
        await asyncio.sleep(0)
        path.write_bytes(b"complete media")
        active.remove(resolved)
        return ""

    downloader._download_one = write

    async def run():
        alias = str(downloader.media_dir / ".." / "media")
        return await asyncio.gather(
            downloader.download_aweme(work(media_count=media_count)),
            downloader.download_aweme(work(media_count=media_count), base_dir=alias))

    results = asyncio.run(run())
    assert all(ok and not error for ok, _, error in results)
    assert len(writes) == len(set(writes)) == media_count
    assert len({Path(path).resolve() for _, path, _ in results}) == 1
    assert all(path.read_bytes() == b"complete media" for path in writes)


def test_different_destinations_still_download_in_parallel(downloader):
    async def run():
        both_started = asyncio.Event()
        active = set()

        async def write(_client, _url, path):
            active.add(path)
            if len(active) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
            path.write_bytes(b"complete media")
            active.remove(path)
            return ""

        downloader._download_one = write
        return await asyncio.gather(*(downloader.download_aweme(work(name))
                                      for name in ("first", "second")))

    assert all(ok and not error for ok, _, error in asyncio.run(run()))


def test_failed_writer_does_not_prevent_the_waiting_download_from_retrying(downloader):
    attempts = []

    async def write(_client, _url, path):
        attempts.append(path)
        if len(attempts) == 1:
            await asyncio.sleep(0)
            return "fixture download failure"
        path.write_bytes(b"complete media")
        return ""

    downloader._download_one = write

    async def run():
        return await asyncio.gather(*(downloader.download_aweme(work()) for _ in range(2)))

    failed, succeeded = asyncio.run(run())
    assert not failed[0] and failed[2] == "fixture download failure"
    assert succeeded[0] and Path(succeeded[1]).read_bytes() == b"complete media"
    assert len(attempts) == 2


def test_canceling_a_writer_releases_the_destination_for_waiters(downloader):
    async def run():
        started = asyncio.Event()

        async def write(_client, _url, path):
            if not started.is_set():
                started.set()
                await asyncio.Future()
            path.write_bytes(b"complete media")
            return ""

        downloader._download_one = write
        first = asyncio.create_task(downloader.download_aweme(work()))
        await started.wait()
        second = asyncio.create_task(downloader.download_aweme(work()))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await asyncio.wait_for(second, timeout=2)

    ok, path, error = asyncio.run(run())
    assert ok and not error and Path(path).read_bytes() == b"complete media"
