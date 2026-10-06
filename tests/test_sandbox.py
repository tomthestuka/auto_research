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

    def test_malformed_figure_dropped_not_fatal(self) -> None:
        """A bad figure dict is dropped with a note, never raised — the whole run must
        survive a freshly-authored plotter emitting a slightly-off FigureRef (F4)."""
        from datetime import UTC, datetime

        from auto_research.agents.react import SandboxToolProvider
        good = {
            "path": "/tmp/ok.png", "caption": "ok", "mime_type": "image/png",
            "produced_by": "tk.plot", "created_at": datetime.now(UTC).isoformat(), "sha256": "a" * 64,
        }
        bad_sha = {**good, "path": "/tmp/b.png", "sha256": "NOT-hex"}   # bad sha256
        extra_key = {**good, "path": "/tmp/c.png", "dpi": 150}          # extra key (extra="forbid")
        sb = _FakeSandbox(tool_fn=lambda m, f, a: {
            "exit_code": 0, "stdout": "plotted", "figures": [good, bad_sha, extra_key]})
        prov = SandboxToolProvider(_FakeRegistry(known={"tk.plot"}, mcp=set()), sb)
        out = asyncio.run(prov.execute("tk.plot", {}))  # must NOT raise
        assert not out.is_error
        assert [str(f.path) for f in out.figures] == ["/tmp/ok.png"]  # only the valid one survives
        assert "2 dropped" in out.text and "plotted" in out.text      # drop surfaced, output kept

    def test_parse_tool_figures_helper(self) -> None:
        """parse_tool_figures keeps valid figures, drops bad ones with a reason, and
        never chokes on a non-list `figures` value."""
        from datetime import UTC, datetime

        from auto_research.sandbox.runner import parse_tool_figures
        good = {
            "path": "/tmp/ok.png", "caption": "ok", "mime_type": "image/png",
            "produced_by": "t", "created_at": datetime.now(UTC).isoformat(), "sha256": "a" * 64,
        }
        figs, notes = parse_tool_figures([good, {"caption": "missing everything else"}])
        assert len(figs) == 1 and len(notes) == 1
        figs2, notes2 = parse_tool_figures({"path": "x"})   # not a list → ignored, noted
        assert figs2 == [] and len(notes2) == 1
        assert parse_tool_figures(None) == ([], [])          # absent → clean empty


def _script_model():
    """A model that runs a tool, writes its report with `edit_report`, then ends on `turn_end`.

    This is the production shape since F5 (2026-09-09). It used to stop calling tools and let the
    harness compose an `AnalystReport` on its behalf — the path that killed slot 5, because it was
    the only one that sent the model the runtime-owned `figures` tree. There is no compose now, so
    a worker that never writes a report has not produced one.
    """
    from types import SimpleNamespace

    from auto_research.models.types import ModelResponse, ToolCall

    class _ScriptModel:
        def __init__(self):
            self.tool_iterations = 0
            self.config = SimpleNamespace(model="fake-tool-model")

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.tool_iterations += 1
            if self.tool_iterations == 1:
                return ModelResponse(content="loading", stop="tool_use",
                                     tool_calls=[ToolCall(id="c1", name="tk_load", arguments={})])
            if self.tool_iterations == 2:
                return ModelResponse(content="writing it up", stop="tool_use", tool_calls=[
                    ToolCall(id="c2", name="edit_report", arguments={
                        "summary": "did the fit",
                        "findings": ["chi2/dof=1.0"],
                        "status": "succeeded",
                        "cited_figures": [{"source": "self", "index": 1}],
                    }),
                ])
            return ModelResponse(content="done", stop="tool_use", tool_calls=[
                ToolCall(id="c3", name="turn_end", arguments={"summary": "fit reported"}),
            ])

    return _ScriptModel()


