"""Lightweight discovery layer for research_toolkit tools.

Convention: each research_toolkit package exposes a module-level dict::

    TOOL_MANIFEST: dict[str, ToolSpec]

where ToolSpec is a TypedDict with keys:
  module      – importable module path (str)
  fn          – callable name within the module (str)
  description – human-readable description shown to analyst workers (str)
  args_schema – JSON Schema object describing the args dict (dict)

The registry imports each manifest lazily, so no network activity occurs on
startup. toolkit_root is prepended to sys.path only during manifest loading,
then removed.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import Any, TypedDict


class ToolSpec(TypedDict):
    module: str
    fn: str
    description: str
    args_schema: dict


# Names of top-level packages in research_toolkit that expose TOOL_MANIFEST.
_MANIFEST_MODULES: list[str] = [
    "xspec_utils",
]


class ToolkitRegistry:
    """Discovers and describes tools available in research_toolkit and via MCP."""

    def __init__(self, toolkit_root: Path, *, disabled_tools: list[str] | None = None) -> None:
        self._root = Path(toolkit_root)
        self._tools: dict[str, ToolSpec] = {}
        self._mcp_tools: dict[str, str] = {}  # kind -> description
        self._mcp_schemas: dict[str, dict] = {}  # kind -> input JSON schema
        self._disabled: set[str] = set(disabled_tools or [])
        self._loaded = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Import TOOL_MANIFEST from every known package. Idempotent."""
        if self._loaded:
            return
        root_str = str(self._root)
        inserted = root_str not in sys.path
        if inserted:
            sys.path.insert(0, root_str)
        try:
            for pkg in _MANIFEST_MODULES:
                try:
                    mod = importlib.import_module(pkg)
                    manifest: dict[str, ToolSpec] = getattr(mod, "TOOL_MANIFEST", {})
                    # Enrich descriptions with function docstrings when available.
                    for name, spec in manifest.items():
                        try:
                            fn_mod = importlib.import_module(spec["module"])
                            fn = getattr(fn_mod, spec["fn"])
                            doc = (fn.__doc__ or "").strip()
                            if doc:
                                spec = dict(spec)
                                spec["description"] = doc
                                manifest = dict(manifest)
                                manifest[name] = spec  # type: ignore[assignment]
                        except Exception:  # noqa: BLE001
                            pass
                    self._tools.update(manifest)
                except Exception:  # noqa: BLE001
                    pass  # package not installed or missing manifest – skip silently
        finally:
            if inserted:
                sys.path.remove(root_str)
        for name in self._disabled:
            self._tools.pop(name, None)
        self._loaded = True

    def load_mcp(self, client: Any) -> None:
        """Index tools from a started MCPToolClient into the registry."""
        for kind in client.list_kinds():
            self._mcp_tools[kind] = client.describe(kind)
            schema = client.schema(kind) if hasattr(client, "schema") else None
            self._mcp_schemas[kind] = schema or {"type": "object", "properties": {}}

    def is_mcp(self, kind: str) -> bool:
        return kind in self._mcp_tools

    def is_known(self, tool_name: str) -> bool:
        self.load()
        return tool_name in self._tools or tool_name in self._mcp_tools

    def list_tools(self) -> list[str]:
        self.load()
        return list(self._tools) + list(self._mcp_tools)

    def describe_tool(self, tool_name: str) -> str:
        """Return a short description suitable for injection into analyst prompts."""
        self.load()
        if tool_name in self._mcp_tools:
            return f"{tool_name}: {self._mcp_tools[tool_name]}"
        spec = self._tools[tool_name]
        return f"{tool_name}: {spec['description']}"

    def spec(self, tool_name: str) -> ToolSpec:
        self.load()
        if tool_name not in self._tools:
            raise KeyError(f"unknown tool: {tool_name!r}")
        return self._tools[tool_name]

    def tool_definitions(self) -> list[dict]:
        """Return provider-neutral function definitions for every tool.

        Each entry is ``{"name", "description", "input_schema"}`` covering both
        toolkit tools (their ``args_schema``) and MCP tools (their ``inputSchema``).
        ``name`` is a provider-safe sanitization of the dotted ``kind`` (function
        names must match ``^[A-Za-z0-9_-]{1,64}$``); use :meth:`kind_for_function`
        to map a returned tool-call name back to the canonical ``kind``.
        """
        self.load()
        defs: list[dict] = []
        for kind, spec in self._tools.items():
            defs.append(
                {
                    "name": self._function_name(kind),
                    "description": spec["description"],
                    "input_schema": spec.get("args_schema") or {"type": "object", "properties": {}},
                }
            )
        for kind, desc in self._mcp_tools.items():
            defs.append(
                {
                    "name": self._function_name(kind),
                    "description": desc,
                    "input_schema": self._mcp_schemas.get(kind, {"type": "object", "properties": {}}),
                }
            )
        return defs

    def kind_for_function(self, function_name: str) -> str:
        """Map a provider tool-call name back to the canonical dotted ``kind``."""
        self.load()
        for kind in list(self._tools) + list(self._mcp_tools):
            if self._function_name(kind) == function_name:
                return kind
        # Already canonical (or unknown) — return as-is for the caller to handle.
        return function_name

    @staticmethod
    def _function_name(kind: str) -> str:
        """Sanitize a dotted kind into a provider-safe function name."""
        return re.sub(r"[^A-Za-z0-9_-]", "_", kind)

    def tool_catalogue(self) -> str:
        """Return a multi-line catalogue of all tools for prompt injection."""
        self.load()
        has_toolkit = bool(self._tools)
        has_mcp = bool(self._mcp_tools)
        if not has_toolkit and not has_mcp:
            return "(no tools registered)"
        lines = []
        if has_toolkit:
            lines.append("Available tools in research_toolkit:")
            for name, spec in self._tools.items():
                lines.append(f"  {name}: {spec['description']}")
        if has_mcp:
            lines.append("Available MCP tools:")
            for kind, desc in self._mcp_tools.items():
                lines.append(f"  {kind}: {desc}")
        return "\n".join(lines)
