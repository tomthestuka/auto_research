"""Deterministic tests for sandbox/runner.py, sandbox/registry.py, sandbox/merge_flow.py,
and AnalystWorkerAgent.execute_tools() dispatch/retry routing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_research.sandbox.persistent import PersistentSandbox
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
# merge_flow — Phase 2 step 2 harness
# ---------------------------------------------------------------------------
# `propose_and_pr` is now the real author harness (no longer a NotImplementedError
# stub). Its bounded rework loop is covered deterministically in test_merge_flow.py.


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


class _RestartableFakeSandbox(_FakeSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.is_alive = False
        self.starts = 0

    async def start(self):
        self.starts += 1
        self.is_alive = True


class _FakeRegistry:
    def __init__(self, known: set[str], mcp: set[str], schema: dict | None = None) -> None:
        self._known = set(known)
        self._mcp = set(mcp)
        self._schema = schema or {}

    def is_known(self, k): return k in self._known
    def is_mcp(self, k): return k in self._mcp
    def spec(self, k): return {"module": "mod", "fn": "fn", "args_schema": self._schema}
    def tool_catalogue(self): return "(catalogue)"
    def kind_for_function(self, name): return name
    def tool_definitions(self):
        return [{"name": k, "description": "d", "input_schema": {"type": "object"}} for k in self._known]


class TestSandboxToolProvider:
    """SandboxToolProvider.execute() routing + argument resolution (no LLM)."""

    def test_unknown_tool_returns_127(self) -> None:
        from auto_research.agents.react import SandboxToolProvider
        sb = _FakeSandbox()
        prov = SandboxToolProvider(_FakeRegistry(known=set(), mcp=set()), sb)
        out = asyncio.run(prov.execute("nope_tool", {}))
        assert out.is_error and out.result.exit_code == 127
        assert sb.mcp_calls == [] and sb.tool_calls == []

    def test_bash_exec_cwd_injected(self, tmp_path: Path) -> None:
        from auto_research.agents.react import SandboxToolProvider
        sb = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 0, "stdout": a.get("command", ""), "stderr": ""})
        reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})
        prov = SandboxToolProvider(reg, sb, cwd=tmp_path)
        out = asyncio.run(prov.execute("mcp.builtin.bash_exec", {"command": "echo hi"}))
        assert sb.mcp_calls[0][1] == {"command": "echo hi", "cwd": str(tmp_path)}
        assert not out.is_error and "echo hi" in out.text

    def test_error_outcome_surfaces_stderr(self) -> None:
        from auto_research.agents.react import SandboxToolProvider
        sb = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 1, "stdout": "", "stderr": "boom"})
        reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})
        prov = SandboxToolProvider(reg, sb)
        out = asyncio.run(prov.execute("mcp.builtin.bash_exec", {"command": "x"}))
        assert out.is_error and "boom" in out.text
        assert prov.results[-1].exit_code == 1

    def test_instrument_paths_autofilled_from_config(self, monkeypatch) -> None:
        import auto_research.agents.react as react
        from auto_research.agents.react import SandboxToolProvider
        monkeypatch.setattr(react, "_observation_path_args",
                            lambda: {"ixpe_path": "/cfg/IXPE", "nustar_path": "/cfg/NuSTAR"})
        sb = _FakeSandbox()
        reg = _FakeRegistry(known={"tk.load_data"}, mcp=set(),
                            schema={"properties": {"epoch": {}, "ixpe_path": {}}})
        prov = SandboxToolProvider(reg, sb)
        asyncio.run(prov.execute("tk.load_data", {"epoch": "{}"}))
        _, _, args = sb.tool_calls[0]
        assert args["ixpe_path"] == "/cfg/IXPE"   # injected from config
        assert "nustar_path" not in args           # not declared by tool → not injected

    def test_model_supplied_path_not_overridden(self, monkeypatch) -> None:
        import auto_research.agents.react as react
        from auto_research.agents.react import SandboxToolProvider
        monkeypatch.setattr(react, "_observation_path_args", lambda: {"ixpe_path": "/cfg/IXPE"})
        sb = _FakeSandbox()
        reg = _FakeRegistry(known={"tk.load_data"}, mcp=set(),
                            schema={"properties": {"ixpe_path": {}}})
        prov = SandboxToolProvider(reg, sb)
        asyncio.run(prov.execute("tk.load_data", {"ixpe_path": "/custom/IXPE"}))
        _, _, args = sb.tool_calls[0]
        assert args["ixpe_path"] == "/custom/IXPE"


def _script_model():
    """A model that emits one tool call, then stops, then returns a report."""
    from types import SimpleNamespace

    from auto_research.models.types import ModelResponse, ToolCall

    class _ScriptModel:
        def __init__(self):
            self.tool_iterations = 0
            self.config = SimpleNamespace(model="fake-tool-model")

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            if schema is not None:  # terminal structured report
                return ModelResponse(parsed={
                    "summary": "did the fit", "findings": ["chi2/dof=1.0"],
                    "figures": [], "uncertainty": None,
                })
            self.tool_iterations += 1
            if self.tool_iterations == 1:
                return ModelResponse(content="loading",
                                     tool_calls=[ToolCall(id="c1", name="tk_load", arguments={})])
            return ModelResponse(tool_calls=[])  # no more calls → loop ends

    return _ScriptModel()


class TestWorkerReactLoop:
    """AnalystWorkerAgent.analyze() runs the inner ReAct loop, then reports."""

    def test_loop_runs_tools_then_grounds_report(self) -> None:
        from datetime import UTC, datetime

        from auto_research.agents.roles import AnalystWorkerAgent
        from auto_research.monitor import RunMonitor
        fig = {
            "path": "/tmp/fit.png", "caption": "fit", "mime_type": "image/png",
            "produced_by": "tk_load", "created_at": datetime.now(UTC).isoformat(), "sha256": "a" * 64,
        }
        # Tool returns a figure → exercises figure propagation onto the report.
        sb = _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "loaded indices", "figures": [fig]})
        reg = _FakeRegistry(known={"tk_load"}, mcp=set())
        monitor = RunMonitor(chatter=7)
        w = AnalystWorkerAgent(
            model=_script_model(),
            available_toolkits=[],
            sandbox=sb,
            registry=reg,
            monitor=monitor,
        )
        report = asyncio.run(w.analyze(AnalysisTask(question="q")))
        assert sb.tool_calls  # a tool actually ran inside the loop
        assert report.summary == "did the fit"
        assert [str(f.path) for f in report.figures] == ["/tmp/fit.png"]  # real figure attached
        assert w.last_tool_results and w.last_tool_results[0].exit_code == 0
        events = {event.event: event for event in monitor.events}
        # Input/output are now separate events: `tool_call` (logged before the call runs,
        # carrying the args) and `tool_result` (logged on return, carrying the text). They
        # share a `tool_call_id`, so a hung call is a `tool_call` with no matching result.
        assert "analyst_worker.analyze.tool_call" in events
        assert "analyst_worker.analyze.tool_result" in events
        tc_data = events["analyst_worker.analyze.tool_call"].data
        tr_data = events["analyst_worker.analyze.tool_result"].data
        assert tr_data["model"] == "fake-tool-model"
        assert tr_data["figure_count"] == 1
        # The call ARGS live on the input event; the returned text on the output event
        # (full-fidelity diagnosis from the JSONL alone).
        assert "args" in tc_data and isinstance(tc_data["args"], str)
        assert tc_data["tool_call_id"] == tr_data["tool_call_id"]
        assert "loaded indices" in tr_data["text"]

    def test_starts_dead_sandbox(self) -> None:
        from auto_research.agents.roles import AnalystWorkerAgent
        sb = _RestartableFakeSandbox()
        reg = _FakeRegistry(known={"tk_load"}, mcp=set())
        w = AnalystWorkerAgent(model=_script_model(), available_toolkits=[], sandbox=sb, registry=reg)
        asyncio.run(w.analyze(AnalysisTask(question="q")))
        assert sb.starts == 1

    def test_no_sandbox_falls_back_to_plain_call(self) -> None:
        from auto_research.agents.roles import AnalystWorkerAgent
        from auto_research.models.types import ModelResponse

        class _PlainModel:
            async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                           images=(), documents=(), cache_prefix=False):
                assert tools is None  # no tool loop without a sandbox+registry
                return ModelResponse(parsed={
                    "summary": "no tools", "findings": [], "figures": [], "uncertainty": None,
                })

        w = AnalystWorkerAgent(model=_PlainModel(), available_toolkits=[])  # no sandbox/registry
        report = asyncio.run(w.analyze(AnalysisTask(question="q")))
        assert report.summary == "no tools"


# ---------------------------------------------------------------------------
# _normalize_tool_raw and _append_tool_results_to_memory
# ---------------------------------------------------------------------------

class TestNormalizeToolRaw:
    """Unit tests for _normalize_tool_raw."""

    def test_bash_exec_passthrough(self) -> None:
        """A result that already has stdout is returned unchanged."""
        from auto_research.agents.react import _normalize_tool_raw
        raw = {"stdout": "hello", "stderr": "", "exit_code": 0}
        result = _normalize_tool_raw(raw)
        assert result["stdout"] == "hello"

    def test_list_dir_entries_serialized(self) -> None:
        """list_dir's {path, entries} payload is serialized into stdout."""
        from auto_research.agents.react import _normalize_tool_raw
        raw = {"path": "/foo", "entries": ["a", "b", "c"], "exit_code": 0}
        result = _normalize_tool_raw(raw)
        assert result["stdout"]
        assert "entries" in result["stdout"]
        assert "a" in result["stdout"]

    def test_load_data_indices_dict_serialized(self) -> None:
        """load_data's nested indices dict is serialized into stdout as JSON."""
        from auto_research.agents.react import _normalize_tool_raw
        indices = {"IXPE": {"02008401": {"DU1": [1, 2, 3]}}, "NICER": {}}
        raw = {**indices, "exit_code": 0}
        result = _normalize_tool_raw(raw)
        assert "02008401" in result["stdout"]

    def test_none_return_coerced(self) -> None:
        """A tool returning None (fit_tbabs_hss) produces no extra stdout."""
        from auto_research.agents.react import _normalize_tool_raw
        raw = {"stdout": "None", "exit_code": 0}
        result = _normalize_tool_raw(raw)
        assert result["stdout"] == "None"

    def test_console_tail_folded_into_stdout(self) -> None:
        """console_tail is appended to stdout so model sees XSPEC console prints."""
        from auto_research.agents.react import _normalize_tool_raw
        raw = {"stdout": "fit done", "console_tail": "XSPEC: stat=1.23", "exit_code": 0}
        result = _normalize_tool_raw(raw)
        assert "fit done" in result["stdout"]
        assert "stat=1.23" in result["stdout"]

    def test_console_tail_only(self) -> None:
        """console_tail alone (no stdout) is surfaced without a leading separator."""
        from auto_research.agents.react import _normalize_tool_raw
        raw = {"console_tail": "loading spectra", "exit_code": 0}
        result = _normalize_tool_raw(raw)
        assert "loading spectra" in result["stdout"]

    def test_protocol_keys_excluded_from_serialization(self) -> None:
        """exit_code/stderr/figures/console_tail are not re-serialized into stdout."""
        from auto_research.agents.react import _normalize_tool_raw
        raw = {"exit_code": 0, "stderr": "warn", "figures": [], "console_tail": "log"}
        result = _normalize_tool_raw(raw)
        # Only console_tail → stdout; stderr/figures/exit_code must not appear as JSON keys
        assert "stderr" not in result["stdout"]
        assert "exit_code" not in result["stdout"]


