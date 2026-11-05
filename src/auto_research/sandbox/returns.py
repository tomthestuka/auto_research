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
  layer_b.returns.obsdb      opt-out: every file a call creates or changes in an obs-db (one its
                             arguments name, walked before and after the call, so a subprocess or
                             a rename is seen; or one an audited write reaches) and that still
                             exists is recorded in its catalog, or sits in a folder whose catalog
                             entry the call recorded; the catalog file is written only by
                             ``obsdb_catalog.neutral``; ``obsdb_catalog.healthcheck`` passes after
                             the call. A raised call is judged too. An in-place change to a file
                             the catalog already records needs no new record (ruling Q2)

  layer_b.returns.unclassified  every file a call writes is in exactly one class (user rule
                             2026-11-04): inside an output folder (declared, else ``.placed``
                             fails), inside an obs-db (``.obsdb``), or scratch (interpreter
                             bytecode; the home's caches:
                             ``~/.cache``, ``~/.astropy``, ``~/.xspec``, ``~/.matplotlib``,
                             ``~/.config/{matplotlib,astropy}``, ``$PFILES``, ``$MPLCONFIGDIR``,
                             ``$XDG_CACHE_HOME``, ``/dev/shm``; the temp area outside pytest's
                             ``--basetemp`` and the tree under test); any other file fails. Each
                             output folder and the running test's ``tmp_path`` are walked before
                             and after the call, so a subprocess's or a rename's file is seen

Scratch is not otherwise judged. The real obs-db is read-only
to Layer B, so an opt-out tool's tests build a fixture obs-db under ``tmp_path`` (``<tmp>/<MISSION>/``,
its catalog seeded through ``obsdb_catalog.neutral``) and point the tool at it.

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
import functools, importlib, itertools, json, os, shutil, sys, tempfile, threading, time

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


def _profile_missions():
    """The acquire engine's mission names (``heasarc_utils.lib.heasarc.MISSIONS`` keys), read from
    its source as data: importing it would change the test process (its package edits sys.path)."""
    import ast, importlib.util
    try:
        spec = importlib.util.find_spec("heasarc_utils")
        source = os.path.join(os.path.dirname(spec.origin), "lib", "heasarc.py")
        tree = ast.parse(open(source, encoding="utf-8").read())
    except Exception:
        return set()
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if getattr(target, "id", None) == "MISSIONS" and isinstance(node.value, ast.Dict):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def _obsdb():
    """``(missions, neutral, healthcheck, error)`` from the toolkit's catalog writer, imported on
    first use. ``missions``: every mission name the toolkit knows (the catalog's scanners and
    archive trees, and the acquire engine's mission profiles when that package imports)."""
    if not hasattr(_obsdb, "cache"):
        try:
            from obsdb_catalog import _domain, neutral
            from obsdb_catalog.healthcheck import healthcheck
            missions = set(_domain._SCANNERS) | set(getattr(_domain, "TREES", {})) | _profile_missions()
            _obsdb.cache = ({m.lower() for m in missions}, neutral, healthcheck, None)
        except Exception as exc:
            _obsdb.cache = (set(), None, None, f"{type(exc).__name__}: {exc}")
    return _obsdb.cache


def _within(path, folder):
    return os.path.commonpath([path, folder]) == folder


def _obsdb_root(path, missions, outputs):
    """The obs-db ``path`` is (or lies) in: the nearest folder at or above it that holds a catalog or
    is named after a mission the catalog writer knows; never one of the call's own output folders."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and d != os.path.dirname(d):
        if d in outputs:
            return None
        if os.path.isfile(os.path.join(d, "catalog.json")) or os.path.basename(d).lower() in missions:
            return d
        d = os.path.dirname(d)
    return None


def _catalog_own(rel):
    """A file the catalog writer itself writes: the catalog, its lock, its history, its temp file."""
    base = os.path.basename(rel)
    return (rel in ("catalog.json", "catalog.json.lock") or rel.startswith(".catalog_history" + os.sep)
            or (os.path.dirname(rel) == "" and base.startswith(".catalog-") and base.endswith(".tmp")))


#: The most files one fixture obs-db is walked for; a larger one is judged by its audited writes.
_WALK_CAP = 20000


def _state(root):
    """An obs-db as it stands: its files (``{rel: [size, mtime]}``, the writer's own left out;
    ``None`` past ``_WALK_CAP``), its catalog entries and its health."""
    missions, neutral, healthcheck, error = _obsdb()
    files = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if not _catalog_own(rel):
                files[rel] = _stat(full)
        if len(files) > _WALK_CAP:
            files = None
            break
    try:
        entries = neutral.read(root).get("entries", {})
    except Exception:
        entries = {}
    try:
        health = healthcheck(root)
    except Exception as exc:
        health = {"error": f"{type(exc).__name__}: {exc}"}
    return {"files": files, "entries": entries, "health": health}


def _roots_named(values, outputs):
    """The writable obs-dbs the call's arguments name (only a fixture one is writable to Layer B)."""
    missions, _neutral, _health, error = _obsdb()
    roots = set()
    for value in values:
        if isinstance(value, (str, os.PathLike)) and os.fspath(value).strip() and "\0" not in os.fspath(value):
            root = _obsdb_root(os.path.realpath(os.fspath(value)), missions, outputs)
            if root is not None and os.access(root, os.W_OK):
                roots.add(root)
    return roots


