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

Each package may also carry an optional, pure-data ``tool_config.yaml`` at its root
(read here, never imported) declaring per-tool sandbox call-timeout ceilings — see
``_load_tool_config`` / ``call_timeout_ceiling``. This keeps timeout policy modular
(it travels with the package; the orchestration layer never names toolkit tools) and
hand-editable.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from pathlib import Path
from typing import Any, TypedDict

import yaml

_log = logging.getLogger(__name__)


class ToolSpec(TypedDict):
    module: str
    fn: str
    description: str
    args_schema: dict


# Sandbox per-tool-call timeout. A tool's call is SIGTERM-killed by the sandbox at this many
# seconds (exit 124), independent of any timeout inside the tool itself — so a genuinely
# long-running tool (a large download, a long reduction/fit) must declare a higher ceiling in
# its package ``tool_config.yaml``. The global default comes from ``orchestration.yaml``
# (``sandbox_call_timeout_s``); this module constant is only the fallback when that is absent.
# No hard upper clamp — ``tool_config.yaml`` is trusted human/coder-set infra, never LLM input.
_CALL_TIMEOUT_FALLBACK_S = 300

#: Appended to every toolkit tool's description: how an argument's value arrives, by the kind its
#: manifest declares (``auto_research.provenance.inputs``). The constraint lives where the model
#: reads it; each argument's own description names its kind.
REF_NOTE = (
    '\n\nEach argument\'s description says how its value arrives. A reference, {"$ref": '
    '"<receipt handle>#/<key>/<index>"}, passes the value at that place in one of your own tool '
    'results exactly as the tool returned it ("#" alone passes the whole returned value) and '
    "records that result as this call's source; a peer's value reaches your calls once a tool run "
    'of yours reproduces it. A parameter you choose is typed with its reason, {"value": <the '
    'value>, "reason": "<why this value>"}, and the call records both.'
)

#: What each kind adds to an argument's description, after the toolkit's own words.
_KIND_NOTES = {
    "data": (' Pass {"$ref": "<handle>#/<key>"}, the result of your own call that produced it '
             '(a list of references passes several).'),
    "parameter": (
        ' Pass {"$ref": "<handle>#/<key>"} (one of your results) or {"value": <the value>, '
        '"reason": "<why this value>"}'
    ),
    "path": ' A path as text, or {"$ref": "<handle>"} of a file a tool of yours returned.',
}
#: What an ``output`` argument adds, by its shape: the runtime places it in the call's own folder.
_OUTPUT_NOTES = {
    "folder": " The runtime gives this call its own folder here; leave it out.",
    "file": (" A file name: the runtime places it in this call's own folder, keeping the name alone "
             "(leave it out for {default})."),
    "prefix": (" A file-name prefix: the runtime places it in this call's own folder, keeping the "
               "name alone (leave it out for {default})."),
}
#: The same, for a call path that takes typed values only (the lead's in-process catalog read).
_TYPED_NOTES = {"path": " A path as text."}