class TestWorkerReactLoop:
    """AnalystWorkerAgent.analyze() runs the inner ReAct loop, then reports."""

    def test_loop_runs_tools_then_grounds_report(self, tmp_path) -> None:
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
        # A report workspace is what `edit_report` writes into, and production always wires one
        # (`graph/runtime.py` sets `worker.report_dir` for every worker).
        w.report_dir = tmp_path / "worker0"
        report = asyncio.run(w.analyze(AnalysisTask(question="q")))
        assert sb.tool_calls  # a tool actually ran inside the loop
        assert report.summary == "did the fit"
        assert [str(f.path) for f in report.figures] == ["/tmp/fit.png"]  # real figure attached
        assert w.last_tool_results and w.last_tool_results[0].exit_code == 0
        # Input/output are separate events: `tool_call` (logged before the call runs, carrying
        # the args) and `tool_result` (logged on return, carrying the text). They share a
        # `tool_call_id`, so a hung call is a `tool_call` with no matching result.
        #
        # Selected by TOOL, not by "the last one of that name": the loop now ends on `turn_end`,
        # so several tools report results in one round and the last `tool_result` is the
        # terminal's. Keying a dict on the event name silently read whichever came last.
        calls = [e for e in monitor.events if e.event == "analyst_worker.analyze.tool_call"]
        results = [e for e in monitor.events if e.event == "analyst_worker.analyze.tool_result"]
        assert calls and results
        tc_data = next(e.data for e in calls if e.data.get("tool") == "tk_load")
        tr_data = next(
            e.data for e in results if e.data["tool_call_id"] == tc_data["tool_call_id"]
        )
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
# normalize_tool_raw and _append_tool_results_to_memory
# ---------------------------------------------------------------------------

class TestNormalizeToolRaw:
    """Unit tests for normalize_tool_raw."""

    def test_bash_exec_passthrough(self) -> None:
        """A result that already has stdout is returned unchanged."""
        from auto_research.sandbox.runner import normalize_tool_raw
        raw = {"stdout": "hello", "stderr": "", "exit_code": 0}
        result = normalize_tool_raw(raw)
        assert result["stdout"] == "hello"

    def test_list_dir_entries_serialized(self) -> None:
        """list_dir's {path, entries} payload is serialized into stdout."""
        from auto_research.sandbox.runner import normalize_tool_raw
        raw = {"path": "/foo", "entries": ["a", "b", "c"], "exit_code": 0}
        result = normalize_tool_raw(raw)
        assert result["stdout"]
        assert "entries" in result["stdout"]
        assert "a" in result["stdout"]

    def test_load_data_indices_dict_serialized(self) -> None:
        """load_data's nested indices dict is serialized into stdout as JSON."""
        from auto_research.sandbox.runner import normalize_tool_raw
        indices = {"IXPE": {"02008401": {"DU1": [1, 2, 3]}}, "NICER": {}}
        raw = {**indices, "exit_code": 0}
        result = normalize_tool_raw(raw)
        assert "02008401" in result["stdout"]

    def test_none_return_coerced(self) -> None:
        """A tool returning None (fit_tbabs_hss) produces no extra stdout."""
        from auto_research.sandbox.runner import normalize_tool_raw
        raw = {"stdout": "None", "exit_code": 0}
        result = normalize_tool_raw(raw)
        assert result["stdout"] == "None"

    def test_console_tail_folded_into_stdout(self) -> None:
        """console_tail is appended to stdout so model sees XSPEC console prints."""
        from auto_research.sandbox.runner import normalize_tool_raw
        raw = {"stdout": "fit done", "console_tail": "XSPEC: stat=1.23", "exit_code": 0}
        result = normalize_tool_raw(raw)
        assert "fit done" in result["stdout"]
        assert "stat=1.23" in result["stdout"]

    def test_console_tail_only(self) -> None:
        """console_tail alone (no stdout) is surfaced without a leading separator."""
        from auto_research.sandbox.runner import normalize_tool_raw
        raw = {"console_tail": "loading spectra", "exit_code": 0}
        result = normalize_tool_raw(raw)
        assert "loading spectra" in result["stdout"]

    def test_protocol_keys_excluded_from_serialization(self) -> None:
        """exit_code/stderr/figures/console_tail are not re-serialized into stdout."""
        from auto_research.sandbox.runner import normalize_tool_raw
        raw = {"exit_code": 0, "stderr": "warn", "figures": [], "console_tail": "log"}
        result = normalize_tool_raw(raw)
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

        try:
            orch = load_orchestration_config()
        except RuntimeError:
            pytest.skip("orchestration.yaml not configured (unprovisioned machine)")
        analyze_env = orch.get("analyze_env")
        analyze_ini_sh = orch.get("analyze_ini_sh")
        toolkit_root = orch.get("toolkit_root")
        if not (analyze_env and analyze_ini_sh and toolkit_root):
            pytest.skip("analyze_env / analyze_ini_sh / toolkit_root not configured")
        if not (Path(analyze_env) / "bin" / "python").exists():
            pytest.skip(f"analyze_env python absent ({analyze_env}) — unprovisioned machine")
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