def _obsdb_judged(before, written, outputs):
    """``{root: …}`` for every obs-db the call named or wrote into: the files it created or changed
    that still exist and its catalog does not record (a file in a folder whose catalog entry the
    call recorded is taken as recorded), whether it wrote the catalog file itself, and the health
    before and after."""
    missions, neutral, healthcheck, error = _obsdb()
    real = {os.path.realpath(w) for w in written}
    roots = dict(before)
    for w in real:
        root = _obsdb_root(w, missions, outputs)
        if root is not None:
            roots.setdefault(root, None)
    out = {}
    for root, then in roots.items():
        if error is not None:
            out[root] = {"error": error}
            continue
        now = _state(root)
        audited = {os.path.relpath(w, root) for w in real if _within(w, root)}
        changed = {r for r in audited if not _catalog_own(r) and os.path.isfile(os.path.join(root, r))}
        if then is not None and then["files"] is not None and now["files"] is not None:
            changed |= {r for r, st in now["files"].items() if then["files"].get(r) != st}
        old = then["entries"] if then is not None else {}
        recorded_dirs = {os.path.dirname(p.get("path", "")) for obsid, e in now["entries"].items()
                         if old.get(obsid) != e for p in e.get("products", [])}
        known = {p.get("path") for e in now["entries"].values() for p in e.get("products", [])}
        out[root] = {
            "unrecorded": sorted(r for r in changed if r not in known and os.path.dirname(r) not in recorded_dirs),
            "hand_written": "catalog.json" in audited,
            "health_before": then["health"] if then is not None else None,
            "health": now["health"],
        }
    return out


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
    (HEASoft's PFILES, matplotlib, astropy, XSPEC, the XDG cache), plus shared memory. Read at
    configure time; a root that would hold the tests' tmp_path, the tree or the home is dropped."""
    home = os.path.expanduser("~")
    roots = [os.path.join(home, d) for d in (".cache", ".astropy", ".xspec", ".matplotlib",
                                              os.path.join(".config", "matplotlib"),
                                              os.path.join(".config", "astropy"), "pfiles")]
    for var in ("MPLCONFIGDIR", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
        if os.environ.get(var):
            roots.append(os.environ[var])
    for part in os.environ.get("PFILES", "").split(";")[0].split(":"):
        if part.strip():
            roots.append(part.strip())
    roots.append("/dev/shm")
    guarded = [os.path.realpath(g) for g in (_BASETEMP[0], _TREE[0], home) if g]
    return [r for r in (os.path.realpath(x) for x in roots)
            if not any(_within(g, r) for g in guarded)]


def _classes(written, out_before, folders, declared, obsdb_roots, inputs, returned=(), started=0):
    """Every file the call left written, by class: ``{"undeclared", "unclassified",
    "at_path_args", "notes"}``.
    Output: inside an output folder, declared (``declared=None``: not judged, the call raised).
    Obs-db: inside an obs-db (judged by `_obsdb_judged`). A file at a path the test passed in a
    ``path`` argument is no class (named apart, so its message says to declare the argument an
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
    # A link's target changed during the call (a write through a staged input) is judged at the target.
    candidates |= {t for t in targets if os.path.isfile(t) and (_stat(t) or [0, 0])[1] >= started}
    outputs = [f[2] for f in folders]
    asked = {os.path.realpath(p) for p in inputs}
    returned = {os.path.realpath(p) for p in returned}
    scratch = _SCRATCH[0]
    temp = os.path.realpath(tempfile.gettempdir())
    base = os.path.realpath(_BASETEMP[0]) if _BASETEMP[0] else None
    out = {"undeclared": [], "unclassified": [], "at_path_args": [], "notes": notes}
    for path in sorted(candidates):
        if any(_within(path, r) for r in obsdb_roots):
            continue  # an obs-db item: `.obsdb` judges it, wherever the test put the fixture
        elif any(_within(path, f) for f in outputs):
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
        outputs = {f[2] for f in folders}
        inputs = {os.path.abspath(os.fspath(v)) for n, v in full.items() if n not in placed
                  and isinstance(v, (str, os.PathLike)) and os.fspath(v).strip() and "\0" not in os.fspath(v)}
        # The files the test named in `path`-kind arguments: a write there is unclassified,
        # named apart so its message fits.
        asked_paths = {
            os.path.abspath(os.fspath(full[n])) for n in path_args or ()
            if isinstance(full.get(n), (str, os.PathLike)) and os.fspath(full[n]).strip() and "\0" not in os.fspath(full[n])}
        try:  # the obs-dbs the arguments name, as they stand before the call
            before_obsdb = {root: _state(root) for root in _roots_named(full.values(), outputs)}
        except Exception as exc:
            before_obsdb, obsdb_error = {}, f"{type(exc).__name__}: {exc}"
        else:
            obsdb_error = None
        out_before = {f[2]: (_files_under(f[2]) if os.path.isdir(f[2]) else {}) for f in folders}
        test_tmp = _TEST_TMP[0]
        if test_tmp and os.path.isdir(test_tmp) and os.path.realpath(test_tmp) not in out_before:
            out_before[os.path.realpath(test_tmp)] = _files_under(test_tmp)
        named = [str(p) for p in _named_paths(args, cwd, ())]
        before = {p: _stat(p) for p in named}
        started = call_started_ns()
        # The call's own, empty HEASoft parameter folder, as the analyst REPL gives each call (G4);
        # taken before the call is marked as running, so a failure here leaves nothing half-set.
        params, pfiles = private_param_dir()
        pfiles_before = os.environ.get("PFILES")
        _local.depth, _local.written = 1, set()
        rec = {"tool": key, "started_ns": started, "before": before}

        def classes_now(declared):
            roots = [r for r in rec.get("obsdb", {}) if not r.startswith("(")]
            try:
                return _classes(written, out_before, folders, declared, roots, asked_paths,
                                [os.path.join(str(cwd), p) for _k, p in rec.get("returned_files", [])],
                                started)
            except Exception as exc:
                return {"undeclared": [], "unclassified": [], "error": f"{type(exc).__name__}: {exc}"}

        def obsdb_now():
            if obsdb_error is not None:
                return {"(arguments)": {"error": obsdb_error}}
            try:
                return _obsdb_judged(before_obsdb, written, outputs)
            except Exception as exc:
                return {"(recorder)": {"error": f"{type(exc).__name__}: {exc}"}}

        if pfiles:
            os.environ["PFILES"] = pfiles
        try:
            result = fn(*a, **k)
        except BaseException as exc:
            # A call that raises is a failed call: what it left in an obs-db is judged all the same.
            _local.depth, written, _local.written = 0, _local.written, None
            if pfiles:
                os.environ["PFILES"] = pfiles_before
                shutil.rmtree(params, ignore_errors=True)
            rec["raised"] = f"{type(exc).__name__}: {exc}"
            rec["obsdb"] = obsdb_now()
            rec["classes"] = classes_now(None)
            _write(rec)
            raise
        _local.depth, written, _local.written = 0, _local.written, None
        if pfiles:
            os.environ["PFILES"] = pfiles_before
            shutil.rmtree(params, ignore_errors=True)
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
        rec["obsdb"] = obsdb_now()
        roots = [r for r in rec["obsdb"] if not r.startswith("(")]
        in_obsdb = lambda s: next((r for r in roots if _within(os.path.realpath(s), r)), None)
        rec["in_obsdb"] = {p: root for p in declared if (root := in_obsdb(os.path.join(str(cwd), p)))}
        rec["folders_holding_obsdb"] = sorted(f[0] for f in folders for r in roots if _within(r, f[2]))
        # The opt-in rule, by the runtime's own function (`capture.admit_artifacts`): each figure
        # and product the call returns that exists and is no obs-db item, in the output folder
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
            if os.path.isfile(path) and in_obsdb(path) is None:
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
                # A file inside an output folder, not an echoed input, not an obs-db item.
                if (inside(text) and os.path.isfile(text) and os.path.abspath(text) not in inputs
                        and in_obsdb(text) is None):
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
    for key, entry in _TOOLS.items():
        module, name = entry[0], entry[1]
        placed = entry[2] if len(entry) > 2 else {}
        path_args = entry[3] if len(entry) > 3 else ()
        try:
            fn = getattr(importlib.import_module(module), name)
        except Exception as exc:
            _write({"event": "import_error", "tool": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        fn = getattr(fn, "__ar_returns_original__", fn)
        wrapped = _wrap(key, fn, placed, path_args)
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
        builtin_function_source("private_param_dir"),
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
    process) drops fails; an obs-db item is never declared; a file inside an output returned under
    another key is declared; an output the test leaves unset is named."""
    import os

    folders, cwd = rec.get("folders", []), rec.get("cwd", "")
    in_obsdb = rec.get("in_obsdb", {})
    out: list[CheckResult] = []
    for name in rec.get("folders_holding_obsdb", []):
        out.append(_fail("placed", f"{tool}'s test passes {name} a folder that holds the obs-db, so the "
                                   "check cannot tell products from obs-db items. Pass each output argument "
                                   "a folder of its own (tmp_path / 'out'), apart from the obs-db."))
    names = rec.get("placed_names", [])
    for label, path in artifacts:
        if path in in_obsdb:
            out.append(_fail("placed", f"{tool} returns {label} at {path}, an obs-db item; the runtime never "
                                       "mints one. Record it in its catalog through obsdb_catalog.neutral and "
                                       "leave it out of 'products' and 'figures'; a science artifact goes under "
                                       "an output argument instead."))
        elif not folders and names:
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
            f"{tool} writes {path}, which is in none of the three classes. Write an artifact under an "
            "output argument and declare it, an obs-db item into the observation database through "
            "obsdb_catalog, and the program's own bookkeeping in a temporary folder (tempfile) or "
            "the home's caches."
        )))
    return out


