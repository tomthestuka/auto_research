"""The deterministic mergeability gate — Phase 2 step 3 (CLAUDE.md §9.4/§9.5).

``run_gate`` is the real ``GateHook`` that ``merge_flow.propose_and_pr`` drives the
rework loop against. It is the **trust boundary** and it is *code, not an LLM opinion*:
it verifies an authored tool against properties that must hold and returns a machine-
readable :class:`~auto_research.schemas.merge.GateReport` fed back to the author on
rejection (L2).

Two layers:
  Layer A (generic, domain-blind)  — the no-fabrication scan, structural checks,
                                     determinism (same per-test outcomes across two
                                     runs), provenance.
  Layer B (the tool's own tests)   — run pytest in the worktree.

The no-fabrication scan is split into precision tiers so it stays high-precision on
real toolkit code (which is full of legitimate hardcoded numeric arrays — XSPEC
parameter vectors, energy bins):
  Tier 1 HARD-FAIL  — banned vocabulary (comments/docstrings/strings), any RNG (seeded
                      or not), placeholder function bodies.
  Tier 3 FINDING    — incompleteness-marker vocabulary (e.g. a test "stub", "not
                      implemented") and numeric-literal arrays returned/plotted/framed:
                      recorded on the report but NOT a failure; the step-4 co-lead judges intent.

All scan lists live in ``config/code_gate.yaml`` (tunable without a code edit).

The scanners are pure functions over file *contents* so they unit-test without a real
worktree; the worktree-level orchestration (git enumeration, pytest) is injectable via
the ``test_runner`` seam, mirroring ``merge_flow``'s deterministic-test design.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from functools import lru_cache, partial
from pathlib import Path

import yaml

from auto_research.schemas import CheckResult, GateReport, ToolSpec


# ----------------------------------------------------------------------------
# Config (config/code_gate.yaml) — the single place a human tunes the scan
# ----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _code_gate_config() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "code_gate.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


# ----------------------------------------------------------------------------
# Target enumeration — which .py files (and which added lines) the scan covers
# ----------------------------------------------------------------------------

_TOOLKIT_PKG_RE = re.compile(r"(^|/)[A-Za-z0-9_]+_utils(/|$)")


def _git(worktree: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, check=True
    )
    return out.stdout


def collect_targets(worktree: Path) -> dict[Path, set[int] | None]:
    """Map each changed/new ``*_utils`` ``.py`` file → its added line numbers.

    A value of ``None`` means "scan the whole file" (a new, untracked file). For a
    modified tracked file, the value is the set of added line numbers parsed from the
    unified diff, so AST hits on pre-existing template lines are never reported.
    """
    targets: dict[Path, set[int] | None] = {}
    # -uall lists each file inside a new untracked directory (not just the dir).
    status = _git(worktree, "status", "--porcelain", "-uall")
    for line in status.splitlines():
        if not line.strip():
            continue
        code, _, rel = line[:2], line[2], line[3:].strip()
        rel = rel.split(" -> ")[-1]  # renames
        # Track every changed/new file under a ``*_utils`` package — not only ``.py``.
        # Shell reduction scripts and other text artifacts are part of the tool and must
        # be gated and staged too (§9.2). The package filter keeps scratch outside a
        # ``*_utils`` package invisible, so explicit-path staging never leaks it (§9.9).
        if not _TOOLKIT_PKG_RE.search(rel):
            continue
        path = worktree / rel
        if not path.is_file():
            continue
        if "?" in code or code.strip() == "A":
            targets[path] = None  # untracked / newly added → whole file
        else:
            targets[path] = _added_lines(worktree, rel)
    return targets


_PKG_SEGMENT_RE = re.compile(r"[A-Za-z0-9_]+_utils")


def _touched_packages(worktree: Path, targets: dict[Path, set[int] | None]) -> dict[str, Path]:
    """Map each touched ``*_utils`` package name → its package root under the worktree.

    Derived from the gate's own enumerated changeset (:func:`collect_targets`), so the
    gate verifies **where the tool actually landed** rather than trusting the brief's
    advisory ``target_domain`` — which the lead free-fills, cannot correct in-run, and
    doomed run #12's chain when it wrote a descriptive label instead of the bare dir name
    ([A12]). The package is the first ``*_utils`` segment of each relative path (toolkits
    are top-level packages under the toolkit root); insertion order is preserved.
    """
    pkgs: dict[str, Path] = {}
    for path in targets:
        for part in path.relative_to(worktree).parts:
            if _PKG_SEGMENT_RE.fullmatch(part):
                pkgs.setdefault(part, worktree / part)
                break
    return pkgs


def _added_lines(worktree: Path, rel: str) -> set[int]:
    """Added line numbers (new-file side) from ``git diff`` of one modified file."""
    diff = _git(worktree, "diff", "--unified=0", "--", rel)
    added: set[int] = set()
    new_lineno = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                new_lineno = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.add(new_lineno)
            new_lineno += 1
        elif not line.startswith("-"):
            new_lineno += 1
    return added


# ----------------------------------------------------------------------------
# Layer A.1 — the no-fabrication scan (pure; the unit-testable core)
# ----------------------------------------------------------------------------


def _in_scope(lineno: int, added: set[int] | None) -> bool:
    return added is None or lineno in added


def _match_vocabulary(text_low: str, vocab: list[str]) -> list[str]:
    """Banned phrases present in ``text_low`` (already lowercased), whole-word matched."""
    return [
        phrase
        for phrase in vocab
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", text_low)
    ]


def _dotted_name(node: ast.AST) -> str:
    """Reconstruct a dotted attribute/name chain (``np.random.default_rng``)."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