def provider_schema(schema: dict | None, *, references: bool = True) -> dict:
    """An ``args_schema`` as a provider receives it: ``kind`` and ``default`` taken out (the
    runtime's keys, which some vendors' strict schemas refuse) and said in each description; a data
    argument takes no type (a reference, or a list of them), nor does a parameter (a reference,
    or a value with its reason, whose shape the schema keeps under ``value``), nor a path (text,
    or a reference to a file). An ``output`` argument loses its ``shape`` key and leaves ``required``:
    the runtime fills it in the call's own folder, and its description says so. ``references=False``: a call path that takes typed values only, so
    no description offers a reference (the lead's in-process ``data_catalog``)."""
    import copy
    import json

    from auto_research.provenance.inputs import OUTPUT_SHAPE_KEY, output_name

    out = copy.deepcopy(schema or {"type": "object", "properties": {}})
    properties = out.get("properties") or {}
    for name, prop in list(properties.items()):
        if not isinstance(prop, dict):
            continue
        prop = dict(prop)
        kind = prop.pop("kind", None)
        shape = prop.pop(OUTPUT_SHAPE_KEY, None) if kind == "output" else None
        has_default, default = "default" in prop, prop.pop("default", None)
        note = (_KIND_NOTES if references else _TYPED_NOTES).get(kind, "")
        if kind == "output":
            named = output_name(default) if has_default else ""
            fallback = json.dumps(named) if named else ("the argument's name" if shape == "file" else "the tool's name")
            note, has_default = _OUTPUT_NOTES.get(shape, "").format(default=fallback), False
            if isinstance(out.get("required"), (list, tuple)):
                out["required"] = [r for r in out["required"] if r != name]
        if kind == "parameter":
            note += f", or leave it out for the default, {json.dumps(default)}." if has_default else "."
        elif has_default:
            note += f" Default: {json.dumps(default)}."
        description = (str(prop.get("description") or "").rstrip() + note).strip()
        if kind == "data":  # a reference, or a list or dict of references: no type
            prop = {"description": description}
        elif kind == "parameter":
            value = {k: v for k, v in prop.items() if k != "description"}
            # No type: a reference, a value with its reason, or a list of references.
            prop = {"properties": {"value": value, "reason": {"type": "string"}}, "description": description}
        elif kind == "path" and references:  # text, or a reference object: no type
            prop = {k: v for k, v in prop.items() if k != "type"} | {"description": description}
        elif description:
            prop["description"] = description
        properties[name] = prop
    return out


def _discover_manifest_packages(root: Path) -> list[str]:
    """Find every ``<domain>_utils`` package under ``root`` that could expose TOOL_MANIFEST.

    Convention (CLAUDE.md §9.2): each toolkit is a top-level ``*_utils`` package directory
    with an ``__init__.py``. Discovered dynamically so a newly authored toolkit (e.g.
    ``heasarc_utils``) is picked up on :meth:`ToolkitRegistry.reload` with no code change.
    """
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and p.name.endswith("_utils") and (p / "__init__.py").exists()
    )