class TestAppendToolResultsToMemory:
    """Unit tests for stdout/stderr truncation in _append_tool_results_to_memory."""

    def _make_result(self, stdout: str = "", stderr: str = "") -> ToolResult:
        return ToolResult(
            tool_name="test.tool", stdout=stdout, stderr=stderr,
            exit_code=0, elapsed_s=0.1,
        )

    def test_short_output_written_verbatim(self, tmp_path: Path) -> None:
        from auto_research.agents.roles import _append_tool_results_to_memory
        mem = tmp_path / "memory.md"
        r = self._make_result(stdout="short output")
        _append_tool_results_to_memory(mem, [r])
        assert "short output" in mem.read_text()

    def test_long_stdout_truncated(self, tmp_path: Path) -> None:
        from auto_research.agents.roles import _append_tool_results_to_memory, _STDOUT_MEM_CAP
        mem = tmp_path / "memory.md"
        big = "x" * (_STDOUT_MEM_CAP + 5000)
        r = self._make_result(stdout=big)
        _append_tool_results_to_memory(mem, [r])
        content = mem.read_text()
        # Truncation marker present and file is bounded
        assert "truncated" in content
        assert len(content) < _STDOUT_MEM_CAP + 500  # overhead for headers

    def test_long_stderr_truncated(self, tmp_path: Path) -> None:
        from auto_research.agents.roles import _append_tool_results_to_memory, _STDERR_MEM_CAP
        mem = tmp_path / "memory.md"
        big = "e" * (_STDERR_MEM_CAP + 3000)
        r = self._make_result(stderr=big)
        _append_tool_results_to_memory(mem, [r])
        content = mem.read_text()
        assert "truncated" in content
        assert len(content) < _STDERR_MEM_CAP + 500