class TestShellServerJail:
    """F20: the builtin shell server runs inside the OS jail, under the analysis policy.

    Driven through the run path's own builder (``_builtin_server_config``) and the real
    ``MCPToolClient``. The jail reads a **synthetic** allow-list (the machine's analysis stack,
    read-only, plus the toolkit) so the assertions do not depend on what this host's
    ``agent_fs_allow`` happens to contain -- in particular ``/tmp`` is absent from it, so a write
    that lands proves the cwd bind, not a broad root.
    """

    @staticmethod
    def _orch() -> dict:
        from auto_research.graph.runtime import load_orchestration_config

        try:
            orch = load_orchestration_config()
        except RuntimeError:
            pytest.skip("orchestration.yaml not configured (unprovisioned machine)")
        env, ini, root = orch.get("analyze_env"), orch.get("analyze_ini_sh"), orch.get("toolkit_root")
        if not (env and ini and root and (Path(env) / "bin" / "python").exists()):
            pytest.skip("analysis env not configured on this machine")
        return orch

    @classmethod
    def _server(cls, orch: dict, cwd: Path, toolkit: Path | None = None) -> dict:
        from auto_research.graph.runtime import _builtin_server_config

        return _builtin_server_config(
            Path(orch["analyze_env"]), orch["analyze_ini_sh"], toolkit or Path(orch["toolkit_root"]),
            cwd=cwd, analyze_probe=orch.get("analyze_probe"),
        )

    @staticmethod
    def _synthetic_toolkit(tmp_path: Path) -> Path:
        """A toolkit of our own: a write probe that ever lands, lands here, not in the real one."""
        toolkit = tmp_path / "toolkit"
        (toolkit / "ar_probe_utils").mkdir(parents=True)
        (toolkit / "ar_probe_utils" / "__init__.py").write_text("ORIGIN = 'toolkit'\n")
        return toolkit

    @staticmethod
    def _host_tmp_marker(request: pytest.FixtureRequest) -> Path:
        """A file in the host's real temp root, whatever pytest's basetemp is."""
        import tempfile
        import uuid

        marker = Path(tempfile.gettempdir()) / f"ar_f20_marker_{uuid.uuid4().hex}"
        marker.write_text("host")
        request.addfinalizer(lambda: marker.unlink(missing_ok=True))
        return marker

    @staticmethod
    def _stack_probe(orch: dict) -> str:
        """The configured stack probe minus toolkit packages (the synthetic toolkit has none)."""
        import re

        probe = (orch.get("analyze_probe") or "").strip()
        names = [n.strip() for group in re.findall(r"import\s+([\w., ]+)", probe)
                 for n in group.split(",")]
        stack = [n for n in names if n and not n.endswith("_utils")]
        return f"import {', '.join(stack)}" if stack else "import sys"

    @staticmethod
    def _bash(server: dict, *commands: str) -> list[dict]:
        from auto_research.sandbox.mcp_client import MCPToolClient

        async def go() -> list[dict]:
            client = MCPToolClient(servers=[server])
            await client.start()
            try:
                assert client.is_jailed("mcp.builtin.bash_exec") == server["jailed"]
                return [
                    await client.call_tool("mcp.builtin.bash_exec", {"command": command})
                    for command in commands
                ]
            finally:
                await client.shutdown()

        return asyncio.run(go())

    @staticmethod
    def _synthetic_jail(
        monkeypatch: pytest.MonkeyPatch, orch: dict, runs_root: Path, toolkit: Path,
    ) -> None:
        """The machine's analysis stack read-only, plus -- as production has them -- the toolkit,
        a shared runs root and the host ``/tmp``, all writable in config. What the analysis policy
        then takes away is what the tests assert."""
        from auto_research.sandbox import jail

        readonly = [str(e) for e in (orch.get("agent_fs_readonly") or [orch["analyze_env"]])]
        allow = [*readonly, str(toolkit), str(runs_root), "/tmp"]
        monkeypatch.setattr(jail, "_config", lambda: {
            "agent_os_jail": True, "agent_fs_allow": allow, "agent_fs_readonly": readonly,
        })

    def test_the_jailed_shell_is_confined_clean_and_still_capable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> None:
        from auto_research.sandbox import jail, runner

        if not jail.jail_available()[0]:
            pytest.skip("bwrap cannot run on this host")
        orch = self._orch()
        runs = tmp_path / "runs"
        toolkit = self._synthetic_toolkit(tmp_path)
        self._synthetic_jail(monkeypatch, orch, runs, toolkit)
        # A credential in the launching process must not reach the shell.
        monkeypatch.setenv("AR_F20_FAKE_API_KEY", "sk-should-never-arrive")
        monkeypatch.setattr(runner, "_ini_env_cache", {})

        cwd = runs / "run1"  # the shell's cwd is the run folder, as in production
        cwd.mkdir(parents=True)
        (runs / "old_run").mkdir()
        outside = self._host_tmp_marker(request)
        repo = Path(__file__).resolve().parents[1]
        server = self._server(orch, cwd, toolkit)
        assert server["jailed"] is True and Path(server["command"]).name == "bwrap"
        assert "AR_F20_FAKE_API_KEY" not in server["env"]
        assert not any(k.endswith(("_API_KEY", "_TOKEN")) for k in server["env"]), sorted(server["env"])

        probe = self._stack_probe(orch)
        try:
            results = self._bash(
                server,
                f"ls {repo}",
                f"cat {outside}",
                f"touch {toolkit}/.ar_f20_probe",
                f"ls {toolkit}",
                "touch inside_cwd && echo ok",
                "echo ${AR_F20_FAKE_API_KEY:-absent}",
                f'python -c "{probe}; import ar_probe_utils"',
                f"touch {runs / 'old_run' / 'x'}",
                f"touch {cwd / 'authoring' / 'current_base_ref'}",
                'echo "$PFILES"',
            )
        finally:
            leaked = (toolkit / ".ar_f20_probe").exists()
        (repo_ls, host_tmp, toolkit_write, toolkit_read, cwd_write, secret, capability,
         old_run, authoring, pfiles) = results
        assert repo_ls["exit_code"] != 0, "the jailed shell can list this repository"
        assert host_tmp["exit_code"] != 0, "the jailed shell sees the host /tmp"
        assert toolkit_write["exit_code"] != 0 and not leaked, "the jailed shell can write the toolkit"
        assert toolkit_read["exit_code"] == 0, toolkit_read
        assert cwd_write["exit_code"] == 0 and (cwd / "inside_cwd").exists(), cwd_write
        assert secret["stdout"].strip() == "absent", "a parent credential reached the shell"
        assert capability["exit_code"] == 0, capability  # the analysis stack still imports
        assert old_run["exit_code"] != 0, "the shell can write another run's folder"
        assert authoring["exit_code"] != 0, "the shell can write the run's authoring control files"
        if ";" in (server["env"].get("PFILES") or ""):  # a HEASoft-style stack is configured
            assert pfiles["stdout"].startswith("/tmp/pfiles;"), pfiles  # its own, not a shared one

    def test_the_shell_writes_only_the_workers_folders_not_the_run_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A folder another process binds (a worker's) must not be replaceable by the shell."""
        from auto_research.graph.runtime import _builtin_server_config
        from auto_research.sandbox import jail

        if not jail.jail_available()[0]:
            pytest.skip("bwrap cannot run on this host")
        orch = self._orch()
        runs = tmp_path / "runs"
        toolkit = self._synthetic_toolkit(tmp_path)
        self._synthetic_jail(monkeypatch, orch, runs, toolkit)
        run = runs / "run1"
        worker = run / "analyst_worker_0"
        worker.mkdir(parents=True)
        server = _builtin_server_config(
            Path(orch["analyze_env"]), orch["analyze_ini_sh"], toolkit, cwd=run,
            writable_dirs=[worker],
        )
        own, root, rename = self._bash(
            server, f"touch {worker / 'ok'}", f"touch {run / 'planted'}",
            f"mv {worker} {run / 'moved'}",
        )
        assert own["exit_code"] == 0 and (worker / "ok").exists(), own
        assert root["exit_code"] != 0 and not (run / "planted").exists(), root
        assert rename["exit_code"] != 0 and worker.is_dir(), rename

    def test_shell_server_launches_unwrapped_when_the_jail_is_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from auto_research.sandbox import jail

        orch = self._orch()
        monkeypatch.setattr(jail, "_config", lambda: {"agent_os_jail": False})
        server = self._server(orch, tmp_path)
        assert server["jailed"] is False
        assert Path(server["command"]).name.startswith("python"), server["command"]
        assert server["args"][:2] == ["-P", "-c"]
        (result,) = self._bash(server, "echo ok")
        assert result["exit_code"] == 0 and "ok" in result["stdout"], result

    def test_the_jailed_repl_has_the_same_confinement_and_still_runs_toolkit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> None:
        """The worker REPL shares the shell's policy: toolkit read-only, private /tmp, no secrets."""
        from auto_research.graph.runtime import SandboxSettings
        from auto_research.sandbox import jail, runner

        if not jail.jail_available()[0]:
            pytest.skip("bwrap cannot run on this host")
        orch = self._orch()
        runs = tmp_path / "runs"
        toolkit = self._synthetic_toolkit(tmp_path)
        self._synthetic_jail(monkeypatch, orch, runs, toolkit)
        monkeypatch.setenv("AR_F20_FAKE_API_KEY", "sk-should-never-arrive")
        monkeypatch.setattr(runner, "_ini_env_cache", {})
        settings = SandboxSettings.from_config(orch)
        toolkit_pkg = "ar_probe_utils"
        outside = self._host_tmp_marker(request)
        workdir = runs / "run1" / "analyst_worker_0"
        sibling = runs / "run1" / "analyst_worker_1"
        sibling.mkdir(parents=True)
        # A package of the toolkit's name planted in the REPL's cwd must not shadow the toolkit.
        (workdir / toolkit_pkg).mkdir(parents=True)
        (workdir / toolkit_pkg / "__init__.py").write_text("SHADOW = True\n")
        sandbox = PersistentSandbox(
            toolkit, workdir,
            analyze_env=settings.analyze_env, analyze_ini_sh=settings.analyze_ini_sh,
            repl_init_py=settings.repl_init_py,
        )

        async def go() -> list[dict]:
            try:
                return [
                    await sandbox.call_tool("os", "makedirs", {"name": str(toolkit / ".ar_f20_dir")}),
                    await sandbox.call_tool("os", "stat", {"path": str(outside)}),
                    await sandbox.call_tool("os", "getenv", {"key": "AR_F20_FAKE_API_KEY"}),
                    await sandbox.call_tool("importlib", "import_module", {"name": toolkit_pkg}),
                    await sandbox.call_tool("os", "makedirs", {"name": str(workdir / "ok")}),
                    await sandbox.call_tool("os", "makedirs", {"name": str(sibling / "x")}),
                    await sandbox.call_tool("os", "getenv", {"key": "PFILES"}),
                ]
            finally:
                await sandbox.shutdown()

        try:
            write_tk, host_tmp, secret, imported, write_cwd, write_sibling, pfiles = asyncio.run(go())
        finally:
            leaked = (toolkit / ".ar_f20_dir").exists()
        assert write_tk["exit_code"] != 0 and not leaked, "the jailed REPL can write the toolkit"
        assert host_tmp["exit_code"] != 0, "the jailed REPL sees the host /tmp"
        assert secret["exit_code"] == 0 and secret["stdout"] == "None", secret
        assert imported["exit_code"] == 0, imported
        assert str(toolkit.resolve()) in imported["stdout"], (
            f"the toolkit package was shadowed by the cwd: {imported['stdout']}")
        assert write_cwd["exit_code"] == 0 and (workdir / "ok").is_dir(), write_cwd
        assert write_sibling["exit_code"] != 0, "a REPL can write another worker's folder"
        if ";" in pfiles["stdout"]:  # a HEASoft-style stack is configured
            assert pfiles["stdout"].startswith("/tmp/pfiles;"), pfiles

    def test_the_gate_runs_tests_with_a_private_tmp_and_its_worktree_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer B runs coder-authored code: it gets a private /tmp, never the host's."""
        from auto_research.sandbox import gate, jail

        if not jail.bwrap_path():
            pytest.skip("bwrap is not installed on this host")
        monkeypatch.setattr(jail, "_config", lambda: {
            "agent_os_jail": True, "agent_fs_allow": ["/tmp"], "agent_fs_readonly": [],
        })
        worktree = tmp_path / "wt"
        worktree.mkdir()
        junit = tmp_path / "junit" / "junit1.xml"
        junit.parent.mkdir()
        argv = gate._jailed_pytest(["python", "-m", "pytest"], junit_path=str(junit), worktree=worktree)
        pairs = list(zip(argv, argv[1:], argv[2:]))
        assert ("--bind", str((junit.parent / "tmp").resolve()), "/tmp") in pairs
        assert ("--bind", str(Path("/tmp").resolve()), str(Path("/tmp").resolve())) not in pairs
        assert ("--bind", str(worktree.resolve()), str(worktree.resolve())) in pairs

    def test_a_private_tmp_someone_else_could_control_is_refused(self, tmp_path: Path) -> None:
        """The host temp root is shared and the name predictable: only our own 0700 dir is used."""
        from auto_research.sandbox.jail import _owned_private_dir

        ok = _owned_private_dir(tmp_path / "fresh")
        assert ok.is_dir() and (ok.stat().st_mode & 0o777) == 0o700
        loose = tmp_path / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        with pytest.raises(RuntimeError, match="readable by others"):
            _owned_private_dir(loose)
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        (tmp_path / "planted").symlink_to(target)
        with pytest.raises(RuntimeError, match="not a plain directory"):
            _owned_private_dir(tmp_path / "planted")

    def test_the_init_script_never_picks_up_bashrc_even_with_a_socket_on_stdin(
        self, tmp_path: Path
    ) -> None:
        """bash sources ~/.bashrc when stdin is a socket (a detached backend's case)."""
        import socket
        import subprocess
        import sys

        home = tmp_path / "home"
        home.mkdir()
        (home / ".bashrc").write_text("export AR_BASHRC_MARK=leaked\n")
        ini = tmp_path / "ini.sh"
        ini.write_text("export AR_INI_SET=1\n")
        code = (
            "from auto_research.sandbox.runner import _source_ini_env\n"
            f"env = _source_ini_env({str(ini)!r})\n"
            "print('AR_INI_SET' in env, 'AR_BASHRC_MARK' in env, 'PWD' in env)\n"
        )
        ours, theirs = socket.socketpair()
        try:
            out = subprocess.run(
                [sys.executable, "-c", code], stdin=theirs, capture_output=True, text=True,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin", "AR_SECRET_API_KEY": "x"},
                timeout=60,
            )
        finally:
            ours.close()
            theirs.close()
        assert out.stdout.split() == ["True", "False", "False"], (out.stdout, out.stderr)

    def test_the_command_guard_follows_how_the_server_was_launched(self, tmp_path: Path) -> None:
        """An unjailed server (a configured ``mcp_servers`` entry, the jail off) keeps the scan."""
        from auto_research.agents.react import SandboxToolProvider
        from auto_research.sandbox.registry import ToolkitRegistry

        class _Client:
            def __init__(self, jailed: bool) -> None:
                self._jailed = jailed

            def list_kinds(self): return ["mcp.builtin.bash_exec"]
            def describe(self, kind): return "shell"
            def schema(self, kind): return {"type": "object", "properties": {}}
            def is_jailed(self, kind): return self._jailed

        for jailed in (False, True):
            reg = ToolkitRegistry(tmp_path)
            reg._loaded = True
            reg.load_mcp(_Client(jailed))
            assert reg.mcp_jailed("mcp.builtin.bash_exec") is jailed
            sb = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 0, "stdout": "ran", "stderr": ""})
            prov = SandboxToolProvider(reg, sb, cwd=tmp_path)
            from auto_research.agents import react
            prov._guard = react.PathGuard(roots=[tmp_path])
            out = asyncio.run(prov.execute("mcp.builtin.bash_exec", {"command": "cat /etc/shadow"}))
            if jailed:
                assert "path not permitted" not in out.text
            else:
                assert out.is_error and "path not permitted" in out.text


