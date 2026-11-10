"""小红书创作平台签名(参考 Spider_XHS)。
- x-s / x-t / x-s-common:用 static/xhs_creator_260411.js(execjs + Node + crypto-js)
- x-rap-param:用 static/xhs_rap.js
- 上传文件的 COS 签名(getSignature):纯 Python 复刻(HMAC-SHA1),不走 JS
- traceid 等:Python 随机生成

依赖:Node.js(PATH 里)+ 本项目 node_modules 里的 crypto-js(npm install crypto-js)。
平台改版时应对照参考项目一起复核签名、会话状态与请求字段，保留版本记录。
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from ...runtime_tools import find_node

_STATIC = Path(__file__).parent / "static"
_NODE_MODULES = Path(__file__).resolve().parents[3] / "node_modules"

# 让 execjs 启动的 node 能 require 到 crypto-js
_existing = os.environ.get("NODE_PATH", "")
if str(_NODE_MODULES) not in _existing:
    os.environ["NODE_PATH"] = (str(_NODE_MODULES) + os.pathsep + _existing).rstrip(os.pathsep)

_JS_CACHE: dict = {}
_AVAILABLE: bool | None = None
_WORKERS: dict[str, "_NodeSigner"] = {}
_WORKERS_LOCK = threading.Lock()


class _NodeSigner:
    """Keep one Node process alive per signing bundle.

    PyExecJS launches a new Node process for every call. A normal XHS
    collection page signs multiple requests, so process startup dominates the
    API latency. This line-delimited JSON bridge loads the existing reviewed JS
    bundle once and serializes calls through one account-independent runtime.
    """

    def __init__(self, filename: str):
        node = find_node()
        if not node:
            raise RuntimeError("node runtime not found")
        module_path = str((_STATIC / filename).resolve())
        bridge = r"""
