"""Manual stable-release discovery. Never downloads or executes installer code."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

REPOSITORY = "3441293738/creatorhub"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d{1,8}\.\d{1,8}\.\d{1,8}(?:\.\d{1,8})?", value):
        return None
    parts = tuple(map(int, value.removeprefix("v").split(".")))
    return parts + (0,) * (4 - len(parts))


def release_info(data, current):
    if not isinstance(data, dict):
        raise ValueError("Invalid release")
    tag = data.get("tag_name")
    latest = numeric_version(tag)
    if data.get("draft") or data.get("prerelease") or latest is None:
        raise ValueError("Not a supported stable release")
    version = tag.removeprefix("v")
    asset_name = f"CreatorHub-Setup-{version}-windows-x64.exe"
    download_url = f"{RELEASES_URL}/download/{quote(tag, safe='')}/{asset_name}"
    asset = next((a for a in data.get("assets", []) if isinstance(a, dict)
                  and a.get("name") == asset_name and a.get("state") == "uploaded"
                  and a.get("browser_download_url") == download_url
                  and isinstance(a.get("size"), int) and a["size"] > 0), None)
    installed = numeric_version(current)
    status = "development" if installed is None else "available" if latest > installed else "current"
    messages = {"development": "当前为源码开发版，请按发布版本自行选择安装。",
                "available": "发现新版本，先看看这次更新了什么。",
                "current": "当前版本已是最新，或比最新正式版更新。"}
    if status == "available" and not asset:
        status = "unavailable"
        messages[status] = "发现新版本，但 Windows x64 安装包尚未发布。"
    return {"status": status, "message": messages[status], "version": version,
            "tag": tag, "release_url": f"{RELEASES_URL}/tag/{quote(tag, safe='')}",
            "notes": str(data.get("body") or "维护者暂未填写更新说明。")[:50000],
            "published_at": str(data.get("published_at") or "")[:30],
            "asset_name": asset_name if asset else None,
            "download_url": download_url if asset else None,
            "size": asset["size"] if asset else None}


def fetch_release(current):
    request = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
        "User-Agent": "CreatorHub-Desktop-Update-Checker", "X-GitHub-Api-Version": "2026-03-10"})
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("Release response too large")
    return release_info(json.loads(body), current)


class UpdateChecker:
    def __init__(self, current):
        self.current = current
        self.lock = threading.RLock()
        self.worker = None
        self.last_attempt = -float("inf")
        self.result = {"status": "idle", "message": "手动检查正式版更新，不会自动下载或安装。"}

    def state(self):
        with self.lock:
            return dict(self.result)

    def check(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            if time.monotonic() - self.last_attempt < 5:
                raise ValueError("刚刚检查过，请稍等几秒再试。")
            self.last_attempt = time.monotonic()
            self.result = {"status": "checking", "message": "正在连接 GitHub，检查正式版本…"}
            self.worker = threading.Thread(target=self.run, daemon=True)
            self.worker.start()

    def run(self):
        try:
            result = fetch_release(self.current)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                result = {"status": "empty", "message": "暂未找到公开的正式版本，请稍后再检查。"}
            elif exc.code in {403, 429}:
                result = {"status": "error", "message": "GitHub 暂时限制了请求，请稍后重试，或前往发布页查看。"}
            else:
                result = {"status": "error", "message": "更新服务暂时不可用，请稍后重试。"}
        except (OSError, ValueError, TypeError, KeyError):
            result = {"status": "error", "message": "检查未完成，请检查网络后重试，或前往发布页查看。"}
        with self.lock:
            self.result = {**result, "checked_at": time.time()}

    def download_url(self, tag):
        with self.lock:
            if self.result.get("tag") != tag or self.result.get("status") not in {"available", "development"} or not self.result.get("download_url"):
                raise ValueError("版本信息已变化或安装包尚未就绪，请重新检查更新。")
            return self.result["download_url"]
