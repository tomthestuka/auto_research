"""MCP client: connects to stdio MCP servers, lists and calls their tools.

Tool kinds use the convention ``mcp.{server_name}.{tool_name}`` so they are
unambiguous alongside TOOL_MANIFEST kinds in ToolkitRegistry.

Usage::

    client = MCPToolClient()           # default: auto_research_builtin server
    await client.start()
    result = await client.call_tool("mcp.builtin.bash_exec", {"command": "ls /"})
    await client.shutdown()
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_BUILTIN_SERVER = Path(__file__).parent / "builtin_server.py"

_DEFAULT_SERVERS: list[dict] = [
    {
        "name": "builtin",
        "command": sys.executable,
        "args": [str(_BUILTIN_SERVER)],
    }
]


class MCPToolClient:
    """Manages one or more stdio MCP server connections for the duration of a run.

    Servers are defined as dicts with keys ``name``, ``command``, and optional
    ``args`` (list of str).  All sessions are held open across tool calls via an
    ``AsyncExitStack``; call ``start()`` before use and ``shutdown()`` when done.
    """

    def __init__(self, servers: list[dict] | None = None) -> None:
        self._servers: list[dict] = servers if servers is not None else list(_DEFAULT_SERVERS)
        self._sessions: dict[str, ClientSession] = {}
        # kind -> (server_name, tool_name)
        self._tool_index: dict[str, tuple[str, str]] = {}
        self._tool_descs: dict[str, str] = {}
        self._tool_schemas: dict[str, dict] = {}  # kind -> input JSON schema
        self._stack: contextlib.AsyncExitStack = contextlib.AsyncExitStack()

    async def start(self) -> None:
        """Spawn all server subprocesses and establish sessions."""
        await self._stack.__aenter__()
        for server in self._servers:
            params = StdioServerParameters(
                command=server["command"],
                args=server.get("args", []),
                env=server.get("env"),
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server["name"]] = session
            result = await session.list_tools()
            for tool in result.tools:
                kind = f"mcp.{server['name']}.{tool.name}"
                self._tool_index[kind] = (server["name"], tool.name)
                self._tool_descs[kind] = tool.description or tool.name
                schema = getattr(tool, "inputSchema", None)
                self._tool_schemas[kind] = schema or {"type": "object", "properties": {}}

    def list_kinds(self) -> list[str]:
        """Return all registered MCP tool kinds."""
        return list(self._tool_index)

    def describe(self, kind: str) -> str:
        """Return a description string for a tool kind."""
        return self._tool_descs.get(kind, kind)

    def schema(self, kind: str) -> dict:
        """Return the input JSON schema for a tool kind."""
        return self._tool_schemas.get(kind, {"type": "object", "properties": {}})

    async def call_tool(self, kind: str, arguments: dict) -> dict:
        """Call a tool by its full kind string and return a result dict.

        Returns a dict with at minimum ``stdout``, ``stderr``, and ``exit_code``.
        """
        if kind not in self._tool_index:
            return {"stdout": "", "stderr": f"MCP tool not found: {kind}", "exit_code": 127}
        server_name, tool_name = self._tool_index[kind]
        session = self._sessions[server_name]
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as exc:
            return {"stdout": "", "stderr": str(exc), "exit_code": 1}
        parts = [item.text for item in result.content if hasattr(item, "text")]
        text = "\n".join(parts)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", 1 if result.isError else 0)
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return {
            "stdout": text,
            "stderr": "",
            "exit_code": 1 if result.isError else 0,
        }

    async def shutdown(self) -> None:
        """Close all sessions and terminate server subprocesses."""
        await self._stack.aclose()
