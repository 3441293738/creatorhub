"""微信视频号 (WeChat Channels / Finder) 离线视频解密与完整性核验。

基于已验证的微信官方播放器 `WxIsaac64` WASM 与 `worker_release.js` 算法还原:
  1. `decode_key` 必须作为精确无符号 64 位十进制整数(0 .. 2^64-1)处理，绝不经过 float / JS Number。
  2. 以 `decode_key` 初始化 64-bit ISAAC64 PRNG(`seed[0] = key`)，生成 131,072 字节(128 KiB)密钥流，
     并按官方 Worker 逻辑对整个 128 KiB 逆序(等价于从 `randcnt=256` 预递减并按大端写出每个 uint64)。
  3. 仅将媒体前 `min(file_size, 131072)` 字节与密钥流逐字节 XOR，131,072 字节之后原样保留。
  4. 落盘前执行 ISO BMFF / MP4 Box 结构校验(`ftyp` + `moov/trak` + `mdat`)，可选调用
     `ffprobe` / `ffmpeg` 做全片严格音视频解码帧数核验；错误 `decode_key` 绝不原子发布伪视频。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

KEYSTREAM_SIZE = 131072  # 128 KiB
_MASK64 = 0xFFFFFFFFFFFFFFFF
_GOLDEN64 = 0x9E3779B97F4A7C13
_UINT64_MAX = (1 << 64) - 1


class ChannelsDecryptError(ValueError):
    """视频号媒体解密参数无效或解密后 MP4 结构/解码校验未通过。"""


def validate_decode_key(decode_key: str | int) -> str:
    """校验并返回规范化的无符号 64 位十进制字符串密钥。"""
    if isinstance(decode_key, (bool, float)) or decode_key is None:
        raise ChannelsDecryptError("decode_key 必须是精确十进制字符串或整数，不能为 float/bool")
    if isinstance(decode_key, int):
        if not 0 <= decode_key <= _UINT64_MAX:
            raise ChannelsDecryptError("decode_key 超出 uint64 范围 (0..2^64-1)")
        return str(decode_key)
    if not isinstance(decode_key, str):
        raise ChannelsDecryptError("decode_key 类型无效")
    text = decode_key.strip()
    if not text or not text.isdecimal():
        raise ChannelsDecryptError("decode_key 必须是非空十进制数字字符串")
    value = int(text)
    if not 0 <= value <= _UINT64_MAX:
        raise ChannelsDecryptError("decode_key 超出 uint64 范围 (0..2^64-1)")
    return str(value)


def _mix64(
    a: int, b: int, c: int, d: int, e: int, f: int, g: int, h: int
) -> tuple[int, int, int, int, int, int, int, int]:
    a = (a - e) & _MASK64
    f ^= h >> 9
    h = (h + a) & _MASK64
    b = (b - f) & _MASK64
    g ^= (a << 9) & _MASK64
    a = (a + b) & _MASK64
    c = (c - g) & _MASK64
    h ^= b >> 23
    b = (b + c) & _MASK64
    d = (d - h) & _MASK64
    a ^= (c << 15) & _MASK64
    c = (c + d) & _MASK64
    e = (e - a) & _MASK64
    b ^= d >> 14
    d = (d + e) & _MASK64
    f = (f - b) & _MASK64
    c ^= (e << 20) & _MASK64
    e = (e + f) & _MASK64
    g = (g - c) & _MASK64
    d ^= f >> 17
    f = (f + g) & _MASK64
    h = (h - d) & _MASK64
    e ^= (g << 14) & _MASK64
    g = (g + h) & _MASK64
    return a, b, c, d, e, f, g, h


def wx_isaac64_keystream(decode_key: str | int, length: int = KEYSTREAM_SIZE) -> bytes:
    """生成与微信官方 `WxIsaac64` WASM + 128KiB 整体逆序逐位一致的密钥流。"""
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ChannelsDecryptError("密钥流长度必须是非负整数")
    key_int = int(validate_decode_key(decode_key))
    if length == 0:
        return b""

    seed = [0] * 256
    mm = [0] * 256
    seed[0] = key_int & _MASK64
    aa = bb = cc = 0

    def step() -> None:
        nonlocal aa, bb, cc
        cc = (cc + 1) & _MASK64
        bb = (bb + cc) & _MASK64
        for i in range(256):
            x = mm[i]
            r = i & 3
            if r == 0:
                aa = (~(aa ^ ((aa << 21) & _MASK64))) & _MASK64
            elif r == 1:
                aa ^= aa >> 5
            elif r == 2:
                aa ^= (aa << 12) & _MASK64
            else:
                aa ^= aa >> 33
            aa = (aa + mm[(i + 128) & 255]) & _MASK64
            y = (mm[(x >> 3) & 255] + aa + bb) & _MASK64
            mm[i] = y
            bb = (mm[(y >> 11) & 255] + x) & _MASK64
            seed[i] = bb

    a = b = c = d = e = f = g = h = _GOLDEN64
    for _ in range(4):
        a, b, c, d, e, f, g, h = _mix64(a, b, c, d, e, f, g, h)

    for i in range(0, 256, 8):
        a = (a + seed[i]) & _MASK64
        b = (b + seed[i + 1]) & _MASK64
        c = (c + seed[i + 2]) & _MASK64
        d = (d + seed[i + 3]) & _MASK64
        e = (e + seed[i + 4]) & _MASK64
        f = (f + seed[i + 5]) & _MASK64
        g = (g + seed[i + 6]) & _MASK64
        h = (h + seed[i + 7]) & _MASK64
        a, b, c, d, e, f, g, h = _mix64(a, b, c, d, e, f, g, h)
        mm[i : i + 8] = [a, b, c, d, e, f, g, h]

    for i in range(0, 256, 8):
        a = (a + mm[i]) & _MASK64
        b = (b + mm[i + 1]) & _MASK64
        c = (c + mm[i + 2]) & _MASK64
        d = (d + mm[i + 3]) & _MASK64
        e = (e + mm[i + 4]) & _MASK64
        f = (f + mm[i + 5]) & _MASK64
        g = (g + mm[i + 6]) & _MASK64
        h = (h + mm[i + 7]) & _MASK64
        a, b, c, d, e, f, g, h = _mix64(a, b, c, d, e, f, g, h)
        mm[i : i + 8] = [a, b, c, d, e, f, g, h]

    step()
    rand_cnt = 256
    out = bytearray()
    while len(out) < length:
        if rand_cnt == 0:
            step()
            rand_cnt = 256
        rand_cnt -= 1
        out.extend(struct.pack(">Q", seed[rand_cnt]))
    return bytes(out[:length])


def decrypt_channels_prefix(
    data: bytes | bytearray | memoryview,
    decode_key: str | int,
    *,
    offset: int = 0,
    enc_limit: int = KEYSTREAM_SIZE,
) -> bytes:
    """对位于文件偏移 `offset` 的数据分片执行有界 XOR 解密；超出 `enc_limit` 的字节保持原样。"""
    if offset < 0:
        raise ChannelsDecryptError("offset 不能为负数")
    raw = bytes(data)
    if not raw or offset >= enc_limit:
        return raw
    stream = wx_isaac64_keystream(decode_key, enc_limit)
    span = min(len(raw), enc_limit - offset)
    prefix = bytes(a ^ b for a, b in zip(raw[:span], stream[offset : offset + span]))
    return prefix + raw[span:]


def parse_mp4_boxes(data: bytes, start: int = 0, end: int | None = None) -> list[dict[str, Any]]:
    """解析内存缓冲区中的 ISO BMFF / MP4 Box 列表。"""
    limit = len(data) if end is None else end
    result: list[dict[str, Any]] = []
    pos = start
    while pos < limit:
        if pos + 8 > limit:
            raise ChannelsDecryptError(f"MP4 box 头在偏移 {pos} 处截断")
        size, raw_type = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > limit:
                raise ChannelsDecryptError(f"MP4 64-bit box 头在偏移 {pos} 处截断")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = limit - pos
        if size < header or pos + size > limit:
            raise ChannelsDecryptError(f"非法 MP4 box {raw_type!r} @ {pos}, size={size}")
        box_type = raw_type.decode("latin1")
        if not all(32 <= ord(ch) <= 126 or ord(ch) == 0xA9 for ch in box_type):
            raise ChannelsDecryptError(f"非法 MP4 box 类型 {raw_type!r} @ {pos}")
        result.append({"type": box_type, "offset": pos, "size": size, "header": header})
        pos += size
    return result


def validate_mp4_bytes(data: bytes) -> dict[str, Any]:
    """核验内存 MP4 数据的顶层 `ftyp`/`moov`/`mdat` 与 `moov/trak` 结构。"""
    top = parse_mp4_boxes(data)
    if not top or top[0]["type"] != "ftyp" or not (16 <= top[0]["size"] <= 1024):
        raise ChannelsDecryptError("缺少有效 MP4 ftyp 文件头(decode_key 可能不匹配)")
    moov = next((x for x in top if x["type"] == "moov"), None)
    if not moov or not any(x["type"] == "mdat" for x in top):
        raise ChannelsDecryptError("缺少 MP4 moov 或 mdat box")
    children = parse_mp4_boxes(
        data, moov["offset"] + moov["header"], moov["offset"] + moov["size"]
    )
    if not any(x["type"] == "trak" for x in children):
        raise ChannelsDecryptError("MP4 moov 中未发现任何媒体轨道(trak)")
    return {
        "valid": True,
        "top_level": top,
        "moov_children": children,
        "header_hex": data[:64].hex(),
    }


def validate_mp4_file(path: Path | str) -> dict[str, Any]:
    """流式扫描磁盘 MP4 文件的顶层 Box 边界与 `moov/trak`，避免把超大 `mdat` 整片读入内存。"""
    fpath = Path(path)
    total_size = fpath.stat().st_size
    if total_size < 32:
        raise ChannelsDecryptError("媒体文件过小，不是有效 MP4")

    top: list[dict[str, Any]] = []
    moov_payload = b""
    header_hex = ""
    with fpath.open("rb") as f:
        header_hex = f.read(64).hex()
        f.seek(0)
        pos = 0
        while pos < total_size:
            if pos + 8 > total_size:
                raise ChannelsDecryptError(f"MP4 box 头在偏移 {pos} 处截断")
            f.seek(pos)
            head_bytes = f.read(16)
            if len(head_bytes) < 8:
                raise ChannelsDecryptError(f"MP4 box 头读取失败 @ {pos}")
            size, raw_type = struct.unpack_from(">I4s", head_bytes, 0)
            header = 8
            if size == 1:
                if len(head_bytes) < 16:
                    raise ChannelsDecryptError(f"MP4 64-bit box 头截断 @ {pos}")
                size = struct.unpack_from(">Q", head_bytes, 8)[0]
                header = 16
            elif size == 0:
                size = total_size - pos
            if size < header or pos + size > total_size:
                raise ChannelsDecryptError(f"非法 MP4 box {raw_type!r} @ {pos}, size={size}")
            box_type = raw_type.decode("latin1")
            if not all(32 <= ord(ch) <= 126 or ord(ch) == 0xA9 for ch in box_type):
                raise ChannelsDecryptError(f"非法 MP4 box 类型 {raw_type!r} @ {pos}")
            box = {"type": box_type, "offset": pos, "size": size, "header": header}
            top.append(box)
            if box_type == "moov":
                body_len = size - header
                if body_len > 128 * 1024 * 1024:
                    raise ChannelsDecryptError("MP4 moov 元数据异常过大")
                f.seek(pos + header)
                moov_payload = f.read(body_len)
            pos += size

    if not top or top[0]["type"] != "ftyp" or not (16 <= top[0]["size"] <= 1024):
        raise ChannelsDecryptError("缺少有效 MP4 ftyp 文件头(decode_key 可能不匹配)")
    moov = next((x for x in top if x["type"] == "moov"), None)
    if not moov or not any(x["type"] == "mdat" for x in top):
        raise ChannelsDecryptError("缺少 MP4 moov 或 mdat box")
    children = parse_mp4_boxes(moov_payload, 0, len(moov_payload))
    for child in children:
        child["offset"] += moov["offset"] + moov["header"]
    if not any(x["type"] == "trak" for x in children):
        raise ChannelsDecryptError("MP4 moov 中未发现任何媒体轨道(trak)")
    return {
        "valid": True,
        "top_level": top,
        "moov_children": children,
        "header_hex": header_hex,
    }


def validate_mp4_structure(target: Path | str | bytes) -> dict[str, Any]:
    """统一校验字节串或文件路径的 MP4 容器结构。"""
    if isinstance(target, (bytes, bytearray, memoryview)):
        return validate_mp4_bytes(bytes(target))
    return validate_mp4_file(Path(target))


def decrypt_channels_video_bytes(
    encrypted: bytes,
    decode_key: str | int,
    *,
    validate_mp4: bool = True,
) -> bytes:
    """在内存中解密视频号 MP4 字节串并按需校验容器结构。"""
    plain = decrypt_channels_prefix(encrypted, decode_key, offset=0)
    if validate_mp4:
        validate_mp4_bytes(plain)
    return plain


def _verify_media_with_ffmpeg(
    media_path: Path,
    *,
    ffmpeg_path: str = "",
    ffprobe_path: str = "",
    timeout: int = 600,
) -> dict[str, Any]:
    ffmpeg_bin = ffmpeg_path or shutil.which("ffmpeg") or ""
    ffprobe_bin = ffprobe_path or shutil.which("ffprobe") or ""
    if not ffmpeg_bin or not ffprobe_bin:
        raise ChannelsDecryptError("未找到 ffmpeg/ffprobe，无法执行全片音视频解码核验")

    probe = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(media_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if probe.returncode != 0 or probe.stderr.strip():
        raise ChannelsDecryptError(f"ffprobe 校验失败: {probe.stderr.strip() or probe.returncode}")
    probe_data = json.loads(probe.stdout or "{}")
    streams = probe_data.get("streams") or []
    if not any(
        s.get("codec_type") == "video" and int(s.get("width") or 0) > 0 and int(s.get("height") or 0) > 0
        for s in streams
    ):
        raise ChannelsDecryptError("ffprobe 未检测到有效视频流")

    full = subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-v", "error", "-xerror", "-err_detect", "explode",
            "-i", str(media_path), "-map", "0:v:0", "-map", "0:a?",
            "-progress", "pipe:1", "-nostats", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    progress: dict[str, str] = {}
    for line in full.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            progress[k] = v
    decoded_frames = int(progress.get("frame", "0") or 0)
    if full.returncode != 0 or full.stderr.strip() or progress.get("progress") != "end" or decoded_frames < 1:
        raise ChannelsDecryptError(
            f"ffmpeg 全片严格解码未通过: {full.stderr.strip() or progress.get('progress') or full.returncode}"
        )
    expected_frames = sum(
        int(s.get("nb_frames") or 0) for s in streams if s.get("codec_type") == "video"
    )
    if expected_frames and expected_frames != decoded_frames:
        raise ChannelsDecryptError(
            f"视频解码帧数不一致: expected={expected_frames}, decoded={decoded_frames}"
        )
    return {
        "ffprobe": probe_data,
        "decoded_frames": decoded_frames,
        "expected_frames": expected_frames,
    }


def decrypt_channels_video_file(
    input_path: Path | str,
    output_path: Path | str,
    decode_key: str | int,
    *,
    overwrite: bool = True,
    validate_mp4: bool = True,
    verify_full_decode: bool = False,
    ffmpeg_path: str = "",
    ffprobe_path: str = "",
) -> dict[str, Any]:
    """将加密的视频号 CDN 媒体文件离线解密并校验后原子发布到 `output_path`。"""
    src = Path(input_path).resolve()
    dst = Path(output_path).resolve()
    if src == dst:
        raise ChannelsDecryptError("输入与输出路径不能相同")
    if not src.is_file():
        raise FileNotFoundError(str(src))
    if dst.exists() and not overwrite:
        raise FileExistsError(f"目标文件已存在: {dst}")

    normalized_key = validate_decode_key(decode_key)
    stream = wx_isaac64_keystream(normalized_key, KEYSTREAM_SIZE)
    dst.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix="channels-dec-", suffix=".mp4.tmp", dir=str(dst.parent))
    tmp_path = Path(tmp_name)
    try:
        with src.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            prefix = reader.read(KEYSTREAM_SIZE)
            writer.write(bytes(x ^ y for x, y in zip(prefix, stream)))
            shutil.copyfileobj(reader, writer, length=1024 * 1024)

        report: dict[str, Any] = {
            "decode_key": normalized_key,
            "keystream_sha256": hashlib.sha256(stream).hexdigest(),
            "size": tmp_path.stat().st_size,
        }
        if validate_mp4:
            report["structure"] = validate_mp4_file(tmp_path)
        if verify_full_decode:
            report["decode_check"] = _verify_media_with_ffmpeg(
                tmp_path, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path
            )
        os.replace(tmp_path, dst)
        report["output"] = str(dst)
        return report
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