def _unhealthy(health) -> dict:
    """What of a ``healthcheck`` report fails the gate: all but ``regressed``, a designed state (a
    re-acquire flags its stale level-3 products for re-reduction; ruling 2026-11-04)."""
    if health is True:
        return {}
    report = health if isinstance(health, dict) else {"report": health}
    return {k: v for k, v in report.items() if k != "regressed"}


def _obsdb_checks(tool: str, rec: dict) -> list[CheckResult]:
    """Opt-out: what the call wrote into an obs-db is in its catalog, written by the catalog writer,
    and the catalog is healthy after the call (``regressed`` accepted; unhealthy before it, the
    fixture is named as the fault)."""
    out: list[CheckResult] = []
    for root, info in sorted((rec.get("obsdb") or {}).items()):
        if "error" in info:
            out.append(_fail("obsdb", f"{tool}'s obs-db writes could not be checked at {root} "
                                      f"({info['error']}). Keep obsdb_catalog importable from the toolkit."))
            continue
        if info.get("hand_written"):
            out.append(_fail("obsdb", f"{tool} writes {root}/catalog.json itself. Record each product through "
                                      "obsdb_catalog.neutral.new or .advance, which write the catalog."))
        if info["unrecorded"]:
            shown = ", ".join(info["unrecorded"][:5]) + (" …" if len(info["unrecorded"]) > 5 else "")
            out.append(_fail("obsdb", (
                f"{tool} wrote {shown} into the obs-db {root} and recorded nothing for it in the catalog. "
                "Record each product it writes through obsdb_catalog.neutral.new or .advance; a science "
                "artifact (an output that depends on a parameter, or on a setting outside the mission "
                "team's defaults) goes under an output argument instead."
            )))
        if _unhealthy(info["health"]):
            report = _unhealthy(info["health"])
            shown = "; ".join(f"{k}: {json.dumps(v, default=str)[:200]}" for k, v in report.items())
            if info.get("health_before") is not None and _unhealthy(info["health_before"]):
                out.append(_fail("obsdb", (
                    f"{tool}'s test gives an obs-db at {root} that is unhealthy before the call "
                    f"(obsdb_catalog.healthcheck: {shown}). Seed the fixture through obsdb_catalog.neutral "
                    "so it is healthy, then the check can judge the tool."
                )))
            else:
                out.append(_fail("obsdb", (
                    f"{tool} leaves the catalog of {root} unhealthy (obsdb_catalog.healthcheck: {shown}). "
                    "Record every product the call writes through obsdb_catalog.neutral."
                )))
    return out


def judge_record(rec: dict, out_dir: Path) -> list[CheckResult]:
    """Every way one recorded call falls short of what the analyst runtime needs from it."""
    tool = rec["tool"]
    if "raised" in rec:  # a call that raised: what it left in an obs-db, and every other write
        return _obsdb_checks(tool, rec) + _class_checks(tool, rec)
    if "transmit_error" in rec:
        return [_fail("transmit", (
            f"{tool} returned a value the tool REPL cannot send ({rec['transmit_error']}), so the "
            "call fails in a real run. Return plain JSON types: float()/int() for numpy scalars, "
            ".tolist() for arrays, str() for paths."
        ))]
    raw = normalize_tool_raw(rec["payload"])
    text, exit_code = tool_text(raw)
    obsdb = _obsdb_checks(tool, rec) + _class_checks(tool, rec)  # judged on a failed call too
    if exit_code != 0:
        return obsdb  # a failed call is receipted as failed and grounds nothing
    out: list[CheckResult] = obsdb
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


def judge(out_dir: Path, tools: dict[str, tuple]) -> tuple[list[CheckResult], list[CheckResult]]:
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
