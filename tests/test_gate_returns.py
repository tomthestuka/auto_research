"""Deterministic tests for the gate's dynamic return check [G15] (`sandbox/returns.py`).

Each case builds a throwaway toolkit repo, commits a base, adds one tool + manifest entry + test
uncommitted (so the tool is "new" to the diff), and runs the REAL `run_gate` with its REAL default
Layer-B runner — two pytest subprocesses with the recorder plugin. Two things are swapped for the
suite: the analysis interpreter is this one (it has what the tools below import), and the jail's
allow-list is empty (the gate binds what its run needs: the worktree, the environment's prefix).
Everything the check judges with is the runtime's own code; the tests run in the real jail.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from auto_research.sandbox import gate, returns
from auto_research.sandbox.gate import run_gate
from tests.test_gate import _git, _make_repo, _spec

_MANIFEST = (
    'TOOL_MANIFEST = {\n  "research_toolkit.demo_utils.my_tool": {\n'
    '    "module": "demo_utils.lib.demo_help", "fn": "my_tool",\n'
    '    "description": "d", "args_schema": {"properties": {\n'
    '        "out_dir": {"kind": "output", "shape": "folder"},\n'
    '        "out_path": {"kind": "output", "shape": "file"},\n'
    '        "data_path": {"kind": "path"}, "save_path": {"kind": "path"},\n'
    '        "log_path": {"kind": "path"}}}}}\n'
)
_FIG = ('{"path": png, "caption": "c", "mime_type": "image/png", "produced_by": "t", '
        '"created_at": "2026-01-01T00:00:00Z", "sha256": hashlib.sha256(open(png, "rb").read()).hexdigest()}')

# (tool body, test body). The test always calls `my_tool(tmp_path)`; every tool takes `out_dir`.
CLEAN = {
    "csv_declared_as_a_product": (  # its numbers stand in the declared file (B2)
        "def my_tool(out_dir):\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': f'wrote {p}', 'pd': 0.0412, 'products': [{'path': p, 'caption': 'PD per band'}]}\n",
        "    res = my_tool(tmp_path)\n",
    ),
    "scratch_in_a_temporary_folder": (  # the program's bookkeeping: never minted, never checked
        "def my_tool(out_dir):\n"
        "    import tempfile\n"
        "    work = tempfile.mkdtemp()\n"
        "    open(os.path.join(work, 'params.par'), 'w').write('p=1\\n')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
    ),
    "a_jit_cache_beside_the_source": (  # numba-style files in the package's __pycache__
        "def my_tool(out_dir):\n"
        "    cache = os.path.join(os.path.dirname(__file__), '__pycache__')\n"
        "    os.makedirs(cache, exist_ok=True)\n"
        "    open(os.path.join(cache, 'demo_help.my_tool-1.py313.nbi'), 'wb').write(b'x')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
    ),
    "an_input_staged_into_the_output_by_a_link": (  # staging (toolkit e1c21f8): no product, no write
        "def my_tool(out_dir, data_path):\n"
        "    os.symlink(os.path.join(str(data_path), 'evt.fits'), os.path.join(str(out_dir), 'evt.fits'))\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'data').mkdir()\n    (tmp_path / 'data' / 'evt.fits').write_bytes(b'EVT')\n"
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out', tmp_path / 'data')\n",
    ),
    "a_path_argument_in_the_temp_area_stays_scratch": (
        "def my_tool(log_path):\n"
        "    open(log_path, 'w').write('fit log\\n')\n"
        "    return {'stdout': 'pd=0.0412', 'pd': 0.0412}\n",
        "    import tempfile, os\n    res = my_tool(os.path.join(tempfile.mkdtemp(), 'fit.log'))\n",
    ),
    "each_watched_call_gets_its_own_parameter_folder": (  # G4: the recorder, as the REPL does
        "def my_tool(out_dir):\n"
        "    user = os.environ['PFILES'].split(';')[0]\n"
        "    seen = len(os.listdir(user))\n"
        "    open(os.path.join(user, 'fit.par'), 'w').write('learned')\n"
        "    return {'stdout': f'ok {seen}'}\n",
        "    import os\n    (tmp_path / 'u').mkdir()\n    os.environ['PFILES'] = f\"{tmp_path / 'u'};/opt/sys\"\n"
        "    first, second = my_tool(tmp_path / 'u'), my_tool(tmp_path / 'u')\n"
        "    assert first['stdout'] == second['stdout'] == 'ok 0', (first, second)\n"
        "    assert os.environ['PFILES'] == f\"{tmp_path / 'u'};/opt/sys\"\n    res = first\n",
    ),
    "scratch_in_the_home_caches": (
        "def my_tool(out_dir):\n"
        "    cache = os.path.join(os.path.expanduser('~'), '.cache', 'demo')\n"
        "    os.makedirs(cache, exist_ok=True)\n"
        "    open(os.path.join(cache, 'index.json'), 'w').write('{}')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
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
    "opt_in_figure_and_table_under_the_output": (
        "def my_tool(out_dir):\n"
        "    png = os.path.join(str(out_dir), 'f.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        f"    return {{'stdout': 'pd=0.0412', 'figures': [{_FIG}], 'table': p,\n"
        "            'products': [{'path': p, 'caption': 'PD per band'}]}\n",
        "    res = my_tool(tmp_path)\n",
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
    "product_file_absent": (
        "def my_tool(out_dir):\n"
        "    p = os.path.join(str(out_dir), 'gone.csv')\n"
        "    return {'stdout': 'done', 'products': [{'path': p, 'caption': 'PD per band'}]}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.receipted",
    ),
    "product_malformed": (
        "def my_tool(out_dir):\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': 'done', 'products': [{'path': p}]}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.receipted",
    ),
    "unparseable_table": (
        "def my_tool(out_dir, out_path):\n"
        "    open(out_path, 'w').write('just one line of prose\\n')\n"
        "    return {'stdout': f'wrote {out_path}', 'products': [{'path': out_path, 'caption': 't'}]}\n",
        "    res = my_tool(tmp_path, out_path=str(tmp_path / 'r.csv'))\n",
        "layer_b.returns.table",
    ),
    "product_outside_output": (
        "def my_tool(out_dir):\n"
        "    p = os.path.join(os.path.dirname(str(out_dir)), 'stray.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': 'pd=0.0412', 'products': [{'path': p, 'caption': 'PD per band'}]}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "figure_outside_output": (
        "def my_tool(out_dir):\n"
        "    png = os.path.join(os.path.dirname(str(out_dir)), 'stray.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        f"    return {{'stdout': 'plotted', 'figures': [{_FIG}]}}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "undeclared_returned_product": (
        "def my_tool(out_dir):\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': 'pd=0.0412', 'table': p}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "product_through_a_link": (
        "def my_tool(out_dir):\n"
        "    real = os.path.join(os.path.dirname(str(out_dir)), 'real.csv')\n"
        "    open(real, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    os.symlink(real, p)\n"
        "    return {'stdout': 'pd=0.0412', 'products': [{'path': p, 'caption': 'PD per band'}]}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "products_past_the_cap": (
        "def my_tool(out_dir):\n"
        "    out = []\n"
        "    for i in range(21):\n"
        "        p = os.path.join(str(out_dir), f'p{i}.txt')\n"
        "        open(p, 'w').write('x')\n"
        "        out.append({'path': p, 'caption': f'part {i}'})\n"
        "    return {'stdout': 'done', 'products': out}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "product_that_is_also_a_figure": (  # the runtime mints the file once and drops the product
        "def my_tool(out_dir):\n"
        "    png = os.path.join(str(out_dir), 'f.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        f"    return {{'stdout': 'plotted', 'figures': [{_FIG}], 'products': [{{'path': png, 'caption': 'f'}}]}}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "undeclared_file_in_a_nested_key": (
        "def my_tool(out_dir):\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
        "    return {'stdout': 'pd=0.0412', 'files': {'tables': [p]}}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "legacy_save_path_figure_elsewhere": (
        "def my_tool(save_path):\n"
        "    png = os.path.join(os.path.dirname(os.path.dirname(str(save_path))), 'stray.png')\n"
        "    open(png, 'wb').write(b'\\x89PNG')\n"
        f"    return {{'stdout': 'plotted', 'figures': [{_FIG}]}}\n",
        "    (tmp_path / 'f').mkdir()\n    res = my_tool(str(tmp_path / 'f' / 'pd.png'))\n",
        "layer_b.returns.placed",
    ),
    "undeclared_file_in_an_output_folder": (  # user rule 2026-11-04: everything there is declared
        "def my_tool(out_dir):\n"
        "    open(os.path.join(str(out_dir), 'params.par'), 'w').write('p=1\\n')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "undeclared_file_written_into_an_output_by_a_subprocess": (  # the folder is walked, not only audited
        "def my_tool(out_dir):\n"
        "    import subprocess\n"
        "    subprocess.run(['sh', '-c', f'printf x > {out_dir}/side.txt'], check=True)\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.placed",
    ),
    "a_test_that_points_matplotlib_at_tmp_path_widens_nothing": (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(os.path.dirname(str(out_dir)), 'stray.txt'), 'w').write('x')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    import os\n    os.environ['MPLCONFIGDIR'] = str(tmp_path)\n"
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out')\n",
        "layer_b.returns.unclassified",
    ),
    "overwrite_of_an_argument_that_is_no_path": (  # only `path`-kind arguments name a file to write
        "def my_tool(out_dir, label):\n"
        "    open(label, 'w').write('x')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out', str(tmp_path / 'label.txt'))\n",
        "layer_b.returns.unclassified",
    ),
    "a_file_written_at_a_path_argument": (  # an output routed through `path` skips minting (retired class)
        "def my_tool(log_path):\n"
        "    open(log_path, 'w').write('fit log\\n')\n"
        "    return {'stdout': 'pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(str(tmp_path / 'fit.log'))\n",
        "layer_b.returns.unclassified",
    ),
    "a_path_argument_naming_a_file_in_the_toolkit_tree": (
        "def my_tool(log_path):\n"
        "    open(log_path, 'w').write('x')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    import os\n    res = my_tool(os.path.join(os.path.dirname(__file__), 'fit.log'))\n",
        "layer_b.returns.unclassified",
    ),
    "a_subprocess_write_into_tmp_path": (  # the test's tmp_path is walked, not only audited
        "def my_tool(out_dir):\n"
        "    import subprocess\n"
        "    subprocess.run(['sh', '-c', f'printf x > {os.path.dirname(str(out_dir))}/stray.txt'], check=True)\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out')\n",
        "layer_b.returns.unclassified",
    ),
    "a_write_through_a_staged_link": (  # a staged input changed in place is a write to its target
        "def my_tool(out_dir, data_path):\n"
        "    link = os.path.join(str(out_dir), 'evt.fits')\n"
        "    os.symlink(os.path.join(str(data_path), 'evt.fits'), link)\n"
        "    open(link, 'ab').write(b'TAG')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'data').mkdir()\n    (tmp_path / 'data' / 'evt.fits').write_bytes(b'EVT')\n"
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out', tmp_path / 'data')\n",
        "layer_b.returns.unclassified",
    ),
    "a_subprocess_write_through_a_staged_link_into_the_tree": (
        "def my_tool(out_dir):\n"
        "    import subprocess\n"
        "    link = os.path.join(str(out_dir), 'src.py')\n"
        "    os.symlink(os.path.abspath(__file__), link)\n"
        "    subprocess.run(['sh', '-c', f'printf \"#\" >> {link}'], check=True)\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out')\n",
        "layer_b.returns.unclassified",
    ),
    "write_into_the_toolkit_tree": (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(os.path.dirname(__file__), 'cache.json'), 'w').write('{}')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out')\n",
        "layer_b.returns.unclassified",
    ),
    "write_into_tmp_path_outside_the_classes": (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(os.path.dirname(str(out_dir)), 'stray.txt'), 'w').write('x')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    (tmp_path / 'out').mkdir()\n    res = my_tool(tmp_path / 'out')\n",
        "layer_b.returns.unclassified",
    ),
    "write_into_the_home_outside_its_caches": (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(os.path.expanduser('~'), 'notes.txt'), 'w').write('x')\n"
        "    return {'stdout': 'done: pd=0.0412', 'pd': 0.0412}\n",
        "    res = my_tool(tmp_path)\n",
        "layer_b.returns.unclassified",
    ),
    "a_raised_call_that_wrote_outside_the_classes": (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(os.path.expanduser('~'), 'notes.txt'), 'w').write('x')\n"
        "    raise ValueError('no data')\n",
        "    import pytest\n    with pytest.raises(ValueError):\n        my_tool(tmp_path)\n    res = 1\n",
        "layer_b.returns.unclassified",
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
    """This interpreter as the toolkit's default environment (what the gate builds for a package
    naming none), the jail's allow-list empty; the gate otherwise untouched."""
    from auto_research.sandbox import jail
    from auto_research.sandbox.envs import Environment
    from auto_research.toolkit_contract import toolkit

    built = gate._gate_environment
    host = Environment(name="host", prefix=Path(sys.prefix), owner="human")

    async def default_is_host(export, env_id):
        return (host, "") if env_id == toolkit().default_env else await built(export, env_id)

    monkeypatch.setattr(gate, "_gate_environment", default_is_host)
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {"env_externals": {
        "heasoft": {"root": "/opt/heasoft", "private": {"PFILES": ";"}}}})
    if not jail.jail_available()[0]:
        pytest.skip("the OS jail is required and bwrap cannot run here")
    monkeypatch.setattr(jail, "_config", lambda: {})
    monkeypatch.setattr(gate, "_gate_test_timeout", lambda worktree, targets: 120)


