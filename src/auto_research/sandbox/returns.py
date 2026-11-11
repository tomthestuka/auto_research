"""The gate's dynamic return check [G15]: what a new tool actually returns, judged as the runtime would.

The static figure-shape scan reads source and is beaten by one indirection (measured 7 of 7, live,
2026-09-25). This check watches the tool run instead. During the gate's Layer-B pytest run a
recorder wraps every new or changed ``TOOL_MANIFEST`` function; each call's return, the pre/post
state of every path the runtime could consult (its figures and declared products), and copies of
the files the call wrote are written down. The gate process then judges those records with the analyst runtime's own functions:

  layer_b.returns.transmit   the payload survives the REPL's ``json.dumps``  (``persistent.repl_payload``)
  layer_b.returns.figure     each figure survives ``parse_tool_figures`` and its file exists
  layer_b.returns.receipted  each declared product survives ``capture.parse_products`` and its file
                             exists
  layer_b.returns.table      each declared ``.csv``/``.tsv`` parses with ``render.parse_delimited``
  layer_b.returns.number     each number in the other returned keys is in the result text or a
                             declared product, by ``bindings.ungrounded_numbers``
  layer_b.returns.placed     opt-in: the runtime's own minting decision, ``capture.admit_artifacts``
                             (placement inside the call's output argument, no link, no duplicate,
                             at most ``_MAX_PRODUCTS``), run in the test process, drops nothing; a
                             file inside an output that the call returns under another key is
                             declared
  layer_b.returns.store      a store's products (step 0b): with a throwaway store in the run
                             (``store.ci``), each watched call is bracketed by the runtime's own
                             ``StoreRecords.bracket`` (the test's producer key; the store's listing
                             and access log, never the tool's word). Every version it wrote lies in
                             a store its manifest names in ``writes``, creates its key (no
                             overwrite, no delete marker), carries the helper's five
                             ``x-amz-meta-*`` entries (``sha256`` = its bytes'), and each recipe hash
                             it wrote under ends with one complete ``recipe.json``
                             (``reconcile.RecipeFile``: the catalog's hash of its recipe, listing
                             every other object of the hash, key, version, sha256 and size). A
                             raised or failed call is judged too, all but the recipe file

  layer_b.returns.unclassified  every file a call writes is in exactly one class (user rule
                             2026-11-04): inside an output folder (declared, else ``.placed``
                             fails), or scratch (interpreter bytecode; the home's caches:
                             ``~/.cache``, ``~/.astropy``, ``~/.xspec``, ``~/.matplotlib``,
                             ``~/.config/{matplotlib,astropy}``, the first folders of each declared private variable
                             (``env_externals.<name>.private``), ``$MPLCONFIGDIR``,
                             ``$XDG_CACHE_HOME``, ``/dev/shm``; the temp area outside pytest's
                             ``--basetemp`` and the tree under test); any other file fails. Each
                             output folder and the running test's ``tmp_path`` are walked before
                             and after the call, so a subprocess's or a rename's file is seen

Scratch is not otherwise judged. A store is reached over S3 only, so its products are judged by
``.store`` from the store's records, never as files.

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
from auto_research.provenance.calls import FIGURE_PATH_ARGS
from auto_research.provenance.capture import (
    _ASK,
    _ASK_FEWER,
    _MAX_PRODUCTS,
    _named_paths,
    PRODUCT_KEYS,
    DeclaredProduct,
    _product_problem,
    admit_artifacts,
    call_started_ns,
    parse_products,
)
from auto_research.provenance.inputs import declared_outputs
from auto_research.provenance.store import checkable_text
from auto_research.report.render import parse_delimited
from auto_research.sandbox.persistent import repl_payload
from auto_research.sandbox.runner import (
    PROTOCOL_KEYS,
    builtin_function_source,
    normalize_tool_raw,
    parse_tool_figures,
    tool_text,
)
from auto_research.schemas import CheckResult
from auto_research.schemas.figures import FigureRef

PLUGIN_NAME = "ar_returns"
RECORDS = "returns.jsonl"
#: Bytes copied per call for the table and number checks; a larger product is recorded uncopied.
COPY_CAP = 64 * 2**20
_TABLE_SUFFIXES = (".csv", ".tsv")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".svg")
#: The actions the gate's notes name for the coder (the runtime's notes name the worker's).
_GATE_ASK = "write the file itself under the call's output argument"
_GATE_ASK_FEWER = "gather them into fewer files"
#: The keys a returned figure carries, read off ``FigureRef`` (the recorder cannot import it).
_FIGURE_REQUIRED = frozenset(k for k, f in FigureRef.model_fields.items() if f.is_required())
_FIGURE_ALLOWED = frozenset(FigureRef.model_fields)

_RECORDER_BODY = r'''
import functools, importlib, itertools, json, os, shutil, socket, sys, tempfile, threading, time, uuid

_OUT = Path(os.environ["AR_RETURNS_OUT"])
_TOOLS = json.loads(os.environ["AR_RETURNS_TOOLS"])
_CAP = int(os.environ["AR_RETURNS_COPY_CAP"])
_local = threading.local()
_seq = itertools.count()
#: The runtime's bracket socket (``store.ci``), read once at load: unset when the run has no store.
_STORE = os.environ.get("AR_STORE_CALLS") or None
#: Seconds the recorder waits for the runtime to list the store before or after a call.
_STORE_WAIT = 600


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


def _within(path, folder):
    return os.path.commonpath([path, folder]) == folder


#: The bracket open in this process, if any: a store tool called while it is open, from any thread,
#: joins it (its writes are that call's), so a call fanning out to threads never waits on itself.
_OPEN = {"count": 0, "call": None, "lock": threading.Lock()}


def _store_open(key):
    """Open the runtime's bracket around one call of ``key`` (``store.ci``): ``(connection, lines,
    record)``; the runtime has listed the store when this returns. ``(None, None, None)`` when the
    run has no store; ``(None, None, {"call", "joined"})`` inside an open bracket (any thread);
    ``(None, None, {"call", "error"})`` when the bracket could not open."""
    if not _STORE:
        return None, None, None
    with _OPEN["lock"]:
        _OPEN["count"] += 1
        if _OPEN["count"] > 1:
            return None, None, {"call": _OPEN["call"], "joined": True}
        call = _OPEN["call"] = uuid.uuid4().hex
    try:
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(_STORE_WAIT)
        conn.connect(_STORE)
        conn.sendall((json.dumps({"call": call, "tool": key}) + "\n").encode())
        lines = conn.makefile("rb")
        answer = json.loads(lines.readline() or b"{}")
    except Exception as exc:
        return None, None, {"call": call, "error": f"{type(exc).__name__}: {exc}"}
    if not answer.get("ok"):
        conn.close()
        return None, None, {"call": call, "error": str(answer.get("error") or "the runtime gave no answer")}
    return conn, lines, {"call": call}


def _store_close(conn, lines, info):
    """Close the bracket (the outermost call's): the runtime lists the store again and records what
    the call wrote."""
    if info is None:
        return
    with _OPEN["lock"]:
        _OPEN["count"] -= 1
    if conn is None:
        return
    try:
        conn.sendall(b'{"end": true}\n')
        if not json.loads(lines.readline() or b"{}").get("ok"):
            info["error"] = "the runtime did not record the call's store writes"
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


#: pytest's base temporary folder (`--basetemp`, the tests' `tmp_path` lives under it), set at
#: configure time: a write there outside the classes is "other", the rest of the temp area scratch.
_BASETEMP = [None]
#: The tree under test (pytest runs from it): a write into it is never scratch, wherever it lives.
_TREE = [None]
#: The scratch roots, read once from the gate's own environment at configure time (a test or a
#: tool that sets a variable cannot widen them).
_SCRATCH = [[]]
#: The running test's ``tmp_path``, walked before and after each call so a write a subprocess,
#: a thread, C code or a rename makes there is seen.
_TEST_TMP = [None]
#: The most files one output folder is walked for, before and after a call.
_OUTPUT_WALK_CAP = 5000


def _files_under(folder, links=None):
    """``{realpath: [size, mtime]}`` of the files under ``folder`` (``None`` past the cap). A link
    is left out: one the call makes (an input staged into its output folder) is staging, never a
    product and never a write to the file it points to. ``links``, when given, collects each
    link's resolved target, so a target the call changed through it is judged where it lies."""
    found = {}
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                if links is not None:
                    links.add(os.path.realpath(full))
                continue
            found[os.path.realpath(full)] = _stat(full)
        if len(found) > _OUTPUT_WALK_CAP:
            return None
    return found


def _scratch_roots():
    """Where libraries keep their own caches: the jail home's caches and the parameter folders
    (each declared private variable's first part, matplotlib, astropy, XSPEC, the XDG cache), plus
    shared memory. A tool's own home default for a variable nobody declared is no root: a write
    there is unclassified, which tells the human the declaration is missing. Read at configure
    time; a root that would hold the tests' tmp_path, the tree or the home is dropped."""
    home = os.path.expanduser("~")
    roots = [os.path.join(home, d) for d in (".cache", ".astropy", ".xspec", ".matplotlib",
                                              os.path.join(".config", "matplotlib"),
                                              os.path.join(".config", "astropy"))]
    for var in ("MPLCONFIGDIR", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
        if os.environ.get(var):
            roots.append(os.environ[var])
    for name, sep in json.loads(os.environ.get("AR_PRIVATE_FOLDERS") or "{}").items():
        for part in os.environ.get(name, "").split(sep)[0].split(os.pathsep):
            if part.strip():
                roots.append(part.strip())
    roots.append("/dev/shm")
    guarded = [os.path.realpath(g) for g in (_BASETEMP[0], _TREE[0], home) if g]
    return [r for r in (os.path.realpath(x) for x in roots)
            if not any(_within(g, r) for g in guarded)]


def _classes(written, out_before, folders, declared, inputs, returned=(), started=0):
    """Every file the call left written, by class: ``{"undeclared", "unclassified",
    "at_path_args", "notes"}``.
    Output: inside an output folder, declared (``declared=None``: not judged, the call raised).
    A file at a path the test passed in a ``path`` argument is no class (named apart, so its message says to declare the argument an
    ``output``). Scratch: interpreter bytecode, the home's caches, the temp area outside
    the tests' own ``tmp_path`` and the tree under test. Anything else is unclassified."""
    candidates = {os.path.realpath(w) for w in written if os.path.isfile(w)}
    notes = []
    targets = set()
    for real, then in out_before.items():
        now = _files_under(real, targets) if os.path.isdir(real) else {}
        if then is None or now is None:  # past the walk cap: only the audited writes are judged there
            notes.append(f"{real} holds over {_OUTPUT_WALK_CAP} files; only Python writes there were judged")
            continue
        candidates |= {p for p, st in now.items() if then.get(p) != st}
    # A link's target changed during the call (a write through a staged input) is judged at the target:
    # one in a folder walked before the call by its stat then, any other by its mtime (the coarse
    # clock stamps a file written in the call's first tick with the call's own start, hence >=).
    known = {}
    for then in out_before.values():
        known.update(then or {})
    candidates |= {t for t in targets if os.path.isfile(t) and (
        known[t] != _stat(t) if t in known else (_stat(t) or [0, 0])[1] >= started)}
    outputs = [f[2] for f in folders]
    asked = {os.path.realpath(p) for p in inputs}
    returned = {os.path.realpath(p) for p in returned}
    scratch = _SCRATCH[0]
    temp = os.path.realpath(tempfile.gettempdir())
    base = os.path.realpath(_BASETEMP[0]) if _BASETEMP[0] else None
    out = {"undeclared": [], "unclassified": [], "at_path_args": [], "notes": notes}
    for path in sorted(candidates):
        if any(_within(path, f) for f in outputs):
            if declared is not None and path not in declared and path not in returned:
                out["undeclared"].append(path)  # (a returned path is named once, by `.placed`)
        elif "__pycache__" in path.split(os.sep):
            continue  # interpreter and JIT caches (bytecode, numba's index and data files)
        elif _TREE[0] and _within(path, _TREE[0]):
            out["unclassified"].append(path)
        elif any(_within(path, r) for r in scratch):
            continue
        elif _within(path, temp) and not (base and _within(path, base)) and "pytest-of-" not in path:
            continue
        elif path in asked:  # an output routed through a `path` argument skips minting: no class
            out["at_path_args"].append(path)
        else:
            out["unclassified"].append(path)
    return out


def _wrap(key, fn, placed, path_args=None):
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
            bound = sig.bind_partial(*a, **k) if sig is not None else None
            args = dict(bound.arguments) if bound is not None else dict(k)
            if bound is not None:
                bound.apply_defaults()
            full = dict(bound.arguments) if bound is not None else args
        except TypeError:
            args = full = dict(k)
        cwd = Path.cwd()
        # Where the runtime would place the call's outputs: each output (and figure) argument's
        # folder, as the test passed it (a file's or prefix's folder is the one it sits in).
        folders = []
        for name, shape in placed.items():
            value = full.get(name)
            if isinstance(value, (str, os.PathLike)) and os.fspath(value).strip():
                where = os.path.abspath(os.fspath(value))
                folder = where if shape == "folder" else os.path.dirname(where)
                folders.append([name, folder, os.path.realpath(folder)])
        inputs = {os.path.abspath(os.fspath(v)) for n, v in full.items() if n not in placed
                  and isinstance(v, (str, os.PathLike)) and os.fspath(v).strip() and "\0" not in os.fspath(v)}
        # The files the test named in `path`-kind arguments: a write there is unclassified,
        # named apart so its message fits.
        asked_paths = {
            os.path.abspath(os.fspath(full[n])) for n in path_args or ()
            if isinstance(full.get(n), (str, os.PathLike)) and os.fspath(full[n]).strip() and "\0" not in os.fspath(full[n])}
        out_before = {f[2]: (_files_under(f[2]) if os.path.isdir(f[2]) else {}) for f in folders}
        test_tmp = _TEST_TMP[0]
        if test_tmp and os.path.isdir(test_tmp) and os.path.realpath(test_tmp) not in out_before:
            out_before[os.path.realpath(test_tmp)] = _files_under(test_tmp)
        named = [str(p) for p in _named_paths(args, cwd, ())]
        before = {p: _stat(p) for p in named}
        started = call_started_ns()
        # The call's own, empty private folders, as the analyst REPL gives each call (G4); taken
        # before the call is marked as running, so a failure here leaves nothing half-set.
        private_env, made = private_folders(os.environ)
        private_before = {k: os.environ.get(k) for k in private_env if os.environ.get(k) != private_env[k]}
        rec = {"tool": key, "started_ns": started, "before": before}
        # The runtime's bracket of the store (a run with a store): opened once the store is listed.
        conn, lines, store = _store_open(key)
        if store is not None:
            rec["store"] = store
        _local.depth, _local.written = 1, set()

        def classes_now(declared):
            try:
                return _classes(written, out_before, folders, declared, asked_paths,
                                [os.path.join(str(cwd), p) for _k, p in rec.get("returned_files", [])],
                                started)
            except Exception as exc:
                return {"undeclared": [], "unclassified": [], "error": f"{type(exc).__name__}: {exc}"}

        os.environ.update(private_env)
        try:
            result = fn(*a, **k)
        except BaseException as exc:
            # A call that raises is a failed call: what it wrote is judged all the same.
            _local.depth, written, _local.written = 0, _local.written, None
            _store_close(conn, lines, store)
            os.environ.update(private_before)
            for folder in made:
                shutil.rmtree(folder, ignore_errors=True)
            rec["raised"] = f"{type(exc).__name__}: {exc}"
            rec["classes"] = classes_now(None)
            _write(rec)
            raise
        _local.depth, written, _local.written = 0, _local.written, None
        _store_close(conn, lines, store)
        os.environ.update(private_before)
        for folder in made:
            shutil.rmtree(folder, ignore_errors=True)
        try:
            payload = repl_payload(result)
            rec["payload"] = json.loads(json.dumps(payload))
        except Exception as exc:
            rec["transmit_error"] = f"{type(exc).__name__}: {exc}"
            _write(rec)
            return result
        declared = set()
        for kind in ("figures", "products"):  # each keyed as its reader reads it (products: stripped)
            entries = rec["payload"].get(kind)
            if isinstance(entries, list):
                declared |= {str(Path(f["path"].strip() if kind == "products" else f["path"])) for f in entries
                             if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"].strip()}
        paths = set(named) | set(written) | declared
        rec["after"] = {p: _stat(p) for p in paths}
        rec["is_file"] = {p: os.path.isfile(p) for p in paths}
        rec["written"] = sorted(written)
        rec["cwd"], rec["folders"], rec["placed_names"] = str(cwd), folders, sorted(placed)
        # The opt-in rule, by the runtime's own function (`capture.admit_artifacts`): each figure
        # and product the call returns that exists, in the output folder
        # that holds it (the runtime has one folder per call; a test may pass several).
        def _home(path):
            held = [f[1] for f in folders if _within(path, f[1])]
            return held[0] if held else (folders[0][1] if folders else None)

        # Read as the runtime reads them: products by `parse_products` itself; figures by the keys
        # `FigureRef` takes (its value checks need pydantic, which this interpreter lacks; a figure
        # they drop also fails `.figure`).
        declared_products, _notes = parse_products(rec["payload"].get("products"))
        figures = rec["payload"].get("figures")
        entries = [("figure", str(f.get("caption")), f["path"]) for f in (figures if isinstance(figures, list) else [])
                   if isinstance(f, dict) and _FIGURE_REQUIRED <= set(f) <= _FIGURE_ALLOWED
                   and isinstance(f.get("path"), str) and f["path"].strip()]
        entries += [("product", d.caption, str(d.path)) for d in declared_products]
        artifacts = []
        for singular, caption, declared_path in entries:
            path = os.path.join(str(cwd), declared_path)
            if os.path.isfile(path):
                home = _home(os.path.abspath(path))
                real = next((f[2] for f in folders if f[1] == home), None)
                artifacts.append((singular, caption, Path(path), Path(home) if home else None,
                                  Path(os.path.dirname(home)) if home else None,
                                  Path(real) if real else None))
        # With no output folder the judge names the fix itself (take an output argument, or pass it).
        rec["admit_notes"] = [] if not folders else admit_artifacts(
            artifacts, lambda _path: True, ask=_GATE_ASK, ask_fewer=_GATE_ASK_FEWER)[1]
        inside = lambda s: any(_within(os.path.abspath(s), f[1]) for f in folders)
        returned = []

        def _strings(key, value):
            if isinstance(value, str) and value.strip() and len(value) < 4096 and "\n" not in value and "\0" not in value:
                text = value.strip()
                # A file inside an output folder, not an echoed input.
                if inside(text) and os.path.isfile(text) and os.path.abspath(text) not in inputs:
                    returned.append([key, text])
            elif isinstance(value, dict):
                for item in value.values():
                    _strings(key, item)
            elif isinstance(value, list):
                for item in value:
                    _strings(key, item)

        if folders:
            for key_, value in rec["payload"].items():
                if key_ not in PROTOCOL_KEYS:
                    _strings(key_, value)
        rec["returned_files"] = returned
        rec["classes"] = classes_now({os.path.realpath(os.path.join(str(cwd), d)) for d in declared})
        rec["copies"], budget = {}, _CAP
        for p in sorted(declared):  # only what is judged: a scratch file spends no budget
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


def _bracket_only(key, fn):
    """A store tool this change leaves unchanged: bracketed (its writes are a call's, not the
    test's), never judged."""
    @functools.wraps(fn)
    def inner(*a, **k):
        if getattr(_local, "depth", 0):
            return fn(*a, **k)
        conn, lines, info = _store_open(key)
        _local.depth = 1
        try:
            return fn(*a, **k)
        finally:
            _local.depth = 0
            _store_close(conn, lines, info)
            if info is not None and "error" in info:  # its writes would read as written outside a call
                _write({"event": "store_error", "tool": key, "error": info["error"]})

    inner.__ar_returns_original__ = fn
    return inner


def pytest_configure(config):
    _BASETEMP[0] = getattr(config.option, "basetemp", None)
    _TREE[0] = os.path.realpath(os.getcwd())
    _SCRATCH[0] = _scratch_roots()


def pytest_runtest_call(item):
    tmp = getattr(item, "funcargs", {}).get("tmp_path")
    _TEST_TMP[0] = str(tmp) if tmp is not None else None
    (_OUT / "files").mkdir(parents=True, exist_ok=True)
    _write({"event": "loaded"})


def pytest_collection_finish(session):
    store_tools = json.loads(os.environ.get("AR_STORE_TOOLS") or "{}") if _STORE else {}
    for key, entry in [*_TOOLS.items(), *((k, e) for k, e in store_tools.items() if k not in _TOOLS)]:
        module, name = entry[0], entry[1]
        placed = entry[2] if len(entry) > 2 else {}
        path_args = entry[3] if len(entry) > 3 else ()
        if key not in _TOOLS and module not in sys.modules:
            continue  # an unchanged store tool no collected test imports: nothing of the run calls it
        try:
            fn = getattr(importlib.import_module(module), name)
        except Exception as exc:
            if key in _TOOLS:
                _write({"event": "import_error", "tool": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        fn = getattr(fn, "__ar_returns_original__", fn)
        wrapped = _wrap(key, fn, placed, path_args) if key in _TOOLS else _bracket_only(key, fn)
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
        f"_MAX_PRODUCTS = {_MAX_PRODUCTS!r}\n_ASK = {_ASK!r}\n_ASK_FEWER = {_ASK_FEWER!r}",
        f"_GATE_ASK = {_GATE_ASK!r}\n_GATE_ASK_FEWER = {_GATE_ASK_FEWER!r}",
        f"_FIGURE_REQUIRED = frozenset({sorted(_FIGURE_REQUIRED)!r})\n"
        f"_FIGURE_ALLOWED = frozenset({sorted(_FIGURE_ALLOWED)!r})\nPRODUCT_KEYS = {PRODUCT_KEYS!r}",
        "from collections.abc import Mapping\nfrom dataclasses import dataclass",
        textwrap.dedent(inspect.getsource(DeclaredProduct)),
        textwrap.dedent(inspect.getsource(parse_products)),
        textwrap.dedent(inspect.getsource(_product_problem)),
        textwrap.dedent(inspect.getsource(admit_artifacts)),
        textwrap.dedent(inspect.getsource(repl_payload)),
        builtin_function_source("private_folders"),
        textwrap.dedent(inspect.getsource(normalize_tool_raw)),
        textwrap.dedent(inspect.getsource(_named_paths)),
        textwrap.dedent(inspect.getsource(call_started_ns)),
        _RECORDER_BODY,
    ])


def install(out_dir: Path, tools: dict[str, tuple], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
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


def _placed_checks(tool: str, rec: dict, artifacts: list[tuple[str, str]]) -> list[CheckResult]:
    """Opt-in: what the runtime's own minting decision (``capture.admit_artifacts``, run in the test
    process) drops fails; a file inside an output returned under another key is declared; an output
    the test leaves unset is named."""
    import os

    folders, cwd = rec.get("folders", []), rec.get("cwd", "")
    out: list[CheckResult] = []
    names = rec.get("placed_names", [])
    for label, path in artifacts:
        if not folders and names:
            out.append(_fail("placed", f"{tool} returns {label} at {path}, but its test leaves "
                                       f"{', '.join(names)} unset; the runtime always fills it. Pass it a "
                                       "folder of the test's own (tmp_path / 'out') and write there."))
        elif not folders:
            out.append(_fail("placed", f"{tool} returns {label} at {path}, but the tool takes no output "
                                       "argument, so the runtime gives its call no folder and drops it. Take "
                                       "an output-kind argument (shape folder, file or prefix) and write every "
                                       "product under it."))
    # Placement, links, duplicates and the cap: the runtime's own decision, taken in the test process.
    shown = ", ".join(f"{f[0]} ({f[1]})" for f in folders)
    for note in rec.get("admit_notes", []):
        if "already receipted" in note:
            action = "The runtime drops it the same way; declare each file once."
        elif note[:1].isdigit():  # the cap
            action = "The runtime drops them the same way."
        else:
            action = f"The runtime drops it the same way (the call's output arguments: {shown})."
        out.append(_fail("placed", f"{tool}: {note}. {action}"))
    declared = {os.path.normpath(os.path.join(cwd, p)) for _label, p in artifacts}
    for key, path in rec.get("returned_files", []):
        if os.path.normpath(os.path.join(cwd, path)) not in declared:
            where = "'figures' as a FigureRef" if path.lower().endswith(_IMAGE_SUFFIXES) else \
                "'products' as {\"path\": …, \"caption\": …}"
            out.append(_fail("placed", f"{tool} returns the file {path} under '{key}' without declaring it, "
                                       f"so the runtime receipts nothing for it. Return it under {where}."))
    return out


def _class_checks(tool: str, rec: dict) -> list[CheckResult]:
    """Every file a call writes is in exactly one class (user rule 2026-11-04): a file inside an
    output folder is declared (``.placed``); a file in no class fails ``.unclassified``."""
    classes = rec.get("classes") or {}
    out: list[CheckResult] = []
    if "error" in classes:
        out.append(_fail("unclassified", f"{tool}'s writes could not be classified ({classes['error']})."))
    for note in classes.get("notes", []):
        out.append(CheckResult(name="finding.returns_unwalked", layer="B", passed=True, detail=f"{tool}: {note}"))
    for path in classes.get("undeclared", []):
        out.append(_fail("placed", (
            f"{tool} writes {path} inside its output argument and declares it nowhere. Return it under "
            "'products' (a PNG under 'figures'); a file the tool keeps for itself goes in a temporary "
            "folder (tempfile) instead."
        )))
    for path in classes.get("at_path_args", []):
        out.append(_fail("unclassified", (
            f"{tool} writes {path}, a file its test names in a `path` argument; the runtime mints "
            "nothing written there. Declare that argument as an output (\"kind\": \"output\", "
            "\"shape\": \"file\") and return the file under 'products', or write the tool's own "
            "bookkeeping in a temporary folder (tempfile); a tool that changes an input writes the "
            "changed copy under an output argument."
        )))
    for path in classes.get("unclassified", []):
        out.append(_fail("unclassified", (
            f"{tool} writes {path}, which is in no class. Write an artifact under an output argument "
            "and declare it, a product for a store into the store over S3 (the tool's \"writes\"), and "
            "the program's own bookkeeping in a temporary folder (tempfile) or the home's caches."
        )))
    return out


def _store_meta() -> tuple[str, ...]:
    """The metadata the toolkit's store helper writes on every object (``store.reconcile.META_*``)."""
    from auto_research.store import reconcile

    return (reconcile.META_RECIPE_HASH, reconcile.META_SHA256, reconcile.META_PRODUCER,
            reconcile.META_TOOLKIT_COMMIT, reconcile.META_ENV_FINGERPRINT)


def _where(w: dict) -> str:
    return f"{w['store']} {w['bucket']}/{w['key']} (version {w['version_id']})"


def _store_checks(tool: str, rec: dict, calls: dict[str, dict] | None, *, complete: bool) -> list[CheckResult]:
    """What one bracketed call wrote in the run's throwaway store, as the store recorded it
    (``store.ci``): every version in a store the tool's manifest names in ``writes``, creating its key,
    with the helper's metadata; with ``complete`` (the call returned and did not fail), every recipe
    hash it wrote under ends with one complete recipe file, naming this tool. ``calls`` is ``None`` for
    a run without a store; in a run with one, a call the recorder did not bracket fails."""
    from pydantic import ValidationError

    from auto_research.store.reconcile import META_RECIPE_HASH, META_SHA256, RECIPE_FILE, RecipeFile

    info = rec.get("store")
    if calls is None:
        return []
    if info is None:
        return [_fail("store", f"the CI pipeline's run had a store and {tool}'s call was not recorded there "
                               "(the recorder found no bracket). Run the tests again; when it repeats, message "
                               "the lead with this check.")]
    facts = calls.get(str(info.get("call")))
    error = info.get("error") or (facts or {}).get("error")
    if error or facts is None:
        return [_fail("store", f"the CI pipeline could not record what {tool} wrote in its store "
                               f"({error or 'no record of the call'}). Run the tests again; when it repeats, "
                               "message the lead with this check.")]
    out: list[CheckResult] = []
    if "unverified" in facts.get("flags", []):
        notes = facts.get("notes", [])
        unpinned = [n for n in notes if "without a version" in n]
        if unpinned:
            out.append(_fail("store", f"{tool} " + "; ".join(unpinned) + ". Read each object by the version_id "
                                      "its record names."))
        rest = [n for n in notes if n not in unpinned]
        if rest or not unpinned:
            out.append(_fail("store", f"the store's records of {tool}'s call are incomplete: "
                                      + "; ".join(rest) + ". Write each product once, through one store key, "
                                      "within the call."))
    writes = facts.get("writes", [])
    removals = [n for n in facts.get("notes", []) if n.startswith("removed ")]
    for note in removals:
        out.append(_fail("store", f"{tool} {note}. Keep every version: write a new product instead."))
    if "undeclared_write" in facts.get("flags", []) and "unverified" not in facts.get("flags", []):
        # A write the store's log shows and its listing does not: named from the records' own notes.
        for note in facts.get("notes", []):
            if not note.startswith(("removed ", "wrote ")):
                out.append(_fail("store", f"{tool}: {note}. Write products with single or multipart PUTs "
                                          "of new keys only."))
    for w in writes:
        where = _where(w)
        if w.get("delete_marker"):
            out.append(_fail("store", f"{tool} deletes {where}. Keep every object; write a new product instead."))
            continue
        if not w.get("declared", True):
            out.append(_fail("store", (
                f"{tool} writes {where}, and its manifest names no {w['store']} under \"writes\". List each "
                "store the tool creates products in under its \"writes\", with its \"algorithm_version\".")))
        if w.get("overwrote"):
            out.append(_fail("store", (
                f"{tool} writes {where} over the key's earlier version {w['overwrote']}. Write each key once, "
                "create-only (If-None-Match: *), in the product's own recipe folder.")))
        if w.get("read_error"):
            out.append(_fail("store", f"{tool} wrote {where}, which the CI pipeline could not read back "
                                      f"({w['read_error']}). Write it with a single PUT, as the store helper does."))
            continue
        meta = w.get("metadata") or {}
        missing = [m for m in _store_meta() if not meta.get(m)]
        if missing:
            out.append(_fail("store", (
                f"{tool} stores {where} without the metadata {', '.join('x-amz-meta-' + m for m in missing)}. "
                "Store every object through the toolkit's store helper, which writes the recipe hash, sha256, "
                "producer, toolkit commit and environment fingerprint on each.")))
        elif meta.get(META_SHA256) != w.get("sha256"):
            out.append(_fail("store", f"{tool} stores {where} with x-amz-meta-{META_SHA256} "
                                      f"{meta.get(META_SHA256)!r}, which is not the sha256 of its bytes "
                                      f"({w.get('sha256')}). Name each object's own sha256."))
    if not complete:
        return out
    groups: dict[tuple[str, str], list[dict]] = {}
    for w in writes:
        recipe_hash = (w.get("metadata") or {}).get(META_RECIPE_HASH)
        if recipe_hash and not w.get("delete_marker"):
            groups.setdefault((w["bucket"], recipe_hash), []).append(w)
    for (bucket, recipe_hash), group in sorted(groups.items()):
        files = [w for w in group if w["key"].rsplit("/", 1)[-1] == RECIPE_FILE]
        members = [w for w in group if w not in files]
        if len(files) != 1:
            shown = ", ".join(w["key"] for w in group[:5]) + (" …" if len(group) > 5 else "")
            out.append(_fail("store", (
                f"{tool} stores {len(group)} object(s) of recipe {recipe_hash[:12]} in {bucket} ({shown}) and "
                f"{len(files)} {RECIPE_FILE}. End each product with one {RECIPE_FILE}, written last, listing "
                "every other object of the product (key, version_id, sha256, size).")))
            continue
        marker = files[0]
        try:
            listed = RecipeFile.model_validate_json(marker.get("recipe_file") or "")
        except ValidationError as exc:
            out.append(_fail("store", f"{tool}'s {_where(marker)} is not a recipe file ({exc.error_count()} "
                                      f"problems: {exc.errors()[0]['msg']}). Write it in the store helper's form."))
            continue
        if listed.recipe_hash != recipe_hash or marker.get("recipe_hash_computed") != recipe_hash:
            out.append(_fail("store", (
                f"{tool}'s {_where(marker)} names recipe hash {listed.recipe_hash[:12]}; its objects carry "
                f"{recipe_hash[:12]} and the catalog hashes its recipe to "
                f"{str(marker.get('recipe_hash_computed'))[:12]}. Name the hash catalog.claim answered.")))
        if listed.recipe.get("tool") != tool:
            out.append(_fail("store", (
                f"{tool}'s {_where(marker)} names the tool {listed.recipe.get('tool')!r}. Name the tool by its "
                "manifest key, as the reconciler qualifies a product by it; a toolkit tool calls library code "
                "only, so every product names the tool the agent called.")))
        entries = {(o.key, o.version_id): o for o in listed.objects}
        for w in members:
            o = entries.pop((w["key"], w["version_id"]), None)
            if o is None:
                out.append(_fail("store", f"{tool}'s {_where(marker)} leaves out {_where(w)}. List every "
                                          "object of the product in it."))
            elif o.size != w.get("size") or o.sha256 != w.get("sha256"):
                out.append(_fail("store", f"{tool}'s {_where(marker)} lists {_where(w)} with a size or sha256 "
                                          "its bytes do not have. List each object's own."))
        for (key, version), _o in sorted(entries.items()):
            out.append(_fail("store", f"{tool}'s {_where(marker)} lists {bucket}/{key} version {version}, which "
                                      "the call did not store. List the objects the call stored."))
    out.extend(_product_checks(tool, facts, sorted({h for _b, h in groups})))
    return out


def _product_checks(tool: str, facts: dict, hashes: list[str]) -> list[CheckResult]:
    """Ruling A, for a call that returned: each product it stored is claimed and committed in the
    catalog by the call's own producer login; and, per call (main's ruling 1), the union of its
    products' recipe inputs covers every version the call read from outside itself (the version
    itself, or the product it belongs to); a version of the call's own products, read back or chained
    into a later product of the same call, is no outside read."""
    if "catalog_error" in facts:
        return [_fail("store", f"the CI pipeline could not read the catalog after {tool}'s call "
                               f"({facts['catalog_error']}). Run the tests again; when it repeats, message the lead.")]
    out: list[CheckResult] = []
    own = {(w.get("store"), w.get("bucket"), w.get("key"), w.get("version_id")) for w in facts.get("writes", [])}
    reads = [r for r in facts.get("reads", [])
             if (r["store"], r["bucket"], r["key"], r["version_id"]) not in own and r.get("product") not in hashes]
    names: set[tuple] = set()
    parents: set[str] = set()
    for recipe_hash in hashes:
        product = (facts.get("products") or {}).get(recipe_hash)
        if product is None:
            out.append(_fail("store", (
                f"{tool} stores objects of recipe {recipe_hash[:12]} that the catalog holds no claim for. Claim "
                "each recipe (catalog.claim) before storing its objects, then commit them (catalog.commit).")))
            continue
        if product["owner"] != facts.get("producer"):
            out.append(_fail("store", f"{tool}'s product {recipe_hash[:12]} is claimed by {product['owner']}, not by "
                                      "the call's own login. Claim and commit through the tool's own store keys."))
        if product["state"] not in ("committed", "verified"):
            out.append(_fail("store", (
                f"{tool}'s product {recipe_hash[:12]} is {product['state']} in the catalog when the call returns. "
                "Commit it (catalog.commit) once its recipe.json is stored.")))
        names |= {(i.get("store"), i.get("bucket"), i.get("key"), i.get("version_id")) for i in product["inputs"]}
        parents |= {i.get("product") for i in product["inputs"]} - {None}
    if hashes:
        for r in reads:
            if (r["store"], r["bucket"], r["key"], r["version_id"]) not in names and (
                    r.get("product") is None or r["product"] not in parents):
                out.append(_fail("store", (
                    f"{tool} read {_where(r)}, and no product the call stored names it among its recipe's "
                    "inputs. List every record a product is made from among its inputs (a product's "
                    "recipe.json names the whole product).")))
    return out


def judge_record(rec: dict, out_dir: Path, calls: dict[str, dict] | None = None) -> list[CheckResult]:
    """Every way one recorded call falls short of what the analyst runtime needs from it. ``calls``:
    what the run's store recorded of each bracketed call (``store.ci``), by call id; ``None`` for a
    run without a store."""
    tool = rec["tool"]
    if "raised" in rec:  # a call that raised: what it left in the store, and every other write
        return _store_checks(tool, rec, calls, complete=False) + _class_checks(tool, rec)
    if "transmit_error" in rec:
        return _store_checks(tool, rec, calls, complete=False) + [_fail("transmit", (
            f"{tool} returned a value the tool REPL cannot send ({rec['transmit_error']}), so the "
            "call fails in a real run. Return plain JSON types: float()/int() for numpy scalars, "
            ".tolist() for arrays, str() for paths."
        ))]
    raw = normalize_tool_raw(rec["payload"])
    text, exit_code = tool_text(raw)
    # Judged on a failed call too (all but the recipe file: a failed production ends without one).
    written = _store_checks(tool, rec, calls, complete=exit_code == 0) + _class_checks(tool, rec)
    if exit_code != 0:
        return written  # a failed call is receipted as failed and grounds nothing
    out: list[CheckResult] = written
    is_file = rec.get("is_file", {})

    figures, notes = parse_tool_figures(raw.get("figures"))
    for note in notes:
        out.append(_fail("figure", f"{tool}: {note}. The runtime drops this figure; give each "
                                   "figure the keys and values FigureRef accepts."))
    for fig in figures:
        if not is_file.get(str(fig.path)):
            out.append(_fail("figure", (
                f"{tool} returned figure '{fig.caption}' whose file {fig.path} does not exist when "
                "the call returns. The runtime drops it; return the path of the PNG the call wrote."
            )))

    declared, notes = parse_products(raw.get("products"))
    for note in notes:
        out.append(_fail("receipted", f"{tool}: {note}. The runtime receipts only a well-formed "
                                      "product; return each as {\"path\": …, \"caption\": …}."))
    products: list[Path] = []
    for product in declared:
        if is_file.get(str(product.path)):
            products.append(product.path)
        else:
            out.append(_fail("receipted", (
                f"{tool} declared product '{product.caption}' whose file {product.path} does not exist "
                "when the call returns. The runtime drops it; return the path of the file the call wrote."
            )))
    out.extend(_placed_checks(tool, rec, [(f"figure '{f.caption}'", str(f.path)) for f in figures
                                          if is_file.get(str(f.path))]
                              + [(f"product '{d.caption}'", str(d.path)) for d in declared if is_file.get(str(d.path))]))

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
                    "value in stdout as returned, or write it to a file the tool returns under 'products'."
                )))
    return out


def judge(out_dir: Path, tools: dict[str, tuple],
          store_calls: list[dict] | None = None) -> tuple[list[CheckResult], list[CheckResult]]:
    """``(checks, findings)`` for one Layer-B run's records. No record of the plugin loading is a
    failure (the check did not run); a watched tool no test called is a finding for the co-lead.
    ``store_calls``: what the run's throwaway store recorded of each bracketed call (``CIStore.calls``);
    ``None`` for a run without a store."""
    calls = None if store_calls is None else {str(c.get("call")): c for c in store_calls}
    path = Path(out_dir) / RECORDS
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    records = [json.loads(line) for line in lines if line.strip()]
    if not any(r.get("event") == "loaded" for r in records):
        return [CheckResult(
            name="layer_b.returns.recorder", layer="B", passed=False,
            detail=("the return check did not run: its pytest plugin never loaded. Leave pytest "
                    "plugin loading to the CI pipeline (no -p no:... in pytest.ini, setup.cfg or conftest)."),
        )], []
    checks: list[CheckResult] = []
    called: set[str] = set()
    for rec in records:
        if rec.get("event") == "import_error":
            checks.append(_fail("recorder", f"{rec['tool']} could not be imported to watch: {rec['error']}"))
        elif rec.get("event") == "store_error":
            checks.append(_fail("store", f"the CI pipeline could not record what {rec['tool']} wrote in its store "
                                         f"({rec['error']}). Run the tests again; when it repeats, message the lead "
                                         "with this check."))
        elif "tool" in rec and "event" not in rec:
            called.add(rec["tool"])
            checks.extend(judge_record(rec, Path(out_dir), calls))
    # One line per distinct problem: a test that calls a tool twenty times reports it once.
    checks = list({c.detail: c for c in checks}.values())
    noted = [c for c in checks if c.name.startswith("finding.")]  # for the co-lead, not a failure
    checks = [c for c in checks if not c.name.startswith("finding.")]
    if not checks:
        checks = [CheckResult(name="layer_b.returns", layer="B", passed=True,
                              detail=f"{len(called)} of {len(tools)} watched tool(s) called")]
    findings = noted + [
        CheckResult(name="finding.returns_unobserved", layer="B", passed=True,
                    detail=f"{key}: no test calls it, so its returns were never checked")
        for key in sorted(set(tools) - called)
    ]
    return checks, findings


def watched_tools(worktree: Path, targets: dict[Path, set[int] | None], head_manifest) -> dict[str, tuple]:
    """The new or changed tools of this changeset: ``{manifest key: (module, fn, placed, paths)}``,
    where ``placed`` is :func:`manifest_placed`'s ``{argument: shape}`` and ``paths`` the
    ``path``-kind arguments (:func:`manifest_paths`): what the recorder needs for the class checks.

    New = a key the working-tree manifest has and ``head_manifest(rel)`` (the committed one) lacks;
    changed = a key whose function body contains an added line. Legacy tools in a touched package
    are left alone, since the toolkit predates this contract.
    """
    import ast

    watched: dict[str, tuple] = {}
    manifests = {p for p in targets if p.name == "__init__.py" and p.parent.name == "skills"}
    packages = {p.relative_to(worktree).parts[0] for p in targets}
    for pkg in packages:
        manifests.add(worktree / pkg / "skills" / "__init__.py")
    for manifest in sorted(manifests):
        if not manifest.is_file():
            continue
        manifest_source = manifest.read_text(encoding="utf-8")
        entries = manifest_entries(manifest_source)
        out: dict[str, tuple[str, str]] = {}
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
        watched.update(watch_entries(manifest_source, out))
    return watched


def watch_entries(source: str, keys) -> dict[str, tuple]:
    """The recorder's watch list for ``keys`` of one ``TOOL_MANIFEST`` source: ``{key: (module, fn,
    placed, paths)}`` (:func:`manifest_placed`, :func:`manifest_paths`), the 4-tuple ``install`` and
    the class checks need. The one builder, used by the gate (:func:`watched_tools`) and by any
    re-judge of a committed tool, so neither can lose a tool's output kinds."""
    entries, placed, paths = manifest_entries(source), manifest_placed(source), manifest_paths(source)
    return {key: (*entries[key], placed.get(key, {}), paths.get(key, [])) for key in keys}  # a stale key raises


def _manifest_schemas(source: str) -> dict[str, dict]:
    """``{key: args_schema}`` from a ``TOOL_MANIFEST`` literal (none when it is not a literal)."""
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
        try:
            manifest = ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
            return {}
        out: dict[str, dict] = {}
        for key, entry in (manifest.items() if isinstance(manifest, dict) else ()):
            schema = entry.get("args_schema") if isinstance(entry, dict) else None
            if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
                out[str(key)] = schema
        return out
    return {}


def manifest_placed(source: str) -> dict[str, dict[str, str]]:
    """``{key: {argument: shape}}``: the arguments the runtime places in a call's own folder, from a
    ``TOOL_MANIFEST`` literal: each ``output`` with its shape, and each legacy figure name
    (``FIGURE_PATH_ARGS``) declared otherwise, as a file. A manifest that is not a literal gives none."""
    out: dict[str, dict[str, str]] = {}
    for key, schema in _manifest_schemas(source).items():
        shapes = declared_outputs(schema)
        shapes.update({name: "file" for name in FIGURE_PATH_ARGS if name in schema["properties"] and name not in shapes})
        out[key] = shapes
    return out


def manifest_paths(source: str) -> dict[str, list[str]]:
    """``{key: [argument]}``: the ``path``-kind arguments; a file the call writes at one is
    unclassified, and ``_classes`` names it apart so its message says to declare an ``output``."""
    return {key: sorted(name for name, prop in schema["properties"].items()
                        if isinstance(prop, dict) and prop.get("kind") == "path" and name not in FIGURE_PATH_ARGS)
            for key, schema in _manifest_schemas(source).items()}


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
