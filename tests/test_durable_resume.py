"""Deterministic tests for durable mid-run resume — Stage 1 (§12).

Two layers, no pip sqlite package and no real pipeline needed:
- The LangGraph checkpointer *mechanism* via the in-memory `MemorySaver` (ships with
  langgraph-checkpoint): a graph that dies at its 2nd node resumes from the 1st node's
  snapshot and finishes, proving node-level continuation (the earlier node does not re-run).
- `run_research_nohup`'s resume plumbing against a fake launcher: the overwrite/resume guard,
  a dead backend re-launching under `--resume`, a crashed (`failed`) run recovering, and the
  launcher adding `--resume` to its argv.

The live sqlite `AsyncSqliteSaver` path (real killed-and-resumed run) is exercised separately
and is NOT LIVE TESTED until the pip package is installed + a real run is killed and resumed.
"""

from __future__ import annotations

import asyncio
import json
import operator
from pathlib import Path
from typing import Annotated, TypedDict

import pytest

from auto_research.graph import detached as d
from auto_research.graph.detached import (
    RunDied,
    _proc_create_time,
    resolve_run_id,
    run_research_nohup,
)
from auto_research.schemas.review import ReviewVerdict


# ── The checkpointer mechanism (MemorySaver — no pip package needed) ──────────

class _S(TypedDict):
    ran: Annotated[list, operator.add]
    done: bool


def _build_toy_graph(saver, should_fail: dict):
    """A 2-node graph (a → b); node b raises while ``should_fail['v']`` is True."""
    from langgraph.graph import END, START, StateGraph

    def node_a(state: _S) -> dict:
        return {"ran": ["a"]}

    def node_b(state: _S) -> dict:
        if should_fail["v"]:
            raise RuntimeError("boom in b")
        return {"ran": ["b"], "done": True}

    builder = StateGraph(_S)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=saver)


def test_checkpointer_resumes_from_last_completed_node():
    """Die at node b, then resume: node a's snapshot is reused (a runs ONCE), b completes."""
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    should_fail = {"v": True}
    graph = _build_toy_graph(saver, should_fail)
    config = {"configurable": {"thread_id": "t1"}}

    async def run() -> _S:
        with pytest.raises(RuntimeError, match="boom in b"):
            await graph.ainvoke({"ran": [], "done": False}, config=config, durability="sync")
        # A checkpoint exists now (node a completed and was snapshotted).
        assert await saver.aget_tuple(config) is not None
        should_fail["v"] = False           # fix b, then resume with input=None
        return await graph.ainvoke(None, config=config, durability="sync")

    final = asyncio.run(run())
    assert final["ran"] == ["a", "b"]      # a ran exactly once (not ["a","a","b"]); b re-ran
    assert final["done"] is True


def test_real_sqlite_resume_survives_process_boundary(tmp_path):
    """The real AsyncSqliteSaver: a checkpoint written by one saver instance is resumed by a
    BRAND-NEW instance on the same file — i.e. it survives the process death MemorySaver can't
    model. Skips where the pip package isn't installed (deterministic suite stays hermetic)."""
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = str(tmp_path / "checkpoint.sqlite")
    cfg = {"configurable": {"thread_id": "t1"}}
    should_fail = {"v": True}

    async def run() -> _S:
        async with AsyncSqliteSaver.from_conn_string(path) as s1:   # "process 1"
            with pytest.raises(RuntimeError):
                await _build_toy_graph(s1, should_fail).ainvoke(
                    {"ran": [], "done": False}, config=cfg, durability="sync")
        assert Path(path).exists()                                  # checkpoint flushed to disk
        should_fail["v"] = False
        async with AsyncSqliteSaver.from_conn_string(path) as s2:   # "process 2" — fresh instance
            assert await s2.aget_tuple(cfg) is not None             # persisted across instances
            return await _build_toy_graph(s2, should_fail).ainvoke(
                None, config=cfg, durability="sync")

    final = asyncio.run(run())
    assert final["ran"] == ["a", "b"] and final["done"] is True