def _repo(tmp_path, tool: str, test: str, *, extra_files: dict[str, str] | None = None,
          base_files: dict[str, str] | None = None):
    repo = _make_repo(tmp_path)
    pkg = repo / "demo_utils"
    for d in ("lib", "skills", "tests"):
        (pkg / d).mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "lib" / "__init__.py").write_text("")
    (pkg / "skills" / "__init__.py").write_text("TOOL_MANIFEST = {}\n")
    for rel, body in (base_files or {}).items():  # the toolkit as it was: committed before the change
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
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


def test_a_decorator_added_or_a_key_repointed_makes_a_tool_watched(tmp_path):
    repo = _make_repo(tmp_path)
    pkg = repo / "demo_utils"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "skills").mkdir()
    (pkg / "lib" / "demo_help.py").write_text(
        "def my_tool(x):\n    return {'stdout': 'a', 'pd': 0.5}\n\n\ndef _helper(x):\n    return {'stdout': 'h'}\n")
    (pkg / "skills" / "__init__.py").write_text(_MANIFEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "legacy tool")
    head = lambda rel: gate._head_file(repo, rel)  # noqa: E731
    (pkg / "lib" / "demo_help.py").write_text(
        "def _inject(f):\n    return lambda x: {'stdout': 'x', 'value': 0.731}\n\n\n@_inject\n"
        "def my_tool(x):\n    return {'stdout': 'a', 'pd': 0.5}\n\n\ndef _helper(x):\n    return {'stdout': 'h'}\n")
    assert "research_toolkit.demo_utils.my_tool" in returns.watched_tools(repo, gate.collect_targets(repo), head)
    _git(repo, "checkout", "--", ".")
    (pkg / "skills" / "__init__.py").write_text(_MANIFEST.replace('"fn": "my_tool"', '"fn": "_helper"'))
    assert "research_toolkit.demo_utils.my_tool" in returns.watched_tools(repo, gate.collect_targets(repo), head)