# ---------------------------------------------------------------------------
# bash_exec non-interactive isolation (regression guard for the MCP-transport hang)
# ---------------------------------------------------------------------------


class TestBashExecIsolation:
    """A prompting/forking command must never hang the MCP server or outlive its
    timeout. stdin is closed (a stdin read gets EOF, not a block on the transport)
    and a timeout kills the whole process group (a forked grandchild dies with it)."""

    def test_stdin_is_closed(self) -> None:
        """`cat` with no file reads stdin; DEVNULL gives immediate EOF, so it exits
        0 fast instead of blocking until the timeout."""
        import time

        from auto_research.sandbox.builtin_server import bash_exec

        t0 = time.time()
        out = bash_exec("cat", timeout_s=5)
        assert out["exit_code"] == 0, out
        assert (time.time() - t0) < 3, "cat blocked on stdin — stdin is not DEVNULL"

    def test_timeout_kills_grandchild(self, tmp_path: Path) -> None:
        """On timeout the whole process group is killed, so a backgrounded
        grandchild cannot survive to touch its marker file."""
        import time

        from auto_research.sandbox.builtin_server import bash_exec

        marker = tmp_path / "grandchild_ran"
        cmd = f"( sleep 3; touch {marker!s} ) & echo spawned; wait"
        t0 = time.time()
        out = bash_exec(cmd, timeout_s=1)
        elapsed = time.time() - t0
        assert out["exit_code"] == 124, out
        assert elapsed < 3, "did not return at the timeout — group kill/drain hung"
        time.sleep(3.5)  # long enough for a surviving grandchild to have run
        assert not marker.exists(), "grandchild survived the timeout process-group kill"