class ToolkitRegistry:
    """Discovers and describes tools available in research_toolkit and via MCP."""

    def __init__(self, toolkit_root: Path, *, disabled_tools: list[str] | None = None) -> None:
        self._root = Path(toolkit_root)
        self._tools: dict[str, ToolSpec] = {}
        self._mcp_tools: dict[str, str] = {}  # kind -> description
        self._mcp_schemas: dict[str, dict] = {}  # kind -> input JSON schema
        self._mcp_jailed: dict[str, bool] = {}  # kind -> its server runs inside the OS jail
        self._disabled: set[str] = set(disabled_tools or [])
        # Per-tool sandbox call-timeout ceiling (seconds), from each package's
        # ``tool_config.yaml``; absent tools fall back to the global default
        # (``orchestration.yaml: sandbox_call_timeout_s``, resolved lazily in ``load``).
        self._call_timeout_ceiling: dict[str, int] = {}
        #: Per-tool tested environment (``tool_config.yaml`` ``env:``), env plan B3b.
        self._tool_env: dict[str, str] = {}
        self._default_call_timeout_s: int = _CALL_TIMEOUT_FALLBACK_S
        # The manifest packages imported on the last load() — reused by reload().
        self._manifest_packages: list[str] = []
        #: package → why it exposed no tools on the last load (an import error, no TOOL_MANIFEST).
        self.import_errors: dict[str, str] = {}
        #: why a loaded tool's arguments cannot be validated (``provenance.inputs.manifest_problems``).
        self.manifest_errors: list[str] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Import TOOL_MANIFEST from every discovered ``*_utils`` package. Idempotent."""
        if self._loaded:
            return
        self._call_timeout_ceiling = {}
        self.import_errors = {}
        try:
            from auto_research.env import sandbox_call_timeout_default

            self._default_call_timeout_s = sandbox_call_timeout_default()
        except Exception:  # noqa: BLE001 — fall back to the module constant if config is unreadable
            self._default_call_timeout_s = _CALL_TIMEOUT_FALLBACK_S
        root_str = str(self._root)
        inserted = root_str not in sys.path
        if inserted:
            sys.path.insert(0, root_str)
        try:
            self._manifest_packages = _discover_manifest_packages(self._root)
            for pkg in self._manifest_packages:
                try:
                    mod = importlib.import_module(pkg)
                    # The manifest entry IS the agent-facing interface (§9.2 two halves):
                    # ``skills/TOOL_MANIFEST`` is the ai-half, ``lib/`` docstrings are the
                    # human-half. The description is taken verbatim from the manifest.
                    manifest: dict[str, ToolSpec] | None = getattr(mod, "TOOL_MANIFEST", None)
                    if not manifest:
                        self.import_errors[pkg] = "it exposes no TOOL_MANIFEST"
                        continue
                    self._tools.update(manifest)
                    self._load_tool_config(pkg, list(manifest.keys()))
                except Exception as exc:  # noqa: BLE001 — recorded, and refused at launch
                    self.import_errors[pkg] = f"{type(exc).__name__}: {exc}"
                    _log.warning("toolkit package %s exposes no tools: %s", pkg, self.import_errors[pkg])
        finally:
            if inserted:
                sys.path.remove(root_str)
        for name in self._disabled:
            self._tools.pop(name, None)
        from auto_research.provenance.inputs import manifest_problems

        self.manifest_errors = [problem for name, spec in self._tools.items()
                                for problem in manifest_problems(
                                    name, spec.get("args_schema") if isinstance(spec, dict) else None)]
        self._loaded = True

    def reload(self) -> None:
        """Re-import toolkit manifests to pick up newly committed/authored tools (§9.10 step 7).

        Used after the runtime authors a tool (``merge_flow`` commits to branch ``ai``): the
        manifest is pure data discovered in the orchestration env, so reloading it surfaces a
        new ``extend_existing`` entry or a brand-new ``*_utils`` package to analyst workers.
        Drops the toolkit tool index and re-runs :meth:`load`, force-reimporting any already
        cached manifest modules (Python would otherwise serve the stale ``skills/__init__``).
        The MCP index and the ``disabled`` filter are preserved.
        """
        import importlib

        importlib.invalidate_caches()
        # Force a fresh import of every cached manifest package + its skills submodule, so an
        # edited TOOL_MANIFEST is re-read rather than served from sys.modules.
        candidates = set(self._manifest_packages) | set(_discover_manifest_packages(self._root))
        root_str = str(self._root)
        inserted = root_str not in sys.path
        if inserted:
            sys.path.insert(0, root_str)
        failed: dict[str, str] = {}
        try:
            for pkg in candidates:
                for mod_name in (f"{pkg}.skills", pkg):
                    mod = sys.modules.get(mod_name)
                    if mod is not None:
                        try:
                            importlib.reload(mod)
                        except Exception as exc:  # noqa: BLE001 — recorded, never crashes discovery
                            failed.setdefault(pkg, f"reload: {type(exc).__name__}: {exc}")
                            for stale in [m for m in sys.modules if m.split(".")[0] == pkg]:
                                del sys.modules[stale]  # so load() imports afresh and records why
        finally:
            if inserted:
                sys.path.remove(root_str)
        self._tools = {}
        self._tool_env = {}
        self._loaded = False
        self.load()
        for pkg, why in failed.items():
            self.import_errors.setdefault(pkg, why)

    def load_mcp(self, client: Any) -> None:
        """Index tools from a started MCPToolClient into the registry."""
        for kind in client.list_kinds():
            self._mcp_tools[kind] = client.describe(kind)
            schema = client.schema(kind) if hasattr(client, "schema") else None
            self._mcp_schemas[kind] = schema or {"type": "object", "properties": {}}
            self._mcp_jailed[kind] = bool(getattr(client, "is_jailed", lambda _k: False)(kind))

    def is_mcp(self, kind: str) -> bool:
        return kind in self._mcp_tools

    def mcp_jailed(self, kind: str) -> bool:
        """Whether the MCP server serving ``kind`` was launched inside the OS jail."""
        return self._mcp_jailed.get(kind, False)

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

    def _load_tool_config(self, pkg: str, tool_names: list[str]) -> None:
        """Read ``<pkg>/tool_config.yaml`` (optional) for per-tool sandbox call-timeout ceilings.

        Pure data, modular (lives with the package — adding a toolkit never touches
        orchestration config). Shape::

            call_timeout_s:        # seconds, keyed by the tool's short name (the manifest
              acquire: 1800        #   key's last segment); a slow download declares a higher ceiling
            default_call_timeout_s: 300   # optional per-package fallback for tools not listed

        Stored as-is (positive ints) against the full tool name — no upper clamp, since this is
        trusted human/coder-set infra (never LLM input). Precedence: per-tool ``call_timeout_s``
        > per-package ``default_call_timeout_s`` > the global ``orchestration.yaml`` default.
        """
        cfg_path = self._root / pkg / "tool_config.yaml"
        if not cfg_path.is_file():
            return
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 — recorded: its tools would run outside their environment
            self.import_errors[pkg] = f"tool_config.yaml unreadable: {type(exc).__name__}: {exc}"
            return
        if not isinstance(cfg, dict):
            self.import_errors[pkg] = "tool_config.yaml is not a mapping"
            return
        env = cfg.get("env")
        if isinstance(env, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", env):
            # The package's tested environment (env plan B3b): its tools run there.
            for full in tool_names:
                self._tool_env[full] = env
        per_tool = cfg.get("call_timeout_s") or {}
        pkg_default = cfg.get("default_call_timeout_s")
        for full in tool_names:
            short = full.split(".")[-1]
            value = per_tool.get(short, pkg_default)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                self._call_timeout_ceiling[full] = int(value)

    def tool_env(self, tool_name: str) -> str | None:
        """The environment ``tool_name``'s package names in its ``tool_config.yaml`` (``env:``) —
        built as ``ar-<env>`` — or ``None``: the worker's own (analysis) environment."""
        self.load()
        return self._tool_env.get(tool_name)

    def call_timeout_ceiling(self, tool_name: str) -> int:
        """The sandbox per-call timeout (seconds) for ``tool_name``.

        Per-tool value from the package ``tool_config.yaml`` if declared, else the global default
        (``orchestration.yaml: sandbox_call_timeout_s``) — also what MCP/unlisted tools get. This
        is a fixed hard SIGTERM backstop, not a model knob: set a tool's value a bit above its own
        graceful timeout so the tool times out cleanly first (``SandboxToolProvider.execute`` uses
        it directly as the per-call timeout).
        """
        self.load()
        return self._call_timeout_ceiling.get(tool_name, self._default_call_timeout_s)

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
            schema = dict(spec.get("args_schema") or {"type": "object", "properties": {}})
            properties = dict(schema.get("properties") or {})
            env = self._tool_env.get(kind)
            description = spec["description"]
            if "env" not in properties:
                # The caller's environment choice (B3b), declared on every toolkit tool.
                properties["env"] = {"type": "string", "description": (
                    "An environment name from list_env to run this call in instead of the tool's tested one; "
                    "its result is marked as from an untested environment.")}
                schema["properties"] = properties
                if env:
                    description += f"\n\nRuns in its tested environment ar-{env}."
            description += REF_NOTE
            defs.append({"name": self._function_name(kind), "description": description,
                         "input_schema": provider_schema(schema)})
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


