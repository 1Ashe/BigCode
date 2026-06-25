"""MCP 客户端管理器。

学习思路：BigCode 本身不强依赖 FastMCP；如果安装了 fastmcp，这里负责连接服务器并列出工具、资源和 prompt。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from bigcode.config.models import McpServerConfig


@dataclass
class McpCapability:
    """MCP discovery 得到的一项能力。

    kind 区分 tool/resource/prompt，server 标记来自哪个 MCP server。
    """
    kind: str
    server: str
    name: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    read_only_hint: bool = False
    destructive_hint: bool = False
    open_world_hint: bool = False
    search_hint: str = ""
    always_load: bool = False


class McpClientManager:
    """MCP 客户端生命周期管理器。

    它延迟创建 FastMCP Client，并缓存每个 server 的连接。
    """
    def __init__(self, servers: dict[str, McpServerConfig], *, enabled: bool = True) -> None:
        """保存 MCP server 配置，检测 fastmcp 是否可导入，并准备客户端缓存。"""
        self.servers = servers
        self.enabled = enabled
        self.capabilities: list[McpCapability] = []
        self._fastmcp_available = _fastmcp_available()
        self._clients: dict[str, Any] = {}
        self._server_descriptions: dict[str, str] = {}

    def _timeout_for(self, server_name: str | None = None) -> float:
        server = self.servers.get(server_name or "") if server_name else None
        raw = server.config.get("timeout") if server else None
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
        return 30.0

    @property
    def fastmcp_available(self) -> bool:
        """返回当前 Python 环境是否安装了 fastmcp。"""
        return self._fastmcp_available

    async def discover(self, server_name: str | None = None) -> list[McpCapability]:
        """向已启用的 MCP server 查询工具、资源和 prompt 能力。"""
        if not self.enabled:
            return []
        if not self._fastmcp_available:
            return []

        # server_name 为空表示发现所有 server；传入名字时只检查那一个。
        names = [server_name] if server_name else list(self.servers)
        discovered: list[McpCapability] = []
        for name in names:
            server = self.servers.get(name or "")
            if not server or not server.enabled:
                continue
            try:
                # _client_for 会懒创建并缓存 client；discover 多次调用不会重复连接。
                timeout = self._timeout_for(server.name)
                client = await self._with_timeout(self._client_for(server.name), server.name, "connect", timeout)
                # 从 MCP serverInfo 提取服务级描述
                if server.name not in self._server_descriptions:
                    self._server_descriptions[server.name] = _extract_server_description(client)
                tools = await self._with_timeout(_maybe_await(client.list_tools()), server.name, "list_tools", timeout)
                for tool in tools or []:
                    # 不同 MCP/FastMCP 版本字段名可能略有差异，所以用 _obj_get
                    # 同时兼容 dict、对象属性、inputSchema/input_schema。
                    annotations = _obj_get(tool, "annotations", {}) or {}
                    meta = _obj_get(tool, "_meta", {}) or {}
                    discovered.append(
                        McpCapability(
                            kind="tool",
                            server=server.name,
                            name=_obj_get(tool, "name", ""),
                            description=_obj_get(tool, "description", ""),
                            schema=_obj_get(tool, "inputSchema", {}) or _obj_get(tool, "input_schema", {}) or {},
                            read_only_hint=_hint_bool(tool, annotations, "readOnlyHint", "read_only_hint"),
                            destructive_hint=_hint_bool(tool, annotations, "destructiveHint", "destructive_hint"),
                            open_world_hint=_hint_bool(tool, annotations, "openWorldHint", "open_world_hint"),
                            search_hint=_meta_str(meta, "anthropic/searchHint"),
                            always_load=bool(_obj_get(meta, "anthropic/alwaysLoad", False)),
                        )
                    )
                for resource in await self._with_timeout(_safe_list(client, "list_resources"), server.name, "list_resources", timeout):
                    discovered.append(McpCapability(kind="resource", server=server.name, name=_obj_get(resource, "uri", "") or _obj_get(resource, "name", ""), description=_obj_get(resource, "description", "")))
                for prompt in await self._with_timeout(_safe_list(client, "list_prompts"), server.name, "list_prompts", timeout):
                    discovered.append(McpCapability(kind="prompt", server=server.name, name=_obj_get(prompt, "name", ""), description=_obj_get(prompt, "description", "")))
            except Exception:
                # discovery 是能力索引，失败时不能影响主会话；doctor 的探测会单独报告错误。
                continue
        self.capabilities = discovered
        return discovered

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict[str, Any]:
        """调用指定 MCP server 上的工具，并把返回值转换成普通 Python/JSON 结构。"""
        if not self.enabled:
            raise RuntimeError("MCP is disabled.")
        if not self._fastmcp_available:
            raise RuntimeError("FastMCP is not installed in this environment.")
        timeout = self._timeout_for(server_name)
        client = await self._with_timeout(self._client_for(server_name), server_name, "connect", timeout)
        return _to_plain(await self._with_timeout(_maybe_await(client.call_tool(tool_name, arguments, raise_on_error=False)), server_name, f"call_tool:{tool_name}", timeout))

    async def list_resources(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """列出一个或所有 MCP server 暴露的资源。"""
        if not self._fastmcp_available:
            raise RuntimeError("FastMCP is not installed in this environment.")
        names = [server_name] if server_name else list(self.servers)
        resources: list[dict[str, Any]] = []
        for name in names:
            timeout = self._timeout_for(name or "")
            client = await self._with_timeout(self._client_for(name or ""), name or "", "connect", timeout)
            for resource in await self._with_timeout(_safe_list(client, "list_resources"), name or "", "list_resources", timeout):
                item = _to_plain(resource)
                if isinstance(item, dict):
                    # 补上 server 字段，调用方就能知道这个资源来自哪个 MCP server。
                    item.setdefault("server", name)
                resources.append(item if isinstance(item, dict) else {"server": name, "resource": item})
        return resources

    async def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        """读取指定 MCP server 上的一个资源 URI。"""
        if not self._fastmcp_available:
            raise RuntimeError("FastMCP is not installed in this environment.")
        timeout = self._timeout_for(server_name)
        client = await self._with_timeout(self._client_for(server_name), server_name, "connect", timeout)
        return _to_plain(await self._with_timeout(_maybe_await(client.read_resource(uri)), server_name, "read_resource", timeout))

    async def list_prompts(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """列出一个或所有 MCP server 暴露的 prompts。"""
        if not self._fastmcp_available:
            raise RuntimeError("FastMCP is not installed in this environment.")
        names = [server_name] if server_name else list(self.servers)
        prompts: list[dict[str, Any]] = []
        for name in names:
            timeout = self._timeout_for(name or "")
            client = await self._with_timeout(self._client_for(name or ""), name or "", "connect", timeout)
            for prompt in await self._with_timeout(_safe_list(client, "list_prompts"), name or "", "list_prompts", timeout):
                item = _to_plain(prompt)
                if isinstance(item, dict):
                    item.setdefault("server", name)
                prompts.append(item if isinstance(item, dict) else {"server": name, "prompt": item})
        return prompts

    async def get_prompt(self, server_name: str, name: str, arguments: dict | None = None) -> dict[str, Any]:
        """从指定 MCP server 获取一个 prompt，可传入 prompt 参数。"""
        if not self._fastmcp_available:
            raise RuntimeError("FastMCP is not installed in this environment.")
        timeout = self._timeout_for(server_name)
        client = await self._with_timeout(self._client_for(server_name), server_name, "connect", timeout)
        return _to_plain(await self._with_timeout(_maybe_await(client.get_prompt(name, arguments or {})), server_name, "get_prompt", timeout))

    def server_summaries(self) -> list[tuple[str, str]]:
        """返回已发现 MCP server 的 (name, description) 列表。

        优先级：mcp.json 配置 description > MCP serverInfo.title/description > 工具数量提示。
        不泄露具体工具名——模型应通过 Tool_Search 按需发现。
        只在 discover() 成功调用后有数据。
        """
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cap in self.capabilities:
            if cap.kind != "tool":
                continue
            if cap.server in seen:
                continue
            seen.add(cap.server)
            server = self.servers.get(cap.server)
            if server and server.description:
                result.append((cap.server, server.description))
                continue
            srv_desc = self._server_descriptions.get(cap.server, "")
            if srv_desc:
                result.append((cap.server, srv_desc))
                continue
            tool_count = sum(1 for c in self.capabilities if c.kind == "tool" and c.server == cap.server)
            result.append((cap.server, f"{tool_count} tool(s) available"))
        return result

    async def close_all(self) -> None:
        """关闭并清空已经创建的 MCP client。"""
        try:
            for name, client in list(self._clients.items()):
                close = getattr(client, "close", None)
                if close:
                    await self._with_timeout(_maybe_await(close()), name, "close", self._timeout_for(name))
        finally:
            self._clients.clear()

    async def _with_timeout(self, awaitable: Any, server_name: str, operation: str, timeout: float) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"MCP server {server_name!r} {operation} timed out after {timeout:g}s") from exc

    async def _client_for(self, server_name: str) -> Any:
        """返回指定 server 的 FastMCP client；没有缓存时才创建。"""
        if server_name in self._clients:
            return self._clients[server_name]
        server = self.servers.get(server_name)
        if not server or not server.enabled:
            raise RuntimeError(f"MCP server {server_name!r} is not configured or disabled.")
        from fastmcp import Client  # type: ignore

        config = _client_config(server)
        client = Client(config)
        enter = getattr(client, "__aenter__", None)
        if enter:
            # 有些 FastMCP Client 支持异步上下文管理。手动进入后缓存，
            # close_all() 时再统一关闭。
            client = await enter()
        self._clients[server_name] = client
        return client


def _fastmcp_available() -> bool:
    """检测当前环境是否能 import fastmcp。"""
    try:
        import fastmcp  # noqa: F401
    except Exception:
        return False
    return True


def _client_config(server: McpServerConfig) -> Any:
    """把 BigCode 的 MCP server 配置转换成 FastMCP Client 接受的格式。"""
    cfg = dict(server.config)
    transport = cfg.get("transport")
    if transport in {"http", "sse"} and cfg.get("url"):
        return cfg["url"]
    if transport == "stdio":
        return {"mcpServers": {server.name: cfg}}
    return cfg


async def _safe_list(client: Any, method: str) -> list[Any]:
    """安全调用可选的 list_* 方法；方法不存在时返回空列表。"""
    fn = getattr(client, method, None)
    if not fn:
        return []
    result = await _maybe_await(fn())
    return list(result or [])


async def _maybe_await(value: Any) -> Any:
    """如果 value 是 awaitable 就 await，否则原样返回。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def _obj_get(obj: Any, name: str, default: Any = None) -> Any:
    """同时支持从 dict 或对象属性中取字段。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _hint_bool(tool: Any, annotations: Any, camel_name: str, snake_name: str) -> bool:
    """从 tool 顶层或 annotations 中读取 MCP hint。"""
    return bool(
        _obj_get(tool, camel_name, False)
        or _obj_get(tool, snake_name, False)
        or _obj_get(annotations, camel_name, False)
        or _obj_get(annotations, snake_name, False)
    )


def _meta_str(meta: Any, name: str) -> str:
    """读取字符串型 MCP _meta 字段，并压缩空白避免污染工具列表。"""
    value = _obj_get(meta, name, "")
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _extract_server_description(client: Any) -> str:
    """从 FastMCP client 的 serverInfo 中提取服务级描述。

    serverInfo 是 MCP 协议 Implementation 类型，包含 title、version、websiteUrl
    以及 extra_data 中可能携带的 description。
    """
    try:
        init_result = getattr(client, "initialize_result", None)
        if init_result is None:
            return ""
        server_info = getattr(init_result, "serverInfo", None)
        if server_info is None:
            return ""
        # Implementation.title — MCP 规范定义的可读标题
        title = _obj_get(server_info, "title", "") or ""
        if title:
            return str(title).strip()
        # Implementation 支持 **extra_data，部分 server 可能填了 description
        extra_desc = _obj_get(server_info, "description", "") or ""
        if extra_desc:
            return str(extra_desc).strip()
        return ""
    except Exception:
        return ""


def _to_plain(value: Any) -> Any:
    """把 FastMCP 返回对象递归转换成普通 Python/JSON 结构。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {k: _to_plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value