const readline = require('readline');
const mod = require(process.argv[1]);
const rl = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
rl.on('line', line => {
  let request;
  try {
    request = JSON.parse(line);
    const fn = mod[request.function];
    if (typeof fn !== 'function') throw new Error('unknown function: ' + request.function);
    const result = fn.apply(null, request.args || []);
    process.stdout.write('__CREATORHUB_SIGNER__' + JSON.stringify({
      id: request.id, ok: true, result
    }) + '\n');
  } catch (error) {
    process.stdout.write('__CREATORHUB_SIGNER__' + JSON.stringify({
      id: request && request.id, ok: false,
      error: String(error && (error.stack || error.message) || error)
    }) + '\n');
  }
});
"""
        env = os.environ.copy()
        env["NODE_PATH"] = os.environ.get("NODE_PATH", "")
        self._process = subprocess.Popen(
            [node, "-e", bridge, module_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            bufsize=1, env=env, creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        self._lock = threading.Lock()
        self._request_id = 0

    def call(self, function: str, *args):
        with self._lock:
            if self._process.poll() is not None:
                error = self._process.stderr.read() if self._process.stderr else ""
                raise RuntimeError(f"xhs signer exited: {error.strip()}")
            self._request_id += 1
            payload = json.dumps({
                "id": self._request_id, "function": function, "args": args,
            }, ensure_ascii=False, separators=(",", ":"))
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            self._process.stdin.write(payload + "\n")
            self._process.stdin.flush()
            marker = "__CREATORHUB_SIGNER__"
            while True:
                line = self._process.stdout.readline()
                if not line:
                    error = self._process.stderr.read() if self._process.stderr else ""
                    raise RuntimeError(
                        f"xhs signer produced no response: {error.strip()}")
                if line.startswith(marker):
                    response = json.loads(line[len(marker):])
                    break
            if response.get("id") != self._request_id:
                raise RuntimeError("xhs signer response id mismatch")
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "xhs signer failed"))
            return response.get("result")

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def _worker(filename: str) -> _NodeSigner:
    with _WORKERS_LOCK:
        current = _WORKERS.get(filename)
        if current is None or current._process.poll() is not None:
            if current is not None:
                current.close()
            current = _NodeSigner(filename)
            _WORKERS[filename] = current
        return current


def close_signers() -> None:
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(close_signers)


def _ctx(filename: str):
    if filename not in _JS_CACHE:
        import execjs
        runtime = execjs
        if getattr(sys, "frozen", False):
            from execjs._external_runtime import ExternalRuntime
            from execjs._runner_sources import Node
            node = find_node()
            if not node:
                raise RuntimeError("随包 Node 运行环境缺失")
            class BundledNode(ExternalRuntime):
                def _binary(self):
                    # ExecJS's Windows PATH search appends PATHEXT even to an
                    # absolute .exe path. Use the already verified executable.
                    return [node]
            runtime = BundledNode("CreatorHub Node", [node], Node, encoding="utf8")
        _JS_CACHE[filename] = runtime.compile((_STATIC / filename).read_text(encoding="utf-8"))
    return _JS_CACHE[filename]


def available() -> bool:
    """检查本地签名运行环境；不代表线上兼容，也不触发发布通道回退。"""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        r = _worker("xhs_creator_260411.js").call(
            "get_request_headers_params", "/api/test", "", "a1test")
        _AVAILABLE = bool(r and r.get("xs"))
    except Exception:
        _AVAILABLE = False
    return _AVAILABLE


def generate_xs_xs_common(a1: str, api: str, data="") -> tuple:
    ret = _worker("xhs_creator_260411.js").call(
        "get_request_headers_params", api, data, a1)
    return ret["xs"], ret["xt"], ret["xs_common"]


def generate_xsc_main(a1: str, api: str, data="", method: str = "GET") -> dict:
    """网页主签名(xhs_main_260411.js,带 method)。用于 www/edith 的带参 GET 等。
    api 传"路径?query"(splice_str 结果),data='',method='GET'(对齐 Spider_XHS)。"""
    ret = _worker("xhs_main_260411.js").call(
        "get_request_headers_params", api, data, a1, method)
    return {
        "x-s": ret["xs"], "x-t": str(ret["xt"]), "x-s-common": ret["xs_common"],
        "x-b3-traceid": gen_b3_traceid(), "x-xray-traceid": gen_xray_traceid(),
    }


def generate_x_rap_param(api: str, data="") -> str:
    if isinstance(data, (dict, list)):
        data = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    # xhs_rap.js is not a CommonJS export. It is only needed for the final
    # publish submit, not routine high-volume reads, so retain ExecJS here.
    return _ctx("xhs_rap.js").call("generate_x_rap_param", api, data or "", None)


def _rand_hex(n: int) -> str:
    return "".join(random.choice("abcdef0123456789") for _ in range(n))


def gen_b3_traceid() -> str:
    return _rand_hex(16)


def gen_xray_traceid() -> str:
    return _rand_hex(32)


def generate_xsc(a1: str, api: str, data="") -> dict:
    xs, xt, xs_common = generate_xs_xs_common(a1, api, data)
    return {
        "x-s": xs, "x-t": str(xt), "x-s-common": xs_common,
        "x-b3-traceid": gen_b3_traceid(), "x-xray-traceid": gen_xray_traceid(),
    }


def cos_signature(message: str, file_id: str, content_length: int,
                  host: str = "ros-upload.xiaohongshu.com") -> str:
    """复刻 static/xhs_creator_signature.js 的 getSignature(腾讯云 COS 风格 HMAC-SHA1)。"""
    k1 = hmac.new(b"null", message.encode(), hashlib.sha1).hexdigest()
    new_message = (f"put\n/spectrum/{file_id}\n\n"
                   f"content-length={content_length}&host={host}\n")
    params = hashlib.sha1(new_message.encode()).hexdigest()
    sign_msg = f"sha1\n{message}\n{params}\n"
    return hmac.new(k1.encode(), sign_msg.encode(), hashlib.sha1).hexdigest()


def splice_str(api: str, params: dict) -> str:
    return api + "?" + urlencode(
        {k: ("" if v is None else v) for k, v in params.items()}, doseq=True)


def trans_cookies(cookie_str: str) -> dict:
    sep = "; " if "; " in cookie_str else ";"
    out = {}
    for part in cookie_str.split(sep):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v
    return out


# 13 位时间戳辅助
def now_ms() -> int:
    return int(time.time() * 1000)
