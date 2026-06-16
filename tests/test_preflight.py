"""Deterministic tests for the launch-time preflight checklist.

The ``Preflight`` mechanics are tested with fake checks (no env/LLM); the real
author↔reviewer decorrelation check is exercised against the project config; the
human-channel ping is exercised with fake handlers.
"""

import asyncio

import pytest

from auto_research.preflight import (
    Preflight,
    PreflightError,
    build_default_preflight,
)
from auto_research.sandbox.review import check_author_reviewer_decorrelation
from auto_research.schemas import HumanQuery, HumanResponse


def test_run_checklist_runs_every_check_in_order():
    order = []
    p = Preflight()
    p.register("a", lambda: order.append("a"))
    p.register("b", lambda: order.append("b"))

    asyncio.run(p.run_checklist())

    assert order == ["a", "b"]
    assert p.names() == ["a", "b"]


def test_run_checklist_awaits_async_checks():
    seen = []

    async def async_check():
        seen.append("async")

    p = Preflight()
    p.register("sync", lambda: seen.append("sync"))
    p.register("async", async_check)

    asyncio.run(p.run_checklist())
    assert seen == ["sync", "async"]


def test_run_checklist_aggregates_all_failures():
    p = Preflight()

    def bad_env():
        raise RuntimeError("missing OPENAI_API_KEY")

    async def bad_channel():
        raise RuntimeError("channel did not reply")

    p.register("required_env", bad_env)
    p.register("ok", lambda: None)
    p.register("human_channel", bad_channel)

    with pytest.raises(PreflightError) as excinfo:
        asyncio.run(p.run_checklist())

    msg = str(excinfo.value)
    assert "[required_env] missing OPENAI_API_KEY" in msg
    assert "[human_channel] channel did not reply" in msg


def test_run_checklist_passes_silently_when_all_ok():
    p = Preflight()
    p.register("noop", lambda: None)
    assert asyncio.run(p.run_checklist()) is None


def test_build_default_preflight_registers_known_checks():
    names = build_default_preflight().names()
    assert "required_env" in names
    assert "author_reviewer_decorrelation" in names
    assert "toolkit_git_identity" in names
    # The human-channel ping is only registered when a handler is resolved.
    assert "human_channel" not in names


def test_build_default_preflight_registers_channel_check_when_handler_present():
    async def handler(query: HumanQuery) -> HumanResponse:
        return HumanResponse(query_id=query.id, answer="ack")

    names = build_default_preflight(human_handler=handler).names()
    assert "human_channel" in names


def test_human_channel_check_green_only_when_reply_returns():
    from auto_research.hitl import make_human_channel_check

    async def replier(query: HumanQuery) -> HumanResponse:
        return HumanResponse(query_id=query.id, answer="live")

    async def empty(query: HumanQuery) -> HumanResponse:
        return HumanResponse(query_id=query.id, answer="   ")

    # A real reply → green.
    asyncio.run(make_human_channel_check(replier)())
    # An empty reply → fail.
    with pytest.raises(RuntimeError, match="empty reply"):
        asyncio.run(make_human_channel_check(empty)())
    # A timeout → fail.
    async def slow(query: HumanQuery) -> HumanResponse:
        await asyncio.sleep(1)
        return HumanResponse(query_id=query.id, answer="late")

    with pytest.raises(RuntimeError, match="did not reply"):
        asyncio.run(make_human_channel_check(slow, timeout_s=0.05)())


def test_author_reviewer_decorrelation_passes_on_project_config():
    # Project config: coder = anthropic, analyst_colead = openai → decorrelated. No LLM call.
    check_author_reviewer_decorrelation()


def test_toolkit_git_identity_passes_when_set(tmp_path, monkeypatch):
    import subprocess

    from auto_research.sandbox import merge_flow

    repo = tmp_path / "research_toolkit"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "auto_research"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "auto-research@localhost"], cwd=repo, check=True)
    monkeypatch.setattr(merge_flow, "_resolve_toolkit_root", lambda: repo)
    merge_flow.check_toolkit_git_identity()  # no raise


def test_toolkit_git_identity_raises_when_unset(tmp_path, monkeypatch):
    import subprocess

    from auto_research.sandbox import merge_flow

    repo = tmp_path / "research_toolkit"
    repo.mkdir()
    # A repo-local config with no identity; global/system config redirected to empty files
    # so an ambient identity on the test host can't satisfy the check.
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty_global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "empty_system"))
    monkeypatch.setattr(merge_flow, "_resolve_toolkit_root", lambda: repo)
    with pytest.raises(RuntimeError, match="no git commit identity"):
        merge_flow.check_toolkit_git_identity()
