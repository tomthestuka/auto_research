"""The configuration directory is a concept, and every reader agrees on it.

Before this seam existed, eleven modules each computed the location of the four config files
relative to their own source file, six of them reading the orchestration file independently and
four caching the answer for the life of the process. Nothing was wrong with that until something
needed the pipeline pointed at a different configuration, at which point patching one reader left
five others quietly reading the original -- a failure that produces a *plausible* run rather than
an error.

So what is pinned here is not the helper (it is nine lines) but the property the helper exists to
create: **unset behaves exactly as before, and set is obeyed by every reader**. The second half is
asserted reader by reader, because a reader that was missed is invisible from the outside.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from auto_research import config_paths


@pytest.fixture
def elsewhere(tmp_path, monkeypatch) -> Path:
    """A throwaway configuration directory this process is pointed at."""
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(tmp_path))
    return tmp_path


# ── unset: the default is unchanged ─────────────────────────────────────────


def test_unset_resolves_to_the_repository_config(monkeypatch):
    monkeypatch.delenv(config_paths.CONFIG_DIR_ENV, raising=False)
    assert config_paths.config_dir() == config_paths.repo_config_dir()
    assert config_paths.config_dir().name == "config"


def test_the_four_files_are_named_where_the_project_has_always_kept_them(monkeypatch):
    monkeypatch.delenv(config_paths.CONFIG_DIR_ENV, raising=False)
    repo = config_paths.repo_config_dir()
    assert config_paths.orchestration_yaml() == repo / "orchestration.yaml"
    assert config_paths.models_yaml() == repo / "models.yaml"
    assert config_paths.observations_yaml() == repo / "observations.yaml"
    assert config_paths.code_gate_yaml() == repo / "code_gate.yaml"


def test_the_live_repository_configuration_is_actually_there(monkeypatch):
    """A machine-local guard: the four files are gitignored, so a fresh clone skips this."""
    monkeypatch.delenv(config_paths.CONFIG_DIR_ENV, raising=False)
    if not config_paths.orchestration_yaml().exists():
        pytest.skip("no live configuration on this machine")
    assert config_paths.models_yaml().exists()
    assert config_paths.code_gate_yaml().exists()


# ── set: resolved per call, not captured at import ──────────────────────────


def test_the_override_is_read_on_every_call(elsewhere):
    assert config_paths.config_dir() == elsewhere
    assert config_paths.orchestration_yaml() == elsewhere / "orchestration.yaml"


def test_setting_it_after_import_still_takes_effect(tmp_path, monkeypatch):
    """The whole point: every module here was imported long before the variable was set.

    A default argument or a module constant would have pinned the answer at import time, which is
    exactly the shape the seam replaces.
    """
    monkeypatch.delenv(config_paths.CONFIG_DIR_ENV, raising=False)
    assert config_paths.config_dir() == config_paths.repo_config_dir()
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(tmp_path))
    assert config_paths.config_dir() == tmp_path


def test_a_user_path_is_expanded(monkeypatch):
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, "~/somewhere")
    assert "~" not in str(config_paths.config_dir())


# ── every reader obeys it ───────────────────────────────────────────────────
#
# One test per reader, because the failure mode is a reader nobody remembered, and that is not
# observable from the outside: the run simply uses the wrong configuration and looks fine.

_ORCH = "runtime_dir: /tmp/elsewhere-runtime\ntoolkit_root: /tmp/elsewhere-toolkit\nmax_iterations: 7\nreact_loop_cap: 11\nprovider_retry_max: 2\nhuman_channel: none\ngate_test_timeout_s: 42\ntool_author_cap: 9\nagent_fs_allow:\n  - /tmp/elsewhere-toolkit\n"


def _write_orchestration(directory: Path) -> None:
    (directory / "orchestration.yaml").write_text(_ORCH, encoding="utf-8")


def test_the_graph_runtime_reads_it(elsewhere):
    from auto_research.graph.runtime import load_orchestration_config

    _write_orchestration(elsewhere)
    assert load_orchestration_config()["max_iterations"] == 7


def test_env_paths_read_it(elsewhere):
    from auto_research import env

    _write_orchestration(elsewhere)
    assert env.runtime_data_dir() == Path("/tmp/elsewhere-runtime")
    assert env.toolkit_root() == Path("/tmp/elsewhere-toolkit")
    assert env.provider_retry_policy().max_retries == 2


def test_the_filesystem_allow_list_reads_it(elsewhere):
    """It names the private roots a differently-configured run must be allowed to touch, so a
    reader that missed the override would deny the very paths the run was given."""
    from auto_research.sandbox.fs_guard import load_fs_allowlist

    _write_orchestration(elsewhere)
    assert load_fs_allowlist() == [Path("/tmp/elsewhere-toolkit")]


def test_the_react_caps_read_it(elsewhere):
    from auto_research.agents import roles

    _write_orchestration(elsewhere)
    roles._load_orchestration.cache_clear()
    assert roles._orchestration_config()["react_loop_cap"] == 11


def test_the_authoring_flow_reads_it(elsewhere):
    from auto_research.sandbox import merge_flow

    _write_orchestration(elsewhere)
    merge_flow._load_orchestration.cache_clear()
    assert merge_flow._orchestration_config()["tool_author_cap"] == 9


def test_the_gate_reads_both_of_its_files(elsewhere):
    from auto_research.sandbox import gate

    _write_orchestration(elsewhere)
    (elsewhere / "code_gate.yaml").write_text("banned_phrases:\n  - elsewhere-only\n", encoding="utf-8")
    gate._load_yaml.cache_clear()
    assert gate._orchestration_config()["gate_test_timeout_s"] == 42
    assert gate._code_gate_config()["banned_phrases"] == ["elsewhere-only"]


def test_the_human_channel_resolver_reads_it(elsewhere):
    from auto_research.hitl import resolve as hitl_resolve

    _write_orchestration(elsewhere)
    hitl_resolve._load_orchestration.cache_clear()
    assert hitl_resolve._orchestration_config()["human_channel"] == "none"


def test_the_analysis_stack_probe_reads_it(elsewhere):
    """No ``analyze_probe`` configured is a documented no-op, so reaching it without raising is
    the observation: it read this directory rather than the repository's."""
    from auto_research.sandbox.runner import check_analyze_probe

    _write_orchestration(elsewhere)
    check_analyze_probe()


