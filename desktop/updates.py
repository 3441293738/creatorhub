"""Quiet release discovery; downloads and installation always require user actions."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit
import uuid
from desktop.update_download import DownloadCancelled, download_file
from desktop.update_manifest import (MAX_MANIFEST_SIZE, manifest_name, delta_name,
                                    parse_manifest, verify_tree)
from desktop.update_policy import UpdatePolicy

REPOSITORY = "3441293738/creatorhub"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_INSTALLER_SIZE = 2 * 1024 ** 3
MAX_CHECKSUM_SIZE = 64 * 1024
BUSY_STATES = {"checking", "downloading", "verifying", "preparing", "installing"}


def allowed_download_url(url):
    """Release assets may redirect to GitHub's HTTPS asset CDN, never arbitrary hosts."""
    parts = urlsplit(url)
    return (parts.scheme == "https" and not parts.username and not parts.password
            and parts.port in (None, 443) and not parts.fragment
            and (parts.hostname in {"release-assets.githubusercontent.com",
                                    "objects.githubusercontent.com",
                                    "github-releases.githubusercontent.com"}
                 or (parts.hostname == "github.com"
                     and parts.path.startswith(f"/{REPOSITORY}/releases/download/"))))


class ReleaseRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_download_url(newurl):
            raise ValueError("更新下载地址校验失败。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_asset(url, offset=0):
    if not allowed_download_url(url):
        raise ValueError("更新下载地址校验失败。")
    headers = {"User-Agent": "CreatorHub-Desktop-Updater", "Accept": "application/octet-stream",
               "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.build_opener(ReleaseRedirectHandler()).open(request, timeout=15)


def checksum_for_release(info):
    """Trust only the matching HTTPS release's SHA256.txt or GitHub asset digest."""
    expected = info.get("asset_sha256")
    if info.get("checksum_url"):
        with open_asset(info["checksum_url"]) as response:
            raw = response.read(MAX_CHECKSUM_SIZE + 1)
        if len(raw) > MAX_CHECKSUM_SIZE:
            raise ValueError("更新校验文件过大。")
        matches = []
        for line in raw.decode("utf-8-sig").splitlines():
            match = re.fullmatch(r"([a-fA-F0-9]{64})[ \t]+\*?(.+)", line)
            if match and match[2] == info["asset_name"]:
                matches.append(match[1].lower())
        if len(matches) != 1 or (expected and not hmac.compare_digest(expected, matches[0])):
            raise ValueError("安装包的 SHA-256 发布记录不匹配。")
        expected = matches[0]
    if not expected or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("此版本缺少完整的 SHA-256 校验信息，请前往发布页查看。")
    return expected


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d{1,8}\.\d{1,8}\.\d{1,8}(?:\.\d{1,8})?", value):
        return None
    parts = tuple(map(int, value.removeprefix("v").split(".")))
    return parts + (0,) * (4 - len(parts))


def release_asset(data, name, limit=MAX_INSTALLER_SIZE):
    url = f"{RELEASES_URL}/download/{quote(data['tag_name'], safe='')}/{name}"
    candidates = [a for a in data.get("assets", []) if isinstance(a, dict)
                  and a.get("name") == name and a.get("state") == "uploaded"
                  and a.get("browser_download_url") == url
                  and type(a.get("size")) is int and 0 < a["size"] <= limit]
    if len(candidates) != 1:
        return None
    asset = candidates[0]
    digest = asset.get("digest", "")
    return {"asset_name": name, "download_url": url, "size": asset["size"],
            "asset_sha256": digest[7:].lower() if isinstance(digest, str)
            and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) else None}


def fetch_manifest(info):
    asset = {**info["manifest_asset"], "checksum_url": info.get("checksum_url")}
    expected = checksum_for_release(asset)
    with open_asset(asset["download_url"]) as response:
        raw = response.read(MAX_MANIFEST_SIZE + 1)
    if len(raw) != asset["size"] or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("增量更新清单校验失败。")
    return parse_manifest(raw, info["version"]), raw


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
                  and type(a.get("size")) is int and 0 < a["size"] <= MAX_INSTALLER_SIZE), None)
    checksum_url = f"{RELEASES_URL}/download/{quote(tag, safe='')}/SHA256.txt"
    checksum_asset = next((a for a in data.get("assets", []) if isinstance(a, dict)
                           and a.get("name") == "SHA256.txt" and a.get("state") == "uploaded"
                           and a.get("browser_download_url") == checksum_url
                           and type(a.get("size")) is int
                           and 0 < a["size"] <= MAX_CHECKSUM_SIZE), None)
    digest = asset.get("digest", "") if asset else ""
    asset_sha256 = digest[7:].lower() if isinstance(digest, str) and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) else None
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
            "size": asset["size"] if asset else None,
            "checksum_url": checksum_url if checksum_asset else None,
            "asset_sha256": asset_sha256,
            "verified_download": bool(asset and (checksum_asset or asset_sha256)),
            "download_kind": "full", "download_size": asset["size"] if asset else None,
            "manifest_asset": release_asset(data, manifest_name(version), MAX_MANIFEST_SIZE),
            "delta_asset": release_asset(data, delta_name(current.removeprefix('v'), version))
                           if installed is not None and latest > installed else None}


