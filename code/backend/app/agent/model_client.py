"""OpenAI Responses API 适配器；不写日志、不保存服务端对话状态。"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr


class ModelProviderError(RuntimeError):
    """可安全呈现的模型服务故障，不包含供应商响应正文或密钥。"""

    def __init__(self, code: str, *, retryable: bool, attempts: int = 1):
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """只保留调用器所需的公开响应字段，不暴露隐藏推理。"""

    response_id: str
    model: str
    output: tuple[dict[str, Any], ...]
    output_text: str
    usage: dict[str, int] | None
    attempts: int


class OpenAIResponsesClient:
    """通过 OpenAI Responses API 执行真实模型请求与 JSON Schema 工具选择。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model.strip() or not api_key.get_secret_value().strip():
            raise ValueError("模型名称与 API 密钥必须配置")
        if timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("模型超时和重试配置无效")
        parsed_url = httpx.URL(base_url)
        if (
            (
                parsed_url.scheme != "https"
                and not (
                    parsed_url.scheme == "http"
                    and parsed_url.host in {"localhost", "127.0.0.1", "::1"}
                )
            )
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("模型服务地址必须使用 HTTPS（本机回环调试可使用 HTTP）")
        self.model = model
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._transport = transport

    async def create_response(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        """最多进行有限次数的网络重试；仅重试超时、限流和供应商暂时故障。"""
        if not instructions.strip() or not input_items:
            raise ValueError("模型指令与输入不能为空")
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            # 完整请求/响应只保留在调用方内存与本地受保护轨迹中。
            "store": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        attempts = 0
        for attempt in range(1, self.max_retries + 2):
            attempts = attempt
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout_seconds),
                    transport=self._transport,
                ) as client:
                    response = await client.post(
                        f"{self.base_url}/responses", headers=headers, json=body
                    )
                if response.status_code >= 400:
                    quota_exhausted = (
                        response.status_code == 429
                        and _has_quota_exhaustion_code(response)
                    )
                    retryable = not quota_exhausted and (
                        response.status_code in {408, 409, 425, 429}
                        or response.status_code >= 500
                    )
                    if retryable and attempt <= self.max_retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    code = (
                        "MODEL_QUOTA_EXHAUSTED"
                        if quota_exhausted
                        else "MODEL_RATE_LIMITED"
                        if response.status_code == 429
                        else "MODEL_PROVIDER_REJECTED"
                        if 400 <= response.status_code < 500
                        else "MODEL_UNAVAILABLE"
                    )
                    raise ModelProviderError(
                        code, retryable=retryable, attempts=attempt
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ModelProviderError(
                        "MODEL_RESPONSE_INVALID", retryable=False, attempts=attempt
                    ) from exc
                return _parse_response(
                    payload, attempts=attempt, fallback_model=self.model
                )
            except ModelProviderError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt <= self.max_retries:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                code = (
                    "MODEL_TIMEOUT"
                    if isinstance(exc, httpx.TimeoutException)
                    else "MODEL_UNAVAILABLE"
                )
                raise ModelProviderError(
                    code, retryable=True, attempts=attempt
                ) from exc
            except httpx.HTTPError as exc:
                raise ModelProviderError(
                    "MODEL_TRANSPORT_ERROR", retryable=False, attempts=attempt
                ) from exc
        raise ModelProviderError("MODEL_UNAVAILABLE", retryable=True, attempts=attempts)


def _has_quota_exhaustion_code(response: httpx.Response) -> bool:
    """只检查供应商稳定错误码，不读取或记录可能含敏感信息的错误消息。"""
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    codes = {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_hard_limit_reached",
    }
    return any(error.get(field) in codes for field in ("type", "code"))


def _backoff(attempt: int) -> float:
    """有限指数退避加少量随机抖动，防止并发重试同时冲击供应商。"""
    return min(2.0, 0.25 * (2 ** (attempt - 1))) + random.uniform(0, 0.1)


def _parse_response(
    value: object, *, attempts: int, fallback_model: str
) -> ModelResponse:
    if not isinstance(value, dict):
        raise ModelProviderError(
            "MODEL_RESPONSE_INVALID", retryable=False, attempts=attempts
        )
    response_id = value.get("id")
    output = value.get("output")
    if not isinstance(response_id, str) or not isinstance(output, list):
        raise ModelProviderError(
            "MODEL_RESPONSE_INVALID", retryable=False, attempts=attempts
        )
    if value.get("status") != "completed":
        code = (
            "MODEL_RESPONSE_INCOMPLETE"
            if value.get("status") == "incomplete"
            else "MODEL_RESPONSE_FAILED"
        )
        raise ModelProviderError(code, retryable=False, attempts=attempts)
    normalized: list[dict[str, Any]] = []
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ModelProviderError(
                "MODEL_RESPONSE_INVALID", retryable=False, attempts=attempts
            )
        # Responses API 的完整公开输出项必须回填，以满足 function_call 配对契约；
        # 此处不读取或记录隐藏推理文本。
        normalized.append(item)
        if item.get("type") == "message" and isinstance(item.get("content"), list):
            for part in item["content"]:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise ModelProviderError(
                        "MODEL_REFUSED", retryable=False, attempts=attempts
                    )
                if part.get("type") == "output_text":
                    text_value = part.get("text")
                    if isinstance(text_value, str):
                        texts.append(text_value)
    model = value.get("model")
    usage_value = value.get("usage")
    usage: dict[str, int] | None = None
    if isinstance(usage_value, dict):
        input_tokens = usage_value.get("input_tokens")
        output_tokens = usage_value.get("output_tokens")
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens >= 0
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0
        ):
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
    return ModelResponse(
        response_id=response_id,
        model=model if isinstance(model, str) and model else fallback_model,
        output=tuple(normalized),
        output_text="\n".join(texts),
        usage=usage,
        attempts=attempts,
    )
