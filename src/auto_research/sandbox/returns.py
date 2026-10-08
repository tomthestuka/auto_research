"""The gate's dynamic return check [G15]: what a new tool actually returns, judged as the runtime would.

The static figure-shape scan reads source and is beaten by one indirection (measured 7 of 7, live,
2026-09-25). This check watches the tool run instead. During the gate's Layer-B pytest run a
recorder wraps every new or changed ``TOOL_MANIFEST`` function; each call's return, the pre/post
state of every path the runtime could consult, and copies of the files the call wrote are written
down. The gate process then judges those records with the analyst runtime's own functions:

  layer_b.returns.transmit   the payload survives the REPL's ``json.dumps``  (``persistent.repl_payload``)
  layer_b.returns.figure     each figure survives ``parse_tool_figures`` and its file exists
  layer_b.returns.receipted  each file the call wrote is one ``capture.data_products`` receipts
  layer_b.returns.table      each receipted ``.csv``/``.tsv`` parses with ``render.parse_delimited``
  layer_b.returns.number     each number in the other returned keys is in the result text or a
                             receipted file, by ``bindings.ungrounded_numbers``

The recorder runs in the analysis interpreter, which cannot import this package, so it is assembled
from the runtime's source text (``inspect.getsource``) rather than restated: the payload rule, the
normaliser and the named-path rule are the same code in both places. Limits, left to the co-lead:
only what the tests call is seen, a file written from C (cfitsio, XSPEC) is invisible to the audit
hook, and the recorder shares a process with the coder's tests.
"""

from __future__ import annotations

import inspect
import json
import re
import textwrap
from pathlib import Path

from auto_research.provenance.bindings import normalise, ungrounded_numbers
from auto_research.provenance.capture import (
    _PATH_IN_TEXT,
    CallInputs,
    _named_paths,
    call_started_ns,
    data_products,
)
from auto_research.provenance.store import checkable_text
from auto_research.report.render import parse_delimited
from auto_research.sandbox.persistent import repl_payload
from auto_research.sandbox.runner import PROTOCOL_KEYS, normalize_tool_raw, parse_tool_figures, tool_text
from auto_research.schemas import CheckResult

PLUGIN_NAME = "ar_returns"
RECORDS = "returns.jsonl"
#: Bytes copied per call for the table and number checks; a larger product is recorded uncopied.
COPY_CAP = 64 * 2**20
_TABLE_SUFFIXES = (".csv", ".tsv")
#: Writes that are an interpreter's or a library's housekeeping, never a tool's product: bytecode,
#: per-user caches, lock sidecars (e.g. the obs-db catalog's `catalog.json.lock`, a zero-byte file
#: opened read-write only to hold an flock — measured 2026-09-25 rejecting an honest tool 4x), and
#: the catalog writer's backup snapshots under `.catalog_history/` (copied on every update).
_HOUSEKEEPING = ("__pycache__", "/.cache/", "/.config/", "/.matplotlib/", "/.astropy/",
                 "/.catalog_history/")
_HOUSEKEEPING_SUFFIXES = (".pyc", ".lock")

