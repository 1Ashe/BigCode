"""Anthropic/OpenAI SDK 流式模型客户端。"""
from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from bigcode.config.models import ResolvedModel
from bigcode.context.messages import ApiMessage

from .events import (
    StreamEvent,
    StreamEnd,
    TextDelta,          
    ThinkingComplete,   
    ThinkingDelta,      
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)

try:
    import anthropic
except ImportError:
    class _MissingAnthropicError(Exception):
        pass

    anthropic = SimpleNamespace(
        AsyncAnthropic=None,
        AuthenticationError=type("AuthenticationError", (_MissingAnthropicError,), {}),
        RateLimitError=type("RateLimitError", (_MissingAnthropicError,), {}),
        APIConnectionError=type("APIConnectionError", (_MissingAnthropicError,), {}),
        APIStatusError=type("APIStatusError", (_MissingAnthropicError,), {}),
        APIError=type("APIError", (_MissingAnthropicError,), {}),
    )

try:
    import openai
except ImportError:
    class _MissingOpenAIError(Exception):
        pass

    openai = SimpleNamespace(
        AsyncOpenAI=None,
        AuthenticationError=type("AuthenticationError", (_MissingOpenAIError,), {}),
        RateLimitError=type("RateLimitError", (_MissingOpenAIError,), {}),
        APIConnectionError=type("APIConnectionError", (_MissingOpenAIError,), {}),
        APIStatusError=type("APIStatusError", (_MissingOpenAIError,), {}),
        OpenAIError=type("OpenAIError", (_MissingOpenAIError,), {}),
    )


