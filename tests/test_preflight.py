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
    assert "toolkit_root_configured" in names
    assert "toolkit_git_identity" in names
    assert "toolkit_branch_clean" in names
    # toolkit_root must be validated before the git-identity check that assumes it exists.
    assert names.index("toolkit_root_configured") < names.index("toolkit_git_identity")
    # The human-channel ping is only registered when a handler is resolved.
    assert "human_channel" not in names


def test_check_toolkit_root_configured(tmp_path, monkeypatch):
    import auto_research.sandbox.merge_flow as mf

    # Configured + existing → passes.
    monkeypatch.setattr(mf, "_orchestration_config", lambda: {"toolkit_root": str(tmp_path)})
    mf.check_toolkit_root_configured()

    # Missing key → fails loudly naming the key.
    monkeypatch.setattr(mf, "_orchestration_config", lambda: {})
    with pytest.raises(RuntimeError, match="toolkit_root is not set"):
        mf.check_toolkit_root_configured()

    # Set but not a directory → fails.
    monkeypatch.setattr(
        mf, "_orchestration_config", lambda: {"toolkit_root": str(tmp_path / "nope")}
    )
    with pytest.raises(RuntimeError, match="not a directory"):
        mf.check_toolkit_root_configured()


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


class _RecordingMonitor:
    """Minimal RunMonitor stand-in: captures (event, level, data) tuples."""

    def __init__(self, fail_on: str | None = None):
        self.events: list[tuple[str, int, dict]] = []
        self._fail_on = fail_on

    async def record(self, team, event, *, level, summary, actor=None, data=None):
        if self._fail_on and self._fail_on in event:
            raise RuntimeError("monitor is broken")
        self.events.append((event, level, dict(data or {})))


def test_preflight_records_every_check_onto_the_monitor():
    """The launch checklist belongs in the run ledger, not only terminal scrollback ([J1]a)."""
    preflight = Preflight()
    preflight.register("good", lambda: None)
    monitor = _RecordingMonitor()

    asyncio.run(preflight.run_checklist(monitor=monitor))

    names = [e for e, _, _ in monitor.events]
    assert names == ["preflight.good", "preflight.summary"]
    assert monitor.events[0][1] == 5           # a passing check is detail-level
    assert monitor.events[0][2]["ok"] is True
    assert monitor.events[1][2] == {"passed": 1, "total": 1, "failed": []}


def test_preflight_records_failures_at_level_one_and_still_raises():
    """A failed check kills the launch, so it is recorded at the most significant level."""
    preflight = Preflight()
    preflight.register("good", lambda: None)

    def _bad():
        raise ValueError("nope")

    preflight.register("bad", _bad)
    monitor = _RecordingMonitor()

    with pytest.raises(PreflightError):
        asyncio.run(preflight.run_checklist(monitor=monitor))

    by_name = {e: (lvl, data) for e, lvl, data in monitor.events}
    assert by_name["preflight.good"][0] == 5
    assert by_name["preflight.bad"][0] == 1          # launch-killing
    assert by_name["preflight.bad"][1]["ok"] is False
    assert "nope" in by_name["preflight.bad"][1]["error"]
    # the tally is emitted even on failure, so the record shows how far it got
    assert by_name["preflight.summary"][1] == {"passed": 1, "total": 2, "failed": ["bad"]}


def test_preflight_monitor_fault_never_blocks_the_launch_decision():
    """Observability must not become the thing that stops a runnable launch ([J2] rationale)."""
    preflight = Preflight()
    preflight.register("good", lambda: None)

    asyncio.run(preflight.run_checklist(monitor=_RecordingMonitor(fail_on="preflight.")))

    # ...and a genuinely failing check still raises, monitor fault or not.
    def _bad():
        raise ValueError("nope")

    preflight.register("bad", _bad)
    with pytest.raises(PreflightError):
        asyncio.run(preflight.run_checklist(monitor=_RecordingMonitor(fail_on="preflight.")))


def test_preflight_without_monitor_is_unchanged():
    """Standalone callers (setup.py's smoke test) keep working with no monitor at all."""
    preflight = Preflight()
    preflight.register("good", lambda: None)
    asyncio.run(preflight.run_checklist())