def check_toolkit_manifests(root: Path | None = None) -> None:
    """Launch-time preflight (no LLM): every ``*_utils`` package of ``toolkit_root`` imports in the
    orchestration environment and exposes a ``TOOL_MANIFEST``. A package that does not would
    reach the analysts as missing tools and nothing else (found 2026-10-27: the loader skipped
    such a package silently). Registered in ``auto_research.preflight``.
    """
    disabled: list[str] = []
    if root is None:
        from auto_research.graph.runtime import load_orchestration_config
        from auto_research.sandbox.merge_flow import _resolve_toolkit_root

        root = _resolve_toolkit_root()
        disabled = list(load_orchestration_config().get("disabled_tools") or [])
    registry = ToolkitRegistry(root, disabled_tools=disabled)  # the tools the run will load
    registry.load()
    if registry.import_errors:
        raise RuntimeError(
            "toolkit packages expose no tools: "
            + "; ".join(f"{pkg} ({why})" for pkg, why in sorted(registry.import_errors.items()))
            + f". Fix them in {root} before launching."
        )
    if registry.manifest_errors:
        raise RuntimeError(
            "toolkit arguments the input validator cannot check: " + "; ".join(registry.manifest_errors)
            + f". Declare each argument's kind (and each optional parameter's default) in {root}."
        )