def test_recorder_is_assembled_from_runtime_source():
    """Identical by construction: the rules the recorder applies are the runtime's own text."""
    from auto_research.provenance.capture import _named_paths, _product_problem, admit_artifacts, call_started_ns
    from auto_research.sandbox.persistent import _REPL_SCRIPT, repl_payload
    from auto_research.sandbox.runner import normalize_tool_raw

    src = returns.recorder_source()
    # The opt-in rule is the runtime's own minting decision (E1g): one code path, not a mirror.
    for fn in (repl_payload, normalize_tool_raw, _named_paths, call_started_ns, _product_problem, admit_artifacts):
        assert textwrap.dedent(inspect.getsource(fn)) in src
    assert inspect.getsource(repl_payload) in _REPL_SCRIPT
    # G4: one parameter-folder rule for the REPL, the shell server and the recorder.
    from auto_research.sandbox.runner import builtin_function_source

    assert builtin_function_source("private_folders") in src and builtin_function_source("private_folders") in _REPL_SCRIPT
    compile(src, "recorder", "exec")


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
        "agent_fs_allow": [prefix], "agent_fs_readonly": [prefix],
    })
    from auto_research.sandbox.envs import Environment

    monkeypatch.setattr(gate, "_orchestration_config", lambda: {})
    host = Environment(name="host", prefix=Path(sys.prefix), owner="human")
    result = asyncio.run(gate._default_test_runner(repo, [str(pkg)], timeout_s=120, environment=host))
    assert result["results"][0].passed, result["results"][0].detail
    assert result["deterministic"], result.get("determinism_detail")


