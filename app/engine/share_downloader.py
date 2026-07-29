r"""通用分享链接解析与媒体下载。

输入可以是完整分享文案，不要求调用方先清洗 URL。解析层负责兼容：

* 中文、Emoji、全角标点和零宽字符；
* ``&amp;`` 等 HTML 实体；
* ``https:\/\/``、``\uXXXX`` 等转义；
* ``https%3A%2F%2F...`` 等一至多层 URL 编码；
* 一段文本中的多个链接。

下载层使用 yt-dlp 的站点提取器，输出媒体、封面、字幕和 info.json。站点规则
持续变化，因此这里不复制各平台网页规则，只维护稳定的输入清洗与统一返回结构。
"""
from __future__ import annotations

import argparse
import asyncio
import html
import ipaddress
import json
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit


class ShareLinkError(ValueError):
    """分享文本中没有可用链接。"""


class ShareDownloadError(RuntimeError):
    """链接识别或媒体下载失败。"""


@dataclass(frozen=True)
class ShareURL:
    url: str
    platform: str
    host: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# 域名只用于友好展示和账号选择提示；不在此做白名单限制，未知站点交给通用提取器。
_PLATFORM_DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("douyin", ("douyin.com", "iesdouyin.com")),
    ("xhs", ("xiaohongshu.com", "xhslink.com")),
    ("kuaishou", ("kuaishou.com", "kwai.com", "gifshow.com")),
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("weibo", ("weibo.com", "weibo.cn")),
    ("wechat", ("weixin.qq.com", "qq.com")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("tiktok", ("tiktok.com",)),
    ("instagram", ("instagram.com",)),
    ("facebook", ("facebook.com", "fb.watch")),
    ("twitter", ("twitter.com", "x.com")),
    ("vimeo", ("vimeo.com",)),
    ("acfun", ("acfun.cn",)),
    ("ixigua", ("ixigua.com",)),
    ("toutiao", ("toutiao.com",)),
)

_INVISIBLE_RE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f"
    "\u200b\u200c\u200d\u2060\ufeff]"
)
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_SCHEME_SPACES_RE = re.compile(
    r"(?i)\b(https?|hxxps?)\s*[:：]\s*[\\/／]\s*[\\/／]\s*"
)
_CJK_SHARE_SEPARATOR_RE = re.compile(r"[，。！？；、…《》【】「」『』〔〕〈〉]")

# 第一条处理带 scheme/www 的常规链接；第二条兼容分享文本中被省略 scheme 的域名。
# Unicode 路径允许保留，结尾包裹符号由 _strip_url_edges 再处理。
_SCHEME_URL_RE = re.compile(
    r"(?i)(?:(?:https?|hxxps?)://|(?<![:/\w])//|www\.)"
    r"[^\s<>{}\[\]\"'`|\\]+"
)
_BARE_URL_RE = re.compile(
    r"(?i)(?<![@%\w])"
    r"(?:[a-z0-9\u0080-\uffff](?:[a-z0-9\u0080-\uffff-]{0,62})\.)+"
    r"(?:[a-z\u0080-\uffff]{2,63})"
    r"(?::\d{1,5})?"
    r"(?:/[^\s<>{}\[\]\"'`|\\]*)?"
)
_TRAILING_PUNCTUATION = (
    " \t\r\n"
    ".,!?;:"
    "，。！？；：、"
    "…·"
    "\"'`"
    "》】」』〕〉>}]"
)
_LEADING_PUNCTUATION = " \t\r\n<([{《【「『〔〈\"'`"


def _decode_backslash_escapes(value: str) -> str:
    value = value.replace("\\/", "/")
    value = _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    value = _HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    # 把 \uD83D\uDE00 这一类代理项对还原为正常 Unicode 字符。
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        try:
            value = value.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeError:
            pass
    return value


def normalize_share_text(value: str | bytes) -> str:
    """把剪贴板/二维码/OCR 得到的分享内容规范成可解析文本。"""
    if isinstance(value, bytes):
        for encoding in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                value = value.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        raise TypeError("分享内容必须是字符串或字节串")

    text = _decode_backslash_escapes(value)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    # 先把分享 App 常用的中文包裹标点变成空格，避免 NFKC 把「，；」
    # 变成 URL 本身允许的 `,;` 后，和紧随其后的中文提示语粘成路径。
    text = _CJK_SHARE_SEPARATOR_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)

    # OCR/富文本可能把 scheme 两侧插入空格或替换成 hxxp。
    def _scheme(match: re.Match[str]) -> str:
        scheme = match.group(1).lower().replace("hxxp", "http")
        return f"{scheme}://"

    return _SCHEME_SPACES_RE.sub(_scheme, text).strip()