def test_checkpointer_absent_thread_has_no_tuple():
    """The resume branch-selector: a never-run thread_id has no checkpoint (→ fresh start)."""
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    graph = _build_toy_graph(saver, {"v": False})

    async def run():
        none_yet = await saver.aget_tuple({"configurable": {"thread_id": "never"}})
        await graph.ainvoke({"ran": [], "done": False},
                            config={"configurable": {"thread_id": "used"}}, durability="sync")
        after = await saver.aget_tuple({"configurable": {"thread_id": "used"}})
        return none_yet, after

    none_yet, after = asyncio.run(run())
    assert none_yet is None       # → run_research would start from initial_state
    assert after is not None      # → a completed run leaves a resumable checkpoint


# ── run_research_nohup resume plumbing (fake launcher) ────────────────────────

@pytest.fixture
def rundir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "run_dir", lambda rid: tmp_path / "runs" / rid)
    return lambda rid: tmp_path / "runs" / rid


def _write_pid(rd: Path, pid: int, create_time) -> None:
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "backend.pid").write_text(json.dumps({"pid": pid, "create_time": create_time}))


def _done_result(rationale: str) -> str:
    return json.dumps({
        "status": "done",
        "verdict": ReviewVerdict(accepted=True, rationale=rationale).model_dump(mode="json"),
    })


def test_overwrite_and_resume_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        asyncio.run(run_research_nohup("q", overwrite=True, resume=True))


def test_dead_backend_plus_resume_relaunches(rundir):
    rid = resolve_run_id("q_resume"); rd = rundir(rid)
    _write_pid(rd, 2, 1)                    # pid 2 with a bogus start-time → dead, no result.json
    (rd / "monitor.jsonl").write_text("")
    calls: list[dict] = []

    def _fake_launch(request, run_id, run_directory, **kw):
        calls.append(kw)
        run_directory.mkdir(parents=True, exist_ok=True)
        (run_directory / "result.json").write_text(_done_result("resumed"))
        return 4242

    run = asyncio.run(run_research_nohup("q_resume", resume=True, poll_s=0.01,
                                         _launcher=_fake_launch))
    assert run.verdict.rationale == "resumed"
    assert calls and calls[0].get("resume") is True    # re-launched under --resume


def test_dead_backend_without_resume_still_raises(rundir):
    rid = resolve_run_id("q_dead"); rd = rundir(rid)
    _write_pid(rd, 2, 1)                    # dead, no result.json
    (rd / "monitor.jsonl").write_text("")
    with pytest.raises(RunDied):
        asyncio.run(run_research_nohup("q_dead", resume=False, poll_s=0.01,
                                       _launcher=lambda *a, **k: None))


def test_failed_result_plus_resume_recovers(rundir):
    rid = resolve_run_id("q_failed"); rd = rundir(rid); rd.mkdir(parents=True)
    (rd / "result.json").write_text(json.dumps({"status": "failed", "error": "boom"}))
    (rd / "monitor.jsonl").write_text("")
    calls: list[dict] = []

    def _fake_launch(request, run_id, run_directory, **kw):
        calls.append(kw)
        (run_directory / "result.json").write_text(_done_result("recovered"))
        return 111

    run = asyncio.run(run_research_nohup("q_failed", resume=True, poll_s=0.01,
                                         _launcher=_fake_launch))
    assert run.verdict.rationale == "recovered"        # stale failed marker cleared + relaunched
    assert calls[0].get("resume") is True


def test_default_launcher_adds_resume_flag(rundir, monkeypatch):
    captured: dict = {}

    def _fake_spawn(argv, log):
        captured["argv"] = argv
        return 4321

    monkeypatch.setattr(d, "_spawn_orphaned", _fake_spawn)
    monkeypatch.setattr(d, "_proc_create_time", lambda pid: 123)

    d._default_launcher("q", "rid_r", rundir("rid_r"), chatter=5, report_formats=None,
                        human_channel="none", max_iterations=None, resume=True)
    assert "--resume" in captured["argv"]

    d._default_launcher("q", "rid_n", rundir("rid_n"), chatter=5, report_formats=None,
                        human_channel="none", max_iterations=None, resume=False)
    assert "--resume" not in captured["argv"]
