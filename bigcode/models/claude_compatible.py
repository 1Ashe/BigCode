"""Claude Messages 兼容模型客户端。

学习思路：complete() 把 system/messages/tools 组装成 HTTP 请求，并把返回的 text/tool_use 块转回内部 AssistantMessage。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from bigcode.config.models import ResolvedModel
from bigcode.context.messages import ApiMessage, AssistantMessage, TextBlock, ToolUseBlock


@dataclass
class ModelResponse:
    """模型响应的统一包装。

    message 是 BigCode 内部格式，raw 保留 provider 原始 JSON 方便调试。
    """
    message: AssistantMessage
    raw: dict[str, Any]


class ClaudeCompatibleModelClient:
    """Claude Messages 兼容 HTTP 客户端。"""
    def __init__(self, model: ResolvedModel) -> None:
        """保存当前模型配置，后续 complete() 会从这里读取 base_url、model_id 和鉴权方式。"""
        self.model = model

    async def complete(
        self,
        system_prompt: str | list[ApiMessage],
        messages: list[ApiMessage] | list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """发送一次模型请求并解析响应。

        它支持工具 schema，返回内容中的 text 和 tool_use 会转成内部 ContentBlock。
        """
        if tools is None:
            # 兼容旧调用方式：早期 complete() 只有 (messages, tools) 两个参数，
            # 没有单独的 system_prompt。这里通过 tools is None 判断旧形式。
            api_messages = system_prompt  # type: ignore[assignment]
            tool_schemas = messages  # type: ignore[assignment]
            system = ""
        else:
            system = str(system_prompt)
            api_messages = messages  # type: ignore[assignment]
            tool_schemas = tools

        # api_key_env 只保存环境变量名，不保存真实密钥。这样模型配置文件可以进仓库，
        # 真正的密钥仍留在运行环境里。
        api_key = os.environ.get(self.model.api_key_env or "")
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        headers.update(self.model.default_headers)
        if self.model.api_key_env and not api_key and not _has_auth_header(headers):
            raise RuntimeError(
                f"Missing API key for model {self.model.ref}: set environment variable {self.model.api_key_env}."
            )

        url = f"{self.model.base_url.rstrip('/')}/messages"
        # 这里构造的是 Claude Messages 风格 payload。ApiMessage 是 Pydantic 模型，
        # model_dump(exclude_none=True) 会把 Python 对象变成普通 dict，同时去掉 None 字段。
        payload: dict[str, Any] = {
            "model": self.model.model_id,
            "system": system,
            "messages": [m.model_dump(exclude_none=True) for m in api_messages],  # type: ignore[union-attr]
            "max_tokens": self.model.max_output_tokens or 4096,
        }
        if tool_schemas and self.model.capabilities.supports_tools:
            # 只有模型声明支持工具时才把 tools 发出去，避免不支持工具的 provider 报协议错误。
            payload["tools"] = tool_schemas

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            # HTTPStatusError 说明服务器有响应，但状态码是 4xx/5xx。
            # 截取前 500 字符可以给用户足够线索，又避免把超长错误页塞进异常。
            detail = exc.response.text.strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"Model request failed for {self.model.ref}: HTTP {exc.response.status_code} from {url}{suffix}"
            ) from exc
        except httpx.RequestError as exc:
            # RequestError 通常是 DNS、连接、超时等网络层问题。
            detail = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(f"Model request failed for {self.model.ref} at {url}: {detail}") from exc
        except ValueError as exc:
            # resp.json() 解析失败会走这里，说明 provider 返回的不是合法 JSON。
            detail = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(f"Model response from {self.model.ref} was not valid JSON: {detail}") from exc

        blocks = []
        for block in data.get("content") or []:
            block_type = block.get("type")
            if block_type == "text":
                blocks.append(TextBlock(text=block.get("text") or ""))
            elif block_type == "tool_use":
                # Claude 工具调用块会带 id/name/input。id 后续必须和 tool_result 对上，
                # 所以即使字段缺失也先填空字符串，保持内部结构完整。
                blocks.append(ToolUseBlock(id=block.get("id") or "", name=block.get("name") or "", input=block.get("input") or {}))
        assistant = AssistantMessage(
            blocks,
            model=self.model.ref,
            stop_reason=data.get("stop_reason"),
            usage=data.get("usage") or {},
        )
        return ModelResponse(message=assistant, raw=data)


def _has_auth_header(headers: dict[str, str]) -> bool:
    """检查默认 headers 是否已经包含鉴权 header。"""
    lowered = {key.lower() for key in headers}
    return bool(lowered.intersection({"x-api-key", "authorization", "api-key"}))