def test_the_jail_supersedes_the_command_scan_without_blocking_everything(monkeypatch, tmp_path):
    """Retiring the shell-command path scan must mean 'nothing denied', not 'deny with no reason'.

    `forbidden_in_command` returns `None` for a clean command and a string for a denied one, and
    the caller tests `is not None`. Returning an empty list to mean 'skip the check' therefore
    rejected EVERY shell command — a total block dressed as a relaxation. Caught by the existing
    sandbox tests before it reached a paid run; pinned here so it cannot come back.
    """
    import asyncio

    from auto_research.agents import react
    from auto_research.agents.react import SandboxToolProvider

    sb = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 0, "stdout": a.get("command", ""), "stderr": ""})
    reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})

    for jailed in (True, False):
        monkeypatch.setattr(react, "_jail_supersedes_command_check", lambda *_a: jailed)
        prov = SandboxToolProvider(reg, sb, cwd=tmp_path)
        out = asyncio.run(prov.execute("mcp.builtin.bash_exec", {"command": "echo hi"}))
        assert not out.is_error, f"a clean command was rejected with jail={jailed}"
        assert "path not permitted" not in out.text


def test_a_denied_path_is_still_denied_when_no_jail_is_running(monkeypatch, tmp_path):
    """The scan stays in force on a host with no kernel enforcement to fall back on."""
    import asyncio

    from auto_research.agents import react
    from auto_research.agents.react import SandboxToolProvider

    monkeypatch.setattr(react, "_jail_supersedes_command_check", lambda *_a: False)
    sb = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 0, "stdout": "", "stderr": ""})
    reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})
    prov = SandboxToolProvider(reg, sb, cwd=tmp_path)
    prov._guard = react.PathGuard(roots=[tmp_path])
    out = asyncio.run(prov.execute("mcp.builtin.bash_exec", {"command": "cat /etc/shadow"}))
    assert out.is_error and "path not permitted" in out.text


