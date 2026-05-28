"""Deterministic tests for sandbox/runner.py, sandbox/registry.py, and sandbox/merge_flow.py."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from auto_research.sandbox.merge_flow import propose_and_pr
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.runner import run_tool
from auto_research.schemas import ToolCallRequest
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
# Graph routing (error-feedback routing logic, no LLM)
# ---------------------------------------------------------------------------

class TestGraphToolRouting:
    """Verify tool_dispatch_node routing logic with mocked workers."""

    def test_unknown_tool_produces_unavailable_result(self, tmp_path: Path) -> None:
        """tool_dispatch_node: unknown tool → ToolResult exit_code 127 in tool_results."""
        from auto_research.graph.builder import build_graph
        from auto_research.schemas import ResearchRequest, AnalysisTask

        reg = _make_registry(tmp_path, manifest={})  # empty: all tools unknown

        # Stub worker that requests a tool the registry doesn't know.
        class StubWorker:
            async def analyze(self, task: Any, *, peer_reports: Any = ()) -> Any:
                from auto_research.schemas import AnalystReport
                return AnalystReport(summary="stub", findings=[])

            async def request_tools(self, task: Any) -> list[ToolCallRequest]:
                return [ToolCallRequest(
                    requested_by="worker",
                    kind="nonexistent_tool",
                    purpose="testing",
                )]

        class StubLead:
            async def judge_convergence(self, task, reports, *, round_index, max_rounds):
                from auto_research.schemas import ReviewVerdict
                return ReviewVerdict(accepted=True, rationale="ok")

            async def adjudicate(self, task, reports):
                from auto_research.schemas import AnalystReport
                return AnalystReport(summary="adjudicated", findings=[])

        class StubReviewer:
            async def review(self, request, *, literature=(), report=None):
                from auto_research.schemas import ReviewVerdict
                return ReviewVerdict(accepted=True, rationale="ok")

        class StubIO:
            async def compose_search_strategy(self, request, *, follow_up=None):
                from auto_research.schemas import SearchRequest
                return SearchRequest(query="test")

            async def search(self, request):
                return []

            async def ingest_results(self, results):
                pass

        class StubRetrieval:
            async def text_search(self, query, *, limit=10):
                return []

        import asyncio

        async def _run():
            graph = build_graph(
                reviewer=StubReviewer(),
                io_agent=StubIO(),
                retrieval=StubRetrieval(),
                workers=[StubWorker()],
                lead=StubLead(),
                max_iterations=1,
                registry=reg,
                tool_retry_cap=1,
            )
            state = await graph.ainvoke({
                "request": ResearchRequest(question="test"),
                "literature": [],
                "report": None,
                "verdict": None,
                "lit_iterations": 0,
                "analysis_iterations": 0,
                "max_iterations": 1,
                "tool_results": [],
                "tool_queue": [],
            })
            return state

        final = asyncio.run(_run())
        tool_results: list[ToolResult] = final.get("tool_results", [])
        unavailable = [r for r in tool_results if r.exit_code == 127]
        assert len(unavailable) == 1
        assert "nonexistent_tool" in unavailable[0].tool_name

    def test_retry_cap_stops_loop(self, tmp_path: Path) -> None:
        """tool_execution_node: retry loop stops at tool_retry_cap failures."""
        from auto_research.sandbox.runner import run_tool as _run_tool

        call_count = 0

        async def failing_run_tool(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return ToolResult(
                tool_name="fail_tool",
                stdout="",
                stderr="simulated failure",
                exit_code=1,
                elapsed_s=0.0,
            )

        # A worker that requests the same tool every time (even on error retry).
        class RetryWorker:
            async def analyze(self, task, *, peer_reports=()):
                from auto_research.schemas import AnalystReport
                return AnalystReport(summary="stub", findings=[])

            async def request_tools(self, task):
                return [ToolCallRequest(
                    requested_by="worker",
                    kind="echo_tool",
                    purpose="testing retry",
                )]

        class StubLead:
            async def judge_convergence(self, task, reports, *, round_index, max_rounds):
                from auto_research.schemas import ReviewVerdict
                return ReviewVerdict(accepted=True, rationale="ok")

            async def adjudicate(self, task, reports):
                from auto_research.schemas import AnalystReport
                return AnalystReport(summary="adjudicated", findings=[])

        class StubReviewer:
            async def review(self, request, *, literature=(), report=None):
                from auto_research.schemas import ReviewVerdict
                return ReviewVerdict(accepted=True, rationale="ok")

        class StubIO:
            async def compose_search_strategy(self, request, *, follow_up=None):
                from auto_research.schemas import SearchRequest
                return SearchRequest(query="test")

            async def search(self, request):
                return []

            async def ingest_results(self, results):
                pass

        class StubRetrieval:
            async def text_search(self, query, *, limit=10):
                return []

        retry_cap = 2
        reg = _make_registry(tmp_path)

        import asyncio
        from auto_research.graph.builder import build_graph
        from auto_research.schemas import ResearchRequest

        with patch("auto_research.graph.builder.run_tool", side_effect=failing_run_tool):
            async def _run():
                graph = build_graph(
                    reviewer=StubReviewer(),
                    io_agent=StubIO(),
                    retrieval=StubRetrieval(),
                    workers=[RetryWorker()],
                    lead=StubLead(),
                    max_iterations=1,
                    registry=reg,
                    tool_retry_cap=retry_cap,
                )
                return await graph.ainvoke({
                    "request": ResearchRequest(question="test"),
                    "literature": [],
                    "report": None,
                    "verdict": None,
                    "lit_iterations": 0,
                    "analysis_iterations": 0,
                    "max_iterations": 1,
                    "tool_results": [],
                    "tool_queue": [],
                })

            asyncio.run(_run())

        # Each attempt calls run_tool once; retry_cap attempts total.
        assert call_count == retry_cap