# The built-in AST detectors `finding_code_patterns` can switch on by name; every
# other entry in that list is a dotted code pattern (matched by _matches_code_pattern).
_FINDING_DETECTORS = frozenset(
    {"literal_array_return", "literal_array_plot", "literal_array_dataframe"}
)


def _matches_code_pattern(dotted: str, pattern: str) -> bool:
    if not dotted:
        return False
    if pattern.startswith("."):
        return dotted.endswith(pattern)
    padded = "." + dotted + "."
    return (
        dotted == pattern
        or dotted.startswith(pattern + ".")
        or dotted.endswith("." + pattern)
        or ("." + pattern + ".") in padded
    )


def _is_numeric_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_numeric_literal(node.operand)
    return False


def _is_numeric_literal_seq(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.List, ast.Tuple))
        and len(node.elts) > 0
        and all(_is_numeric_literal(e) for e in node.elts)
    )


def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_placeholder_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # Skip abstract methods — an empty body is their contract, not a placeholder.
    for dec in node.decorator_list:
        if _dotted_name(dec).endswith("abstractmethod"):
            return False
    body = _body_without_docstring(node.body)
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return True
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        name = ""
        if isinstance(exc, ast.Call):
            name = _dotted_name(exc.func)
        elif isinstance(exc, ast.Name):
            name = exc.id
        if name.endswith("NotImplementedError"):
            return True
    return False


