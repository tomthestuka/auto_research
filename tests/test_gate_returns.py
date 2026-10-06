"""Deterministic tests for the gate's dynamic return check [G15] (`sandbox/returns.py`).

Each case builds a throwaway toolkit repo, commits a base, adds one tool + manifest entry + test
uncommitted (so the tool is "new" to the diff), and runs the REAL `run_gate` with its REAL default
Layer-B runner — two pytest subprocesses with the recorder plugin. Two things are swapped for the
suite: the analysis interpreter is this one (it has what the tools below import), and the OS jail
is off. Everything the check judges with is the runtime's own code.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import textwrap

import pytest

from auto_research.sandbox import gate, returns
from auto_research.sandbox.gate import run_gate
from tests.test_gate import _git, _make_repo, _spec

_MANIFEST = (
    'TOOL_MANIFEST = {\n  "research_toolkit.demo_utils.my_tool": {\n'
    '    "module": "demo_utils.lib.demo_help", "fn": "my_tool",\n'
    '    "description": "d", "args_schema": {}}}\n'
)

_FIG = ('{"path": png, "caption": "c", "mime_type": "image/png", "produced_by": "t", '
        '"created_at": "2026-01-01T00:00:00Z", "sha256": hashlib.sha256(open(png, "rb").read()).hexdigest()}')

# (tool body, test body). The test always calls `my_tool(tmp_path)`; every tool takes `out_dir`.
CLEAN = {
    "csv_in_argument": (
        "def my_tool(out_dir, out_path=None):\n"
        "    out_path = out_path or os.path.join(str(out_dir), 'r.csv')\n"
        "    open(out_path, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': f'pd=0.0412 written to {out_path}', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path, out_path=str(tmp_path / 'r.csv'))\n",
    ),
    "csv_named_in_stdout": (
        "def my_tool(out_dir):\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': f'wrote {p}', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
    ),
    "figure": (
        "def my_tool(out_dir):\n"
        "    png = os.path.join(str(out_dir), 'f.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        f"    return {{'stdout': 'plotted', 'figures': [{_FIG}]}}\n",
        "    res = my_tool(tmp_path)\n",
    ),
    "empty_stdout_keys_are_the_text": (
        "def my_tool(out_dir):\n    return {'pd': 0.0412, 'n': 17}\n",
        "    res = my_tool(tmp_path)\n",
    ),
    "catalog_lock_and_backup_are_housekeeping": (
        "def my_tool(out_dir, out_path):\n"
        "    fd = os.open(os.path.join(str(out_dir), 'catalog.json.lock'), os.O_CREAT | os.O_RDWR)\n"
        "    os.close(fd)\n"
        "    os.makedirs(os.path.join(str(out_dir), '.catalog_history'), exist_ok=True)\n"
        "    open(os.path.join(str(out_dir), '.catalog_history', 'catalog-1.json'), 'w').write('{}')\n"
        "    open(out_path, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': f'wrote {out_path}'}\n",
        "    res = my_tool(tmp_path, out_path=str(tmp_path / 'r.csv'))\n",
    ),
    "failed_call_is_not_judged": (
        "def my_tool(out_dir):\n    return {'stdout': 'no data', 'exit_code': 1, 'pd': 0.5}\n",
        "    res = my_tool(tmp_path)\n",
    ),
}

BREACHES = {
    "transmit": (
        "def my_tool(out_dir):\n    return {'stdout': 'n=3', 'n': {3}}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.transmit",
    ),
    "figure_missing_key": (
        "def my_tool(out_dir):\n"
        "    png = os.path.join(str(out_dir), 'f.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        "    fig = dict(path=png, caption='c')\n"
        "    return {'stdout': 'plotted', 'figures': [fig]}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.figure",
    ),
    "figure_file_absent": (
        "def my_tool(out_dir):\n"
        "    png = os.path.join(str(out_dir), 'f.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        f"    fig = {_FIG}\n"
        "    os.remove(png)\n"
        "    return {'stdout': 'plotted', 'figures': [fig]}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.figure",
    ),
    "unreceipted_file": (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(str(out_dir), 'hidden.csv'), 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.receipted",
    ),
    "unparseable_table": (
        "def my_tool(out_dir, out_path):\n"
        "    open(out_path, 'w').write('just one line of prose\\n')\n"
        "    return {'stdout': f'wrote {out_path}'}\n",
        "    res = my_tool(tmp_path, out_path=str(tmp_path / 'r.csv'))\n",
        "layer_b.returns.table",
    ),
    "hidden_number": (
        "def my_tool(out_dir):\n    return {'stdout': 'fit done', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.number",
    ),
    "rounded_in_stdout": (
        "def my_tool(out_dir):\n    pd = 0.041234\n    return {'stdout': f'pd={pd:.3f}', 'pd': pd}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.number",
    ),
}


@pytest.fixture
def analysis_env(monkeypatch):
    """This interpreter as the analysis env, the jail off; the gate otherwise untouched."""
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {"analyze_env": sys.prefix})
    monkeypatch.setattr(gate, "_jailed_pytest", lambda argv, *, junit_path, worktree=None: argv)
    monkeypatch.setattr(gate, "_gate_test_timeout", lambda worktree, targets: 120)


def _repo(tmp_path, tool: str, test: str, *, extra_files: dict[str, str] | None = None):
    repo = _make_repo(tmp_path)
    pkg = repo / "demo_utils"
    for d in ("lib", "skills", "tests"):
        (pkg / d).mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "lib" / "__init__.py").write_text("")
    (pkg / "skills" / "__init__.py").write_text("TOOL_MANIFEST = {}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "package skeleton")
    (pkg / "lib" / "demo_help.py").write_text("import hashlib\nimport os\n\n\n" + tool)
    (pkg / "skills" / "__init__.py").write_text(_MANIFEST)
    (pkg / "tests" / "test_my_tool.py").write_text(
        "from demo_utils.lib.demo_help import my_tool\n\n\n"
        "def test_my_tool(tmp_path):\n" + test + "    assert res is not None\n"
    )
    for rel, body in (extra_files or {}).items():
        (repo / rel).write_text(body)
    return repo


def _gate(repo):
    return asyncio.run(run_gate(repo, _spec()))


def _failed(report, prefix="layer_b.returns"):
    return [c for c in report.layer_b if c.name.startswith(prefix) and not c.passed]


@pytest.mark.parametrize("name", sorted(CLEAN))
def test_clean_returns_pass(tmp_path, analysis_env, name):
    report = _gate(_repo(tmp_path, *CLEAN[name]))
    assert not _failed(report), [c.detail for c in _failed(report)]
    assert report.passed, report.reason


@pytest.mark.parametrize("name", sorted(BREACHES))
def test_breach_is_caught_by_name(tmp_path, analysis_env, name):
    tool, test, check = BREACHES[name]
    report = _gate(_repo(tmp_path, tool, test))
    bad = _failed(report)
    assert not report.passed
    assert any(c.name == check for c in bad), [(c.name, c.detail) for c in bad]
    assert all("research_toolkit.demo_utils.my_tool" in c.detail for c in bad if c.name == check)


def test_recorder_survives_an_ini_switch_off(tmp_path, analysis_env):
    """`-p no:ar_returns` in a package pytest.ini loses to the gate's own `-p ar_returns`."""
    tool, test, _check = BREACHES["hidden_number"]
    repo = _repo(tmp_path, tool, test,
                 extra_files={"demo_utils/pytest.ini": "[pytest]\naddopts = -p no:ar_returns\n"})
    report = _gate(repo)
    assert any(c.name == "layer_b.returns.number" for c in _failed(report))


