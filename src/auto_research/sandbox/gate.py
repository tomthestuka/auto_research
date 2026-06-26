"""The deterministic mergeability gate — Phase 2 step 3 (CLAUDE.md §9.4/§9.5).

``run_gate`` is the real ``GateHook`` that ``merge_flow.propose_and_pr`` drives the
rework loop against. It is the **trust boundary** and it is *code, not an LLM opinion*:
it verifies an authored tool against properties that must hold and returns a machine-
readable :class:`~auto_research.schemas.merge.GateReport` fed back to the author on
rejection (L2).

Two layers:
  Layer A (generic, domain-blind)  — the no-fabrication scan, structural checks,
                                     determinism, provenance.
  Layer B (the tool's own tests)   — run pytest in the worktree.

The no-fabrication scan is split into precision tiers so it stays high-precision on
real toolkit code (which is full of legitimate hardcoded numeric arrays — XSPEC
parameter vectors, energy bins):
  Tier 1 HARD-FAIL  — banned vocabulary (comments/docstrings/strings), any RNG (seeded
                      or not), placeholder function bodies.
  Tier 3 FINDING    — numeric-literal arrays returned/plotted/framed: recorded on the
                      report but NOT a failure; the step-4 co-lead judges intent.

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
from collections.abc import Awaitable, Callable
from functools import lru_cache
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
    vocab = [v.lower() for v in cfg.get("hard_fail_vocabulary", [])]
    rng_patterns = list(cfg.get("hard_fail_code_patterns", []))
    finding_patterns = set(cfg.get("finding_code_patterns", []))
    allow_res = [re.compile(p) for p in cfg.get("allowlist_obsid_tokens", [])]

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
            for phrase in _match_vocabulary(raw.lower(), vocab):
                key = (lineno, phrase)
                if key not in seen_vocab:
                    seen_vocab.add(key)
                    _hard("no_fabrication.vocabulary", lineno, f"banned phrase {phrase!r}")

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

    # ── RNG (hard-fail) + literal-array findings + placeholder bodies (AST) ──────
    rng_lines: set[int] = set()  # one RNG report per line (nested attrs would double up)
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None or not _in_scope(lineno, added):
            continue

        if isinstance(node, (ast.Attribute, ast.Name)) and lineno not in rng_lines:
            dotted = _dotted_name(node) if isinstance(node, ast.Attribute) else node.id
            for pat in rng_patterns:
                if _matches_code_pattern(dotted, pat):
                    rng_lines.add(lineno)
                    _hard("no_fabrication.rng", lineno, f"randomness is banned in toolkit code ({dotted})")
                    break

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_placeholder_body(node):
            _hard("no_fabrication.placeholder_body", lineno, f"function {node.name!r} has a placeholder body")

        # Tier-3 findings
        if "literal_array_return" in finding_patterns and isinstance(node, ast.Return):
            if node.value is not None and _is_numeric_literal_seq(node.value):
                _find("finding.literal_array_return", lineno, "returns a numeric-literal array")
        if isinstance(node, ast.Call):
            fname = _dotted_name(node.func)
            if "literal_array_plot" in finding_patterns and fname.split(".")[-1] in {"plot", "scatter", "errorbar"}:
                if any(_is_numeric_literal_seq(a) for a in node.args):
                    _find("finding.literal_array_plot", lineno, f"numeric literals plotted via {fname}")
            if "literal_array_dataframe" in finding_patterns and fname.split(".")[-1] == "DataFrame":
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
    reduction script). Returns ``(hard_fails, findings)`` with ``findings`` always empty.

    There is no AST, so the RNG / placeholder-body / literal-array / provenance checks
    (all Python concepts) do not apply; the banned-vocabulary scan does, run over every
    physical line (a shell file has no cheap comment/string boundary to narrow to). The
    ObsID allowlist and ``added`` scoping match :func:`scan_python_source`. Pure.
    """
    cfg = config if config is not None else _code_gate_config()
    vocab = [v.lower() for v in cfg.get("hard_fail_vocabulary", [])]
    allow_res = [re.compile(p) for p in cfg.get("allowlist_obsid_tokens", [])]

    hard: list[CheckResult] = []
    seen: set[tuple[int, str]] = set()
    for lineno, raw in enumerate(source.splitlines(), 1):
        if not _in_scope(lineno, added) or any(rx.search(raw) for rx in allow_res):
            continue
        for phrase in _match_vocabulary(raw.lower(), vocab):
            key = (lineno, phrase)
            if key not in seen:
                seen.add(key)
                hard.append(CheckResult(
                    name="no_fabrication.vocabulary", layer="A", passed=False,
                    detail=f"{rel_path}:{lineno} — banned phrase {phrase!r}",
                ))
    return hard, []


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

    # (c) a TOOL_MANIFEST entry exists for the tool in the target package's skills/.
    pkg = tool_spec.target_domain
    manifest = worktree / pkg / "skills" / "__init__.py"
    name = tool_spec.proposed_tool_name
    keys = _manifest_keys(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
    present = any(k == name or k.endswith("." + name) for k in keys)
    out.append(CheckResult(
        name="structural.tool_manifest_entry", layer="A", passed=present,
        detail="" if present else f"no TOOL_MANIFEST entry for {name!r} in {pkg}/skills/__init__.py",
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


async def _default_test_runner(worktree: Path, test_paths: list[str]) -> dict:
    """Run the worktree's tests twice (determinism) with the analyst sandbox interpreter.

    Reuses the ``analyze_env`` / ``analyze_ini_sh`` env-source pattern from
    ``sandbox/runner.py`` so HEASoft/XSPEC are available; PYTHONPATH points at the
    worktree so the freshly-authored package imports.
    """
    from auto_research.sandbox.runner import _source_ini_env

    cfg = _orchestration_config()
    analyze_env = cfg.get("analyze_env")
    ini_sh = cfg.get("analyze_ini_sh")
    python_exec = str(Path(analyze_env) / "bin" / "python") if analyze_env else "python"

    import os

    base_env = _source_ini_env(ini_sh) if ini_sh else dict(os.environ)
    existing = base_env.get("PYTHONPATH", "")
    env = {**base_env, "PYTHONPATH": str(worktree) + (":" + existing if existing else "")}

    def _run() -> tuple[int, str]:
        try:
            proc = subprocess.run(
                [python_exec, "-m", "pytest", "-p", "no:cacheprovider", "-q", *test_paths],
                cwd=str(worktree), capture_output=True, text=True, env=env, timeout=900,
            )
        except subprocess.TimeoutExpired as exc:
            # A test that runs longer than the cap is a Layer-B *failure* (rework, then honest
            # failure), never an exception that aborts the whole run. Keep any partial output.
            partial = "".join(p for p in (exc.stdout, exc.stderr) if isinstance(p, str))
            return 124, f"pytest timed out after {exc.timeout:.0f}s\n{partial}".strip()
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    rc1, out1 = _run()
    rc2, out2 = _run()
    passed = rc1 == 0
    results = [CheckResult(
        name="layer_b.pytest", layer="B", passed=passed,
        detail="" if passed else out1[-2000:],
    )]
    deterministic = (rc1 == rc2) and (_strip_volatile(out1) == _strip_volatile(out2))
    return {
        "results": results,
        "deterministic": deterministic,
        "determinism_detail": "" if deterministic else "two identical test runs produced different output",
    }


def _strip_volatile(text: str) -> str:
    """Drop pytest's inherently-volatile bits (timing, session paths) before diffing."""
    text = re.sub(r"in [0-9.]+s", "in <t>s", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0x<addr>", text)
    return text


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
    run is deterministic. Tier-3 ``findings`` are recorded but never flip ``passed``.
    ``test_runner`` is injectable so the loop/structure is exercised without a live
    science stack; the default runs pytest twice with the analyst interpreter.
    """
    worktree = Path(worktree)
    cfg = config if config is not None else _code_gate_config()
    test_runner = test_runner or _default_test_runner

    targets = collect_targets(worktree)

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