def test_memory_lives_beside_the_ledger_and_is_never_followed_through_a_symlink(tmp_path, monkeypatch):
    """F22: the harness appends tool output to memory.md and reads it into the next prompt,
    outside the jail; inside the agent's own writable folder a planted symlink redirected both."""
    from auto_research.sandbox import workspace

    monkeypatch.setattr(workspace, "runtime_data_dir", lambda: tmp_path / "runtime")
    path = workspace.memory_path("r1", "analyst_worker_0")
    agent_folder = workspace.agent_dir("r1", "analyst_worker_0")
    assert workspace.ledger_dir("r1") in path.parents
    assert agent_folder not in path.parents

    victim = tmp_path / "victim.txt"
    victim.write_text("host\n")
    path.unlink(missing_ok=True)
    path.symlink_to(victim)
    with pytest.raises(OSError):
        workspace.append_memory(path, "model-chosen text")
    with pytest.raises(OSError):
        workspace.read_memory(path)
    assert victim.read_text() == "host\n"


def test_memory_at_the_old_agent_folder_location_is_never_read(tmp_path, monkeypatch):
    """The old location is agent-writable: a file, a symlink, a FIFO there is never read (F22)."""
    import os

    from auto_research.sandbox import workspace

    monkeypatch.setattr(workspace, "runtime_data_dir", lambda: tmp_path / "runtime")
    folder = workspace.agent_dir("r1", "w0")
    folder.mkdir(parents=True)
    (folder / "memory.md").write_text("planted prior results\n")
    assert workspace.read_memory(workspace.memory_path("r1", "w0")) == ""
    folder2 = workspace.agent_dir("r1", "w1")
    folder2.mkdir(parents=True)
    os.mkfifo(folder2 / "memory.md")  # would hang a reader that opened it
    assert workspace.read_memory(workspace.memory_path("r1", "w1")) == ""

