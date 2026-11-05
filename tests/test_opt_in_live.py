"""Live tests are opt-in: the ``live`` marker and ``--live`` (``tests/conftest.py``, user ruling 2026-11-05).

Each case runs pytest in a subprocess on a tiny test tree whose conftest imports the hooks and the
runtime-dir fixture of ``tests/conftest.py`` itself, so the rule under test is the one the suite
runs under.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from auto_research.env import RUNTIME_DIR_ENV

REPO = Path(__file__).resolve().parents[1]
CONFIGURED = "configured-runtime-dir"  # what the fixture tree's tests find when the fixture leaves the env alone

_CONFTEST = (
    "from tests.conftest import (_runtime_dir_in_tmp, _session_runtime_dir, pytest_addoption,\n"
    "                            pytest_collection_modifyitems, pytest_configure)\n"
)
_LIVE_MODULE = f"""import os
import pytest

pytestmark = pytest.mark.live


def test_spends():
    assert os.environ["{RUNTIME_DIR_ENV}"] == "{CONFIGURED}"
    raise AssertionError("a live test ran")
"""
_MIXED_MODULE = f"""import os
import pytest


def test_deterministic():
    assert os.environ["{RUNTIME_DIR_ENV}"] != "{CONFIGURED}"


@pytest.mark.live
def test_one_live_cell():
    assert os.environ["{RUNTIME_DIR_ENV}"] == "{CONFIGURED}"
    raise AssertionError("a live test ran")
"""


def _outcomes(tmp_path: Path, *args: str) -> dict[str, tuple[str, str]]:
    """Run the fixture tree with ``args``; return ``{test name: (outcome, message)}`` from its JUnit XML."""
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_live_fixture.py").write_text(_LIVE_MODULE)
    (tmp_path / "test_mixed_fixture.py").write_text(_MIXED_MODULE)
    report = tmp_path / "report.xml"
    env = {**os.environ, RUNTIME_DIR_ENV: CONFIGURED,
           "PYTHONPATH": os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH")) if p)}
    env.pop("PYTEST_ADDOPTS", None)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--strict-markers", "--rootdir",
         str(tmp_path), f"--junitxml={report}", str(tmp_path), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert report.is_file(), done.stdout[-2000:] + done.stderr[-2000:]
    outcomes = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        child = next((c for c in case if c.tag in ("skipped", "failure", "error")), None)
        outcomes[case.get("name")] = ("passed", "") if child is None else (child.tag, child.get("message", ""))
    return outcomes


def test_without_live_every_live_test_is_skipped_naming_the_option(tmp_path):
    outcomes = _outcomes(tmp_path)
    assert outcomes["test_deterministic"] == ("passed", ""), outcomes
    for name in ("test_spends", "test_one_live_cell"):
        outcome, message = outcomes[name]
        assert outcome == "skipped" and "--live" in message, (name, outcomes[name])


def test_with_live_the_live_tests_run_on_the_configured_runtime_dir(tmp_path):
    outcomes = _outcomes(tmp_path, "--live")
    assert outcomes["test_deterministic"] == ("passed", ""), outcomes
    for name in ("test_spends", "test_one_live_cell"):
        outcome, message = outcomes[name]
        assert outcome == "failure" and "a live test ran" in message, (name, outcomes[name])


def test_not_live_selects_only_the_deterministic_tests(tmp_path):
    """``-m "not live"`` under ``--strict-markers``: the marker is registered, and only the
    deterministic test is collected."""
    assert _outcomes(tmp_path, "-m", "not live") == {"test_deterministic": ("passed", "")}


#: Modules that mix deterministic and live tests, each with the live tests it marks one by one.
_MIXED = {
    "test_live_pressure_cells": ["test_pressure_cell"],
    "test_env_build": ["test_live_a_lock_exported_by_conda_rebuilds_exactly"],
    "test_env_tools": ["test_live_solve_then_build_then_activate",
                       "test_live_a_recorded_state_replays_and_matches_its_conda_part"],
}
#: Live modules not named ``test_live_*``.
_LIVE_ELSEWHERE = ["test_full_suite", "test_sandbox_isolation", "test_smoke"]
_LIVE_MODULES = sorted({p.stem for p in (REPO / "tests").glob("test_live_*.py")} | set(_LIVE_ELSEWHERE) | set(_MIXED))


def _marked(obj) -> bool:
    marks = getattr(obj, "pytestmark", [])
    return any(m.name == "live" for m in (marks if isinstance(marks, list) else [marks]))


@pytest.mark.parametrize("stem", _LIVE_MODULES)
def test_every_live_module_marks_its_live_tests(stem):
    """A live module carries the ``live`` mark on the module; a mixed one on each of its live tests."""
    module = importlib.import_module(f"tests.{stem}")
    if stem in _MIXED:
        unmarked = [name for name in _MIXED[stem] if not _marked(getattr(module, name))]
        assert not unmarked, f"{stem}: live tests without the live mark: {unmarked}"
    else:
        assert _marked(module), f"{stem} carries no module-level live mark"