def test_a_scratch_file_spends_none_of_the_copy_budget(tmp_path, analysis_env, monkeypatch):
    """Review of B2: only declared files are copied for judging, so a scratch file that sorts first
    cannot leave a declared table uncopied and its check skipped."""
    monkeypatch.setattr(returns, "COPY_CAP", 100)
    tool = (
        "def my_tool(out_dir):\n"
        "    open(os.path.join(str(out_dir), 'a_scratch.bin'), 'w').write('x' * 80)\n"
        "    p = os.path.join(str(out_dir), 'r.csv')\n"
        "    open(p, 'w').write('just one line of prose\\n')\n"
        "    return {'stdout': 'done', 'products': [{'path': p, 'caption': 't'}]}\n"
    )
    report = _gate(_repo(tmp_path, tool, "    res = my_tool(tmp_path)\n"))
    assert any(c.name == "layer_b.returns.table" for c in _failed(report)), [c.detail for c in _failed(report)]


def test_a_product_that_is_also_a_figure_is_named_as_the_runtime_names_it(tmp_path, analysis_env):
    tool, test, _check = BREACHES["product_that_is_also_a_figure"]
    report = _gate(_repo(tmp_path, tool, test))
    assert any("already receipted by this call" in c.detail and "declare each file once" in c.detail
               for c in _failed(report) if c.name == "layer_b.returns.placed"), [c.detail for c in _failed(report)]


