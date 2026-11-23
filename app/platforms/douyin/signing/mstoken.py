"""抖音 `msToken` 请求令牌生成与在线申请。

- gen_false_ms_token: 本地随机串(读取公开作品列表通常够用)
- gen_real_ms_token: 向抖音 mssdk 申请真实 token(写操作/风控严时需要)
"""
from __future__ import annotations

import random
import string

from curl_cffi.requests import AsyncSession

from ....browser.manager import normalize_proxy
from ....netfp import impersonate_for_ua

_CHARSET = string.ascii_letters + string.digits + "=_-"

_REAL_URL = "https://mssdk.bytedance.com/web/report"


class _SessionToken(str):
    def __new__(cls, value, source):
        result = super().__new__(cls, value)
        result.source = source
        return result


def gen_false_ms_token(length: int = 126) -> str:
    """生成指定长度的本地随机 `msToken`（默认 126 位）。"""
    return "".join(random.choice(_CHARSET) for _ in range(length))


async def gen_real_ms_token(user_agent: str, timeout: float = 10.0,
                            cookie: str = "", proxy: str = "") -> str:
    """向服务端申请真实 `msToken`，失败时回退本地随机串。"""
    payload = {
        "magic": 538969122,
        "version": 1,
        "dataType": 8,
        "strData": gen_false_ms_token(172),
        "tspFromClient": 0,
    }
    from ....netfp import curl_proxy_url
    norm_proxy = curl_proxy_url(proxy) or None
    try:
        # 用 session 读 cookie jar(curl_cffi 的响应级 cookies 不可靠,set-cookie 进 session jar)
        async with AsyncSession(
            impersonate=impersonate_for_ua(user_agent),
            proxy=norm_proxy,
            trust_env=False,
        ) as cli:
            response = await cli.post(
                _REAL_URL,
                params={"msToken": gen_false_ms_token()},
                json=payload,
                headers={"User-Agent": user_agent, "Content-Type": "application/json",
                         "Cookie": cookie} if cookie else
                        {"User-Agent": user_agent, "Content-Type": "application/json"},
                timeout=timeout,
                proxy=norm_proxy,
            )
            for c in cli.cookies.jar:
                if response.status_code == 200 and c.name == "msToken" and c.value:
                    return _SessionToken(c.value, "sdk")
    except Exception:
        pass
    return _SessionToken(gen_false_ms_token(), "random_fallback")