def test_a_private_param_dir_replaces_only_the_writable_half(tmp_path):
    from auto_research.sandbox.runner import with_private_param_dir

    env = {"PFILES": "/home/u/pfiles;/opt/heasoft/syspfiles", "X": "1"}
    out = with_private_param_dir(env, "/tmp/pfiles", create=tmp_path / "p")
    assert out["PFILES"] == "/tmp/pfiles;/opt/heasoft/syspfiles" and out["X"] == "1"
    assert (tmp_path / "p").is_dir()
    assert with_private_param_dir({"X": "1"}, tmp_path / "q") == {"X": "1"}


def test_a_record_planted_in_a_new_runs_folder_is_never_read(tmp_path):
    """A run with a ledger reads its records ledger-side only (F22): the run folder is writable."""
    from auto_research.graph import detached as d
    from auto_research.sandbox.workspace import record_beside

    run = tmp_path / "runs" / "r"
    run.mkdir(parents=True)
    ledger = tmp_path / "ledgers" / "r"
    ledger.mkdir(parents=True)
    (ledger / "backend.log").write_text("")
    (run / "monitor.jsonl").write_text("{}\n")  # a planted marker must not make it look pre-split
    (run / "result.json").write_text('{"status": "ok", "forged": true}')
    assert record_beside(run, "result.json") == ledger / "result.json"
    assert d.run_status(run) != "done"