def test_a_tool_with_no_output_argument_is_told_to_take_one(tmp_path, analysis_env):
    tool = ("def my_tool(data_path):\n"
            "    p = os.path.join(str(data_path), 'r.csv')\n"
            "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
            "    return {'stdout': 'pd=0.0412', 'products': [{'path': p, 'caption': 'PD per band'}]}\n")
    manifest = _MANIFEST.replace(
        '        "out_dir": {"kind": "output", "shape": "folder"},\n'
        '        "out_path": {"kind": "output", "shape": "file"},\n', "").replace(', "save_path": {"kind": "path"}', "")
    assert "output" not in manifest
    report = _gate(_repo(tmp_path, tool, "    res = my_tool(tmp_path)\n",
                         extra_files={"demo_utils/skills/__init__.py": manifest}))
    assert any("Take an output-kind argument" in c.detail for c in _failed(report)), [c.detail for c in _failed(report)]


def test_a_test_that_passes_a_linked_output_folder_is_not_judged_as_the_tools_link(tmp_path, analysis_env):
    """Review of E1g: the folder is anchored where it resolved before the call, as the runtime's is."""
    tool = ("def my_tool(out_dir):\n"
            "    p = os.path.join(str(out_dir), 'r.csv')\n"
            "    open(p, 'w').write('band,pd\\n2-8,0.0412\\n')\n"
            "    return {'stdout': 'pd=0.0412', 'products': [{'path': p, 'caption': 'PD per band'}]}\n")
    test = "    (tmp_path / 'real').mkdir()\n    (tmp_path / 'out').symlink_to(tmp_path / 'real')\n    res = my_tool(tmp_path / 'out')\n"
    report = _gate(_repo(tmp_path, tool, test))
    assert not _failed(report), [c.detail for c in _failed(report)]


