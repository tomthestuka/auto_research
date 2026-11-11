"""Deterministic tests for sandbox/runner.py, sandbox/registry.py, sandbox/merge_flow.py,
and AnalystWorkerAgent.execute_tools() dispatch/retry routing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.toolkit_contract import toolkit
from auto_research.sandbox.registry import ToolkitRegistry
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
# A toolkit call in the jailed REPL (the one path a toolkit callable runs on)
# ---------------------------------------------------------------------------

class TestJailedCall:
    """``PersistentSandbox.call_tool`` inside the jail (D25): this interpreter stands in for a human
    environment, which the allow-list names read-only."""

    @pytest.fixture(autouse=True)
    def _jail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from auto_research.sandbox import jail

        if not jail.jail_available()[0]:
            pytest.skip("the OS jail is required and bwrap cannot run here")
        monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [sys.prefix],
                                                      "agent_fs_readonly": [sys.prefix]})

    @staticmethod
    def _calls(tmp_path: Path, *calls: tuple[str, str, dict, int | None]) -> list[dict]:
        import sys

        from auto_research.sandbox.envs import Environment

        init = tmp_path / "init.sh"
        init.write_text("export READY=1\n")
        (tmp_path / "toolkit").mkdir(exist_ok=True)
        sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work",
                                    environment=Environment(name="here", prefix=Path(sys.prefix), activate=init))

        async def go() -> list[dict]:
            try:
                return [await sandbox.call_tool(m, f, a, timeout_s=t) for m, f, a, t in calls]
            finally:
                await sandbox.shutdown()

        return asyncio.run(go())

    def test_a_call_runs_in_its_working_folder_and_imports_the_toolkit(self, tmp_path: Path) -> None:
        (tmp_path / "toolkit").mkdir()
        (tmp_path / "toolkit" / "mytool.py").write_text("def greet(name='world'): return f'hello {name}'\n")
        greet, cwd = self._calls(tmp_path, ("mytool", "greet", {"name": "test"}, None), ("os", "getcwd", {}, None))
        assert greet["exit_code"] == 0 and "hello test" in greet["stdout"], greet
        assert cwd["exit_code"] == 0 and str(tmp_path / "work") in cwd["stdout"], cwd

    def test_a_call_past_its_timeout_is_killed_and_the_next_one_runs(self, tmp_path: Path) -> None:
        (tmp_path / "toolkit").mkdir()
        (tmp_path / "toolkit" / "sleeper.py").write_text("import time\ndef long_sleep(secs=60): time.sleep(secs)\n")
        slow, after = self._calls(tmp_path, ("sleeper", "long_sleep", {"secs": 60}, 1), ("os", "getpid", {}, None))
        assert slow["exit_code"] == 124 and "Timed out" in slow["stderr"], slow
        assert after["exit_code"] == 0, after

    def test_a_module_that_does_not_import_is_the_calls_error(self, tmp_path: Path) -> None:
        (bad,) = self._calls(tmp_path, ("no_such_module_xyz_abc", "do_something", {}, None))
        assert bad["exit_code"] != 0, bad


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

    def test_no_path_argument_is_filled_by_the_runtime(self) -> None:
        """Step 0b: a tool names records, never a store's location, so the runtime fills no
        observation folder into a path argument the model left out."""
        from auto_research.agents.react import SandboxToolProvider
        sb = _FakeSandbox()
        reg = _FakeRegistry(known={"tk.load_data"}, mcp=set(),
                            schema={"properties": {"epoch": {"kind": "setting"}, "ixpe_path": {"kind": "path"}}})
        prov = SandboxToolProvider(reg, sb)
        asyncio.run(prov.execute("tk.load_data", {"epoch": "{}"}))
        _, _, args = sb.tool_calls[0]
        assert args == {"epoch": "{}"}

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
                    ToolCall(id="c2", name="write_report", arguments={"content": (
                        "# summary\ndid the fit\n# status\nsucceeded\n# findings\n"
                        "- chi2/dof=1.0\n# figures\n- [self:1]\n"
                    )}),
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
        import hashlib

        # A real file: `turn_end` refuses a placed figure that is not on disk, and only an
        # accepted `turn_end` makes the draft the report (ruling D4).
        png = tmp_path / "fit.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfit")
        fig = {
            "path": str(png), "caption": "fit", "mime_type": "image/png",
            "produced_by": "tk_load", "created_at": datetime.now(UTC).isoformat(),
            "sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
        }
        # Tool returns a figure → exercises figure propagation onto the report.
        sb = _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "loaded indices", "figures": [fig]})
        reg = _FakeRegistry(known={"tk_load"}, mcp=set())
        monitor = RunMonitor(chatter=7)
        w = AnalystWorkerAgent(
            model=_script_model(),
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
        assert [str(f.path) for f in report.figures] == [str(png)]  # real figure attached
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
        w = AnalystWorkerAgent(model=_script_model(), sandbox=sb, registry=reg)
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

        w = AnalystWorkerAgent(model=_PlainModel())  # no sandbox/registry
        report = asyncio.run(w.analyze(AnalysisTask(question="q")))
        assert report.summary == "no tools"


# ---------------------------------------------------------------------------
# normalize_tool_raw
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


# ---------------------------------------------------------------------------
# bash_exec interpreter consistency (regression guard for the analyst env)
# ---------------------------------------------------------------------------


class TestBashExecInterpreterConsistency:
    """A bare ``python`` inside bash_exec must resolve to the toolkit's default environment's
    interpreter — the same one the toolkit tools invoke. Without the PATH pin (the shared
    launcher's, ``envs.in_environment``), bash_exec uses whatever conda env leaked onto the
    launching shell's PATH (so ``import astropy`` / ``import xspec_utils`` breaks
    nondeterministically). This test fails if that wiring regresses."""

    @pytest.fixture(autouse=True)
    def _own_conda_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The runtime's conda folders, which the shell binds read-only: a temp copy, never the
        configured runtime dir."""
        from auto_research import env as app_env

        monkeypatch.setattr(app_env, "agent_conda_dir", lambda: tmp_path / "conda")

    def test_bash_exec_python_matches_the_default_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from auto_research.graph.runtime import _builtin_server_config
        from auto_research.sandbox import jail

        if not jail.jail_available()[0]:
            pytest.skip("the OS jail is required and bwrap cannot run here")
        orch, environment = TestShellServerJail._orch()
        server = _builtin_server_config(environment, toolkit(orch).root, cwd=tmp_path)
        (result,) = TestShellServerJail._bash(server, "command -v python")
        assert result["exit_code"] == 0, result
        resolved = Path(result["stdout"].strip())
        assert resolved.resolve() == environment.python.resolve(), (
            f"bash_exec `python` resolves to {resolved}, not the default environment's "
            f"interpreter {environment.python}"
        )


