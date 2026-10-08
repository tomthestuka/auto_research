"""Session-wide test options. One of them: run any live test against any seat assignment.

Every live test that drives a real role resolves its model the way production does, so it measures
whatever the machine-local configuration names. ``--seats`` (or ``AUTO_RESEARCH_SEATS``) points the
whole session at a configuration directory whose ``models.yaml`` names different members, leaving
every other configured fact byte-identical. No test needs to know::

    AUTO_RESEARCH_SEATS='analyst_lead=openai:gpt-5.6-terra,reviewer=dashscope:qwen3.8-max' \\
        AUTO_RESEARCH_LIVE_FIGURE_TESTS=1 pytest tests/test_live_figure_vision.py -s

    pytest tests/test_live_read_file.py \\
        --seats 'analyst_worker=anthropic:claude-sonnet-5|dashscope:qwen3.7-plus'

**Absent, this file does nothing at all** — no directory is written, the environment is untouched,
and the suite resolves the repository's own ``config/`` exactly as before. That is deliberate: a
conftest is in front of every test in the tree, so its inactive path has to be a no-op rather than
a subtly different default. ``tests/test_seat_overrides.py`` pins both halves.

The override is applied for the whole session rather than per test because the seam is an
environment variable that subprocesses inherit: a sandbox REPL, an author harness or a gate test
runner spawned mid-test reads it too, and switching it under a running child would configure the
parent and the child differently.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from auto_research.config_paths import CONFIG_DIR_ENV
from tests.seats import SEATS_ENV, parse_seats, write_config_dir


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--seats",
        action="store",
        default=None,
        metavar="ROLE=PROVIDER:MODEL[,...]",
        help=(
            "Override which model sits in which role for this session, e.g. "
            "'analyst_lead=openai:gpt-5.6-terra,analyst_worker=anthropic:claude-sonnet-5|"
            "dashscope:qwen3.7-plus'. Members of one role are separated by '|'; a member may "
            "carry '!novision' or '!nodocs'. Overrides only models.yaml; every other configured "
            "fact is copied unchanged."
        ),
    )


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Print the seat assignment in the header, so a logged run states what it measured."""
    spec = config.getoption("--seats") or os.environ.get(SEATS_ENV)
    if not spec:
        return []
    return [f"seats: {spec}"]


@pytest.fixture(scope="session", autouse=True)
def _seat_overrides(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path | None]:
    """Point the session at a configuration directory with the requested seats, or do nothing.

    Yields the directory in use, or ``None`` when no override was asked for, so a test that wants
    to assert on what it is running against can request this fixture by name.
    """
    spec = request.config.getoption("--seats") or os.environ.get(SEATS_ENV)
    if not spec:
        yield None
        return

    overrides = parse_seats(spec)
    directory = write_config_dir(tmp_path_factory.mktemp("seats"), overrides)
    previous = os.environ.get(CONFIG_DIR_ENV)
    os.environ[CONFIG_DIR_ENV] = str(directory)
    try:
        yield directory
    finally:
        if previous is None:
            os.environ.pop(CONFIG_DIR_ENV, None)
        else:
            os.environ[CONFIG_DIR_ENV] = previous


@pytest.fixture(autouse=True)
def _agent_conda_dir_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every build a test makes lands in a temporary folder, never the user's runtime dir (the gate
    builds the environment a judged package names, env plan B3c-2). A test may point it elsewhere."""
    from auto_research import env

    folder = tmp_path_factory.mktemp("agent-conda")
    monkeypatch.setattr(env, "agent_conda_dir", lambda: folder)
    monkeypatch.setattr(env, "env_lock_dir", lambda: folder / "locks")  # a real REPL writes its lock
