"""Session-wide test options: which tests run, and which seats a live test measures.

**Live tests are opt-in.** A test marked ``live`` calls a real provider, the network or an external
service, and is skipped unless ``--live`` is passed; the plain run is the deterministic suite. Mark a
whole module with ``pytestmark = pytest.mark.live``, or one test with ``@pytest.mark.live`` where a
module mixes both. Spend is chosen by path and ``-k``::

    pytest tests/test_live_channel.py --live -s -k matrix

``-m "not live"`` drops the live tests from the report instead of skipping them.

**Seats.** Every live test that drives a real role resolves its model the way production does, so
it measures whatever the machine-local configuration names. ``--seats`` (or ``AUTO_RESEARCH_SEATS``) points the
whole session at a configuration directory whose ``models.yaml`` names different members, leaving
every other configured fact byte-identical. No test needs to know::

    AUTO_RESEARCH_SEATS='analyst_lead=openai:gpt-5.6-terra,reviewer=dashscope:qwen3.8-max' \\
        pytest tests/test_live_figure_vision.py --live -s

    pytest tests/test_live_read_file.py --live \\
        --seats 'analyst_worker=anthropic:claude-sonnet-5|dashscope:qwen3.7-plus'

**Absent, the seat override does nothing at all** — no directory is written, and the suite
resolves the repository's own ``config/`` exactly as before. (The isolation fixtures below always
run: the runtime dir, agent environments and the parse cache live in temporary folders.) That is deliberate: a
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
            "dashscope:qwen3.7-plus'. Members of one role are separated by '|'. Overrides only "
            "models.yaml; every other configured fact is copied unchanged."
        ),
    )
    parser.addoption("--live", action="store_true", default=False,
                     help="also run the tests marked live (real providers, network, external services)")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: calls a real provider, network or external service; opt-in with --live")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every test marked ``live`` unless ``--live`` was passed (the one rule naming a live test)."""
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live: run with --live")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)


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


@pytest.fixture(scope="session")
def _session_runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("runtime")


@pytest.fixture(autouse=True)
def _runtime_dir_in_tmp(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, _session_runtime_dir: Path
) -> Path | None:
    """A deterministic test's runtime dir is a temporary one, never the configured ``runtime_dir``
    (D4). Set as the environment variable ``env.runtime_data_dir`` honours, so subprocesses a test
    starts inherit it; a test may point it at its own folder with ``monkeypatch.setenv``.

    A live test (marked ``live``) keeps the configured runtime dir: it runs against the machine's
    built environments, the jail's allow-list names that dir's ``runs`` root, and its run
    directories are there for a human to inspect.
    """
    if request.node.get_closest_marker("live"):
        return None
    from auto_research.env import RUNTIME_DIR_ENV

    monkeypatch.setenv(RUNTIME_DIR_ENV, str(_session_runtime_dir))
    return _session_runtime_dir


@pytest.fixture(autouse=True)
def _agent_conda_dir_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every build a test makes lands in a temporary folder, never the user's runtime dir (the gate
    builds the environment a judged package names, env plan B3c-2). A test may point it elsewhere."""
    from auto_research import env

    folder = tmp_path_factory.mktemp("agent-conda")
    monkeypatch.setattr(env, "agent_conda_dir", lambda: folder)
    monkeypatch.setattr(env, "env_lock_dir", lambda: folder / "locks")  # a real REPL writes its lock


@pytest.fixture(autouse=True)
def _private_parse_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each test parses documents into its own cache, never the configured runtime dir (A2: every
    member without a native document channel is sent the local parse)."""
    monkeypatch.setattr("auto_research.models.providers._doc_parse.cache_root",
                        lambda: (tmp_path / "parsed_docs").resolve())


#: Stand-in model ids the deterministic tests build members with (``fake``, ``fake-model``, ``m``, ...),
#: and what each provider's stand-in takes: the capability those tests were written against, the
#: same as the provider's real models in ``config/model_capabilities.yaml``.
_STAND_IN_MODELS = ("fake", "fake-model", "fake-lib", "fake-rev", "fake-lead", "m")
_STAND_IN_CAPABILITY = {
    "anthropic": (True, True), "openai": (True, True), "google": (True, True),
    "dashscope": (True, True), "deepseek": (False, False), "zai": (False, False),
}


@pytest.fixture(scope="session")
def _stand_in_capability_table(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import yaml

    from auto_research.config_paths import model_capabilities_yaml

    table = yaml.safe_load(model_capabilities_yaml().read_text(encoding="utf-8")) or {}
    for provider, (images, documents) in _STAND_IN_CAPABILITY.items():
        for model in _STAND_IN_MODELS:
            table.setdefault(provider, {}).setdefault(model, {"images": images, "documents": documents})
    path = tmp_path_factory.mktemp("capabilities") / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump(table, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _stand_in_model_capabilities(
    _stand_in_capability_table: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real capability table plus entries for the stand-in ids (Z1d): every real entry is read
    as configured, and a model that is neither real nor a stand-in still has no entry."""
    monkeypatch.setattr("auto_research.models.capabilities.model_capabilities_yaml",
                        lambda: _stand_in_capability_table)