def _decoded_variants(text: str) -> list[str]:
    """产生原文和最多三层 URL 解码文本，兼容嵌套编码的分享参数。"""
    variants = [text]
    current = text
    for _ in range(3):
        try:
            decoded = unquote(current)
        except Exception:
            break
        decoded = normalize_share_text(decoded)
        if decoded == current or decoded in variants:
            break
        variants.append(decoded)
        current = decoded
    return variants


def _strip_url_edges(value: str) -> str:
    value = value.strip(_LEADING_PUNCTUATION).rstrip(_TRAILING_PUNCTUATION)

    # 只移除没有配对的右括号；URL 路径内成对括号仍保留。
    pairs = (("(", ")"), ("（", "）"))
    for left, right in pairs:
        while value.endswith(right) and value.count(right) > value.count(left):
            value = value[:-1]
    return value.rstrip(_TRAILING_PUNCTUATION)


def _canonical_url(value: str) -> str | None:
    value = _strip_url_edges(value)
    value = value.replace("\\/", "/")
    if value.lower().startswith("hxxps://"):
        value = "https://" + value[8:]
    elif value.lower().startswith("hxxp://"):
        value = "http://" + value[7:]
    elif value.lower().startswith("www."):
        value = "https://" + value
    elif value.startswith("//"):
        value = "https:" + value
    elif not re.match(r"(?i)^https?://", value):
        value = "https://" + value

    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    try:
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError):
        return None
    if not host or any(ch.isspace() for ch in host):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            return None

    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    fragment = quote(parts.fragment, safe="=&%:@!$'()*+,;/?-._~")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, fragment))


def detect_platform(url_or_host: str) -> str:
    """按域名返回平台标识；未知站点返回 ``generic``。"""
    candidate = url_or_host
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return "generic"
    for platform, suffixes in _PLATFORM_DOMAINS:
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return platform
    return "generic"


def _embedded_urls(url: str, max_depth: int = 2) -> list[str]:
    """提取跳转/落地页 query 中嵌套的真实分享 URL。"""
    found: list[str] = []
    pending = [url]
    seen = {url}
    for _ in range(max_depth):
        next_pending: list[str] = []
        for current in pending:
            try:
                values = [value for _, value in parse_qsl(
                    urlsplit(current).query, keep_blank_values=False
                )]
            except ValueError:
                continue
            for value in values:
                normalized = normalize_share_text(value)
                match = _SCHEME_URL_RE.search(normalized)
                if not match:
                    continue
                nested = _canonical_url(match.group(0))
                if not nested or nested in seen:
                    continue
                seen.add(nested)
                found.append(nested)
                next_pending.append(nested)
        pending = next_pending
        if not pending:
            break
    return found


def extract_share_urls(value: str | bytes, limit: int = 10) -> list[ShareURL]:
    """从任意分享文案提取并去重 HTTP(S) 链接。"""
    if limit < 1:
        return []
    text = normalize_share_text(value)
    found: list[ShareURL] = []
    seen: set[str] = set()

    for variant in _decoded_variants(text):
        matches: list[tuple[int, str]] = [
            (m.start(), m.group(0)) for m in _SCHEME_URL_RE.finditer(variant)
        ]
        # 仅在未被上一条正则覆盖的位置补充无 scheme 链接。
        scheme_ranges = [
            (m.start(), m.end()) for m in _SCHEME_URL_RE.finditer(variant)
        ]
        # 整段还带着 %2F/%25 时，裸域名正则可能把编码串中间的
        # `douyin.com` 误当成一条新链接；继续使用下一层解码文本即可。
        has_encoded_url_separators = bool(
            re.search(r"(?i)%(?:25|2f|3a)", variant)
        )
        if not has_encoded_url_separators:
            for match in _BARE_URL_RE.finditer(variant):
                if any(start <= match.start() < end for start, end in scheme_ranges):
                    continue
                matches.append((match.start(), match.group(0)))

        for _, raw in sorted(matches, key=lambda item: item[0]):
            url = _canonical_url(raw)
            if not url:
                continue
            host = (urlsplit(url).hostname or "").lower()
            primary = ShareURL(url=url, platform=detect_platform(host), host=host)
            nested_items = []
            for nested_url in _embedded_urls(url):
                nested_host = (urlsplit(nested_url).hostname or "").lower()
                nested_items.append(ShareURL(
                    url=nested_url,
                    platform=detect_platform(nested_host),
                    host=nested_host,
                ))
            # 通用跳转页中的已知平台链接更可能是用户真正要下载的作品。
            ordered = (
                nested_items + [primary]
                if primary.platform == "generic"
                and any(item.platform != "generic" for item in nested_items)
                else [primary] + nested_items
            )
            for item in ordered:
                if item.url in seen:
                    continue
                seen.add(item.url)
                found.append(item)
                if len(found) >= limit:
                    return found

    return found