class LLMError(Exception):
    """模型 Provider 错误的统一基类。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(LLMError):
    """API key 缺失、无效或无权限。"""


class RateLimitError(LLMError):
    """Provider 限流。"""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = 429,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


class NetworkError(LLMError):
    """连接失败、DNS、超时等网络层错误。"""


class LLMClient(ABC):
    """AgentSession 使用的唯一模型客户端接口。"""

    def __init__(self, model: ResolvedModel) -> None:
        self.model = model
        self.max_output_tokens = model.max_output_tokens or 4096

    @abstractmethod
    async def stream(
        self,
        system_prompt: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if False:
            yield StreamEnd(None, 0, 0)
        raise NotImplementedError


def create_client(model: ResolvedModel) -> LLMClient:
    """根据模型协议创建 SDK 客户端。"""
    if model.protocol == "anthropic":
        return AnthropicClient(model)
    if model.protocol == "openai":
        return OpenAIClient(model)
    raise ValueError(f"Unknown model protocol: {model.protocol}")


def _api_key(model: ResolvedModel) -> str | None:
    return os.environ.get(model.api_key_env or "")


def _has_auth_header(headers: dict[str, str]) -> bool:
    lowered = {key.lower() for key in headers}
    return bool(lowered.intersection({"x-api-key", "authorization", "api-key"}))


def _retry_after_from_exception(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_tool_input(raw: str, fallback: Any = None) -> dict[str, Any]:
    if not raw:
        return fallback if isinstance(fallback, dict) else {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, ApiMessage):
        return message.model_dump(exclude_none=True)
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return dict(message)


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if "input_schema" in tool:
            normalized.append(dict(tool))
            continue
        item = {
            "name": tool["name"],
            "input_schema": tool.get("parameters", {"type": "object"}),
        }
        if tool.get("description"):
            item["description"] = tool["description"]
        normalized.append(item)
    return normalized


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function" and "parameters" in tool:
            normalized.append(dict(tool))
            continue
        item = {
            "type": "function",
            "name": tool["name"],
            "parameters": tool.get("input_schema", {"type": "object"}),
        }
        if tool.get("description"):
            item["description"] = tool["description"]
        normalized.append(item)
    return normalized


class AnthropicClient(LLMClient):
    """基于 ``anthropic.AsyncAnthropic`` 的流式客户端。"""

    def __init__(self, model: ResolvedModel, *, client: Any | None = None) -> None:
        super().__init__(model)
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if anthropic.AsyncAnthropic is None:
                raise LLMError("Anthropic Python SDK 未安装，请安装项目依赖后再使用 anthropic 协议。")
            kwargs: dict[str, Any] = {}
            api_key = _api_key(self.model)
            if api_key:
                kwargs["api_key"] = api_key
            if self.model.base_url:
                kwargs["base_url"] = self.model.base_url
            if self.model.default_headers:
                kwargs["default_headers"] = self.model.default_headers
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    @staticmethod
    def _supports_adaptive_thinking(model_id: str) -> bool:
        match = re.search(r"claude-(?:opus|sonnet)-4-(\d+)(?:-|$)", model_id)
        return bool(match and int(match.group(1)) >= 6)

    def _request_kwargs(
        self,
        system_prompt: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model.model_id,
            "max_tokens": self.max_output_tokens,
            "messages": [_message_to_dict(message) for message in messages],
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools and self.model.capabilities.supports_tools:
            kwargs["tools"] = _anthropic_tools(tools)
        if self.model.temperature is not None:
            kwargs["temperature"] = self.model.temperature
        if self.model.thinking and self.model.capabilities.supports_thinking:
            if self._supports_adaptive_thinking(self.model.model_id):
                kwargs["thinking"] = {"type": "adaptive"}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(1024, self.max_output_tokens - 1),
                }
        return kwargs

    async def stream(
        self,
        system_prompt: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        api_key = _api_key(self.model)
        if self.model.api_key_env and not api_key and not _has_auth_header(self.model.default_headers):
            raise AuthenticationError(f"Missing API key for model {self.model.ref}: set environment variable {self.model.api_key_env}.")

        input_tokens = 0
        output_tokens = 0
        stop_reason: str | None = None
        stream_ended = False
        thinking: dict[int, dict[str, str]] = {}
        tool_calls: dict[int, dict[str, Any]] = {}

        try:
            client = self._get_client()
            kwargs = self._request_kwargs(system_prompt, messages, tools or [])
            async with client.messages.stream(**kwargs) as response_stream:
                async for event in response_stream:
                    event_type = getattr(event, "type", "")

                    if event_type == "message_start":
                        usage = getattr(getattr(event, "message", None), "usage", None)
                        input_tokens = getattr(usage, "input_tokens", 0) or 0
                        output_tokens = getattr(usage, "output_tokens", 0) or 0

                    elif event_type == "content_block_start":
                        index = event.index
                        block = event.content_block
                        block_type = getattr(block, "type", "")
                        if block_type == "thinking":
                            thinking[index] = {
                                "thinking": getattr(block, "thinking", "") or "",
                                "signature": getattr(block, "signature", "") or "",
                            }
                        elif block_type == "tool_use":
                            tool_calls[index] = {
                                "id": block.id,
                                "name": block.name,
                                "json": "",
                                "input": getattr(block, "input", None),
                            }
                            yield ToolCallStart(id=block.id, name=block.name)

                    elif event_type == "content_block_delta":
                        index = event.index
                        delta = event.delta
                        delta_type = getattr(delta, "type", "")
                        if delta_type == "text_delta":
                            yield TextDelta(text=delta.text)
                        elif delta_type == "thinking_delta":
                            value = delta.thinking
                            state = thinking.setdefault(index, {"thinking": "", "signature": ""})
                            state["thinking"] += value
                            yield ThinkingDelta(thinking=value)
                        elif delta_type == "signature_delta":
                            state = thinking.setdefault(index, {"thinking": "", "signature": ""})
                            state["signature"] += delta.signature
                        elif delta_type == "input_json_delta":
                            state = tool_calls[index]
                            state["json"] += delta.partial_json
                            yield ToolCallDelta(id=state["id"], partial_json=delta.partial_json)

                    elif event_type == "content_block_stop":
                        index = event.index
                        if index in thinking:
                            state = thinking.pop(index)
                            yield ThinkingComplete(thinking=state["thinking"], signature=state["signature"])
                        elif index in tool_calls:
                            state = tool_calls.pop(index)
                            yield ToolCallComplete(
                                id=state["id"],
                                name=state["name"],
                                input=_decode_tool_input(state["json"], fallback=state["input"]),
                            )

                    elif event_type == "message_delta":
                        stop_reason = getattr(event.delta, "stop_reason", None)
                        usage = getattr(event, "usage", None)
                        output_tokens = getattr(usage, "output_tokens", output_tokens) or output_tokens

                    elif event_type == "message_stop":
                        yield StreamEnd(stop_reason=stop_reason, input_tokens=input_tokens, output_tokens=output_tokens)
                        stream_ended = True

            if not stream_ended:
                yield StreamEnd(stop_reason, input_tokens, output_tokens)

        except asyncio.CancelledError:
            raise
        except anthropic.AuthenticationError as error:
            raise AuthenticationError(str(error), status_code=getattr(error, "status_code", 401)) from error
        except anthropic.RateLimitError as error:
            raise RateLimitError(str(error), retry_after=_retry_after_from_exception(error), status_code=getattr(error, "status_code", 429)) from error
        except anthropic.APIConnectionError as error:
            raise NetworkError(str(error)) from error
        except anthropic.APIStatusError as error:
            raise LLMError(str(error), status_code=getattr(error, "status_code", None)) from error
        except anthropic.APIError as error:
            raise LLMError(str(error)) from error


class OpenAIClient(LLMClient):
    """基于 ``openai.AsyncOpenAI`` Responses API 的流式客户端。"""

    def __init__(self, model: ResolvedModel, *, client: Any | None = None) -> None:
        super().__init__(model)
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if openai.AsyncOpenAI is None:
                raise LLMError("OpenAI Python SDK 未安装，请安装项目依赖后再使用 openai 协议。")
            kwargs: dict[str, Any] = {}
            api_key = _api_key(self.model)
            if api_key:
                kwargs["api_key"] = api_key
            if self.model.base_url:
                kwargs["base_url"] = self.model.base_url
            if self.model.default_headers:
                kwargs["default_headers"] = self.model.default_headers
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    def _request_kwargs(
        self,
        system_prompt: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model.model_id,
            "input": [_message_to_dict(message) for message in messages],
            "max_output_tokens": self.max_output_tokens,
            "stream": True,
        }
        if system_prompt:
            kwargs["instructions"] = system_prompt
        if tools and self.model.capabilities.supports_tools:
            kwargs["tools"] = _openai_tools(tools)
        if self.model.temperature is not None:
            kwargs["temperature"] = self.model.temperature
        return kwargs

    @staticmethod
    def _stop_reason(response: Any) -> str:
        output = getattr(response, "output", []) or []
        if any(getattr(item, "type", "") == "function_call" for item in output):
            return "tool_use"
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None)
            if reason == "max_output_tokens":
                return "max_tokens"
            return reason or "incomplete"
        return "end_turn"

    async def stream(
        self,
        system_prompt: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        api_key = _api_key(self.model)
        if self.model.api_key_env and not api_key and not _has_auth_header(self.model.default_headers):
            raise AuthenticationError(f"Missing API key for model {self.model.ref}: set environment variable {self.model.api_key_env}.")

        stream_ended = False
        calls_by_item_id: dict[str, dict[str, str]] = {}

        try:
            client = self._get_client()
            response_stream = await client.responses.create(**self._request_kwargs(system_prompt, messages, tools or []))
            async for event in response_stream:
                event_type = getattr(event, "type", "")

                if event_type == "response.output_text.delta":
                    yield TextDelta(text=event.delta)

                elif event_type == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", "") == "function_call":
                        item_id = getattr(item, "id", None) or item.call_id
                        state = {
                            "id": item.call_id,
                            "name": item.name,
                            "json": getattr(item, "arguments", "") or "",
                        }
                        calls_by_item_id[item_id] = state
                        yield ToolCallStart(id=item.call_id, name=item.name)

                elif event_type == "response.function_call_arguments.delta":
                    state = calls_by_item_id[event.item_id]
                    state["json"] += event.delta
                    yield ToolCallDelta(id=state["id"], partial_json=event.delta)

                elif event_type == "response.function_call_arguments.done":
                    state = calls_by_item_id.pop(event.item_id)
                    raw_arguments = event.arguments or state["json"]
                    yield ToolCallComplete(id=state["id"], name=event.name or state["name"], input=_decode_tool_input(raw_arguments))

                elif event_type == "response.completed":
                    response = event.response
                    usage = getattr(response, "usage", None)
                    yield StreamEnd(
                        stop_reason=self._stop_reason(response),
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    )
                    stream_ended = True

            if not stream_ended:
                yield StreamEnd(None, 0, 0)

        except asyncio.CancelledError:
            raise
        except openai.AuthenticationError as error:
            raise AuthenticationError(str(error), status_code=getattr(error, "status_code", 401)) from error
        except openai.RateLimitError as error:
            raise RateLimitError(str(error), retry_after=_retry_after_from_exception(error), status_code=getattr(error, "status_code", 429)) from error
        except openai.APIConnectionError as error:
            raise NetworkError(str(error)) from error
        except openai.APIStatusError as error:
            raise LLMError(str(error), status_code=getattr(error, "status_code", None)) from error
        except openai.OpenAIError as error:
            raise LLMError(str(error)) from error
