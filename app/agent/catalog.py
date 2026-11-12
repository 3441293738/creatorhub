"""Bounded, on-demand upstream model discovery. No inference or settings writes."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from .config import ConnectionSettings
from .privacy import redact

MAX_MODELS = 1000
MAX_BYTES = 2_000_000
CATALOG_TIMEOUT = 20


class CatalogError(Exception):
    """Credential-free diagnostic; never include upstream bodies or headers."""


def status_error(status: int, *, has_key: bool) -> CatalogError:
    if status == 401:
        detail = ("已携带 API Key，请检查密钥是否有效、是否属于该上游，以及鉴权协议是否正确"
                  if has_key else "本次未携带 API Key，请填写该上游的密钥后重试")
        return CatalogError(f"上游返回 401：鉴权未通过。{detail}；未自动重试。")
    if status == 403:
        return CatalogError("上游返回 403：访问被拒绝，请检查密钥权限、模型目录权限或上游访问策略。")
    if status in {404, 405}:
        return CatalogError(f"上游返回 {status}：该基础地址未开放模型列表，请检查 /v1 等前缀；也可直接填写模型 ID。")
    if status == 429:
        return CatalogError("上游返回 429：请求受限，请稍后重试；未自动重试。")
    if 300 <= status < 400:
        return CatalogError("模型列表接口返回重定向，已停止转发密钥；请填写最终模型基础地址。")
    return CatalogError(f"获取模型列表失败：上游返回 HTTP {status}，请检查基础地址、协议及服务状态。")


def parse_models(payload, api_key: str = "") -> dict:
    rows = payload.get("data", payload.get("models")) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise CatalogError("上游模型列表格式不符合预期，需要 data 或 models 数组；仍可手动填写模型 ID。")
    models, seen = [], set()
    skipped = 0
    for item in rows[:MAX_MODELS]:
        if isinstance(item, str):
            item = {"id": item}
        model_id = item.get("id") if isinstance(item, dict) else None
        if (not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 160
                or any(ord(char) < 32 or ord(char) == 127 for char in model_id)
                or (api_key and api_key in model_id)):
            skipped += 1
            continue
        model_id = model_id.strip()
        if model_id in seen:
            continue
        seen.add(model_id)
        name = item.get("display_name") or item.get("name") or model_id
        name = redact(name, (api_key,))[:200] if isinstance(name, str) else model_id
        name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip() or model_id
        models.append({"id": model_id, "name": name})
    truncated = len(rows) > MAX_MODELS or bool(isinstance(payload, dict) and payload.get("has_more"))
    return {"models": models, "count": len(models), "truncated": truncated, "skipped": skipped}


async def discover_models(settings: ConnectionSettings) -> dict:
    if not settings.base_url:
        raise CatalogError("请先填写模型基础地址。")
    if settings.base_url.endswith("/models"):
        raise CatalogError("请填写模型基础地址（例如 https://HOST/v1），不要带 /models。")
    headers = {"Accept": "application/json"}
    if settings.api_key:
        if settings.provider == "anthropic":
            headers["x-api-key"] = settings.api_key
        else:
            headers["Authorization"] = "Bearer " + settings.api_key
    if settings.provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"

    async def fetch():
        # The official Responses model catalog has no limit parameter. Compatible
        # gateways commonly support it; only a rejected query gets one fallback.
        params = {} if settings.provider == "openai_responses" else {"limit": MAX_MODELS}
        attempts = [params, {}] if settings.provider == "openai_compatible" else [params]
        endpoints = [settings.base_url + "/models"]
        # A bare domain can expose a gateway alias at /models or the standard
        # /v1/models route. Preserve an explicit path; never guess another host.
        if not urlsplit(settings.base_url).path:
            endpoints.append(settings.base_url + "/v1/models")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            for endpoint_index, endpoint in enumerate(endpoints):
                for index, query in enumerate(attempts):
                    async with client.stream("GET", endpoint, headers=headers, params=query) as response:
                        if response.status_code in {404, 405} and endpoint_index + 1 < len(endpoints):
                            break
                        if response.status_code in {400, 422} and index + 1 < len(attempts):
                            continue
                        if response.status_code != 200:
                            raise status_error(response.status_code, has_key=bool(settings.api_key))
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_BYTES:
                                raise CatalogError("上游模型列表超过 2 MB 限制；仍可手动填写模型 ID。")
                    return {**parse_models(json.loads(data), settings.api_key), "path_prefix_added": endpoint_index > 0}

    try:
        return await asyncio.wait_for(fetch(), timeout=CATALOG_TIMEOUT)
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise CatalogError("获取模型列表超时，请检查网络或上游服务；未自动重试。") from exc
    except httpx.HTTPError as exc:
        raise CatalogError("连接上游模型列表失败，请检查基础地址、网络和代理。") from exc
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise CatalogError("上游模型列表格式异常，请检查基础地址和接口协议；仍可手动填写模型 ID。") from exc