_RECORDER_BODY = r'''
import functools, importlib, itertools, json, os, shutil, sys, threading, time

_OUT = Path(os.environ["AR_RETURNS_OUT"])
_TOOLS = json.loads(os.environ["AR_RETURNS_TOOLS"])
_CAP = int(os.environ["AR_RETURNS_COPY_CAP"])
_local = threading.local()
_seq = itertools.count()


def _write(rec):
    with open(_OUT / "returns.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def _stat(p):
    try:
        st = os.stat(p)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _audit(event, args):
    written = getattr(_local, "written", None)
    if event != "open" or written is None:
        return
    path, mode, flags = args
    if isinstance(path, int) or path is None:
        return
    writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT))
    if writes:
        written.add(os.path.abspath(os.fsdecode(path)))


sys.addaudithook(_audit)


def _wrap(key, fn):
    import inspect as _inspect
    try:
        sig = _inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None

    @functools.wraps(fn)
    def inner(*a, **k):
        if getattr(_local, "depth", 0):
            return fn(*a, **k)
        try:
            args = dict(sig.bind_partial(*a, **k).arguments) if sig is not None else dict(k)
        except TypeError:
            args = dict(k)
        cwd = Path.cwd()
        named = [str(p) for p in _named_paths(args, cwd, ())]
        before = {p: _stat(p) for p in named}
        started = call_started_ns()
        _local.depth, _local.written = 1, set()
        try:
            result = fn(*a, **k)
        finally:
            _local.depth, written, _local.written = 0, _local.written, None
        rec = {"tool": key, "started_ns": started, "before": before}
        try:
            payload = repl_payload(result)
            rec["payload"] = json.loads(json.dumps(payload))
        except Exception as exc:
            rec["transmit_error"] = f"{type(exc).__name__}: {exc}"
            _write(rec)
            return result
        text = normalize_tool_raw(rec["payload"]).get("stdout") or ""
        paths = set(named) | set(written)
        paths |= {str(Path(m.group(1).rstrip("."))) for m in _PATH_IN_TEXT.finditer(text)}
        figs = rec["payload"].get("figures")
        if isinstance(figs, list):
            paths |= {str(Path(f["path"])) for f in figs if isinstance(f, dict) and "path" in f}
        rec["after"] = {p: _stat(p) for p in paths}
        rec["is_file"] = {p: os.path.isfile(p) for p in paths}
        rec["written"] = sorted(written)
        rec["copies"], budget = {}, _CAP
        for p in sorted(paths):
            size = (rec["after"].get(p) or [None])[0]
            if rec["is_file"].get(p) and size is not None and size <= budget:
                name = f"f{next(_seq)}"
                try:
                    shutil.copyfile(p, _OUT / "files" / name)
                except OSError:
                    continue
                rec["copies"][p] = name
                budget -= size
        _write(rec)
        return result

    inner.__ar_returns_original__ = fn
    return inner


def pytest_configure(config):
    (_OUT / "files").mkdir(parents=True, exist_ok=True)
    _write({"event": "loaded"})


def pytest_collection_finish(session):
    for key, (module, name) in _TOOLS.items():
        try:
            fn = getattr(importlib.import_module(module), name)
        except Exception as exc:
            _write({"event": "import_error", "tool": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        fn = getattr(fn, "__ar_returns_original__", fn)
        wrapped = _wrap(key, fn)
        for mod in list(sys.modules.values()):
            for attr, value in list(getattr(mod, "__dict__", {}).items()):
                if value is fn:
                    setattr(mod, attr, wrapped)
'''


def recorder_source() -> str:
    """The pytest plugin, assembled from the runtime's own source for every rule it applies."""
    return "\n\n".join([
        "from __future__ import annotations\nimport json, re\nfrom pathlib import Path",
        f"PROTOCOL_KEYS = frozenset({sorted(PROTOCOL_KEYS)!r})",
        f"_PATH_IN_TEXT = re.compile({_PATH_IN_TEXT.pattern!r})",
        textwrap.dedent(inspect.getsource(repl_payload)),
        textwrap.dedent(inspect.getsource(normalize_tool_raw)),
        textwrap.dedent(inspect.getsource(_named_paths)),
        textwrap.dedent(inspect.getsource(call_started_ns)),
        _RECORDER_BODY,
    ])


