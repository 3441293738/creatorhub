"""Shared response validation for web and creator API compatibility clients."""
from __future__ import annotations


class XhsApiError(Exception):
    def __init__(self, message: str, *, category: str = "business",
                 status_code: int | None = None, signal: str = "", payload=None,
                 code: int | None = None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.signal = signal or category
        # Retained for internal diagnostics/classification, never logged here.
        self.payload = payload
        self.code = code


def validate_payload(payload, *, status_code: int = 200) -> dict:
    """Validate an envelope without replacing a failure with its partial data."""
    def ambiguous(message):
        return XhsApiError(
            message, category="risk", status_code=status_code,
            signal="ambiguous_response", payload=payload)

    if not isinstance(payload, dict):
        raise ambiguous("接口响应应为 JSON 对象")
    raw_code = payload.get("code", payload.get("result"))
    code = None
    if raw_code is not None:
        if isinstance(raw_code, bool) or not isinstance(raw_code, (int, str)):
            raise ambiguous("接口返回了异常的业务状态码")
        try:
            code = int(raw_code)
        except ValueError as exc:
            raise ambiguous("接口返回了异常的业务状态码") from exc
    success = payload.get("success")
    if success is False or (code is not None and code != 0):
        message = str(payload.get("msg") or payload.get("message") or "")
        text = message.lower()
        if code in {-100, -101, 401}:
            category, signal = "auth", "auth_expired"
        elif code == 407:
            category, signal = "network", "proxy_auth"
        elif code in {403, 406, 429, 461, 471}:
            category, signal = "risk", f"api_code_{code}"
        elif code is not None and 500 <= code < 600:
            category, signal = "network", "network_failure"
        elif any(marker in text for marker in (
                "风控", "频控", "频繁", "验证码", "验证", "限流", "risk", "captcha")):
            category, signal = "risk", f"api_code_{code}"
        elif any(marker in text for marker in (
                "登录", "login expired", "session expired", "cookie expired", "logged_out")):
            category, signal = "auth", "auth_expired"
        elif any(marker in text for marker in (
                "timeout", "network", "connection", "proxy", "dns", "tls")):
            category, signal = "network", "network_failure"
        else:
            category, signal = "business", f"api_code_{code}"
        raise XhsApiError(
            f"接口失败 code={code} msg={message}", category=category,
            status_code=status_code, signal=signal, payload=payload, code=code)
    if (("success" in payload and not isinstance(success, bool))
            or (success is not True and code != 0)):
        raise ambiguous("接口响应缺少明确的成功状态")
    data = payload.get("data")
    if data is not None and not isinstance(data, dict):
        raise ambiguous("接口 data 应为 JSON 对象")
    return payload


def check_http_status(status: int) -> None:
    """Shared status validation, including non-JSON media upload responses."""
    if status in (403, 406, 429, 461, 471):
        raise XhsApiError(
            f"触发验证码/风控(HTTP {status}),请稍后再试",
            category="risk", status_code=status, signal=f"http_{status}")
    if status == 401:
        raise XhsApiError(
            "登录状态已失效", category="auth", status_code=401, signal="http_401")
    if status == 407:
        raise XhsApiError(
            "代理认证失败", category="network", status_code=407, signal="proxy_auth")
    if status >= 500:
        raise XhsApiError(
            f"平台服务异常(HTTP {status})", category="network",
            status_code=status, signal=f"http_{status}")
    if not 200 <= status < 300:
        raise XhsApiError(
            f"接口 HTTP 状态异常({status})", category="business",
            status_code=status, signal=f"http_{status}")


def parse_response(response) -> dict:
    """Return the validated envelope; HTTP failure always takes precedence."""
    status = response.status_code
    check_http_status(status)
    try:
        payload = response.json()
    except Exception as exc:
        raise XhsApiError(
            f"非 JSON 响应(HTTP {status})", category="risk", status_code=status,
            signal="ambiguous_response") from exc
    return validate_payload(payload, status_code=status)
