"""Deterministic tests for sandbox/runner.py, sandbox/registry.py, sandbox/merge_flow.py,
and AnalystWorkerAgent.execute_tools() dispatch/retry routing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_research.sandbox.merge_flow import propose_and_pr
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.runner import run_tool
from auto_research.schemas import AnalysisTask, ToolCallRequest
from auto_research.schemas.tools import ToolResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_MANIFEST = {
    "echo_tool": {
        "module": "echo_mod",
        "fn": "echo",
        "description": "Echoes its argument back as stdout.",
        "args_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
}


def _make_registry(tmp_path: Path, manifest: dict | None = None) -> ToolkitRegistry:
    """Return a ToolkitRegistry pre-loaded with FIXTURE_MANIFEST (no disk needed)."""
    reg = ToolkitRegistry(tmp_path)
    reg._tools = manifest if manifest is not None else dict(FIXTURE_MANIFEST)
    reg._loaded = True
    return reg


# ---------------------------------------------------------------------------
# ToolResult schema
# ---------------------------------------------------------------------------

class TestToolResultSchema:
    def test_round_trip(self) -> None:
        r = ToolResult(
            tool_name="xspec_utils.fit",
            stdout="chi2=1.03",
            stderr="",
            exit_code=0,
            elapsed_s=12.5,
        )
        data = r.model_dump(mode="json")
        r2 = ToolResult.model_validate(data)
        assert r2.tool_name == r.tool_name
        assert r2.exit_code == r.exit_code
        assert r2.elapsed_s == r.elapsed_s

    def test_figures_default_empty(self) -> None:
        r = ToolResult(tool_name="x", stdout="", stderr="", exit_code=0, elapsed_s=0.0)
        assert r.figures == []

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            ToolResult.model_validate(
                {"tool_name": "x", "stdout": "", "stderr": "", "exit_code": 0,
                 "elapsed_s": 0.0, "unexpected": "field"}
            )


# ---------------------------------------------------------------------------
# ToolkitRegistry
# ---------------------------------------------------------------------------

class TestToolkitRegistry:
    def test_is_known(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        assert reg.is_known("echo_tool")
        assert not reg.is_known("nonexistent_tool")

    def test_list_tools(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        assert "echo_tool" in reg.list_tools()

    def test_describe_tool(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        desc = reg.describe_tool("echo_tool")
        assert "echo_tool" in desc
        assert "Echoes" in desc

    def test_spec(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        spec = reg.spec("echo_tool")
        assert spec["module"] == "echo_mod"
        assert spec["fn"] == "echo"

    def test_spec_unknown_raises(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        with pytest.raises(KeyError):
            reg.spec("nonexistent_tool")

    def test_tool_catalogue(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        cat = reg.tool_catalogue()
        assert "echo_tool" in cat

    def test_load_idempotent(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        reg.load()  # already loaded; should not reset
        assert reg.is_known("echo_tool")

    def test_empty_registry_catalogue(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path, manifest={})
        assert reg.tool_catalogue() == "(no tools registered)"


# ---------------------------------------------------------------------------
# Runner — subprocess sandbox
# ---------------------------------------------------------------------------

class TestRunner:
    def test_success(self, tmp_path: Path) -> None:
        """A simple Python tool returns exit_code 0."""
        result = asyncio.run(run_tool(
            "json",
            "dumps",
            {"obj": {"key": "value"}},
            timeout_s=10,
            workdir=tmp_path,
            toolkit_root=tmp_path,
        ))
        assert result.exit_code == 0
        assert result.tool_name == "json.dumps"
        assert result.elapsed_s >= 0

    def test_timeout(self, tmp_path: Path) -> None:
        """A tool that exceeds timeout_s is killed and returns exit_code 124."""
        # Write a tiny module that actually sleeps so the timeout fires.
        sleeper = tmp_path / "sleeper.py"
        sleeper.write_text("import time\ndef long_sleep(secs=60): time.sleep(secs)\n")
        result = asyncio.run(run_tool(
            "sleeper",
            "long_sleep",
            {"secs": 60},
            timeout_s=1,
            workdir=tmp_path,
            toolkit_root=tmp_path,
        ))
        assert result.exit_code == 124
        assert "Timed out" in result.stderr

    def test_bad_module(self, tmp_path: Path) -> None:
        """An importable error returns non-zero exit."""
        result = asyncio.run(run_tool(
            "no_such_module_xyz_abc",
            "do_something",
            {},
            timeout_s=10,
            workdir=tmp_path,
            toolkit_root=tmp_path,
        ))
        assert result.exit_code != 0

    def test_workdir_used(self, tmp_path: Path) -> None:
        """The subprocess runs in the given workdir."""
        result = asyncio.run(run_tool(
            "os",
            "getcwd",
            {},
            timeout_s=10,
            workdir=tmp_path,
            toolkit_root=tmp_path,
        ))
        assert result.exit_code == 0
        assert str(tmp_path) in result.stdout

    def test_toolkit_root_in_pythonpath(self, tmp_path: Path) -> None:
        """A module in toolkit_root is importable by the subprocess."""
        # Write a tiny module into tmp_path (acts as toolkit root).
        mod = tmp_path / "mytool.py"
        mod.write_text("def greet(name='world'): return f'hello {name}'\n")
        result = asyncio.run(run_tool(
            "mytool",
            "greet",
            {"name": "test"},
            timeout_s=10,
            workdir=tmp_path,
            toolkit_root=tmp_path,
        ))
        assert result.exit_code == 0
        assert "hello test" in result.stdout


# ---------------------------------------------------------------------------
# merge_flow — Phase 2 stub
# ---------------------------------------------------------------------------

class TestMergeFlowStub:
    def test_raises_not_implemented(self) -> None:
        req = ToolCallRequest(
            requested_by="worker",
            kind="heasarc_retrieve",
            purpose="retrieve NICER obs",
        )
        with pytest.raises(NotImplementedError, match="Phase 2"):
            asyncio.run(propose_and_pr(req, lead=object()))


# ---------------------------------------------------------------------------
# execute_tools() dispatch + retry routing (no LLM; fake registry/sandbox)
# ---------------------------------------------------------------------------

class _FakeSandbox:
    """Records dispatches; result dicts come from injectable callables."""

    def __init__(self, *, mcp_fn=None, tool_fn=None) -> None:
        self.is_alive = True
        self.mcp_calls: list[tuple[str, dict]] = []
        self.tool_calls: list[tuple[str, str, dict]] = []
        self._mcp_fn = mcp_fn or (lambda kind, args: {"exit_code": 0, "stdout": "", "stderr": ""})
        self._tool_fn = tool_fn or (lambda module, fn, args: {"exit_code": 0, "stdout": "", "stderr": ""})

    async def call_mcp_tool(self, kind, args, *, timeout_s=None):
        self.mcp_calls.append((kind, dict(args)))
        return self._mcp_fn(kind, args)

    async def call_tool(self, module, fn, args, *, timeout_s=None):
        self.tool_calls.append((module, fn, dict(args)))
        return self._tool_fn(module, fn, args)


class _FakeRegistry:
    def __init__(self, known: set[str], mcp: set[str], schema: dict | None = None) -> None:
        self._known = set(known)
        self._mcp = set(mcp)
        self._schema = schema or {}

    def is_known(self, k): return k in self._known
    def is_mcp(self, k): return k in self._mcp
    def spec(self, k): return {"module": "mod", "fn": "fn", "args_schema": self._schema}
    def tool_catalogue(self): return "(catalogue)"


def _worker(sandbox):
    from auto_research.agents.roles import AnalystWorkerAgent
    w = AnalystWorkerAgent(model=object(), available_toolkits=[])
    w.sandbox = sandbox
    w.memory_path = None
    return w


def _script_requests(worker, batches: list[list[ToolCallRequest]]) -> None:
    """Make worker.request_tools() yield successive batches, then []."""
    state = {"i": 0}

    async def rt(task):
        i = state["i"]
        state["i"] += 1
        return list(batches[i]) if i < len(batches) else []

    worker.request_tools = rt


class TestExecuteToolsDispatch:
    """Routing/retry behavior of AnalystWorkerAgent.execute_tools()."""

    def test_command_field_forwarded(self, tmp_path: Path) -> None:
        """The ToolCallRequest.command field is merged into args for shell tools."""
        def mcp_fn(kind, args):
            ok = "command" in args
            return {"exit_code": 0 if ok else 1,
                    "stdout": args.get("command", ""),
                    "stderr": "" if ok else "command Field required"}

        sb = _FakeSandbox(mcp_fn=mcp_fn)
        w = _worker(sb)
        reqs = [ToolCallRequest(requested_by="w", kind="mcp.builtin.bash_exec",
                                purpose="p", command="echo hi", arguments={})]
        _script_requests(w, [reqs])
        reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})

        results = asyncio.run(w.execute_tools(AnalysisTask(question="q"), reg, tool_retry_cap=3))
        assert sb.mcp_calls[0][1] == {"command": "echo hi"}
        assert results[-1].exit_code == 0

    def test_retry_reroutes_on_kind_change(self, tmp_path: Path) -> None:
        """A retry returning a different kind is routed (and labelled) by that kind."""
        sb = _FakeSandbox(
            mcp_fn=lambda k, a: {"exit_code": 1, "stdout": "", "stderr": "fail"},
            tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "loaded", "stderr": ""},
        )
        w = _worker(sb)
        first = [ToolCallRequest(requested_by="w", kind="mcp.builtin.bash_exec",
                                 purpose="p", arguments={})]
        revised = [ToolCallRequest(requested_by="w", kind="tk.load_data",
                                   purpose="p", arguments={"x": "1"})]
        _script_requests(w, [first, revised])
        reg = _FakeRegistry(known={"mcp.builtin.bash_exec", "tk.load_data"},
                            mcp={"mcp.builtin.bash_exec"})

        results = asyncio.run(w.execute_tools(AnalysisTask(question="q"), reg, tool_retry_cap=3))
        # The retried toolkit kind went to call_tool, never to the MCP transport.
        assert len(sb.tool_calls) == 1
        assert all(kind != "tk.load_data" for kind, _ in sb.mcp_calls)
        assert any(r.tool_name == "tk.load_data" and r.exit_code == 0 for r in results)

    def test_unknown_tool_returns_127(self, tmp_path: Path) -> None:
        """An unknown kind yields exit_code 127 and is never dispatched to the sandbox."""
        sb = _FakeSandbox()
        w = _worker(sb)
        reqs = [ToolCallRequest(requested_by="w", kind="nope_tool", purpose="p")]
        _script_requests(w, [reqs])
        reg = _FakeRegistry(known=set(), mcp=set())

        results = asyncio.run(w.execute_tools(AnalysisTask(question="q"), reg, tool_retry_cap=1))
        assert len(results) == 1
        assert results[0].exit_code == 127
        assert "nope_tool" in results[0].tool_name
        assert sb.mcp_calls == [] and sb.tool_calls == []

    def test_retry_cap_stops_loop(self, tmp_path: Path) -> None:
        """A persistently failing tool is attempted exactly tool_retry_cap times."""
        sb = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 1, "stdout": "", "stderr": "fail"})
        w = _worker(sb)
        failing = [ToolCallRequest(requested_by="w", kind="mcp.builtin.bash_exec",
                                   purpose="p", command="x")]

        # Worker re-requests the same failing tool on every retry.
        async def rt(task):
            return list(failing)

        w.request_tools = rt
        reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})

        cap = 2
        results = asyncio.run(w.execute_tools(AnalysisTask(question="q"), reg, tool_retry_cap=cap))
        assert len(sb.mcp_calls) == cap
        assert all(r.exit_code == 1 for r in results)

    def test_instrument_paths_autofilled_from_config(self, tmp_path: Path, monkeypatch) -> None:
        """ixpe_path declared by the tool but omitted by the model is filled from config."""
        import auto_research.agents.roles as roles
        monkeypatch.setattr(roles, "_observation_path_args",
                            lambda: {"ixpe_path": "/cfg/IXPE", "nustar_path": "/cfg/NuSTAR"})

        sb = _FakeSandbox()
        w = _worker(sb)
        reqs = [ToolCallRequest(requested_by="w", kind="tk.load_data",
                                purpose="p", arguments={"epoch": "{}"})]  # no ixpe_path
        _script_requests(w, [reqs])
        # tool declares ixpe_path (and not nustar_path) in its schema
        schema = {"properties": {"epoch": {}, "ixpe_path": {}}}
        reg = _FakeRegistry(known={"tk.load_data"}, mcp=set(), schema=schema)

        asyncio.run(w.execute_tools(AnalysisTask(question="q"), reg, tool_retry_cap=1))
        _, _, args = sb.tool_calls[0]
        assert args["ixpe_path"] == "/cfg/IXPE"   # injected from config
        assert "nustar_path" not in args           # not declared by tool → not injected

    def test_model_supplied_path_not_overridden(self, tmp_path: Path, monkeypatch) -> None:
        """A path the model explicitly supplies is respected (config only fills gaps)."""
        import auto_research.agents.roles as roles
        monkeypatch.setattr(roles, "_observation_path_args",
                            lambda: {"ixpe_path": "/cfg/IXPE"})

        sb = _FakeSandbox()
        w = _worker(sb)
        reqs = [ToolCallRequest(requested_by="w", kind="tk.load_data",
                                purpose="p", arguments={"ixpe_path": "/custom/IXPE"})]
        _script_requests(w, [reqs])
        reg = _FakeRegistry(known={"tk.load_data"}, mcp=set(),
                            schema={"properties": {"ixpe_path": {}}})

        asyncio.run(w.execute_tools(AnalysisTask(question="q"), reg, tool_retry_cap=1))
        _, _, args = sb.tool_calls[0]
        assert args["ixpe_path"] == "/custom/IXPE"