def scan_python_source(
    rel_path: str,
    source: str,
    *,
    added: set[int] | None = None,
    config: dict | None = None,
) -> tuple[list[CheckResult], list[CheckResult]]:
    """Scan one Python file's source. Returns ``(hard_fails, findings)``.

    ``added=None`` scans the whole file (a new file); otherwise only nodes/lines in the
    added set are reported (a modified file). Pure: no filesystem/git access.
    """
    cfg = config if config is not None else _code_gate_config()
    # `or []`: an emptied YAML section parses to None — it means "nothing to flag
    # in this category", never a crash.
    vocab = [v.lower() for v in cfg.get("hard_fail_vocabulary") or []]
    finding_vocab = [v.lower() for v in cfg.get("finding_vocabulary") or []]
    hard_code_patterns = list(cfg.get("hard_fail_code_patterns") or [])
    # `finding_code_patterns` is dual-purpose: an entry naming a built-in detector
    # switches it on; any other entry is a dotted code pattern matched like the hard
    # tier but recorded as a finding. Which list a pattern sits in decides its
    # severity — e.g. RNG demoted to a co-lead-judged finding (Monte-Carlo tools
    # need randomness, 2026-07-16).
    finding_cfg = set(cfg.get("finding_code_patterns") or [])
    finding_detectors = finding_cfg & _FINDING_DETECTORS
    finding_code_patterns = sorted(finding_cfg - _FINDING_DETECTORS)
    allow_res = [re.compile(p) for p in cfg.get("allowlist_obsid_tokens") or []]

    hard: list[CheckResult] = []
    findings: list[CheckResult] = []
    source_lines = source.splitlines()

    # Physical source lines that reference an ObsID/path token are exempt from the
    # vocabulary scan (§9.5 fixtures load a marked `synth…` ObsID directly).
    allow_lines = {
        i for i, line in enumerate(source_lines, 1) if any(rx.search(line) for rx in allow_res)
    }

    def _hard(name: str, lineno: int, detail: str) -> None:
        hard.append(CheckResult(name=name, layer="A", passed=False, detail=f"{rel_path}:{lineno} — {detail}"))

    def _find(name: str, lineno: int, detail: str) -> None:
        findings.append(CheckResult(name=name, layer="A", passed=True, detail=f"{rel_path}:{lineno} — {detail}"))

    # ── Vocabulary: scan comment / docstring / string-literal TEXT ──────────────
    seen_vocab: set[tuple[int, str]] = set()

    def _scan_text(text: str, base_line: int) -> None:
        for offset, raw in enumerate(text.splitlines()):
            lineno = base_line + offset
            if not _in_scope(lineno, added) or lineno in allow_lines:
                continue
            low = raw.lower()
            for phrase in _match_vocabulary(low, vocab):
                key = (lineno, phrase)
                if key not in seen_vocab:
                    seen_vocab.add(key)
                    _hard("no_fabrication.vocabulary", lineno, f"banned phrase {phrase!r}")
            for phrase in _match_vocabulary(low, finding_vocab):
                key = (lineno, phrase)
                if key not in seen_vocab:
                    seen_vocab.add(key)
                    _find("no_fabrication.vocabulary_finding", lineno,
                          f"incompleteness marker {phrase!r} (recorded, not a hard-fail)")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        _hard("no_fabrication.syntax", exc.lineno or 0, f"file does not parse: {exc.msg}")
        return hard, findings

    # Comments come from the raw text (AST drops them); strings/docstrings from the AST.
    for offset, raw in enumerate(source.splitlines()):
        lineno = offset + 1
        if "#" in raw:
            # Only the comment portion (cheap heuristic: text after the last # that is
            # not inside an obvious string is good enough for a vocabulary scan).
            comment = raw[raw.index("#") :]
            _scan_text(comment, lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            _scan_text(node.value, getattr(node, "lineno", 0))

    # ── Code patterns (hard + finding tiers) + literal-array findings + placeholder bodies (AST) ──
    pattern_lines: set[int] = set()  # one pattern report per line (nested attrs would double up)
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None or not _in_scope(lineno, added):
            continue

        if isinstance(node, (ast.Attribute, ast.Name)) and lineno not in pattern_lines:
            dotted = _dotted_name(node) if isinstance(node, ast.Attribute) else node.id
            for pat in hard_code_patterns:
                if _matches_code_pattern(dotted, pat):
                    pattern_lines.add(lineno)
                    _hard("no_fabrication.code_pattern", lineno, f"banned code pattern ({dotted})")
                    break
            else:
                for pat in finding_code_patterns:
                    if _matches_code_pattern(dotted, pat):
                        pattern_lines.add(lineno)
                        _find("no_fabrication.code_pattern_finding", lineno,
                              f"flagged code pattern ({dotted}) — the co-lead judges intent")
                        break

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_placeholder_body(node):
            _hard("no_fabrication.placeholder_body", lineno, f"function {node.name!r} has a placeholder body")

        # Tier-3 findings
        if "literal_array_return" in finding_detectors and isinstance(node, ast.Return):
            if node.value is not None and _is_numeric_literal_seq(node.value):
                _find("finding.literal_array_return", lineno, "returns a numeric-literal array")
        if isinstance(node, ast.Call):
            fname = _dotted_name(node.func)
            if "literal_array_plot" in finding_detectors and fname.split(".")[-1] in {"plot", "scatter", "errorbar"}:
                if any(_is_numeric_literal_seq(a) for a in node.args):
                    _find("finding.literal_array_plot", lineno, f"numeric literals plotted via {fname}")
            if "literal_array_dataframe" in finding_detectors and fname.split(".")[-1] == "DataFrame":
                for a in node.args:
                    if isinstance(a, ast.Dict) and any(_is_numeric_literal_seq(v) for v in a.values):
                        _find("finding.literal_array_dataframe", lineno, "DataFrame built from numeric literals")

    return hard, findings


def scan_text_source(
    rel_path: str,
    source: str,
    *,
    added: set[int] | None = None,
    config: dict | None = None,
) -> tuple[list[CheckResult], list[CheckResult]]:
    """Vocabulary-only no-fabrication scan for a **non-Python text** file (e.g. a shell
    reduction script). Returns ``(hard_fails, findings)`` — hard-fails from the banned
    vocabulary, findings from the Tier-3 ``finding_vocabulary`` (incompleteness markers).

    There is no AST, so the RNG / placeholder-body / literal-array / provenance checks
    (all Python concepts) do not apply; the vocabulary scans do, run over every physical
    line (a shell file has no cheap comment/string boundary to narrow to). The ObsID
    allowlist and ``added`` scoping match :func:`scan_python_source`. Pure.
    """
    cfg = config if config is not None else _code_gate_config()
    vocab = [v.lower() for v in cfg.get("hard_fail_vocabulary") or []]
    finding_vocab = [v.lower() for v in cfg.get("finding_vocabulary") or []]
    allow_res = [re.compile(p) for p in cfg.get("allowlist_obsid_tokens") or []]

    hard: list[CheckResult] = []
    findings: list[CheckResult] = []
    seen: set[tuple[int, str]] = set()
    for lineno, raw in enumerate(source.splitlines(), 1):
        if not _in_scope(lineno, added) or any(rx.search(raw) for rx in allow_res):
            continue
        low = raw.lower()
        for phrase in _match_vocabulary(low, vocab):
            key = (lineno, phrase)
            if key not in seen:
                seen.add(key)
                hard.append(CheckResult(
                    name="no_fabrication.vocabulary", layer="A", passed=False,
                    detail=f"{rel_path}:{lineno} — banned phrase {phrase!r}",
                ))
        for phrase in _match_vocabulary(low, finding_vocab):
            key = (lineno, phrase)
            if key not in seen:
                seen.add(key)
                findings.append(CheckResult(
                    name="no_fabrication.vocabulary_finding", layer="A", passed=True,
                    detail=f"{rel_path}:{lineno} — incompleteness marker {phrase!r} (recorded, not a hard-fail)",
                ))
    return hard, findings


# ----------------------------------------------------------------------------
# Layer A.2 — provenance (pure, AST): FigureRef must carry sha256 + produced_by
# ----------------------------------------------------------------------------


def scan_provenance(rel_path: str, source: str, *, added: set[int] | None = None) -> list[CheckResult]:
    """A ``FigureRef(...)`` constructed by the tool must populate ``sha256`` and
    ``produced_by`` so a downstream result computed on a bad upstream is detectable
    (§7/§9.4). Vacuously passes when the tool produces no figures."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[CheckResult] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted_name(node.func).endswith("FigureRef"):
            lineno = getattr(node, "lineno", 0)
            if not _in_scope(lineno, added):
                continue
            kw = {k.arg for k in node.keywords if k.arg}
            missing = {"sha256", "produced_by"} - kw
            if missing:
                out.append(CheckResult(
                    name="provenance.figure_ref", layer="A", passed=False,
                    detail=f"{rel_path}:{lineno} — FigureRef missing {sorted(missing)}",
                ))
    return out


# ----------------------------------------------------------------------------
# Layer A.3 — structural checks (filesystem)
# ----------------------------------------------------------------------------

_TEST_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")


def check_structural(worktree: Path, tool_spec: ToolSpec, targets: dict[Path, set[int] | None]) -> list[CheckResult]:
    out: list[CheckResult] = []
    rels = [str(p.relative_to(worktree)) for p in targets]

    # (a) only allowed paths touched: every changed .py is under a *_utils package.
    stray = [r for r in rels if not _TOOLKIT_PKG_RE.search(r)]
    out.append(CheckResult(
        name="structural.allowed_paths", layer="A", passed=not stray,
        detail="" if not stray else f"changes outside a *_utils package: {stray}",
    ))

    # (b) a test file exists among the changes (untested tool is not mergeable, §9.4).
    has_test = any(_TEST_RE.search(r) for r in rels)
    out.append(CheckResult(
        name="structural.test_present", layer="A", passed=has_test,
        detail="" if has_test else "no test_*.py / *_test.py added for the tool",
    ))

    # (c) a TOOL_MANIFEST entry for the tool exists in one of the packages the changeset
    #     actually touched — derived from the diff, never from the brief's advisory
    #     ``target_domain`` (a descriptive value there once built a nonexistent manifest path
    #     and rejected forever with a misleading "no entry" message, [A12]). Searching the
    #     touched packages keeps ``fix_existing`` working (a lib-only fix still touches its
    #     package, whose existing entry is found) and stays tight (a name that collides with a
    #     tool in an *untouched* package cannot false-pass).
    name = tool_spec.proposed_tool_name
    packages = _touched_packages(worktree, targets)
    found = False
    key_report: list[str] = []
    for pkg_name, pkg_root in packages.items():
        manifest = pkg_root / "skills" / "__init__.py"
        keys = _manifest_keys(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
        if any(k == name or k.endswith("." + name) for k in keys):
            found = True
        key_report.append(f"{pkg_name}: {sorted(keys) if keys else 'no skills/__init__.py TOOL_MANIFEST'}")
    detail = "" if found else (
        f"no TOOL_MANIFEST entry for {name!r} in any touched package "
        f"[{'; '.join(key_report) if key_report else 'no *_utils package touched'}]"
    )
    out.append(CheckResult(
        name="structural.tool_manifest_entry", layer="A", passed=found, detail=detail,
    ))
    return out


def _manifest_keys(source: str) -> list[str]:
    """Extract the string keys of the ``TOOL_MANIFEST`` dict literal (pure-data, §9.2).

    Accepts both a plain assignment (``TOOL_MANIFEST = {...}`` → ``ast.Assign``) and an
    annotated one (``TOOL_MANIFEST: dict = {...}`` → ``ast.AnnAssign``). The annotated form is
    what ``xspec_utils`` uses; matching only ``Assign`` would extract zero keys and make every
    extend-``xspec_utils`` tool's manifest entry invisible to the structural check.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if "TOOL_MANIFEST" in names and isinstance(node.value, ast.Dict):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return []


# ----------------------------------------------------------------------------
# Layer B + determinism — run the tool's tests (injectable for deterministic tests)
# ----------------------------------------------------------------------------

# A test runner returns: {"results": list[CheckResult(layer="B")], "deterministic": bool,
#                         "determinism_detail": str}
TestRunner = Callable[[Path, list[str]], Awaitable[dict]]


def _layer_b_verdict(rc1: int, outcomes: dict[str, str], out1: str) -> tuple[bool, str]:
    """Decide Layer B pass/fail from the first run's exit code + per-test outcome map.

    Pass ⇔ pytest exited 0 **and ≥1 test PASSED**. ``rc == 0`` alone is insufficient:
    pytest also exits 0 when every test is SKIPPED, so a suite whose data-guards
    (``skipif(not path.exists(...))``) all fire — the normal-path case when the validation
    ObsID is absent or a path is hardcoded/off-host — would merge a tool that never ran,
    breaking §9.4 ("an untested tool is not mergeable"). Nothing verified ⇒ fail loud
    (→ rework or honest failure), never a silent pass.
    """
    if rc1 != 0:
        return False, out1[-2000:]
    if not any(outcome == "PASSED" for outcome in outcomes.values()):
        return False, (
            f"Layer-B suite exited 0 but no test PASSED "
            f"({len(outcomes)} collected, all skipped/none executed) — the tool was not "
            f"verified (§9.4). Point the tests at the ToolSpec validation ObsID via "
            f"config/obs_db_path so they actually run, not a hardcoded or off-host path.\n"
            + out1[-1500:]
        )
    return True, ""


async def _default_test_runner(
    worktree: Path, test_paths: list[str], *, timeout_s: int = 900
) -> dict:
    """Run the worktree's tests twice and compare **per-test outcomes** for determinism.

    Reuses the ``analyze_env`` / ``analyze_ini_sh`` env-source pattern from
    ``sandbox/runner.py`` so HEASoft/XSPEC are available; PYTHONPATH points at the
    worktree so the freshly-authored package imports.

    Each run writes a JUnit XML report (``--junitxml``) parsed into a ``{test_id: outcome}``
    map; the run is deterministic ⇔ the two maps are identical. This ignores log/timing/
    stdout volatility (chatty science tooling — growing INFO filelists, version banners) that
    a byte-compare false-flagged, and catches what matters: a test *outcome* that flips
    run-to-run (CLAUDE.md §9.4). ``timeout_s`` bounds **each** pytest invocation (resolved
    per-package by :func:`_gate_test_timeout`).

    Layer B passes only when pytest exits 0 **and** ≥1 test PASSED: an all-SKIPPED suite
    (validation data absent / hardcoded / off-host) also exits 0, and merging it would land a
    tool that never ran (§9.4). Nothing verified ⇒ fail, not silent pass.
    """
    import os

    from auto_research.sandbox.runner import _source_ini_env

    cfg = _orchestration_config()
    analyze_env = cfg.get("analyze_env")
    ini_sh = cfg.get("analyze_ini_sh")
    python_exec = str(Path(analyze_env) / "bin" / "python") if analyze_env else "python"

    base_env = _source_ini_env(ini_sh) if ini_sh else dict(os.environ)
    existing = base_env.get("PYTHONPATH", "")
    env = {**base_env, "PYTHONPATH": str(worktree) + (":" + existing if existing else "")}

    def _run(junit_path: str) -> tuple[int, str, dict[str, str]]:
        try:
            proc = subprocess.run(
                [python_exec, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                 f"--junitxml={junit_path}", *test_paths],
                cwd=str(worktree), capture_output=True, text=True, env=env, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            # A test that runs longer than the cap is a Layer-B *failure* (rework, then honest
            # failure), never an exception that aborts the whole run. Keep any partial output.
            partial_out = "".join(p for p in (exc.stdout, exc.stderr) if isinstance(p, str))
            return 124, f"pytest timed out after {exc.timeout:.0f}s\n{partial_out}".strip(), {}
        return (
            proc.returncode,
            (proc.stdout + proc.stderr).strip(),
            _parse_junit_outcomes(Path(junit_path)),
        )

    # JUnit reports go to a throwaway dir outside the worktree, so they never pollute the
    # changeset (collect_targets) or survive a later git clean.
    with tempfile.TemporaryDirectory() as td:
        rc1, out1, map1 = _run(str(Path(td) / "junit1.xml"))
        _rc2, _out2, map2 = _run(str(Path(td) / "junit2.xml"))

    passed, detail = _layer_b_verdict(rc1, map1, out1)
    results = [CheckResult(name="layer_b.pytest", layer="B", passed=passed, detail=detail)]
    deterministic, detail = _outcomes_determinism(map1, map2)
    return {
        "results": results,
        "deterministic": deterministic,
        "determinism_detail": detail,
    }


def _parse_junit_outcomes(junit_path: Path) -> dict[str, str]:
    """Parse a pytest ``--junitxml`` report into ``{test_id: outcome}``.

    ``test_id`` is ``"<classname>::<name>"``; ``outcome`` ∈ ``{PASSED, FAILED, ERROR,
    SKIPPED}`` read from each ``<testcase>``'s child element (``<error>``→ERROR,
    ``<failure>``→FAILED, ``<skipped>``→SKIPPED, none→PASSED). Only the per-test verdict is
    read — timing/log/stdout volatility is ignored. Returns ``{}`` if the report is absent or
    unparseable (e.g. a collection crash), in which case Layer B's ``rc != 0`` fails the gate
    on its own.
    """
    try:
        root = ET.parse(str(junit_path)).getroot()
    except (OSError, ET.ParseError):
        return {}
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome = "PASSED"
        for child in case:
            if child.tag == "error":
                outcome = "ERROR"
                break
            if child.tag == "failure":
                outcome = "FAILED"
                break
            if child.tag == "skipped":
                outcome = "SKIPPED"
                break
        outcomes[test_id] = outcome
    return outcomes


def _outcomes_determinism(map1: dict[str, str], map2: dict[str, str]) -> tuple[bool, str]:
    """Determinism ⇔ the two per-test outcome maps are identical (keys **and** values).

    Catches an outcome that flips run-to-run and the same-count/different-tests swap a bare
    return-code compare would miss; ignores log/stdout volatility (never in the maps).
    Returns ``(deterministic, detail)`` — ``detail`` names every diverging test.
    """
    if map1 == map2:
        return True, ""
    ids = sorted(set(map1) | set(map2))
    diffs = [
        f"{tid}: {map1.get(tid, '<absent>')} → {map2.get(tid, '<absent>')}"
        for tid in ids
        if map1.get(tid) != map2.get(tid)
    ]
    return False, "test outcomes differ between two runs: " + "; ".join(diffs)


def _gate_test_timeout(worktree: Path, targets: dict[Path, set[int] | None]) -> int:
    """Resolve the per-pytest-invocation timeout (seconds) for the gate's Layer-B run.

    Value: ``max`` of ``orchestration.yaml: gate_test_timeout_s`` (default 900) and every
    ``tool_config.yaml: gate_test_timeout_s`` declared by a package the changeset actually
    touched. No upper clamp — this is trusted human/coder infra (mirrors ``call_timeout_s``,
    sandbox/DOCS.md); a slow tool (e.g. a real archive download, run twice by a
    reproducibility test) sets its own ceiling beside its ``call_timeout_s``. Reading the
    touched packages (not the brief's advisory ``target_domain``, [A12]) means one pytest run
    over a whole authored chain honours the slowest package's declared ceiling.
    """
    best = int(_orchestration_config().get("gate_test_timeout_s") or 900)
    for pkg_root in _touched_packages(worktree, targets).values():
        pkg_cfg = pkg_root / "tool_config.yaml"
        try:
            with pkg_cfg.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            val = data.get("gate_test_timeout_s")
            if val is not None:
                best = max(best, int(val))
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            pass
    return best


@lru_cache(maxsize=1)
def _orchestration_config() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


# ----------------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------------


def _report_hash(layer_a: list[CheckResult], layer_b: list[CheckResult]) -> str:
    payload = json.dumps(
        [[c.name, c.passed, c.detail] for c in (*layer_a, *layer_b)], sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def run_gate(
    worktree: Path,
    tool_spec: ToolSpec,
    *,
    test_runner: TestRunner | None = None,
    config: dict | None = None,
) -> GateReport:
    """Run the deterministic gate over an authored tool in ``worktree`` (§9.4).

    ``GateReport.passed`` ⇔ every Layer-A hard check passes ∧ Layer-B tests pass ∧ the
    run is deterministic. Determinism here means **the two runs yield the same per-test
    outcomes** (a junit ``{test_id: outcome}`` map compare), not byte-identical output.
    Tier-3 ``findings`` are recorded but never flip ``passed``. ``test_runner`` is injectable
    so the loop/structure is exercised without a live science stack; the default runs pytest
    twice with the analyst interpreter (timeout per :func:`_gate_test_timeout`).
    """
    worktree = Path(worktree)
    cfg = config if config is not None else _code_gate_config()

    targets = collect_targets(worktree)

    if test_runner is None:
        # Bind the per-package timeout into the default runner only; an injected runner keeps
        # its (worktree, test_paths) signature untouched. Derived from the touched changeset
        # (like every other check), so it no longer depends on the brief's advisory field.
        test_runner = partial(_default_test_runner, timeout_s=_gate_test_timeout(worktree, targets))

    layer_a: list[CheckResult] = []
    findings: list[CheckResult] = []

    # A.1 no-fabrication + A.2 provenance, per changed file.
    for path, added in targets.items():
        rel = str(path.relative_to(worktree))
        raw_bytes = path.read_bytes()
        # Binary / undecodable files in a toolkit diff are data, which never belongs in
        # the toolkit (§9.5). Hard-fail to the author (it flows through the L2 rework loop
        # like any other gate failure) — never an exception that breaks the pipeline.
        if b"\x00" in raw_bytes:
            layer_a.append(CheckResult(
                name="no_fabrication.binary", layer="A", passed=False,
                detail=f"{rel} — binary file in toolkit diff; data must not live in the toolkit (§9.5)",
            ))
            continue
        try:
            source = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            layer_a.append(CheckResult(
                name="no_fabrication.binary", layer="A", passed=False,
                detail=f"{rel} — undecodable (non-UTF-8) file in toolkit diff; data must not live in the toolkit (§9.5)",
            ))
            continue
        if rel.endswith(".py"):
            hard, finds = scan_python_source(rel, source, added=added, config=cfg)
            layer_a.extend(hard)
            findings.extend(finds)
            layer_a.extend(scan_provenance(rel, source, added=added))
        else:
            hard, finds = scan_text_source(rel, source, added=added, config=cfg)
            layer_a.extend(hard)
            findings.extend(finds)

    # A.3 structural.
    structural = check_structural(worktree, tool_spec, targets)
    layer_a.extend(structural)

    # Summarising no-fabrication/provenance as explicit PASS checks aids the feedback msg.
    if not any(c.name.startswith("no_fabrication") and not c.passed for c in layer_a):
        layer_a.append(CheckResult(name="no_fabrication.scan", layer="A", passed=True, detail="clean"))

    # Layer B + determinism — only when structure is sound (else the run is moot).
    layer_b: list[CheckResult] = []
    structural_ok = all(c.passed for c in structural)
    if structural_ok:
        test_paths = sorted(str(p.relative_to(worktree)) for p in targets if _TEST_RE.search(str(p)))
        outcome = await test_runner(worktree, test_paths)
        layer_b.extend(outcome.get("results", []))
        layer_a.append(CheckResult(
            name="determinism", layer="A", passed=bool(outcome.get("deterministic", True)),
            detail=outcome.get("determinism_detail", ""),
        ))
    else:
        layer_a.append(CheckResult(
            name="determinism", layer="A", passed=False,
            detail="skipped — structural checks failed; fix structure before tests run",
        ))

    # passed ⇔ all Layer-A hard checks pass ∧ at least one Layer-B test ran ∧ all pass.
    # (An untested run must never pass, §9.4; findings never enter this.)
    passed = all(c.passed for c in layer_a) and bool(layer_b) and all(c.passed for c in layer_b)

    reason = "gate passed" if passed else _failure_reason(layer_a, layer_b)
    return GateReport(
        passed=passed, layer_a=layer_a, layer_b=layer_b, findings=findings,
        reason=reason, report_hash=_report_hash(layer_a, layer_b),
    )


def _failure_reason(layer_a: list[CheckResult], layer_b: list[CheckResult]) -> str:
    failed = [c for c in (*layer_a, *layer_b) if not c.passed]
    if not failed:
        return "no Layer-B tests ran (untested tool is not mergeable)"
    return "; ".join(f"{c.name}: {c.detail}" for c in failed[:6])