def test_recorder_that_never_loaded_fails(tmp_path):
    checks, findings = returns.judge(tmp_path, {"k": ("m", "f")})
    assert [c.name for c in checks] == ["layer_b.returns.recorder"] and not checks[0].passed
    assert findings == []


def test_uncalled_tool_is_a_finding_not_a_failure(tmp_path, analysis_env):
    tool = "def my_tool(out_dir):\n    return {'stdout': 'fit done', 'pd': 0.0412}\n"
    report = _gate(_repo(tmp_path, tool, "    res = 1\n"))
    assert not _failed(report)
    assert any(f.name == "finding.returns_unobserved" for f in report.findings)


def test_legacy_tool_is_not_watched(tmp_path):
    """A tool committed at HEAD and untouched by the diff is legacy: not judged."""
    repo = _make_repo(tmp_path)
    pkg = repo / "demo_utils"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "skills").mkdir()
    (pkg / "lib" / "demo_help.py").write_text("def my_tool(x):\n    return {'stdout': 'a', 'pd': 0.5}\n")
    (pkg / "skills" / "__init__.py").write_text(_MANIFEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "legacy tool")
    (pkg / "lib" / "other.py").write_text("X = 1\n")
    targets = gate.collect_targets(repo)
    head = lambda rel: gate._head_file(repo, rel)  # noqa: E731
    assert returns.watched_tools(repo, targets, head) == {}
    # …and becomes watched once its body changes.
    (pkg / "lib" / "demo_help.py").write_text("def my_tool(x):\n    return {'stdout': 'b', 'pd': 0.5}\n")
    targets = gate.collect_targets(repo)
    assert "research_toolkit.demo_utils.my_tool" in returns.watched_tools(repo, targets, head)