def test_the_recorder_runs_in_a_bare_standard_library_interpreter(tmp_path):
    """The analysis interpreter cannot import this package: the assembled recorder's shared rules
    (`parse_products`, `admit_artifacts`) run with the standard library alone."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "r.csv").write_text("a\n1\n")
    script = tmp_path / "drive.py"
    script.write_text(
        "import os, sys\nos.environ.update(AR_RETURNS_OUT=sys.argv[1], AR_RETURNS_TOOLS='{}', AR_RETURNS_COPY_CAP='0')\n"
        "exec(open(sys.argv[2]).read())\n"
        "products, notes = parse_products([{'path': sys.argv[3], 'caption': 'c'}, {'path': 'x'}])\n"
        "admitted, dropped = admit_artifacts([('product', p.caption, p.path, Path(sys.argv[4]), None) for p in products]"
        " + [('product', 'twice', products[0].path, Path(sys.argv[4]), None)], lambda p: True)\n"
        "print(len(notes), admitted[0] is not None, admitted[1] is None, len(dropped))\n")
    (tmp_path / "rec.py").write_text(returns.recorder_source())
    done = subprocess.run([sys.executable, "-I", "-S", str(script), str(tmp_path), str(tmp_path / "rec.py"),
                           str(out / "r.csv"), str(out)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1", "True", "True", "1"], done.stdout


def test_one_file_is_reported_once(tmp_path, analysis_env):
    """Review of E1h: an undeclared file the call also returns is named once (`.placed`)."""
    tool, test, _check = BREACHES["undeclared_returned_product"]
    details = [c.detail for c in _failed(_gate(_repo(tmp_path, tool, test)))]
    assert sum("r.csv" in d for d in details) == 1, details


def test_a_file_at_a_path_argument_is_told_to_become_an_output(tmp_path, analysis_env):
    tool, test, _check = BREACHES["a_file_written_at_a_path_argument"]
    report = _gate(_repo(tmp_path, tool, test))
    assert any("Declare that argument as an output" in c.detail for c in _failed(report)), \
        [c.detail for c in _failed(report)]


# -- regression: the toolkit's earlier fast tests (user rule 2026-11-04) ---------------------------

_SHARED = {"demo_utils/lib/shared.py": "def two():\n    return 2\n",
           "demo_utils/tests/test_shared.py": "from demo_utils.lib.shared import two\n\n\n"
                                              "def test_two_is_two():\n    assert two() == 2\n"}
_CLEAN_TOOL = ("def my_tool(out_dir):\n    return {'stdout': 'pd=0.0412', 'pd': 0.0412}\n",
               "    res = my_tool(tmp_path)\n")


def test_an_earlier_fast_test_the_change_breaks_fails_the_gate(tmp_path, analysis_env):
    repo = _repo(tmp_path, *_CLEAN_TOOL, base_files=_SHARED,
                 extra_files={"demo_utils/lib/shared.py": "def two():\n    return 3\n"})
    report = _gate(repo)
    bad = [c for c in report.layer_b if c.name == "layer_b.regression" and not c.passed]
    assert bad and "an earlier test that fails with this change: " in bad[0].detail, bad
    assert "test_two_is_two" in bad[0].detail and "assert 3 == 2" in bad[0].detail  # what it returned
    assert not report.passed


def test_the_earlier_fast_tests_pass_and_slow_ones_are_left_to_the_toolkit(tmp_path, analysis_env):
    slow = {"demo_utils/tests/test_slow.py": "import pytest\n\n\n@pytest.mark.slow\ndef test_slow():\n    assert False\n"}
    report = _gate(_repo(tmp_path, *_CLEAN_TOOL, base_files={**_SHARED, **slow}))
    regression = [c for c in report.layer_b if c.name == "layer_b.regression"]
    assert regression and regression[0].passed and report.passed, [(c.name, c.detail) for c in report.layer_b]


def test_an_earlier_test_calling_a_changed_tool_is_judged_by_the_recorder(tmp_path, analysis_env):
    """An earlier fast test is judged by the return recorder only through the watched tools it
    calls: here a changed tool writes outside the classes, seen only from the earlier test."""
    base = {"demo_utils/lib/demo_help.py": "import os\n\n\ndef my_tool(out_dir):\n    return {'stdout': 'done'}\n",
            "demo_utils/skills/__init__.py": _MANIFEST,
            "demo_utils/tests/test_earlier.py": "from demo_utils.lib.demo_help import my_tool\n\n\n"
                                                "def test_earlier(tmp_path):\n    (tmp_path / 'out').mkdir()\n"
                                                "    assert my_tool(tmp_path / 'out')['stdout'] == 'done'\n"}
    tool = ("def my_tool(out_dir):\n"
            "    open(os.path.join(os.path.expanduser('~'), 'notes.txt'), 'w').write('x')\n"
            "    return {'stdout': 'done'}\n")
    report = _gate(_repo(tmp_path, tool, "    res = 1\n", base_files=base))
    assert any(c.name == "layer_b.returns.unclassified" for c in _failed(report)), \
        [(c.name, c.detail) for c in report.layer_b]


def test_earlier_tests_that_all_skip_leave_the_recorder_nothing_to_judge(tmp_path, analysis_env):
    """Skipped at setup, an earlier test never reaches its call, where the recorder loads."""
    skipped = {"demo_utils/tests/test_absent.py": "import pytest\n\n\n"
                                                  "@pytest.mark.skipif(True, reason='data absent')\n"
                                                  "def test_absent():\n    assert False\n"}
    report = _gate(_repo(tmp_path, *_CLEAN_TOOL, base_files=skipped))
    assert report.passed, [(c.name, c.detail) for c in report.layer_b if not c.passed]


def test_earlier_tests_that_run_past_the_limit_fail_as_unfinished(tmp_path, analysis_env, monkeypatch):
    monkeypatch.setattr(gate, "_gate_test_timeout", lambda worktree, targets: 15)
    slow = {"demo_utils/tests/test_sleeps.py": "import time\n\n\ndef test_sleeps():\n    time.sleep(60)\n"}
    report = _gate(_repo(tmp_path, *_CLEAN_TOOL, base_files=slow))
    bad = [c for c in report.layer_b if c.name == "layer_b.regression" and not c.passed]
    assert bad and "did not finish within 15s" in bad[0].detail and "fails with this change" not in bad[0].detail, bad


def test_earlier_tests_run_in_the_environment_their_package_names(tmp_path, monkeypatch):
    """A change to one package runs another package's earlier fast tests in the environment that
    package names, built for the gate; when it cannot build, the check says whose tests waited."""
    from tests.test_gate import _derived_package

    from auto_research.sandbox import jail

    if not jail.jail_available()[0]:
        pytest.skip("the OS jail is required and bwrap cannot run here")
    monkeypatch.setattr(jail, "_config", lambda: {})
    monkeypatch.setattr(gate, "_gate_test_timeout", lambda worktree, targets: 120)
    repo = _make_repo(tmp_path)
    _derived_package(repo, monkeypatch)
    lib_test = repo / "demo_utils" / "lib" / "test_demo.py"
    (repo / "demo_utils" / "tests").mkdir()
    lib_test.rename(repo / "demo_utils" / "tests" / "test_earlier.py")  # asserts the stack's activation
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "demo_utils in its stack")
    other = repo / "other_utils"
    for d in ("lib", "skills", "tests"):
        (other / d).mkdir(parents=True)
    (other / "__init__.py").write_text("")
    (other / "lib" / "__init__.py").write_text("")
    (other / "lib" / "other_help.py").write_text("def my_tool(obsid):\n    return 1\n")
    (other / "skills" / "__init__.py").write_text(
        'TOOL_MANIFEST = {\n  "research_toolkit.other_utils.my_tool": {\n'
        '    "module": "other_utils.lib.other_help", "fn": "my_tool",\n'
        '    "description": "d", "args_schema": {}}}\n')
    (other / "tests" / "test_other.py").write_text(
        "from other_utils.lib.other_help import my_tool\n\n\n"
        "def test_my_tool():\n    assert my_tool('synth240611-aa') == 1\n")
    report = asyncio.run(run_gate(repo, _spec(target_domain="other_utils")))
    regression = [c for c in report.layer_b if c.name == "layer_b.regression"]
    assert regression and regression[0].passed, [(c.name, c.detail) for c in report.layer_b]
    async def unbuilt(export, env_id):  # the build itself (conda) fails, say for a lost network
        return None, "conda exited 1"
    monkeypatch.setattr(gate, "_gate_environment", unbuilt)
    report = asyncio.run(run_gate(repo, _spec(target_domain="other_utils")))
    env = [c for c in report.layer_b if c.name == "layer_b.env.stack"]
    assert env and not env[0].passed and "earlier fast tests of the packages naming it" in env[0].detail, report.reason
    assert "conda exited 1" in env[0].detail


def test_earlier_test_files_sharing_a_name_across_packages_both_run(tmp_path, analysis_env):
    """One pytest call per package: two packages' ``tests/test_shared.py`` (no ``__init__.py``)
    would clash as one session's modules; each package's call collects only its own."""
    twin = {"other_utils/tests/test_shared.py": "def test_other_is_fine():\n    assert True\n"}
    report = _gate(_repo(tmp_path, *_CLEAN_TOOL, base_files={**_SHARED, **twin}))
    regression = [c for c in report.layer_b if c.name == "layer_b.regression"]
    assert regression and regression[0].passed, regression[0].detail
    assert report.passed, report.reason


def test_a_failing_earlier_test_shows_its_assertion_and_what_it_printed(tmp_path, analysis_env):
    """What each failing earlier test returned: the assertion, its captured output, and a teardown
    error beside the failure; a package stopped at a collection error says its other tests waited."""
    tests = {"demo_utils/tests/test_tells.py": (
                 "import pytest\n\n\n@pytest.fixture\ndef torn():\n    yield\n    raise RuntimeError('teardown boom')\n\n\n"
                 "def test_prints_why(torn):\n    print('fhelp: ERROR caldb not found')\n    assert 5 == 6\n"),
             "other_utils/tests/test_broken_import.py": "import no_such_module_here\n\n\ndef test_never():\n    pass\n"}
    report = _gate(_repo(tmp_path, *_CLEAN_TOOL, base_files=tests))
    bad = [c for c in report.layer_b if c.name == "layer_b.regression" and not c.passed]
    assert bad, [(c.name, c.detail) for c in report.layer_b]
    detail = bad[0].detail
    assert "assert 5 == 6" in detail and "teardown boom" in detail and "fhelp: ERROR caldb not found" in detail, detail
    assert "no_such_module_here" in detail and "collection error in other_utils/tests" in detail, detail


def test_many_failing_earlier_tests_stay_within_the_output_cap(tmp_path, analysis_env, monkeypatch):
    cap = 3000
    monkeypatch.setattr(gate, "_orchestration_config",
                        lambda: {"gate_layer_b_output_cap": cap})
    many = {"demo_utils/tests/test_many.py": (
        "import pytest\n\n\n@pytest.mark.parametrize('i', range(60))\n"
        "def test_each(i):\n    print('x' * 2000)\n    assert i < 0\n")}
    report = _gate(_repo(tmp_path, *_CLEAN_TOOL, base_files=many))
    detail = next(c for c in report.layer_b if c.name == "layer_b.regression").detail
    assert "and 50 more earlier tests that fail with this change" in detail and "and 30 more" in detail, detail[-500:]
    assert len(detail) < 2 * cap, len(detail)


# ── the data test class (Phase 3 step 0a W3): the toolkit names a data set, the host provides it ──

_DATA_TEST = ('import os\n\nimport pytest\n\n\n@pytest.mark.data("demo_rows")\n'
              'def test_reads_the_named_rows():\n'
              '    assert os.listdir(os.environ["TOOLKIT_DATA_DEMO_ROWS"]) == ["rows.csv"]\n')


def _with_test_data(monkeypatch, test_data: dict) -> None:
    """The host's configuration as the gate reads it, with ``test_data``."""
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {
        "env_externals": {"heasoft": {"root": "/opt/heasoft", "private": {"PFILES": ";"}}}, "test_data": test_data})