def test_reading_an_old_run_never_flips_it_to_the_new_layout(tmp_path):
    from auto_research.sandbox.workspace import record_beside, resolve_record  # noqa: F401

    run = tmp_path / "runs" / "old"
    run.mkdir(parents=True)
    (run / "monitor.jsonl").write_text("{}\n")
    assert record_beside(run, "result.json") == run / "result.json"
    assert record_beside(run, "receipts", legacy_ok=False) == tmp_path / "ledgers" / "old" / "receipts"
    assert not (tmp_path / "ledgers" / "old").exists()
    assert record_beside(run, "monitor.jsonl") == run / "monitor.jsonl"


def test_a_symlinked_writable_root_or_home_is_refused(tmp_path, monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "_config", lambda: {"agent_os_jail": True, "agent_fs_allow": [], "agent_fs_readonly": []})
    target = tmp_path / "toolkit"
    target.mkdir()
    link = tmp_path / "worker"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        jail.analysis_policy(label="repl", toolkit_root=target, writable=(str(link),), tmp=tmp_path / "t")
    real = tmp_path / "real_worker"
    real.mkdir()
    with pytest.raises(RuntimeError, match="symlink"):
        jail.analysis_policy(label="repl", toolkit_root=target, writable=(str(real),), tmp=tmp_path / "t",
                             home=link)


def test_an_analysis_home_is_mounted_outside_tmp(tmp_path, monkeypatch):
    """Homes live under the host temp root, which is the process's own /tmp inside the jail; a
    destination there could be redirected by a symlink planted earlier (F22 review)."""
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "_config", lambda: {"agent_os_jail": True, "agent_fs_allow": [], "agent_fs_readonly": []})
    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    home = tmp_path / "home"
    policy = jail.analysis_policy(label="repl", toolkit_root=tmp_path / "tk", tmp=tmp_path / "t", home=home)
    argv = jail.jail_argv(policy)
    assert ("--bind", str(home.resolve()), jail.JAIL_HOME_AT) in list(zip(argv, argv[1:], argv[2:]))
    assert ("--setenv", "HOME", jail.JAIL_HOME_AT) in list(zip(argv, argv[1:], argv[2:]))
