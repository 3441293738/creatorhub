"""msToken 生成。对应逆向符号 signing.GenMsToken / genRealMsToken / genFalseMsToken。

- gen_false_ms_token: 本地随机串(读取公开作品列表通常够用)
- gen_real_ms_token: 向抖音 mssdk 申请真实 token(写操作/风控严时需要)
"""
from __future__ import annotations

import random
import string

from curl_cffi.requests import AsyncSession

from ....netfp import impersonate_for_ua

_CHARSET = string.ascii_letters + string.digits + "=_-"

_REAL_URL = "https://mssdk.bytedance.com/web/report"


def gen_false_ms_token(length: int = 126) -> str:
    """对应 genFalseMsToken:本地伪造,长度 126。"""
    return "".join(random.choice(_CHARSET) for _ in range(length))


async def gen_real_ms_token(user_agent: str, timeout: float = 10.0,
                            cookie: str = "", proxy: str = "") -> str:
    """对应 genRealMsToken:失败时回退到伪造 token。"""
    payload = {
        "magic": 538969122,
        "version": 1,
        "dataType": 8,
        "strData": gen_false_ms_token(172),
        "tspFromClient": 0,
    }
    try:
        # 用 session 读 cookie jar(curl_cffi 的响应级 cookies 不可靠,set-cookie 进 session jar)
        async with AsyncSession(impersonate=impersonate_for_ua(user_agent)) as cli:
            await cli.post(
                _REAL_URL,
                params={"msToken": gen_false_ms_token()},
                json=payload,
                headers={"User-Agent": user_agent, "Content-Type": "application/json",
                         "Cookie": cookie} if cookie else
                        {"User-Agent": user_agent, "Content-Type": "application/json"},
                timeout=timeout,
                proxy=proxy or None,
            )
            for c in cli.cookies.jar:
                if c.name == "msToken":
                    return c.value
    except Exception:
        pass
    return gen_false_ms_token()
