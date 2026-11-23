"""抖音 SSO 二维码扫码登录客户端（API 轮询方式）。

流程:
  1. get_qrcode  -> 拿到二维码图片 + token
  2. 用户用抖音 App 扫码
  3. check_qrconnect 轮询 -> status: 1待扫 2已扫 3已确认(带 redirect_url)
  4. 跟随 redirect_url 落地,收集登录后的 Cookie

⚠️ sso.douyin.com 接口字段偶有调整;若 get_qrcode 拿不到 token,
   对照浏览器 Network 里 get_qrcode 响应改 _parse_qrcode。
"""
from __future__ import annotations

import random
import string
import time
import uuid
from typing import Dict, Optional

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua

SSO = "https://sso.douyin.com"
SERVICE = "https://www.douyin.com"


def gen_verify_fp() -> str:
    """生成 s_v_web_id / verifyFp,抖音多个接口需要。"""
    chars = string.ascii_letters + string.digits
    t = int(time.time() * 1000)
    base36 = ""
    n = t
    table = "0123456789abcdefghijklmnopqrstuvwxyz"
    while n > 0:
        base36 = table[n % 36] + base36
        n //= 36
    rand = "".join(random.choice(chars) for _ in range(36))
    body = list(rand)
    last = ""
    for i, ch in enumerate(body):
        if i:
            last = body[i - 1]
        body[i] = ch
    return f"verify_{base36}_{''.join(body)}"


class QRLoginSession:
    """一次扫码会话,持有独立 cookie jar。"""

    def __init__(self, user_agent: str, proxy: str = ""):
        from ...netfp import curl_proxy_url
        self.ua = user_agent
        self.proxy = curl_proxy_url(proxy) or None
        self.verify_fp = gen_verify_fp()
        # curl_cffi 持久会话:对齐浏览器 TLS 握手指纹,沿用 cookie jar 收集登录态
        self.client = AsyncSession(
            timeout=20,
            impersonate=impersonate_for_ua(user_agent),
            headers={"User-Agent": user_agent},
            cookies={"s_v_web_id": self.verify_fp},
            proxy=self.proxy,
            trust_env=False,
        )
        self.token: Optional[str] = None
        self.status: str = "new"           # new | scanned | confirmed | expired | error
        self.cookie: Optional[str] = None
        self.created_at = time.time()

    def _params(self) -> Dict[str, str]:
        return {
            "service": SERVICE,
            "need_logo": "false",
            "need_short_url": "false",
            "passport_jssdk_version": "1.0.20",
            "aid": "6383",
            "account_sdk_source": "sso",
            "sdk_version": "2.2.7",
            "language": "zh",
            "verifyFp": self.verify_fp,
            "fp": self.verify_fp,
        }

    async def start(self) -> Dict[str, str]:
        """返回 {qrcode: <base64 png>, token}。"""
        # 先访问主站拿 ttwid 等基础 cookie
        try:
            await self.client.get(SERVICE + "/", headers={"User-Agent": self.ua})
        except Exception:
            pass

        try:
            r = await self.client.get(SSO + "/get_qrcode/", params=self._params())
        except Exception as e:
            self.status = "error"
            return {"status": "error", "error": f"请求 get_qrcode 失败: {e!r}"}

        try:
            payload = r.json()
        except Exception:
            self.status = "error"
            snippet = (r.text or "")[:300]
            return {
                "status": "error",
                "error": f"get_qrcode 返回非 JSON (HTTP {r.status_code}, "
                         f"content-type={r.headers.get('content-type')})",
                "raw": snippet,
            }

        data = (payload or {}).get("data") or {}
        self.token = data.get("token")
        qrcode = data.get("qrcode") or ""       # base64 png(不含前缀)
        qrcode_url = data.get("qrcode_index_url") or data.get("frontend_show_qrcode")
        if not self.token:
            self.status = "error"
            return {
                "status": "error",
                "error": "get_qrcode 未返回 token",
                "raw": str(payload)[:300],
                "verify_fp": self.verify_fp,
            }
        return {
            "status": "new",
            "token": self.token,
            "qrcode_base64": qrcode,
            "qrcode_url": qrcode_url or "",
            "verify_fp": self.verify_fp,
        }

    async def poll(self) -> Dict[str, str]:
        """轮询扫码状态。"""
        if not self.token:
            return {"status": "error"}
        params = self._params()
        params["token"] = self.token
        try:
            r = await self.client.get(SSO + "/check_qrconnect/", params=params)
            data = (r.json() or {}).get("data") or {}
        except Exception:
            return {"status": self.status}

        code = str(data.get("status", ""))
        if code == "1":
            self.status = "new"
        elif code == "2":
            self.status = "scanned"
        elif code in ("3", "4") or data.get("redirect_url"):
            self.status = "confirmed"
            await self._finalize(data.get("redirect_url"))
        elif code == "5":
            self.status = "expired"
        return {"status": self.status, "nickname": data.get("nickname", "")}

    async def _finalize(self, redirect_url: Optional[str]):
        """跟随确认后的跳转,落地登录 Cookie。"""
        try:
            if redirect_url:
                await self.client.get(redirect_url)
            # 再访问一次主站,确保 sessionid 等写入
            await self.client.get(SERVICE + "/", headers={"User-Agent": self.ua})
        except Exception:
            pass
        self.cookie = self._dump_cookie()

    def _dump_cookie(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.client.cookies.jar)

    async def close(self):
        await self.client.close()