def require_share_urls(value: str | bytes, limit: int = 10) -> list[ShareURL]:
    links = extract_share_urls(value, limit=limit)
    if not links:
        raise ShareLinkError("分享内容中没有识别到 http(s) 链接")
    return links


def is_private_url(url: str) -> bool:
    """判断显式 localhost/内网 IP；域名不做 DNS 查询，避免解析阶段产生网络请求。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


class _YDLLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        # yt-dlp 会把部分普通信息也送到 debug，不需要写进接口返回。
        return None

    def warning(self, message: str) -> None:
        self.warnings.append(str(message))

    def error(self, message: str) -> None:
        self.errors.append(str(message))


def _quality_format(quality: str, *, allow_merge: bool = True) -> str:
    q = (quality or "highest").lower().strip()
    if q in {"highest", "best", "原画"}:
        return "bv*+ba/b" if allow_merge else "b/bv*"
    if q in {"lowest", "worst", "省流"}:
        return "wv*+wa/w" if allow_merge else "w/wv*"
    if q in {"audio", "音频"}:
        # 有 ffmpeg 时允许回退到音视频合一流，随后由后处理器提取音轨；
        # 没有 ffmpeg 时只能选择平台提供的独立音频，不能把 MP4 当成“仅音频”返回。
        return "ba/b" if allow_merge else "ba"
    height_match = re.fullmatch(r"(\d{3,4})(?:p)?", q)
    if height_match:
        height = int(height_match.group(1))
        if not 144 <= height <= 4320:
            raise ShareDownloadError(f"不支持的画质高度：{height}")
        if allow_merge:
            return (
                f"bv*[height<={height}]+ba/"
                f"b[height<={height}]/bv*+ba/b"
            )
        return f"b[height<={height}]/bv*[height<={height}]/b/bv*"
    raise ShareDownloadError(f"不支持的画质：{quality}")


def _is_audio_quality(quality: str) -> bool:
    return (quality or "").lower().strip() in {"audio", "音频"}


def _find_ffmpeg() -> str:
    """优先使用系统 ffmpeg，找不到时使用 Python 依赖附带的可执行文件。"""
    executable = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if executable:
        return executable
    try:
        from imageio_ffmpeg import get_ffmpeg_exe  # type: ignore
        executable = get_ffmpeg_exe()
    except Exception:
        return ""
    return executable if executable and Path(executable).is_file() else ""


def _download_format_options(quality: str, ffmpeg_location: str = "") -> dict[str, Any]:
    """构建格式和后处理参数，保证“仅音频”不会直接产出带画面的文件。"""
    audio_only = _is_audio_quality(quality)
    options: dict[str, Any] = {
        "format": _quality_format(quality, allow_merge=bool(ffmpeg_location)),
    }
    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location
    if audio_only:
        if ffmpeg_location:
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                # best 会尽量复制原音频编码，只更换为合适的纯音频容器。
                "preferredcodec": "best",
            }]
    else:
        options["merge_output_format"] = "mp4"
    return options


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return str(value)


def _one_summary(info: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id", "title", "description", "uploader", "uploader_id", "channel",
        "duration", "timestamp", "upload_date", "view_count", "like_count",
        "comment_count", "repost_count", "thumbnail", "webpage_url",
        "original_url", "extractor", "extractor_key", "ext", "format_id",
        "format", "width", "height", "fps", "vcodec", "acodec", "filesize",
        "filesize_approx",
    )
    out = {key: _json_value(info.get(key)) for key in fields if info.get(key) is not None}
    out["platform"] = detect_platform(
        str(info.get("webpage_url") or info.get("original_url") or "")
    )
    return out


def summarize_info(info: Any, max_entries: int = 20) -> dict[str, Any]:
    """把 yt-dlp 的非 JSON 安全对象压缩成稳定的接口字段。"""
    if not isinstance(info, dict):
        return {"value": str(info)}
    entries = info.get("entries")
    summary = _one_summary(info)
    if entries:
        cleaned = [
            _one_summary(entry) for entry in entries
            if isinstance(entry, dict)
        ][:max_entries]
        summary["entries"] = cleaned
        summary["entry_count"] = len(cleaned)
    return summary


def _file_role(path: Path) -> str:
    suffix = path.suffix.lower()
    if path.name.lower().endswith(".info.json"):
        return "metadata"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        return "thumbnail"
    if suffix in {".srt", ".vtt", ".ass", ".lrc", ".ttml"}:
        return "subtitle"
    if suffix in {".json", ".description"}:
        return "metadata"
    if suffix in {
        ".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v",
        ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac",
    }:
        return "media"
    return "other"


def _list_output_files(job_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if not job_dir.exists():
        return files
    for path in sorted(job_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() in {".part", ".ytdl"}:
            continue
        files.append({
            "name": path.name,
            "path": str(path.resolve()),
            "relative_path": path.relative_to(job_dir).as_posix(),
            "size": path.stat().st_size,
            "role": _file_role(path),
        })
    return files


class ShareDownloader:
    """基于 yt-dlp 的异步外观；阻塞下载在工作线程中运行。"""

    def __init__(
        self,
        output_dir: str | os.PathLike[str],
        user_agent: str = "",
        timeout: float = 120.0,
        *,
        allow_private: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser()
        self.user_agent = user_agent
        self.timeout = max(5.0, float(timeout))
        self.allow_private = allow_private

    @staticmethod
    def _load_yt_dlp():
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise ShareDownloadError(
                "缺少 yt-dlp 依赖，请先执行：python -m pip install -r requirements.txt"
            ) from exc
        return yt_dlp

    def _check_url(self, url: str) -> None:
        canonical = _canonical_url(url)
        if not canonical:
            raise ShareDownloadError("链接格式无效，只接受 http(s) URL")
        if not self.allow_private and is_private_url(canonical):
            raise ShareDownloadError("链接指向本机或内网地址，已停止请求")

    def _base_options(
        self,
        *,
        logger: _YDLLogger,
        proxy: str = "",
        cookie_file: str = "",
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": logger,
            "socket_timeout": self.timeout,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            "file_access_retries": 2,
            "continuedl": True,
            "concurrent_fragment_downloads": 4,
            "noplaylist": True,
            "playlistend": 20,
            "windowsfilenames": True,
            "trim_file_name": 180,
            "http_headers": {
                "User-Agent": self.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            },
        }
        if proxy:
            options["proxy"] = proxy
        if cookie_file:
            options["cookiefile"] = cookie_file
        return options

    def _inspect_sync(
        self,
        url: str,
        *,
        proxy: str = "",
        cookie_file: str = "",
    ) -> dict[str, Any]:
        self._check_url(url)
        yt_dlp = self._load_yt_dlp()
        logger = _YDLLogger()
        options = self._base_options(logger=logger, proxy=proxy, cookie_file=cookie_file)
        options.update({
            "skip_download": True,
            "simulate": True,
        })
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            detail = logger.errors[-1] if logger.errors else str(exc)
            raise ShareDownloadError(_clean_ydl_error(detail)) from exc
        return {
            "ok": True,
            "url": url,
            "metadata": summarize_info(info),
            "warnings": logger.warnings[-10:],
        }

    async def inspect(
        self,
        url: str,
        *,
        proxy: str = "",
        cookie_file: str = "",
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._inspect_sync, url, proxy=proxy, cookie_file=cookie_file
        )

    def _download_sync(
        self,
        url: str,
        *,
        quality: str = "highest",
        save_metadata: bool = True,
        save_thumbnail: bool = True,
        save_subtitles: bool = False,
        proxy: str = "",
        cookie_file: str = "",
        max_filesize_mb: int = 0,
    ) -> dict[str, Any]:
        self._check_url(url)
        yt_dlp = self._load_yt_dlp()
        logger = _YDLLogger()
        root = self.output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_id = f"share_{stamp}_{uuid.uuid4().hex[:8]}"
        job_dir = root / job_id
        temp_dir = job_dir / ".tmp"
        job_dir.mkdir(parents=True, exist_ok=False)
        temp_dir.mkdir(parents=True, exist_ok=True)

        progress: dict[str, Any] = {"status": "starting"}
        ffmpeg_location = _find_ffmpeg()
        has_ffmpeg = bool(ffmpeg_location)

        def hook(data: dict[str, Any]) -> None:
            status = str(data.get("status") or "")
            progress["status"] = status
            for key in (
                "downloaded_bytes", "total_bytes", "total_bytes_estimate",
                "elapsed", "eta", "speed", "filename",
            ):
                if data.get(key) is not None:
                    progress[key] = _json_value(data.get(key))

        options = self._base_options(logger=logger, proxy=proxy, cookie_file=cookie_file)
        options.update({
            # 没有 ffmpeg 时只选自带音视频的单文件格式，避免下载完两条流却合并失败。
            "paths": {"home": str(job_dir), "temp": str(temp_dir)},
            "outtmpl": {
                "default": (
                    "%(extractor_key|generic)s/%(uploader|unknown)s/"
                    "%(id|media)s_%(title).120B.%(ext)s"
                ),
                "thumbnail": (
                    "%(extractor_key|generic)s/%(uploader|unknown)s/"
                    "%(id|media)s_%(title).120B.%(ext)s"
                ),
                "subtitle": (
                    "%(extractor_key|generic)s/%(uploader|unknown)s/"
                    "%(id|media)s_%(title).120B.%(language)s.%(ext)s"
                ),
                "infojson": (
                    "%(extractor_key|generic)s/%(uploader|unknown)s/"
                    "%(id|media)s_%(title).120B.%(ext)s"
                ),
            },
            "writeinfojson": bool(save_metadata),
            "writethumbnail": bool(save_thumbnail),
            "writesubtitles": bool(save_subtitles),
            "writeautomaticsub": bool(save_subtitles),
            "subtitleslangs": ["zh-Hans", "zh-Hant", "zh", "en"],
            "progress_hooks": [hook],
        })
        options.update(_download_format_options(quality, ffmpeg_location))
        if max_filesize_mb:
            if max_filesize_mb < 1:
                raise ShareDownloadError("max_filesize_mb 必须大于 0")
            options["max_filesize"] = int(max_filesize_mb) * 1024 * 1024

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            detail = logger.errors[-1] if logger.errors else str(exc)
            files = _list_output_files(job_dir)
            raise ShareDownloadError(
                f"{_clean_ydl_error(detail)}"
                + (f"；已保留 {len(files)} 个临时/附属文件：{job_dir}" if files else "")
            ) from exc
        finally:
            try:
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()
            except OSError:
                pass

        files = _list_output_files(job_dir)
        media_files = [item for item in files if item["role"] == "media"]
        if not media_files:
            raise ShareDownloadError(
                f"提取已完成，但没有生成媒体文件；请检查该链接是否为可下载作品：{job_dir}"
            )
        warnings = logger.warnings[-10:]
        if not has_ffmpeg and (quality or "").lower() not in {"audio", "音频"}:
            warnings.append(
                "未检测到 ffmpeg，已优先选择平台提供的音视频合一格式；"
                "安装 ffmpeg 后可下载并合并更高画质的分离流"
            )
        return {
            "ok": True,
            "job_id": job_id,
            "url": url,
            "output_dir": str(job_dir.resolve()),
            "metadata": summarize_info(info),
            "files": files,
            "progress": progress,
            "warnings": warnings,
        }

    async def download(
        self,
        url: str,
        *,
        quality: str = "highest",
        save_metadata: bool = True,
        save_thumbnail: bool = True,
        save_subtitles: bool = False,
        proxy: str = "",
        cookie_file: str = "",
        max_filesize_mb: int = 0,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._download_sync,
            url,
            quality=quality,
            save_metadata=save_metadata,
            save_thumbnail=save_thumbnail,
            save_subtitles=save_subtitles,
            proxy=proxy,
            cookie_file=cookie_file,
            max_filesize_mb=max_filesize_mb,
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_ydl_error(message: str) -> str:
    clean = _ANSI_RE.sub("", str(message or "")).strip()
    clean = re.sub(r"(?i)^error:\s*", "", clean)
    lowered = clean.lower()
    if (
        "unsupported url" in lowered
        and (
            "iesdouyin.com/share/user/" in lowered
            or "douyin.com/user/" in lowered
            or "douyin.com/share/user/" in lowered
        )
    ):
        return (
            "这条分享口令对应的是抖音用户主页，不是单条视频作品；"
            "请进入具体视频后点击“分享→复制链接”。"
            "如需持续下载该账号作品，可把该主页链接添加到“作品监控”"
        )
    if (
        "unsupported url" in lowered
        and (
            "douyin.com/note/" in lowered
            or "douyin.com/slides/" in lowered
            or "iesdouyin.com/share/slides/" in lowered
        )
    ):
        return (
            "这是抖音图文作品；请在“复用账号登录态”中选择一个已登录的抖音账号，"
            "CreatorHub 将通过原生抖音接口下载全部图片和作品元数据"
        )
    return clean or "媒体提取失败"


async def download_from_share_text(
    share_text: str | bytes,
    output_dir: str | os.PathLike[str],
    *,
    all_links: bool = False,
    quality: str = "highest",
    save_metadata: bool = True,
    save_thumbnail: bool = True,
    save_subtitles: bool = False,
    proxy: str = "",
    cookie_file: str = "",
    max_filesize_mb: int = 0,
    user_agent: str = "",
    timeout: float = 120.0,
    allow_private: bool = False,
) -> dict[str, Any]:
    """通用入口：从分享文案提取链接并下载第一条或全部链接。"""
    normalized = normalize_share_text(share_text)
    links = require_share_urls(normalized)
    selected: Sequence[ShareURL] = links if all_links else links[:1]
    downloader = ShareDownloader(
        output_dir, user_agent=user_agent, timeout=timeout, allow_private=allow_private
    )
    results: list[dict[str, Any]] = []
    for link in selected:
        try:
            result = await downloader.download(
                link.url,
                quality=quality,
                save_metadata=save_metadata,
                save_thumbnail=save_thumbnail,
                save_subtitles=save_subtitles,
                proxy=proxy,
                cookie_file=cookie_file,
                max_filesize_mb=max_filesize_mb,
            )
            result["input_platform"] = link.platform
            results.append(result)
        except ShareDownloadError as exc:
            results.append({
                "ok": False,
                "url": link.url,
                "input_platform": link.platform,
                "error": str(exc),
            })
    return {
        "ok": bool(results) and all(item.get("ok") for item in results),
        "normalized_text": normalized,
        "links": [item.to_dict() for item in links],
        "results": results,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从平台分享文案中提取链接并下载媒体与元数据"
    )
    parser.add_argument("share_text", help="分享链接或包含链接的完整分享文案")
    parser.add_argument("-o", "--output", default="./data/media/share")
    parser.add_argument("-q", "--quality", default="highest")
    parser.add_argument("--all", action="store_true", help="下载文案里的所有链接")
    parser.add_argument("--no-metadata", action="store_true")
    parser.add_argument("--no-thumbnail", action="store_true")
    parser.add_argument("--subtitles", action="store_true")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--max-filesize-mb", type=int, default=0)
    parser.add_argument("--allow-private", action="store_true")
    parser.add_argument(
        "--links-only", action="store_true", help="只清洗并输出链接，不请求网络"
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.links_only:
        links = require_share_urls(args.share_text)
        print(json.dumps([item.to_dict() for item in links], ensure_ascii=False, indent=2))
        return 0
    result = asyncio.run(download_from_share_text(
        args.share_text,
        args.output,
        all_links=args.all,
        quality=args.quality,
        save_metadata=not args.no_metadata,
        save_thumbnail=not args.no_thumbnail,
        save_subtitles=args.subtitles,
        proxy=args.proxy,
        max_filesize_mb=args.max_filesize_mb,
        allow_private=args.allow_private,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