def _data_set(tmp_path) -> Path:
    folder = tmp_path / "rows"
    folder.mkdir()
    (folder / "rows.csv").write_text("a,b\n1,2\n")
    return folder


@pytest.mark.parametrize("earlier", [False, True])
def test_a_data_test_the_gate_skips_fails_the_change(tmp_path, analysis_env, monkeypatch, earlier):
    """The toolkit's root conftest skips a data test whose data set is absent, with the reason; the
    gate counts that skip from its JUnit report and fails the change, naming the data set, whether
    the test is the change's own or an earlier one."""
    from tests.fixtures.toolkit import ROOT_CONFTEST

    _with_test_data(monkeypatch, {"elsewhere": str(_data_set(tmp_path))})
    test = {"demo_utils/tests/test_rows.py": _DATA_TEST}
    repo = _repo(tmp_path, *_CLEAN_TOOL, base_files={"conftest.py": ROOT_CONFTEST, **(test if earlier else {})},
                 extra_files=None if earlier else test)
    report = _gate(repo)
    status = {j.name: j for j in report.jobs}
    check = next(c for c in report.layer_b if c.name == ("layer_b.regression" if earlier else "layer_b.data"))
    assert not report.passed and not check.passed, [(c.name, c.detail) for c in report.layer_b]
    assert "test_reads_the_named_rows (data demo_rows) was skipped: data: demo_rows is absent" in check.detail
    assert "Data sets this pipeline provides: elsewhere" in check.detail
    assert status["regression" if earlier else "tests"].status == "fail"