# ---------------------------------------------------------------------------
# bash_exec interpreter consistency (regression guard for the analyst env)
# ---------------------------------------------------------------------------


class TestBashExecInterpreterConsistency:
    """A bare ``python`` inside bash_exec must resolve to the configured ``analyze_env``
    interpreter — the same one the toolkit tools invoke. Without the PATH pin in
    ``_builtin_server_config``, bash_exec uses whatever conda env leaked onto the
    launching shell's PATH (so ``import astropy`` / ``import xspec_utils`` breaks
    nondeterministically). This test fails if that wiring regresses."""

    def test_bash_exec_python_matches_analyze_env(self) -> None:
        import shutil

        from auto_research.graph.runtime import (
            _builtin_server_config,
            load_orchestration_config,
        )

        orch = load_orchestration_config()
        analyze_env = orch.get("analyze_env")
        analyze_ini_sh = orch.get("analyze_ini_sh")
        toolkit_root = orch.get("toolkit_root")
        if not (analyze_env and analyze_ini_sh and toolkit_root):
            pytest.skip("analyze_env / analyze_ini_sh / toolkit_root not configured")
        try:
            cfg = _builtin_server_config(
                Path(analyze_env), analyze_ini_sh, Path(toolkit_root)
            )
        except (RuntimeError, FileNotFoundError) as exc:
            pytest.skip(f"cannot source analyst env (HEASoft unavailable?): {exc}")

        resolved = shutil.which("python", path=cfg["env"]["PATH"])
        assert resolved is not None, "no `python` resolvable on the bash_exec PATH"
        expected = Path(analyze_env) / "bin" / "python"
        assert Path(resolved).resolve() == expected.resolve(), (
            f"bash_exec `python` resolves to {resolved}, not the configured analyze_env "
            f"interpreter {expected}"
        )
