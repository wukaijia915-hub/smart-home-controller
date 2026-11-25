"""轻量级 MCP（Model Context Protocol）Server。
基于 JSON-RPC 2.0 over HTTP，将设备控制工具暴露为 MCP 工具，
供外部 AI 客户端（如 Claude Desktop、Cursor 等）动态发现和调用。

参考：https://modelcontextprotocol.io/
"""
from __future__ import annotations
import json, logging
from typing import Optional

logger = logging.getLogger("mcp")

MCP_VERSION = "2024-11-05"
SERVER_INFO = {"name": "lingxi-smart-home", "version": "2.0.0"}


class MCPServer:
    """MCP Server：将工具注册表暴露为 MCP 协议接口。"""
    def __init__(self, tool_registry):
        self.registry = tool_registry
        self._initialized = False

    def handle_request(self, request: dict) -> Optional[dict]:
        """处理 JSON-RPC 请求，返回响应（notification 无 id 时返回 None）。"""
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})

        # notification（无 id）不返回响应
        if req_id is None:
            if method == "notifications/initialized":
                self._initialized = True
            return None

        try:
            if method == "initialize":
                return self._response(req_id, self._handle_initialize(params))
            elif method == "tools/list":
                return self._response(req_id, self._handle_tools_list(params))
            elif method == "tools/call":
                return self._response(req_id, self._handle_tools_call(params))
            elif method == "ping":
                return self._response(req_id, {})
            else:
                return self._error(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            logger.error(f"MCP 处理错误: {e}")
            return self._error(req_id, -32603, str(e))

    def _handle_initialize(self, params: dict) -> dict:
        self._initialized = True
        return {
            "protocolVersion": MCP_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": SERVER_INFO,
        }

    def _handle_tools_list(self, params: dict | None = None) -> dict:
        """列出工具。

        可选 `params.intent`：按意图裁剪（非 MCP 标准字段，标准客户端会忽略）。
        存在的理由是工具数量与选择精度成反比 —— 外部 AI 客户端一次拿 64 个工具
        时，与"用户这句话只该看见 6 个"是两回事。对照项目 Aether 的做法是
        按回合变体裁剪工具集（full / rule_create / no_create / clean）。
        """
        intent = ""
        if isinstance(params, dict):
            intent = str(params.get("intent") or "")
        tools, meta = self.registry.list_tools_scoped(intent)
        out = []
        for t in tools:
            schema = t.to_schema()["function"]
            out.append({
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            })
        result = {"tools": out}
        if intent:
            result["scope"] = meta
        return result

    def _handle_tools_call(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments", {})
        result = self.registry.call(name, args)
        if result.get("ok"):
            return {
                "content": [{"type": "text", "text": json.dumps(result.get("result", {}), ensure_ascii=False)}],
                "isError": False,
            }
        else:
            return {
                "content": [{"type": "text", "text": result.get("error", "Unknown error")}],
                "isError": True,
            }

    def _response(self, req_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error(self, req_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class MCPClient:
    """简化版 MCP Client：连接外部 MCP Server，动态发现和调用工具。
    支持 Streamable HTTP 传输（POST JSON-RPC）。
    """
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._session_id = None
        self._tools = []

    def _post(self, payload: dict) -> dict:
        import requests
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = requests.post(self.base_url, json=payload, headers=headers, timeout=10)
        # 处理 SSE 响应
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            for line in resp.text.split("\n"):
                if line.startswith("data: "):
                    return json.loads(line[6:])
        return resp.json()

    def initialize(self) -> bool:
        """初始化 MCP 连接。"""
        import uuid
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "lingxi-client", "version": "1.0.0"},
            },
        }
        resp = self._post(payload)
        if "result" in resp:
            self._initialized = True
            return True
        return False

    def list_tools(self) -> list:
        """列出远程 MCP Server 的工具。"""
        import uuid
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/list"}
        resp = self._post(payload)
        self._tools = resp.get("result", {}).get("tools", [])
        return self._tools

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        """调用远程 MCP 工具。"""
        import uuid
        arguments = arguments or {}
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self._post(payload)
        result = resp.get("result", {})
        if result.get("isError"):
            return {"ok": False, "error": result.get("content", [{}])[0].get("text", "Unknown error")}
        text = result.get("content", [{}])[0].get("text", "{}")
        try:
            return {"ok": True, "result": json.loads(text)}
        except Exception:
            return {"ok": True, "result": {"text": text}}