def test_recorder_is_assembled_from_runtime_source():
    """Identical by construction: the rules the recorder applies are the runtime's own text."""
    from auto_research.provenance.capture import _named_paths, call_started_ns
    from auto_research.sandbox.persistent import _REPL_SCRIPT, repl_payload
    from auto_research.sandbox.runner import normalize_tool_raw

    src = returns.recorder_source()
    for fn in (repl_payload, normalize_tool_raw, _named_paths, call_started_ns):
        assert textwrap.dedent(inspect.getsource(fn)) in src
    assert inspect.getsource(repl_payload) in _REPL_SCRIPT
    compile(src, "recorder", "exec")


def test_every_check_has_a_seeded_breach():
    names = {c for _t, _s, c in BREACHES.values()}
    assert names == {f"layer_b.returns.{k}" for k in ("transmit", "figure", "receipted", "table", "number")}


def test_run_ones_pytest_scratch_is_gone_before_run_two(tmp_path, monkeypatch):
    """Human ruling 2026-10-06: a toolkit suite left 65 GB of pytest scratch in 10 min and two
    determinism runs filled the disk; run 1's scratch is removed before run 2. Under the real jail
    (the private /tmp is where the scratch goes): run 1 leaves a marker in its scratch, and run 2
    finds it anywhere under /tmp only if that scratch survived -- then its outcome flips."""
    from auto_research.sandbox import jail

    if not jail.jail_available()[0]:
        pytest.skip("bwrap cannot run on this host")
    repo = _make_repo(tmp_path)
    pkg = repo / "demo_utils" / "tests"
    pkg.mkdir(parents=True)
    (repo / "demo_utils" / "__init__.py").write_text("")
    (pkg / "test_scratch.py").write_text(
        "def test_session_scratch_is_fresh(tmp_path):\n"
        "    import glob, tempfile\n"
        "    leftovers = glob.glob(tempfile.gettempdir() + '/**/ar_marker', recursive=True)\n"
        "    (tmp_path / 'ar_marker').write_text('left by an earlier run')\n"
        "    assert not leftovers, leftovers\n"
    )
    prefix = sys.prefix  # its configured spelling: the jail links it to the resolved path
    monkeypatch.setattr(jail, "_config", lambda: {
        "agent_os_jail": True, "agent_fs_allow": [prefix], "agent_fs_readonly": [prefix],
    })
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {"analyze_env": sys.prefix})
    result = asyncio.run(gate._default_test_runner(repo, [str(pkg)], timeout_s=120))
    assert result["results"][0].passed, result["results"][0].detail
    assert result["deterministic"], result.get("determinism_detail")