def install(out_dir: Path, tools: dict[str, tuple[str, str]], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Write the plugin into ``out_dir``; return ``(pytest args, env)`` that load it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{PLUGIN_NAME}.py").write_text(recorder_source(), encoding="utf-8")
    env = {
        **env,
        "PYTHONPATH": f"{out_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(out_dir),
        "AR_RETURNS_OUT": str(out_dir),
        "AR_RETURNS_TOOLS": json.dumps({k: list(v) for k, v in tools.items()}),
        "AR_RETURNS_COPY_CAP": str(COPY_CAP),
    }
    return ["-p", PLUGIN_NAME], env


def _fail(check: str, detail: str) -> CheckResult:
    return CheckResult(name=f"layer_b.returns.{check}", layer="B", passed=False, detail=detail)


def _housekeeping(path: str, out_dir: Path) -> bool:
    return (path.startswith(str(out_dir)) or any(h in path for h in _HOUSEKEEPING)
            or path.endswith(_HOUSEKEEPING_SUFFIXES))


def judge_record(rec: dict, out_dir: Path) -> list[CheckResult]:
    """Every way one recorded call falls short of what the analyst runtime needs from it."""
    tool = rec["tool"]
    if "transmit_error" in rec:
        return [_fail("transmit", (
            f"{tool} returned a value the tool REPL cannot send ({rec['transmit_error']}), so the "
            "call fails in a real run. Return plain JSON types: float()/int() for numpy scalars, "
            ".tolist() for arrays, str() for paths."
        ))]
    raw = normalize_tool_raw(rec["payload"])
    text, exit_code = tool_text(raw)
    if exit_code != 0:
        return []  # a failed call is receipted as failed and grounds nothing; only transmit applies
    out: list[CheckResult] = []
    after = {p: tuple(s) if s else None for p, s in rec.get("after", {}).items()}
    is_file = rec.get("is_file", {})

    figures, notes = parse_tool_figures(raw.get("figures"))
    for note in notes:
        out.append(_fail("figure", f"{tool}: {note}. The runtime drops this figure; give each "
                                   "figure the keys and values FigureRef accepts."))
    figure_paths: set[str] = set()
    for fig in figures:
        if not is_file.get(str(fig.path)):
            out.append(_fail("figure", (
                f"{tool} returned figure '{fig.caption}' whose file {fig.path} does not exist when "
                "the call returns. The runtime drops it; return the path of the PNG the call wrote."
            )))
        else:
            figure_paths.add(str(Path(fig.path).resolve()))

    state = CallInputs(
        before={p: tuple(s) if s else None for p, s in rec.get("before", {}).items()},
        started_ns=rec["started_ns"],
    )
    products = data_products(
        state, text, failed=False, figure_paths=figure_paths,
        stat=lambda p: after.get(str(p)), is_file=lambda p: bool(is_file.get(str(p))),
    )
    receipted = {str(Path(p).resolve()) for p in products} | figure_paths
    for path in rec.get("written", []):
        if not is_file.get(path) or _housekeeping(path, out_dir):
            continue
        if str(Path(path).resolve()) not in receipted:
            out.append(_fail("receipted", (
                f"{tool} wrote {path}, which the runtime does not receipt, so nothing in it can be "
                "cited. Take the output path as an argument, or print its absolute path in stdout."
            )))

    texts: dict[str, str] = {"result": normalise(text, "result")}
    for path in products:
        copy = rec.get("copies", {}).get(str(path))
        body = checkable_text((out_dir / "files" / copy).read_bytes()) if copy else None
        if body is None:
            continue
        texts[str(path)] = normalise(body, "data")
        if path.suffix.lower() in _TABLE_SUFFIXES and parse_delimited(body) is None:
            out.append(_fail("table", (
                f"{tool} wrote the table {path}, which does not parse as a delimited table (a "
                "header row and at least one data row). Write it as CSV or TSV with a header."
            )))

    if rec["payload"].get("stdout"):   # with stdout empty, the other keys ARE the result text
        for key, value in raw.items():
            if key in PROTOCOL_KEYS:
                continue
            prose = json.dumps(value, default=str)
            missing = ungrounded_numbers(prose, texts)
            if missing:
                shown = ", ".join(m.group() for m in missing[:5])
                more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
                out.append(_fail("number", (
                    f"{tool} returns '{key}' carrying {shown}{more}, which appear in neither its "
                    "stdout nor a file the runtime receipts, so no report can cite them. Print each "
                    "value in stdout as returned, or write it to the output file."
                )))
    return out


def judge(out_dir: Path, tools: dict[str, tuple[str, str]]) -> tuple[list[CheckResult], list[CheckResult]]:
    """``(checks, findings)`` for one Layer-B run's records. No record of the plugin loading is a
    failure (the check did not run); a watched tool no test called is a finding for the co-lead."""
    path = Path(out_dir) / RECORDS
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    records = [json.loads(line) for line in lines if line.strip()]
    if not any(r.get("event") == "loaded" for r in records):
        return [CheckResult(
            name="layer_b.returns.recorder", layer="B", passed=False,
            detail=("the return check did not run: its pytest plugin never loaded. Leave pytest "
                    "plugin loading to the gate (no -p no:... in pytest.ini, setup.cfg or conftest)."),
        )], []
    checks: list[CheckResult] = []
    called: set[str] = set()
    for rec in records:
        if rec.get("event") == "import_error":
            checks.append(_fail("recorder", f"{rec['tool']} could not be imported to watch: {rec['error']}"))
        elif "tool" in rec and "event" not in rec:
            called.add(rec["tool"])
            checks.extend(judge_record(rec, Path(out_dir)))
    # One line per distinct problem: a test that calls a tool twenty times reports it once.
    checks = list({c.detail: c for c in checks}.values())
    if not checks:
        checks = [CheckResult(name="layer_b.returns", layer="B", passed=True,
                              detail=f"{len(called)} of {len(tools)} watched tool(s) called")]
    findings = [
        CheckResult(name="finding.returns_unobserved", layer="B", passed=True,
                    detail=f"{key}: no test calls it, so its returns were never checked")
        for key in sorted(set(tools) - called)
    ]
    return checks, findings


def watched_tools(worktree: Path, targets: dict[Path, set[int] | None], head_manifest) -> dict[str, tuple[str, str]]:
    """The new or changed tools of this changeset: ``{manifest key: (module, fn)}``.

    New = a key the working-tree manifest has and ``head_manifest(rel)`` (the committed one) lacks;
    changed = a key whose function body contains an added line. Legacy tools in a touched package
    are left alone, since the toolkit predates this contract.
    """
    import ast

    out: dict[str, tuple[str, str]] = {}
    manifests = {p for p in targets if p.name == "__init__.py" and p.parent.name == "skills"}
    packages = {p.relative_to(worktree).parts[0] for p in targets}
    for pkg in packages:
        manifests.add(worktree / pkg / "skills" / "__init__.py")
    for manifest in sorted(manifests):
        if not manifest.is_file():
            continue
        entries = manifest_entries(manifest.read_text(encoding="utf-8"))
        old = manifest_entries(head_manifest(str(manifest.relative_to(worktree))))
        for key, (module, fn) in entries.items():
            if old.get(key) != (module, fn):  # new, or re-pointed to another module or function
                out[key] = (module, fn)
                continue
            base = worktree / module.replace(".", "/")
            source = next((p for p in (base.with_suffix(".py"), base / "__init__.py") if p in targets), None)
            if source is None:
                continue
            added = targets[source]
            try:
                text = source.read_bytes().decode("utf-8")
                tree = ast.parse(text)
            except (OSError, SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
                out[key] = (module, fn)  # cannot be read: watched, never skipped
                continue
            if re.search(r"\r(?!\n)", text):
                # Python and git count lines differently around a lone CR: the span cannot be
                # mapped to git's added lines, so the tool is watched (B3c-1 review 25).
                out[key] = (module, fn)
                continue
            top = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn]
            if not top:  # defined under an `if`/`try`, or not as a plain `def`: watched, never skipped
                out[key] = (module, fn)
                continue
            for node in top:
                # From the first decorator: one added above an unchanged `def` changes the tool.
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                span = range(first, (node.end_lineno or node.lineno) + 1)
                if added is None or any(n in added for n in span):
                    out[key] = (module, fn)
    return out


def manifest_entries(source: str) -> dict[str, tuple[str, str]]:
    """``{key: (module, fn)}`` from a ``TOOL_MANIFEST`` dict literal; entries not written as literals
    are skipped."""
    import ast

    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return {}
    for node in ast.walk(tree):
        target = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not any(isinstance(t, ast.Name) and t.id == "TOOL_MANIFEST" for t in target):
            continue
        if not isinstance(node.value, ast.Dict):
            return {}
        out: dict[str, tuple[str, str]] = {}
        for k, v in zip(node.value.keys, node.value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Dict)):
                continue
            fields = {fk.value: fv.value for fk, fv in zip(v.keys, v.values)
                      if isinstance(fk, ast.Constant) and isinstance(fv, ast.Constant)}
            if isinstance(fields.get("module"), str) and isinstance(fields.get("fn"), str):
                out[k.value] = (fields["module"], fields["fn"])
        return out
    return {}