def fetch_release(current):
    request = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
        "User-Agent": "CreatorHub-Desktop-Update-Checker", "X-GitHub-Api-Version": "2026-03-10"})
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("Release response too large")
    return release_info(json.loads(body), current)


class UpdateChecker:
    def __init__(self, current, cache_dir=None, install_dir=None):
        self.current = current
        self.lock = threading.RLock()
        self.worker = None
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self.cancel = threading.Event()
        self.artifact = None
        self.install_dir = Path(install_dir).resolve() if install_dir else None
        self.manifest = None
        self.force_full_version = None
        self.last_attempt = -float("inf")
        self.policy = UpdatePolicy(self.cache_dir / "preferences.json" if self.cache_dir else None)
        self.monitor_stop = threading.Event()
        self.monitor = None
        self.manual_notice = False
        self.result = {"status": "idle", "message": "只检查正式版更新，不会自动下载或安装。"}

    def state(self):
        with self.lock:
            status = self.result.get("status")
            notice = status in {"available", "downloaded", "download_error", "cancelled", "install_error"}
            if notice and not self.manual_notice:
                notice = not self.policy.suppressed(self.result.get("tag"), time.time())
            return {**self.result, "auto_check": self.policy.data["auto_check"],
                    "notification": bool(self.result.get("tag")) and (
                        notice or status in {"downloading", "verifying", "preparing", "installing"})}

    def configure(self, auto_check):
        if type(auto_check) is not bool:
            raise ValueError("自动检查更新设置应为开或关。")
        with self.lock:
            self.policy.save(auto_check=auto_check)

    def dismiss(self, tag, *, skip=False):
        with self.lock:
            allowed = {"available"} if skip else {"available", "downloaded", "download_error", "cancelled", "install_error"}
            if not tag or tag != self.result.get("tag") or self.result.get("status") not in allowed:
                raise ValueError("版本状态已变化，请刷新后再试。")
            self.policy.dismiss(tag, time.time(), skip=skip)
            self.manual_notice = False

    def start_monitor(self):
        """Native packaged app only. Loading/reloading the UI never duplicates timers."""
        with self.lock:
            if (not self.install_dir or numeric_version(self.current) is None
                    or self.monitor_stop.is_set() or (self.monitor and self.monitor.is_alive())):
                return
            self.monitor = threading.Thread(target=self._monitor, daemon=True)
            self.monitor.start()

    def _monitor(self):
        if self.monitor_stop.wait(15):
            return
        while not self.monitor_stop.is_set():
            self.check(automatic=True)
            if self.monitor_stop.wait(60):
                return

    def stop_monitor(self):
        self.monitor_stop.set()
        if self.monitor and self.monitor is not threading.current_thread():
            self.monitor.join(timeout=2)

    def check(self, *, automatic=False):
        with self.lock:
            if automatic and (self.monitor_stop.is_set() or not self.install_dir
                    or numeric_version(self.current) is None or not self.policy.due(time.time())
                    or self.artifact
                    or self.result.get("status") not in {"idle", "current", "empty", "error", "available", "unavailable"}
                    or (self.worker and self.worker.is_alive()) or time.monotonic() - self.last_attempt < 5):
                return False
            if self.result.get("status") in BUSY_STATES - {"checking"}:
                raise ValueError("更新正在处理中，请等待完成或取消下载。")
            if self.artifact:
                raise ValueError("新版已经下载，请安装或稍后继续，无需重新检查。")
            if self.worker and self.worker.is_alive():
                return False
            if time.monotonic() - self.last_attempt < 5:
                raise ValueError("刚刚检查过，请稍等几秒再试。")
            self.last_attempt = time.monotonic()
            self.policy.save(last_check=time.time(), best_effort=True)
            self.manual_notice = not automatic
            self.artifact = None
            self.manifest = None
            self.result = {"status": "checking", "message": "正在连接 GitHub，检查正式版本…"}
            self.worker = threading.Thread(target=self.run, daemon=True)
            self.worker.start()
            return True

    def run(self):
        try:
            result = fetch_release(self.current)
            if (result.get("status") == "available" and self.install_dir
                    and result.get("manifest_asset") and result.get("delta_asset")
                    and self.force_full_version != result["version"]):
                try:
                    manifest, raw = fetch_manifest(result)
                    delta = manifest.get("delta")
                    asset = result["delta_asset"]
                    if (not delta or delta["from_version"] != self.current
                            or delta["asset_name"] != asset["asset_name"]
                            or delta["size"] != asset["size"] or delta["size"] >= result["size"]):
                        raise ValueError("Delta is not compatible or smaller")
                    if checksum_for_release({**asset, "checksum_url": result.get("checksum_url")}) != delta["sha256"]:
                        raise ValueError("Delta release digest mismatch")
                    verify_tree(self.install_dir, delta["base_files"])
                    self.manifest = (manifest, raw)
                    result.update(download_kind="delta", download_size=delta["size"],
                                  reused_bytes=sum(f["size"] for f in manifest["files"]
                                      if f in delta["base_files"]),
                                  message="发现新版本，可增量更新：仅下载变化文件，未变化的依赖继续复用。")
                except (OSError, ValueError, TypeError, KeyError):
                    result["fallback_reason"] = "增量包不适用或本地文件有变化，将使用完整安装包。"
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
            if self.result.get("tag") != tag or self.result.get("status") not in {"available", "development", "download_error", "cancelled", "downloaded", "install_error"} or not self.result.get("download_url"):
                raise ValueError("版本信息已变化或安装包尚未就绪，请重新检查更新。")
            return self.result["download_url"]

    def download(self, tag):
        with self.lock:
            self.download_url(tag)
            if not self.cache_dir or not self.result.get("verified_download"):
                raise ValueError("此版本缺少完整的 SHA-256 校验信息，请前往发布页查看。")
            if self.worker and self.worker.is_alive():
                raise ValueError("更新正在处理中，请稍候。")
            info = dict(self.result)
            self.manual_notice = True
            self.artifact = None
            self.cancel.clear()
            self.result.update(status="downloading", message="正在下载新版，当前任务继续运行。",
                               downloaded_bytes=0, progress=0)
            self.worker = threading.Thread(target=self._download, args=(info,), daemon=True)
            self.worker.start()

    def cancel_download(self):
        with self.lock:
            if self.result.get("status") in {"downloading", "verifying"}:
                self.cancel.set()
                self.result["message"] = "正在取消下载，当前任务不受影响。"

    def _download(self, info):
        stage = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            kind = info.get("download_kind", "full")
            manifest = None
            if kind == "delta" and self.manifest:
                try:
                    manifest, raw = self.manifest
                    verify_tree(self.install_dir, manifest["delta"]["base_files"], self._cancelled)
                    selected = {**info["delta_asset"], "sha256": manifest["delta"]["sha256"]}
                    package = download_file(selected, self.cache_dir / "downloads", self.cancel, self._progress, open_asset)
                except (ValueError, KeyError):
                    kind, manifest = "full", None
                    self.force_full_version = info["version"]
                    with self.lock:
                        self.result.update(download_kind="full", download_size=info["size"],
                            fallback_reason="增量文件校验未通过，已切换完整安装包。")
            else:
                kind = "full"
            if kind == "full":
                selected = {**info, "sha256": checksum_for_release(info)}
                package = download_file(selected, self.cache_dir / "downloads", self.cancel, self._progress, open_asset)
            self._cancelled()
            if shutil.disk_usage(self.cache_dir).free < selected["size"] + 64 * 1024 ** 2:
                raise ValueError("安装暂存目录空间不足；下载缓存已保留。")
            stage = self.cache_dir / ("update-" + uuid.uuid4().hex)
            stage.mkdir()
            with self.lock:
                self.result.update(status="verifying", message="下载完成，正在核对 SHA-256…")
            installer = stage / selected["asset_name"]
            shutil.copyfile(package, installer)
            artifact = {"installer": str(installer), "sha256": selected["sha256"],
                        "size": selected["size"], "version": info["version"], "tag": info["tag"], "kind": kind}
            if manifest:
                manifest_path = stage / manifest_name(info["version"])
                manifest_path.write_bytes(raw)
                artifact.update(manifest=str(manifest_path), manifest_sha256=hashlib.sha256(raw).hexdigest(),
                                from_version=manifest["delta"]["from_version"])
            with self.lock:
                self._cancelled()
                self.artifact = artifact
                self.result.update(status="downloaded", progress=100,
                                   message="新版已下载并通过 SHA-256 校验，确认后安装并重启。")
        except Exception as exc:
            # Only this attempt's two fixed files are removed; never touch user data.
            if stage is not None:
                for name in (info["asset_name"], (info.get("delta_asset") or {}).get("asset_name"), manifest_name(info["version"])):
                    if not name:
                        continue
                    try:
                        (stage / name).unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    stage.rmdir()
                except OSError:
                    pass
            cancelled = isinstance(exc, DownloadCancelled)
            with self.lock:
                self.artifact = None
                self.result.update(status="cancelled" if cancelled else "download_error",
                    message="下载已取消，下载进度已保留，可继续下载；旧版和当前任务保持不变。" if cancelled else
                            str(exc) if isinstance(exc, ValueError) else
                            "下载未完成，已保留下载进度；检查网络和磁盘空间后重试，旧版和当前任务保持不变。")

    def _cancelled(self):
        if self.cancel.is_set():
            raise DownloadCancelled()

    def _progress(self, downloaded, size, resumed):
        with self.lock:
            self.result.update(downloaded_bytes=downloaded, download_size=size, resumed_bytes=resumed,
                               progress=min(99, int(downloaded * 100 / size)))

    def begin_install(self, tag):
        with self.lock:
            if (self.result.get("status") not in {"downloaded", "install_error"}
                    or not self.artifact or self.artifact["tag"] != tag):
                raise ValueError("请先下载并校验当前版本，再确认安装。")
            self.result.update(status="preparing", message="正在准备安装并检查下载文件…")
            return dict(self.artifact)

    def install_status(self, status, message):
        with self.lock:
            self.result.update(status=status, message=message)