def test_the_model_registry_reads_it(elsewhere):
    """The role list is DERIVED from `REQUIRED_ROLES`, never written out here.

    A hand-written list makes a valid configuration fail the check that exists to catch an
    invalid one: adding `compactor` to the required set broke this test while the config it
    validates was correct (the same shape as the 2026-09-09 `_VALID_PROVIDERS` literal).
    """
    from auto_research.models import registry
    from auto_research.models.registry import REQUIRED_ROLES

    def _member(role: str) -> str:
        # The coder member names a harness as well as a model; every other role is a plain pair.
        extra = ", harness: claude_sdk" if role == "coder" else ""
        provider = "anthropic" if role == "coder" else "openai"
        return (
            f"  {role}: {{members: [{{name: n-{role}, provider: {provider}, "
            f"model: m-{role}{extra}}}]}}"
        )

    (elsewhere / "models.yaml").write_text(
        "roles:\n" + "\n".join(_member(r) for r in sorted(REQUIRED_ROLES)) + "\n",
        encoding="utf-8",
    )
    assert registry.resolve("analyst_lead").handles[0].config.model == "m-analyst_lead"


def test_the_observation_database_location_reads_it(elsewhere):
    from auto_research import observations

    (elsewhere / "observations.yaml").write_text(
        f"obs_db_root: {elsewhere / 'obsdb'}\n", encoding="utf-8"
    )
    (elsewhere / "obsdb").mkdir()
    assert observations.load_observations_config().obs_db_root == elsewhere / "obsdb"


def test_the_cost_plotter_reads_it(elsewhere):
    from auto_research.observability import plot_run

    (elsewhere / "models.yaml").write_text("roles: {}\n", encoding="utf-8")
    assert plot_run._load_models_yaml() == {"roles": {}}


# ── the caches key on the path they read ────────────────────────────────────


def test_a_cached_reader_is_not_served_another_directory_s_answer(tmp_path, monkeypatch):
    """Four readers cache their file for the life of the process. Keyed on the resolved path, so
    two directories are two entries rather than one stale one."""
    from auto_research.agents import roles

    first, second = tmp_path / "a", tmp_path / "b"
    for directory, cap in ((first, 11), (second, 22)):
        directory.mkdir()
        (directory / "orchestration.yaml").write_text(f"react_loop_cap: {cap}\n", encoding="utf-8")

    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(first))
    assert roles._orchestration_config()["react_loop_cap"] == 11
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(second))
    assert roles._orchestration_config()["react_loop_cap"] == 22


# ── nothing is left hardwired ───────────────────────────────────────────────


def test_no_module_still_computes_a_config_path_of_its_own():
    """The seam is only worth having if it is the only way to name the directory.

    ``config_paths`` itself holds the one remaining literal, which is the default; the installer is
    excluded because it writes the live configuration *from* templates that live in the repository
    whatever a run is configured with.
    """
    package = Path(config_paths.__file__).parent
    offenders = [
        path.relative_to(package)
        for path in package.rglob("*.py")
        if path.name not in {"config_paths.py", "setup.py"}
        and '/ "config"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