class TestShellServerJail:
    """F20: the builtin shell server runs inside the OS jail, under the analysis policy.

    Driven through the run path's own builder (``_builtin_server_config``) and the real
    ``MCPToolClient``. The jail reads a **synthetic** allow-list (the machine's analysis stack,
    read-only, plus the toolkit) so the assertions do not depend on what this host's
    ``agent_fs_allow`` happens to contain -- in particular ``/tmp`` is absent from it, so a write
    that lands proves the cwd bind, not a broad root.
    """

    @pytest.fixture(autouse=True)
    def _own_conda_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The runtime's conda folders, which the shell binds read-only: a temp copy, never the
        configured runtime dir."""
        from auto_research import env as app_env

        monkeypatch.setattr(app_env, "agent_conda_dir", lambda: tmp_path / "conda")

    @staticmethod
    def _orch() -> tuple[dict, Any]:
        """This machine's configuration and its toolkit's default environment, resolved as the run
        path resolves it (``env.analysis_environment``, into the test's own conda folder)."""
        from auto_research.env import analysis_environment
        from auto_research.graph.runtime import load_orchestration_config

        try:
            orch = load_orchestration_config()
            environment = analysis_environment(orch)
        except (RuntimeError, ValueError, OSError) as exc:
            pytest.skip(f"the default environment does not resolve on this machine: {exc}")
        if not environment.python.exists():
            pytest.skip("the default environment's interpreter is absent on this machine")
        return orch, environment

    @classmethod
    def _server(cls, orch: tuple[dict, Any], cwd: Path, toolkit_dir: Path | None = None) -> dict:
        from auto_research.env import default_definition
        from auto_research.graph.runtime import _builtin_server_config

        config, environment = orch
        return _builtin_server_config(
            environment, toolkit_dir or toolkit(config).root,
            cwd=cwd, probe=default_definition().probe or None,
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
    def _stack_probe(orch: tuple[dict, Any]) -> str:
        """The default environment's probe minus toolkit packages (the synthetic toolkit has none)."""
        import re

        from auto_research.env import default_definition

        probe = default_definition().probe
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
                return [
                    await client.call_tool("mcp.builtin.bash_exec", {"command": command})
                    for command in commands
                ]
            finally:
                await client.shutdown()

        return asyncio.run(go())

    @staticmethod
    def _synthetic_jail(
        monkeypatch: pytest.MonkeyPatch, orch: tuple[dict, Any], runs_root: Path, toolkit: Path,
    ) -> None:
        """The machine's analysis stack read-only, plus -- as production has them -- the toolkit,
        a shared runs root and the host ``/tmp``, all writable in config. What the analysis policy
        then takes away is what the tests assert."""
        from auto_research.sandbox import jail

        config, environment = orch
        readonly = [str(e) for e in (config.get("agent_fs_readonly") or [environment.prefix])]
        allow = [*readonly, str(toolkit), str(runs_root), "/tmp"]
        monkeypatch.setattr(jail, "_config", lambda: {
            "agent_fs_allow": allow, "agent_fs_readonly": readonly,
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
        assert Path(server["command"]).name == "bwrap"
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
        from auto_research.env import private_variables

        if "PFILES" in private_variables(orch[0]):  # declared on an external (D19)
            assert pfiles["stdout"].startswith("/tmp/pfiles/call-"), pfiles  # this command's own (G4)

    def test_the_shell_can_look_into_a_built_environment_and_not_write_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """use_env: built environments are bound read-only into the jailed shell."""
        from auto_research import env as app_env
        from auto_research.agents.react import _in_environment
        from auto_research.sandbox import jail, runner
        from auto_research.sandbox.envs import Environment

        if not jail.jail_available()[0]:
            pytest.skip("bwrap cannot run on this host")
        orch = self._orch()
        runs = tmp_path / "runs"
        toolkit = self._synthetic_toolkit(tmp_path)
        self._synthetic_jail(monkeypatch, orch, runs, toolkit)
        monkeypatch.setattr(runner, "_ini_env_cache", {})
        prefix = tmp_path / "conda" / "envs" / "ar-look-0123456789abcdef"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "look-hello").write_text("#!/bin/sh\necho HELLO-FROM-LOOK $LOOK_ACTIVE\n")
        activation = tmp_path / "conda" / "activation" / "0123456789abcdef" / "activate.sh"
        activation.parent.mkdir(parents=True)
        activation.write_text("export LOOK_ACTIVE=sourced-in-jail\n")
        (prefix / "bin" / "look-hello").chmod(0o755)
        cwd = runs / "run1"
        cwd.mkdir(parents=True)
        server = self._server(orch, cwd, toolkit)
        look = Environment(name="ar-look", prefix=prefix, owner="pipeline", activate=activation)
        looked, wrote = self._bash(
            server,
            _in_environment(look, "look-hello", toolkit),
            _in_environment(look, f"touch {prefix}/bin/planted", toolkit),
        )
        assert looked["stdout"].strip() == "HELLO-FROM-LOOK sourced-in-jail", looked
        assert wrote["exit_code"] != 0 and not (prefix / "bin" / "planted").exists(), wrote

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
        server = _builtin_server_config(orch[1], toolkit, cwd=run, writable_dirs=[worker])
        own, root, rename = self._bash(
            server, f"touch {worker / 'ok'}", f"touch {run / 'planted'}",
            f"mv {worker} {run / 'moved'}",
        )
        assert own["exit_code"] == 0 and (worker / "ok").exists(), own
        assert root["exit_code"] != 0 and not (run / "planted").exists(), root
        assert rename["exit_code"] != 0 and worker.is_dir(), rename

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
        settings = SandboxSettings.from_config(orch[0])
        toolkit_pkg = "ar_probe_utils"
        outside = self._host_tmp_marker(request)
        workdir = runs / "run1" / "analyst_worker_0"
        sibling = runs / "run1" / "analyst_worker_1"
        sibling.mkdir(parents=True)
        # A package of the toolkit's name planted in the REPL's cwd must not shadow the toolkit.
        (workdir / toolkit_pkg).mkdir(parents=True)
        (workdir / toolkit_pkg / "__init__.py").write_text("SHADOW = True\n")
        sandbox = PersistentSandbox(toolkit, workdir, environment=settings.environment)

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
        from auto_research.env import private_variables

        if "PFILES" in private_variables():  # declared on an external (D19)
            assert pfiles["stdout"].startswith("/tmp/pfiles/call-"), pfiles  # this call's own (G4)

    def test_the_gate_runs_tests_with_a_private_tmp_and_its_worktree_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer B runs coder-authored code: it gets a private /tmp, never the host's."""
        from auto_research.sandbox import gate, jail

        if not jail.bwrap_path():
            pytest.skip("bwrap is not installed on this host")
        monkeypatch.setattr(jail, "_config", lambda: {
            "agent_fs_allow": ["/tmp"], "agent_fs_readonly": [],
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


def test_a_shell_command_is_confined_by_the_jail_not_scanned(tmp_path):
    """The shell server runs inside the OS jail, which holds only the allow-list; the runtime
    applies no command-string scan on top (its false denials cost loop iterations, 2026-09-01). A
    command naming a path outside the allow-list reaches the server, and the jail answers it."""
    import asyncio

    from auto_research.agents import react
    from auto_research.agents.react import SandboxToolProvider

    sent: list[str] = []
    sb = _FakeSandbox(mcp_fn=lambda k, a: sent.append(a.get("command", "")) or
                      {"exit_code": 0, "stdout": "ran", "stderr": ""})
    reg = _FakeRegistry(known={"mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"})
    prov = SandboxToolProvider(reg, sb, cwd=tmp_path)
    prov._guard = react.PathGuard(roots=[tmp_path])
    for command in ("echo hi", "cat /etc/shadow"):
        out = asyncio.run(prov.execute("mcp.builtin.bash_exec", {"command": command}))
        assert not out.is_error and "path not permitted" not in out.text, (command, out.text)
    assert sent == ["echo hi", "cat /etc/shadow"]


def test_a_declared_private_variable_gets_a_folder_of_its_own(tmp_path):
    """D19: the external declares the variable and its separator; the part before it is replaced."""
    from auto_research.env import private_variables
    from auto_research.sandbox.runner import private_folders

    config = {"env_externals": {"tool": {"root": "/opt/tool", "private": {"TOOLPARAMS": ";"}}, "data": {"root": "/d"}}}
    private = private_variables(config)
    assert private == {"TOOLPARAMS": ";"}
    env = {"TOOLPARAMS": "/home/u/params;/opt/tool/sys", "X": "1"}
    out, made = private_folders(env, private, at="/tmp", create=tmp_path / "p")
    assert out["TOOLPARAMS"] == "/tmp/toolparams;/opt/tool/sys" and out["X"] == "1" and made == []
    assert (tmp_path / "p" / "toolparams").is_dir() and json.loads(out["AR_PRIVATE_FOLDERS"]) == private
    assert private_folders({"X": "1"}, private, at=tmp_path / "q")[0]["X"] == "1"  # no value: unchanged
    assert "TOOLPARAMS" not in private_folders({"X": "1"}, private, at=tmp_path / "q")[0]
    alone, _ = private_folders({"X": "1"}, private, at="/tmp", create=tmp_path / "r", alone=True)
    assert alone["TOOLPARAMS"] == "/tmp/toolparams" and (tmp_path / "r" / "toolparams").is_dir()
    # One call's folder: fresh, inside the process's own, the kept part and the declaration from the env.
    own, _ = private_folders(env, private, at=tmp_path / "own")
    call, made = private_folders(own)
    assert made and Path(made[0]).parent == tmp_path / "own" / "toolparams"
    assert call["TOOLPARAMS"] == made[0] + ";/opt/tool/sys"
    with pytest.raises(ValueError, match="VARIABLE: separator"):
        private_variables({"env_externals": {"tool": {"private": {"TOOLPARAMS": ""}}}})
    for bad, says in (({"tool": {"private": ["TOOLPARAMS"]}}, "write env_externals as"),
                      ({"tool": "TOOLPARAMS"}, "write env_externals as"),
                      ({"tool": {"private": {"AR_PRIVATE_FOLDERS": ";"}}}, "reserved"),
                      ({"tool": {"private": {"MARK": ";"}}}, "reserved"),
                      ({"a": {"private": {"TOOLPARAMS": ";"}}, "b": {"private": {"ToolParams": ";"}}},
                       "already declared on a")):
        with pytest.raises(ValueError, match=says):
            private_variables({"env_externals": bad})


def test_an_undeclared_variable_is_left_shared(tmp_path, monkeypatch):
    """D19: with no declaration nothing is isolated: the value stays as the activation set it, no
    declaration is recorded, and a shell command sees the shared folder."""
    import auto_research.sandbox.builtin_server as bs
    from auto_research.env import private_variables
    from auto_research.sandbox.runner import private_folders

    shared = f"{tmp_path / 'shared'};/opt/heasoft/syspfiles"
    private = private_variables({"env_externals": {"heasoft": {"root": "/opt/heasoft"}}})
    out, made = private_folders({"PFILES": shared}, private, at=tmp_path / "own")
    assert out == {"PFILES": shared} and made == [] and not (tmp_path / "own").exists()
    monkeypatch.setenv("PFILES", shared)
    monkeypatch.delenv("AR_PRIVATE_FOLDERS", raising=False)
    assert bs.bash_exec('echo "$PFILES"')["stdout"].strip() == shared


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

    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [], "agent_fs_readonly": []})
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

    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [], "agent_fs_readonly": []})
    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    home = tmp_path / "home"
    policy = jail.analysis_policy(label="repl", toolkit_root=tmp_path / "tk", tmp=tmp_path / "t", home=home)
    argv = jail.jail_argv(policy)
    assert ("--bind", str(home.resolve()), jail.JAIL_HOME_AT) in list(zip(argv, argv[1:], argv[2:]))
    assert ("--setenv", "HOME", jail.JAIL_HOME_AT) in list(zip(argv, argv[1:], argv[2:]))


def test_the_shell_reads_no_in_tree_bytecode(tmp_path, monkeypatch):
    """git ignores `__pycache__`, so nothing judges it: the shell keeps bytecode in its own folder."""
    from auto_research import env as app_env
    from auto_research.graph.runtime import _builtin_server_config
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")  # the argv only; nothing runs

    monkeypatch.setattr(app_env, "agent_conda_dir", lambda: tmp_path / "conda")
    ini = tmp_path / "activate.sh"
    ini.write_text("export A=1\n")
    (tmp_path / "toolkit").mkdir()
    import sys

    from auto_research.sandbox.envs import Environment

    cfg = _builtin_server_config(Environment(name="host", prefix=Path(sys.prefix), activate=ini),
                                 tmp_path / "toolkit", cwd=tmp_path / "run")
    assert cfg["command"] == "/usr/bin/bwrap"
    assert cfg["env"]["PYTHONPYCACHEPREFIX"] == "/tmp/ar-pycache"  # the jail's private /tmp


def test_each_shell_command_gets_its_own_empty_parameter_folder(tmp_path, monkeypatch):
    """G4: `bash_exec` runs every command with a fresh HEASoft user folder in `PFILES` (the system
    half kept), so a learned value one command writes never reaches the next."""
    import auto_research.sandbox.builtin_server as bs

    monkeypatch.setenv("PFILES", f"{tmp_path / 'shared'};/opt/heasoft/syspfiles")
    # The declaration the server's launch records (graph.runtime._builtin_server_config).
    monkeypatch.setenv("AR_PRIVATE_FOLDERS", json.dumps({"PFILES": ";"}))
    first = bs.bash_exec('echo "$PFILES"; echo learned > "${PFILES%%;*}/fit.par"; ls "${PFILES%%;*}"')
    second = bs.bash_exec('echo "$PFILES"; ls "${PFILES%%;*}"')
    one, two = first["stdout"].split()[0], second["stdout"].split()[0]
    assert one != two and one.endswith(";/opt/heasoft/syspfiles") and two.endswith(";/opt/heasoft/syspfiles")
    assert "fit.par" in first["stdout"] and "fit.par" not in second["stdout"], (first, second)
    assert not Path(one.split(";")[0]).exists()  # removed after the command
    assert Path(one.split(";")[0]).parent == tmp_path / "shared"  # inside the process's own user half


def test_each_repl_call_gets_its_own_empty_parameter_folder(tmp_path, monkeypatch):
    """G4: the worker REPL gives every toolkit call a fresh parameter folder and puts its own back
    after the call; a value one call learns is absent in the next."""
    from auto_research.sandbox.persistent import PersistentSandbox

    from auto_research import env as app_env
    from auto_research.sandbox import jail

    if not jail.jail_available()[0]:
        pytest.skip("the OS jail is required and bwrap cannot run here")
    import sys

    # This interpreter stands in for a human environment, which the allow-list names read-only.
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [sys.prefix], "agent_fs_readonly": [sys.prefix]})
    monkeypatch.setattr(app_env, "_orch_config", lambda: {"env_externals": {"heasoft": {
        "root": "/opt/heasoft", "private": {"PFILES": ";"}}}})
    toolkit = tmp_path / "tk"
    (toolkit / "demo_pf").mkdir(parents=True)
    (toolkit / "demo_pf" / "__init__.py").write_text(
        "import os\n"
        "def learn():\n"
        "    user = os.environ['PFILES'].split(';')[0]\n"
        "    before = sorted(os.listdir(user))\n"
        "    open(os.path.join(user, 'fit.par'), 'w').write('learned')\n"
        "    return {'stdout': 'ok', 'pfiles': os.environ['PFILES'], 'before': before}\n")
    ini = tmp_path / "heasoft_init.sh"  # an activation that sets PFILES the way headas-init does
    ini.write_text(f'export PFILES="{tmp_path / "shared"};/opt/heasoft/syspfiles"\n')
    import sys

    from auto_research.sandbox.envs import Environment

    sandbox = PersistentSandbox(toolkit, tmp_path / "w",
                                environment=Environment(name="host", prefix=Path(sys.prefix), activate=ini))

    async def run():
        await sandbox.start()
        try:
            return [await sandbox.call_tool("demo_pf", "learn", {}) for _ in range(2)]
        finally:
            await sandbox.shutdown()

    first, second = asyncio.run(run())
    assert first["exit_code"] == 0 and second["exit_code"] == 0, (first, second)
    assert first["pfiles"] != second["pfiles"] and second["pfiles"].endswith(";/opt/heasoft/syspfiles")
    assert first["before"] == [] and second["before"] == []  # the learned value did not carry over
    own = Path(first["pfiles"].split(";")[0]).parent  # the REPL's private user half (F23), in its jail's /tmp
    assert own == Path("/tmp/pfiles"), own