def test_a_data_test_runs_on_the_data_set_the_host_names(tmp_path, analysis_env, monkeypatch):
    from tests.fixtures.toolkit import ROOT_CONFTEST

    _with_test_data(monkeypatch, {"demo_rows": str(_data_set(tmp_path))})
    test = {"demo_utils/tests/test_rows.py": _DATA_TEST}
    repo = _repo(tmp_path, *_CLEAN_TOOL, base_files={"conftest.py": ROOT_CONFTEST}, extra_files=test)
    report = _gate(repo)
    assert report.passed, report.reason
    data = next(c for c in report.layer_b if c.name == "layer_b.data")
    assert data.passed and data.detail == "1 data tests ran"
    assert [j.status for j in report.jobs if j.name == "tests"] == ["pass"]


def test_a_live_test_of_the_change_is_deselected(tmp_path, analysis_env, monkeypatch):
    """The gate's test run has no network: a change's live test is left out, never run or skipped."""
    from tests.fixtures.toolkit import ROOT_CONFTEST

    _with_test_data(monkeypatch, {})
    live = {"demo_utils/tests/test_net.py": "import pytest\n\n\n@pytest.mark.live\ndef test_net():\n    assert False\n"}
    repo = _repo(tmp_path, *_CLEAN_TOOL, base_files={"conftest.py": ROOT_CONFTEST}, extra_files=live)
    report = _gate(repo)
    assert report.passed, report.reason
    # Listed for the co-lead, never run and never a failure (yet).
    listed = next(c for c in report.layer_b if c.name == "layer_b.live")
    assert listed.passed and "demo_utils/tests/test_net.py::test_net" in listed.detail, listed.detail
    assert "test_net" not in str(report.findings)


@pytest.mark.parametrize("test,needle", [
    ("import pytest\n\n\ndef test_waits():\n    pytest.skip('needs the archive')\n", "test_waits was skipped: needs the archive"),
    ("import pytest\n\n\n@pytest.mark.xfail(reason='known gap')\ndef test_gap():\n    assert False\n", "test_gap was skipped: known gap"),
    ("import pytest\n\n\n@pytest.mark.xfail(reason='maybe')\ndef test_maybe():\n    assert True\n", "test_maybe is marked xfail"),
])
def test_a_skipped_or_xfail_test_of_the_change_fails_the_tests_job(tmp_path, analysis_env, monkeypatch, test, needle):
    """Every test of the change runs and passes in the gate: a skip, an XFAIL or an XPASS verifies
    nothing about the new tool, so the tests job fails, naming the test and why."""
    _with_test_data(monkeypatch, {})
    repo = _repo(tmp_path, *_CLEAN_TOOL, extra_files={"demo_utils/tests/test_extra.py": test})
    report = _gate(repo)
    check = next(c for c in report.layer_b if c.name == "layer_b.skipped")
    assert not check.passed and needle in check.detail, check.detail
    assert "Make each one run here" in check.detail
    assert not report.passed and {j.name: j.status for j in report.jobs}["tests"] == "fail"


def test_a_conditional_xfail_whose_condition_is_false_is_a_plain_test(tmp_path, analysis_env, monkeypatch):
    """pytest's own evaluation decides whether an xfail mark applies: `xfail(False, ...)` does not."""
    _with_test_data(monkeypatch, {})
    test = ("import sys\n\nimport pytest\n\n\n@pytest.mark.xfail(False, reason='never here')\n"
            "def test_plain():\n    assert True\n\n\n"
            "@pytest.mark.xfail(sys.platform == 'no-such-platform', reason='elsewhere')\n"
            "def test_also_plain():\n    assert True\n")
    repo = _repo(tmp_path, *_CLEAN_TOOL, extra_files={"demo_utils/tests/test_extra.py": test})
    report = _gate(repo)
    assert report.passed, report.reason
    assert next(c for c in report.layer_b if c.name == "layer_b.skipped").passed
