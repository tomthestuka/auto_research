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
                      implemented"), a banned word inside an explicit negation (prose stating
                      the discipline rather than breaking it), and numeric-literal arrays
                      returned/plotted/framed: recorded on the report but NOT a failure; the
                      step-4 co-lead judges intent.

All scan lists live in ``config/code_gate.yaml`` (tunable without a code edit).

The scanners are pure functions over file *contents* so they unit-test without a real
worktree; the worktree-level orchestration (git enumeration, pytest) is injectable via
the ``test_runner`` seam, mirroring ``merge_flow``'s deterministic-test design.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

import yaml

from auto_research.config_paths import code_gate_yaml, orchestration_yaml
from auto_research.sandbox import returns
from auto_research.schemas import CheckResult, GateReport, ToolSpec


# ----------------------------------------------------------------------------
# Config (config/code_gate.yaml) — the single place a human tunes the scan
# ----------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _load_yaml(path: Path) -> dict:
    """Read one config file, cached on its path (best-effort: absent reads as empty)."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def _code_gate_config() -> dict:
    """The scan lists this process is configured with."""
    return _load_yaml(code_gate_yaml())


# ----------------------------------------------------------------------------
# Target enumeration — which .py files (and which added lines) the scan covers
# ----------------------------------------------------------------------------

# A top-level package only: `obsdb_catalog/evil_utils/` is inside the protected catalog writer.
_TOOLKIT_PKG_RE = re.compile(r"^[A-Za-z0-9_]+_utils(/|$)")
#: A software-environment definition file (env plan B3c): ``envs/<name>/<one of its files>``.
_ENV_DEF_RE = re.compile(
    r"^envs/([a-z0-9][a-z0-9_-]{0,47})/(?:env\.yaml|env\.lock|env\.pip|activate\.sh|boot\.py)$"
)
#: Pinned lists, checked line by line by the environment validator rather than scanned as prose.
_ENV_PINNED_RE = re.compile(r"^envs/[^/]+/env\.(?:lock|pip)$")


def _gated(rel: str) -> bool:
    """Whether a changed path is the coder's to change: a ``*_utils`` package file or an
    environment definition file, named without glob characters (git reads ``*?[`` in a pathspec
    as a pattern; a name is only ever a name here)."""
    # `.gitattributes`/`.gitmodules`/`.gitignore` change what git does to, or shows of, every other
    # file (filters, encodings, submodules, hiding): never the coder's.
    try:
        rel.encode("utf-8")  # a name that is not UTF-8 (surrogate-escaped) is never a judged name
    except UnicodeEncodeError:
        return False
    return bool((_TOOLKIT_PKG_RE.search(rel) or _ENV_DEF_RE.match(rel)) and not re.search(r"[*?\[\\]", rel)
                and Path(rel).name not in (".gitattributes", ".gitmodules", ".gitignore"))


def _clear_index_lock(root: Path) -> None:
    """Remove the ``index.lock`` a git call the runtime timed out (and killed) left behind; only the
    runtime runs git in the toolkit (the coder's ``.git`` is read-only), so nothing else holds it.
    The git dir is ``.git`` itself or, in a linked worktree, the folder its ``.git`` file names."""
    dot_git = Path(root) / ".git"
    git_dir = dot_git
    if dot_git.is_file() and not dot_git.is_symlink():
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            git_dir = (Path(root) / text.split(":", 1)[1].strip()).resolve()
    lock = git_dir / "index.lock"
    if lock.is_file() and not lock.is_symlink():
        lock.unlink(missing_ok=True)


def _git(worktree: Path, *args: str) -> str:
    # surrogateescape: a latin-1 byte in a diff or a file name is carried, never a crash (a byte
    # that is not UTF-8 killed the authoring flow, B3c-1 review 10); the file-level check then
    # reports an undecodable file.
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args], cwd=str(worktree), capture_output=True,
            check=True, timeout=300,  # a hang is an error
        )
    except subprocess.TimeoutExpired:
        _clear_index_lock(Path(worktree))
        raise
    # Bytes decoded by hand: text mode would turn a lone CR into a line break, shifting git's lines.
    return out.stdout.decode("utf-8", "surrogateescape")


def _head_file(worktree: Path, rel: str) -> str:
    """``rel`` as committed at HEAD, or ``""`` when HEAD has no such file."""
    proc = subprocess.run(["git", "-C", str(worktree), "show", f"HEAD:{rel}"],
                          capture_output=True, text=True, errors="surrogateescape", timeout=300)
    return proc.stdout if proc.returncode == 0 else ""


def _status(worktree: Path) -> list[tuple[str, str]]:
    """``(XY code, path)`` per changed path, from ``git status --porcelain -z -uall``: NUL-separated,
    so a name with spaces or non-ASCII characters is the name itself, never git's quoted form; a
    rename yields its new path (the old one is consumed)."""
    raw = _git(worktree, "status", "--porcelain", "-z", "-uall").split("\0")
    out, i = [], 0
    while i < len(raw):
        entry = raw[i]
        i += 1
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            i += 1  # the original path of a rename or copy follows (index or work-tree side)
        out.append((code, rel))
    return out


def collect_all_changes(worktree: Path) -> list[str]:
    """Every changed path in ``worktree``, **unfiltered** — the wide enumeration (F2).

    :func:`collect_targets` answers *"which files does this tool consist of"* and so drops
    everything outside a ``*_utils`` package. That filter was then reused to answer *"did the
    author touch anything it should not"*, over a list from which it had already removed every
    violation — a check that could not fail for any input. Measured live 2026-09-08 (slot 4): 193
    changed lines in ``obsdb_catalog``, the toolkit's one hand-built trusted catalog writer, were
    never scanned, never tested, never shown to the reviewer, reported as "only allowed paths
    touched", and unstageable — yielding a commit whose staged code imports a symbol defined only
    in an unstaged file, so it does not run on a clean checkout.

    This is the separate query that question needs. Ignored files never appear (``git status``
    omits them), so ``__pycache__`` and ``.pytest_cache`` do not show up here.
    """
    return sorted({rel for _code, rel in _status(worktree) if rel})


def collect_targets(worktree: Path) -> dict[Path, set[int] | None]:
    """Map each changed/new ``*_utils`` ``.py`` file → its added line numbers.

    A value of ``None`` means "scan the whole file" (a new, untracked file). For a
    modified tracked file, the value is the set of added line numbers parsed from the
    unified diff, so AST hits on pre-existing template lines are never reported.
    """
    targets: dict[Path, set[int] | None] = {}
    # -uall lists each file inside a new untracked directory (not just the dir).
    for code, rel in _status(worktree):
        # Track every changed/new file under a ``*_utils`` package — not only ``.py``.
        # Shell reduction scripts and other text artifacts are part of the tool and must
        # be gated and staged too (§9.2). The package filter keeps scratch outside a
        # ``*_utils`` package invisible, so explicit-path staging never leaks it (§9.9).
        if not _gated(rel):
            continue
        path = worktree / rel
        if not path.is_file():
            continue
        if "?" in code or code.strip() == "A":
            targets[path] = None  # untracked / newly added → whole file
        else:
            targets[path] = _added_lines(worktree, rel)
    return targets


def collect_deleted(worktree: Path) -> list[str]:
    """Gated paths (:func:`_gated`) the changeset deletes. :func:`collect_targets` lists files that
    exist; a deletion must be staged and shown too, or the commit holds a tree the gate never saw
    (B3c-1 review: a deleted ``env.lock`` turned a validated definition into an invalid one)."""
    return sorted({rel for code, rel in _status(worktree)
                   if "D" in code and _gated(rel)
                   and (not os.path.lexists(worktree / rel) or (worktree / rel).is_dir())})


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
    # --text: a file over core.bigFileThreshold would show as "Binary files differ" and yield no
    # added lines, leaving all of it unscanned.
    diff = _git(worktree, "diff", "--text", "--unified=0", "--", rel)
    added: set[int] = set()
    new_lineno, in_hunk = 0, False
    for line in diff.split("\n"):
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            new_lineno, in_hunk = (int(m.group(1)), True) if m else (new_lineno, in_hunk)
            continue
        if not in_hunk or line.startswith("\\"):
            continue  # file headers; "\ No newline at end of file" names no line
        if line.startswith("+"):  # every `+` inside a hunk, `+++` included, is an added line
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


# Clause boundaries: a comma, semicolon, colon, or a dash used as punctuation. A negation
# governing one clause says nothing about the next, which is exactly what separates prose
# stating the discipline from prose deferring the work.
_CLAUSE_BOUNDARY = re.compile(r"[,;:]|\s--?\s|\s[—–]\s|—")
_NEGATION_WORDS = frozenset({"no", "not", "never", "cannot", "nor", "neither", "without"})
_NEGATION_PHRASES = ("rather than", "instead of")


def _negated(text_low: str, phrase: str) -> bool:
    """Whether ``phrase`` sits inside an explicit negation, in the same clause.

    A banned word appearing in a negated construction is the §9.5 discipline being *stated*,
    not violated — "the tool can never fabricate a benchmark", "without any fabricated
    fixture", "on real data rather than a fabricated CSV". Hard-failing those rejects a tool
    for documenting the very rule the scan enforces, which cost a real submission live
    (2026-09-02). Such a match is demoted to the co-lead-judged finding tier, the same
    treatment already given to bare descriptors and to RNG patterns.

    **Scope is the clause, not a token window.** A window is wrong in the direction that
    matters: ``# where the reduction has not run yet, emit fake data for now`` negates
    *running the reduction* and then positively instructs the fabrication, and a proximity
    rule reads that as exculpatory. Requiring the cue in the same comma/semicolon-delimited
    clause, before the phrase, keeps that a hard fail while demoting the honest disclosures.
    A missed negation degrades to today's hard fail; a spurious one degrades to a finding the
    co-lead reads — both directions stay safe.
    """
    index = text_low.find(phrase)
    if index < 0:
        return False
    clause = _CLAUSE_BOUNDARY.split(text_low[:index])[-1]
    if any(p in clause for p in _NEGATION_PHRASES):
        return True
    return bool(_NEGATION_WORDS & set(re.findall(r"[a-z']+", clause)))


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
    source_lines = source.split("\n")  # git's lines (`\n` only), as `added` counts them

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

    def _scan_text(text: str, base_line: int, span: range | None = None) -> None:
        # A string constant is in scope when any physical line it spans was added, and is then
        # scanned whole: its value's own line breaks (a `\n` escape, a form feed) are not git's
        # lines, so mapping them onto line numbers let added text escape (B3c-1 review 25).
        if span is not None:
            if added is not None and not any(line in added for line in span):
                return
            if all(line in allow_lines for line in span):
                return
        for offset, raw in enumerate(text.splitlines()):
            lineno = base_line + offset if span is None else span.start
            if span is None and (not _in_scope(lineno, added) or lineno in allow_lines):
                continue
            low = raw.lower()
            for phrase in _match_vocabulary(low, vocab):
                key = (lineno, phrase)
                if key not in seen_vocab:
                    seen_vocab.add(key)
                    if _negated(low, phrase):
                        _find("no_fabrication.vocabulary_finding", lineno,
                              f"banned phrase {phrase!r} inside a negation (recorded, not a "
                              "hard-fail)")
                    else:
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
    # Lines as git counts them (`\n` only): `splitlines` also breaks on form feeds, `\r`, U+2028…
    for offset, raw in enumerate(source.split("\n")):
        lineno = offset + 1
        if "#" in raw:
            # Only the comment portion (cheap heuristic: text after the last # that is
            # not inside an obvious string is good enough for a vocabulary scan).
            comment = raw[raw.index("#") :]
            # One unit on git's line: a form feed or U+2028 inside it is not a line break there.
            _scan_text(comment, lineno, range(lineno, lineno + 1))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            start = getattr(node, "lineno", 0)
            value = node.value if isinstance(node.value, str) else node.value.decode("utf-8", "replace")
            _scan_text(value, start, range(start, (getattr(node, "end_lineno", None) or start) + 1))

    # ── Code patterns (hard + finding tiers) + literal-array findings + placeholder bodies (AST) ──
    pattern_lines: set[int] = set()  # one pattern report per line (nested attrs would double up)
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        # In scope when any line the node spans was added — not only its first: an added
        # continuation line (`.random.normal(x)`) or a function body changed under an unchanged
        # `def` line escaped otherwise (B3c-1 review 28).
        if lineno is None or not _touches(node, added):
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

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_placeholder_body(node) and (
            _in_scope(lineno, added) or any(_touches(stmt, added) for stmt in _body_without_docstring(node.body))
        ):
            # The `def` line or the body itself changed — never a pre-existing placeholder flagged
            # because some other line of a long function was edited.
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
    for lineno, raw in enumerate(source.split("\n"), 1):  # git's lines (`\n` only)
        if not _in_scope(lineno, added) or any(rx.search(raw) for rx in allow_res):
            continue
        low = raw.lower()
        for phrase in _match_vocabulary(low, vocab):
            key = (lineno, phrase)
            if key not in seen:
                seen.add(key)
                if _negated(low, phrase):
                    findings.append(CheckResult(
                        name="no_fabrication.vocabulary_finding", layer="A", passed=True,
                        detail=f"{rel_path}:{lineno} — banned phrase {phrase!r} inside a "
                               "negation (recorded, not a hard-fail)",
                    ))
                else:
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
# Layer A.2 — provenance (pure, AST): the figure dicts a tool returns match FigureRef
# ----------------------------------------------------------------------------
#
# The runtime contract (prompts/coder.md): a tool returns a dict whose top-level "figures" is a list
# of plain dicts, each validated into a `FigureRef` by `runner.parse_tool_figures`; an invalid entry
# is dropped there. This check fails, at the gate, the entries the runtime would drop and that are
# visible in the source: a literal dict (or `dict(...)` call) reached from a "figures" value within
# one function — inline, through a list/comprehension, through a name the function builds with
# `append`/`extend`/`+=`, or through a name bound to a dict. The keys and literal values are judged
# against `FigureRef` itself, so the check follows the schema. A figure assembled elsewhere (a helper
# function, `update`, `**` spread) is outside the check; the runtime still drops it if malformed.


@lru_cache(maxsize=1)
def _figure_contract() -> tuple[frozenset[str], frozenset[str], dict]:
    """``(required keys, allowed keys, per-key validators)`` read off ``FigureRef``."""
    from typing import Annotated

    from pydantic import TypeAdapter

    from auto_research.schemas.figures import FigureRef

    fields = FigureRef.model_fields
    required = frozenset(k for k, f in fields.items() if f.is_required())
    adapters = {
        k: TypeAdapter(Annotated[(f.annotation, *f.metadata)] if f.metadata else f.annotation)
        for k, f in fields.items()
    }
    return required, frozenset(fields), adapters


def _touches(node: ast.AST, added: set[int] | None) -> bool:
    """Whether any line of ``node`` is in the diff's added lines (all lines when ``added`` is None)."""
    if added is None:
        return True
    first = getattr(node, "lineno", 0)
    last = getattr(node, "end_lineno", first) or first
    return any(n in added for n in range(first, last + 1))


def _figures_values(fn: ast.AST) -> list[ast.expr]:
    """Every expression one function places under a ``"figures"`` key."""
    out: list[ast.expr] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            out.extend(v for k, v in zip(node.keys, node.values)
                       if isinstance(k, ast.Constant) and k.value == "figures")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "figures"):
                    out.append(node.value)
        elif isinstance(node, ast.Call) and _dotted_name(node.func) == "dict":
            out.extend(kw.value for kw in node.keywords if kw.arg == "figures")
    return out


def _bindings(fn: ast.AST, name: str) -> tuple[list[ast.expr], list[ast.expr], set[str]]:
    """What one function binds to ``name``: ``(assigned values, appended elements, subscript-set keys)``."""
    assigned: list[ast.expr] = []
    elements: list[ast.expr] = []
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name:
                    assigned.append(node.value)
                elif (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == name and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str)):
                    keys.add(t.slice.value)
        elif (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
                and node.target.id == name):
            assigned.append(node.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == name):
            if node.func.attr == "append" and node.args:
                elements.append(node.args[0])
            elif node.func.attr == "insert" and len(node.args) == 2:
                elements.append(node.args[1])
            elif node.func.attr == "extend" and node.args:
                assigned.append(node.args[0])
    return assigned, elements, keys


def _list_elements(fn: ast.AST, value: ast.expr, rel: str, added, out: list[CheckResult]) -> list[ast.expr]:
    """The figure entries a ``"figures"`` value holds, failing a literal that is not a list."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return list(value.elts)
    if isinstance(value, ast.ListComp):
        return [value.elt]
    if isinstance(value, ast.Name):
        assigned, elements, _keys = _bindings(fn, value.id)
        for v in assigned:
            if isinstance(v, ast.Name):
                continue
            elements.extend(_list_elements(fn, v, rel, added, out))
        return elements
    kind = {ast.Dict: "a dict", ast.DictComp: "a dict", ast.Set: "a set", ast.SetComp: "a set",
            ast.JoinedStr: "a string"}.get(type(value))
    if kind is None and isinstance(value, ast.Constant) and value.value is not None:
        kind = f"a {type(value.value).__name__}"
    if kind and _touches(value, added):
        out.append(CheckResult(
            name="provenance.figure_shape", layer="A", passed=False,
            detail=(f"{rel}:{value.lineno} — 'figures' is {kind}; return it as a list of figure "
                    "dicts, one per PNG written"),
        ))
    return []


def _check_figure_entry(fn: ast.AST, entry: ast.expr, rel: str, added, out: list[CheckResult]) -> None:
    """Judge one figure entry against ``FigureRef``: a dict with exactly the schema's keys."""
    required, allowed, adapters = _figure_contract()
    extra_keys: set[str] = set()
    if isinstance(entry, ast.Name):
        assigned, _elements, extra_keys = _bindings(fn, entry.id)
        dicts = [v for v in assigned if isinstance(v, (ast.Dict, ast.Call))]
        if not dicts:
            return
        for d in dicts:
            _check_figure_dict(d, extra_keys, rel, added, out, required, allowed, adapters)
        return
    if isinstance(entry, (ast.Dict, ast.Call)):
        _check_figure_dict(entry, extra_keys, rel, added, out, required, allowed, adapters)
        return
    if isinstance(entry, (ast.Constant, ast.JoinedStr, ast.List, ast.Tuple)) and _touches(entry, added):
        kind = "a string" if isinstance(entry, ast.JoinedStr) else (
            "a list" if isinstance(entry, (ast.List, ast.Tuple)) else f"a {type(entry.value).__name__}")
        out.append(CheckResult(
            name="provenance.figure_shape", layer="A", passed=False,
            detail=(f"{rel}:{entry.lineno} — a figure entry is {kind}; make each entry a dict with "
                    f"the keys {sorted(required)}"),
        ))


def _check_figure_dict(node, set_keys, rel, added, out, required, allowed, adapters) -> None:
    """Judge one literal figure dict (or ``dict(...)`` call): keys, then literal values."""
    if isinstance(node, ast.Call):
        if _dotted_name(node.func) != "dict" or node.args:
            return
        items = [(kw.arg, kw.value) for kw in node.keywords]
    else:
        items = [(k.value if isinstance(k, ast.Constant) else None, v)
                 for k, v in zip(node.keys, node.values)]
    if not _touches(node, added):
        return
    keys = {k for k, _v in items if isinstance(k, str)} | set_keys
    opaque = any(not isinstance(k, str) for k, _v in items)   # a ** spread or a computed key
    problems: list[str] = []
    missing = set() if opaque else required - keys
    if missing:
        problems.append(f"missing {sorted(missing)}")
    extra = keys - allowed
    if extra:
        problems.append(f"keys FigureRef rejects {sorted(extra)}")
    for k, v in items:
        if k in adapters and isinstance(v, ast.Constant):
            try:
                adapters[k].validate_python(v.value)
            except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, reported by its message
                msg = exc.errors()[0]["msg"] if hasattr(exc, "errors") else str(exc)
                problems.append(f"{k}={v.value!r} invalid ({msg})")
    if problems:
        out.append(CheckResult(
            name="provenance.figure_shape", layer="A", passed=False,
            detail=(f"{rel}:{node.lineno} — figure dict {'; '.join(problems)}. The runtime drops "
                    f"this figure; give each figure exactly the keys {sorted(required)}"
                    + (f" (optional: {sorted(allowed - required)})" if allowed - required else "")),
        ))


def scan_figure_shape(rel_path: str, source: str, *, added: set[int] | None = None) -> list[CheckResult]:
    """Fail each figure a tool returns that ``FigureRef`` would reject, where the source shows it
    (§7/§9.4): a ``"figures"`` value that is not a list, an entry that is not a dict, a dict with a
    missing or extra key, or a literal value the schema refuses. Vacuously passes when the file
    returns no figures. Scoped to the diff's added lines, like every other scan."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[CheckResult] = []
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in functions or [tree]:
        for value in _figures_values(fn):
            for entry in _list_elements(fn, value, rel_path, added, out):
                _check_figure_entry(fn, entry, rel_path, added, out)
    # A nested function is walked from its parent too; report each problem once.
    return list({c.detail: c for c in out}.values())


# ----------------------------------------------------------------------------
# Layer A.3 — structural checks (filesystem)
# ----------------------------------------------------------------------------

_TEST_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")


def check_structural(
    worktree: Path,
    tool_spec: ToolSpec,
    targets: dict[Path, set[int] | None],
    all_changes: list[str] | None = None,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    rels = [str(p.relative_to(worktree)) for p in targets]

    # (a) changes_allowed — the gate's third job, beside the no-fabrication scan and the double
    # pytest run (§9.4). An **explicit allow-list**: a change is permitted only under a
    # ``*_utils`` package, which is the layout `prompts/coder.md` already teaches and §9.2
    # specifies. Everything else is rejected and named, `obsdb_catalog` included — the toolkit's
    # hand-built trusted catalog writer is human + build-agent work (CLAUDE.md §3), never the
    # runtime coder's, and a review that asks for a change inside it is a message to the lead.
    #
    # Computed over `collect_all_changes`, NOT over `targets`: the old check filtered its input
    # through the very pattern it then tested, so it could not fail for any input (F2). An
    # allow-list is also why a deny-list of protected packages was rejected — that would have left
    # toolkit-root files and any future package not ending in `_utils` silently dropped, which is
    # the shape whose cause went unidentified in slot 5.
    #
    # `run_gate` enumerates once, before Layer B executes, so an artifact a tool's own test writes
    # into the worktree cannot trip this in the same run.
    changes = collect_all_changes(worktree) if all_changes is None else all_changes
    # A link among the changes (or a folder on its path replaced by one) would let the scans read,
    # and Layer B import, files outside the worktree that are never staged.
    # A folder entry is a nested repository (git lists one as `dir/`): its files are run by the
    # tests but never scanned, shown or committed.
    stray = [r for r in changes if not _gated(r) or r.endswith("/") or _through_link(worktree, r)]
    stray += [r for r in untracked_ignore_files(worktree) if r not in stray]
    out.append(CheckResult(
        name="structural.changes_allowed", layer="A", passed=not stray,
        detail="" if not stray else (
            f"changes outside a *_utils package or an environment definition: {stray}. Toolkit "
            "code lives in <domain>_utils/{lib,skills,tests} (§9.2); an environment in "
            "envs/<name>/{env.yaml,env.lock,env.pip,activate.sh,boot.py}. Anything else — the "
            "catalog writer, repo-root files, scratch — is not the coder's to edit: message the "
            "lead (message to='lead') instead."
        ),
    ))
    out.extend(check_env_definitions(
        worktree, changes, _touched_packages(worktree, targets), tool_spec.target_domain,
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


def untracked_ignore_files(worktree: Path) -> list[str]:
    """Every ``.gitignore`` git does not track: written during the attempt, and able to hide
    anything — itself included — from every check (a self-ignoring ``json/.gitignore`` hid a
    ``json`` package that shadowed the standard library for every worker, B3c-1 review 16). The
    ones pytest writes inside its own ``.pytest_cache`` (not an importable name) are left alone."""
    # Ignored or not: one that does not ignore itself hides the rest just the same.
    raw = (_git(worktree, "ls-files", "-o", "-i", "--exclude-standard", "-z")
           + "\0" + _git(worktree, "ls-files", "-o", "--exclude-standard", "-z"))
    # `.gitattributes` too: an untracked one changes how the reset itself checks files out (an
    # encoding attribute left a file re-encoded after the reset, B3c-1 review 24).
    return sorted({rel for rel in raw.split("\0")
                   if rel and Path(rel).name in (".gitignore", ".gitattributes")
                   and ".pytest_cache" not in Path(rel).parts})


def changeset_manifest(worktree: Path) -> dict[str, str]:
    """``{path: sha256 | "deleted"}`` for every gated file the changeset holds — what a commit
    would stage. Recorded on the gate report, compared again before the commit."""
    out = {}
    for path in collect_targets(worktree):
        rel = str(path.relative_to(worktree))
        if not _interpreter_cache(rel):
            out[rel] = _entry(path)
    out.update({rel: "deleted" for rel in collect_deleted(worktree)})
    return out


def _entry(path: Path) -> str:
    """A judged file's identity: ``<mode>:<sha256>`` of the file itself (a link is ``link:<target>``,
    so a file swapped for a link to identical bytes, or made executable, no longer matches)."""
    import stat

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return f"link:{os.readlink(path)}"
    return f"{stat.S_IMODE(info.st_mode):o}:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _export_judged(worktree: Path, manifest: dict[str, str], dest: Path, *, base: str = "HEAD") -> None:
    """A clean tree for Layer B: the commit's parent (``HEAD``) plus exactly the judged files, each
    checked against its manifest entry. Nothing else in the worktree — an ignored file, a stray
    ``.pyc``, a self-ignoring ``.gitignore`` — can be imported by the tests, so what they ran is what
    the commit holds. ``HEAD`` is written from its raw objects (``ls-tree`` + ``cat-file``), never
    through ``git archive``, whose ``.gitattributes`` handling (``export-ignore``, ``export-subst``)
    would make the tested tree differ from the committed one. Raises ``ValueError`` when a judged
    file no longer matches."""
    listing = subprocess.run(["git", "-C", str(worktree), "ls-tree", "-r", "-z", "--full-tree", base],
                             capture_output=True, check=True, timeout=300).stdout.decode("utf-8", "surrogateescape")
    entries = []
    for row in filter(None, listing.split("\0")):
        meta, rel = row.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            entries.append((mode, sha, rel))
    batch = subprocess.run(["git", "-C", str(worktree), "cat-file", "--batch"],
                           input="".join(f"{sha}\n" for _m, sha, _r in entries).encode(),
                           capture_output=True, check=True, timeout=300).stdout
    dest.mkdir(parents=True)
    offset = 0
    for mode, _sha, rel in entries:
        header_end = batch.index(b"\n", offset)
        header = batch[offset:header_end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"HEAD object for {rel} is unreadable ({b' '.join(header).decode(errors='replace')})")
        size = int(header[2])
        body = batch[header_end + 1: header_end + 1 + size]
        offset = header_end + 1 + size + 1
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            os.symlink(body.decode("utf-8", "surrogateescape"), target)
        else:
            target.write_bytes(body)
            target.chmod(0o755 if mode == "100755" else 0o644)
    import stat

    for rel, entry in sorted(manifest.items(), key=lambda kv: kv[1] != "deleted"):  # deletions first
        target = dest / rel
        if entry == "deleted":
            if target.is_symlink() or target.is_file():  # HEAD holds blobs and links only
                target.unlink()
            continue
        # As `git add` sees it: a committed link on the way is replaced by a real folder, so a
        # write never follows one out of the export (B3c-1 review 6).
        walk = dest
        for part in Path(rel).parts[:-1]:
            walk = walk / part
            if walk.is_symlink():
                walk.unlink()
            walk.mkdir(exist_ok=True)
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():  # a folder replaced by a file: its files were all deleted above
            target.rmdir()
        # Hash the bytes actually copied (one read), with the source's mode: what was judged.
        fd = os.open(worktree / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            data = handle.read() if stat.S_ISREG(info.st_mode) else b""
        if f"{stat.S_IMODE(info.st_mode):o}:{hashlib.sha256(data).hexdigest()}" != entry:
            raise ValueError(f"{rel} changed while the gate ran")
        target.write_bytes(data)
        target.chmod(int(entry.split(":", 1)[0], 8))

def _interpreter_cache(rel: str) -> bool:
    """Bytecode Python writes into ``__pycache__`` beside the code it imports: regenerated, never
    authored. Anything else — a ``.py`` placed in ``__pycache__``, a sourceless ``.pyc`` beside the
    code — is a judged file like any other (and the binary check fails a ``.pyc``)."""
    return "__pycache__" in Path(rel).parts and rel.endswith((".pyc", ".pyo"))


def _through_link(worktree: Path, rel: str) -> bool:
    """Whether ``rel``, or any folder on its way inside ``worktree``, is a symbolic link."""
    path = worktree
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            return True
    return False


def check_env_definitions(
    worktree: Path, changes: list[str], packages: dict[str, Path], target_domain: str | None = None,
) -> list[CheckResult]:
    """Each environment definition the changeset touches must validate (``env_build.validate``:
    conda-forge explicit lock with checksums, hash-pinned pip wheels, a human base that exists,
    externals only from the human's declared list) and must ship with a package that runs in it:
    a touched package whose ``tool_config.yaml`` names it (``env: <name>``). One result per
    definition; none when the changeset touches no definition."""
    from auto_research.env import analysis_environment, declared_externals
    from auto_research.sandbox.env_build import validate

    names = sorted({m.group(1) for r in changes if (m := re.match(r"^envs/([^/]+)/", r))})
    # Every package this session works on that names an environment must resolve to a
    # definition: the brief's target package, and every package the changeset touches.
    naming = dict(packages)
    if target_domain and _PKG_SEGMENT_RE.fullmatch(target_domain) and (worktree / target_domain).is_dir():
        naming.setdefault(target_domain, worktree / target_domain)
    from auto_research.sandbox.env_build import _NAME

    users = {pkg: _package_env(root) for pkg, root in naming.items()}
    out = []
    # A name, not a path: `../x`, `/abs`, `x/` resolve nowhere a definition may live.
    unresolved = {pkg: env for pkg, env in users.items() if env is not None and (
        not _NAME.fullmatch(env) or not (worktree / "envs" / env / "env.yaml").is_file())}
    if unresolved or any(env is not None for env in users.values()):
        out.append(CheckResult(
            name="structural.env_resolves", layer="A", passed=not unresolved,
            detail="" if not unresolved else "; ".join(
                (f"{pkg}/tool_config.yaml names env {env!r}, but envs/{env}/env.yaml does not exist: "
                 "write the definition there, or correct the name")
                if _NAME.fullmatch(env) else
                (f"{pkg}/tool_config.yaml names env {env!r}, which is not an env name: give the "
                 "folder name under envs/ (lowercase letters, digits, '_' and '-')")
                for pkg, env in sorted(unresolved.items(), key=lambda kv: kv[0])
            ),
        ))
    if not names:
        return out
    human = analysis_environment()
    bases = {human.name: human} if human is not None else {}
    externals = declared_externals()
    for name in names:
        if not (worktree / "envs" / name).exists():
            # A deleted definition (B3c-4 drops its build from the catalogue at commit): fine
            # unless a package anywhere in the toolkit still runs in it.
            still = sorted(p.name for p in worktree.glob("*_utils") if p.is_dir() and _package_env(p) == name)
            out.append(CheckResult(
                name=f"structural.env_definition.{name}", layer="A", passed=not still,
                detail="" if not still else (f"envs/{name} is deleted but {', '.join(still)} still name it "
                                             "in tool_config.yaml: point them at another env, or keep it."),
            ))
            continue
        problems = validate(worktree / "envs" / name, base_envs=bases, externals=externals)
        if name not in users.values():
            problems.append(
                f"no touched package names envs/{name}: add `env: {name}` to the tool_config.yaml "
                "of the package whose tools run in it, so its tests run there."
            )
        out.append(CheckResult(
            name=f"structural.env_definition.{name}", layer="A", passed=not problems,
            detail="" if not problems else "; ".join(problems),
        ))
    return out


def _package_env(package_root: Path) -> str | None:
    """The environment a package's ``tool_config.yaml`` names (``env:``), or ``None``."""
    path = package_root / "tool_config.yaml"
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — a malformed config fails resolution, visibly
        return "<tool_config.yaml does not parse>"
    if not isinstance(data, dict) or data.get("env") is None:
        return None
    env = data["env"]
    return env if isinstance(env, str) else repr(env)  # a non-name fails resolution, visibly


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
# ``(worktree, test_paths)``; a group of tests whose package names an environment also gets
# ``environment=`` (B3c-2), and the default runner its group's ``returns_tools=``.
TestRunner = Callable[..., Awaitable[dict]]


def _layer_b_output_cap() -> int:
    """How much pytest output a Layer-B failure keeps (``orchestration.yaml``, default 100000).

    Read at call time, never captured at import, so the value a run used travels with that run.

    Was a hardcoded 2,000-character tail (N1). With a chatty science suite that window is
    *entirely* the warnings summary: measured live 2026-09-08 (slot 2), 119 warnings meant the
    kept text was the warnings block plus pytest's ``short test summary info``, so the failing
    test's **name** survived and every assertion and traceback was cut. The coder said so in its
    own words — it ran the single test, the whole file, and the whole file twice more in fresh
    copies, all green, and could not reproduce a failure it was never shown. That cost 3 h 28 m
    and $16.26, 43.7% of the run's wall clock, and two of the fourteen named quantities.
    """
    try:
        return int(_orchestration_config().get("gate_layer_b_output_cap", 100000))
    except (TypeError, ValueError):
        return 100000


def _bounded_pytest_output(out: str) -> str:
    """Bound pytest output for the report while keeping the parts a coder can act on (N1).

    Keeps the **head and the tail**, not the tail alone. pytest prints failure detail — the
    assertion and its traceback — *before* the warnings summary and the ``short test summary
    info`` line, so a pure tail window keeps the least useful end. That is exactly what happened
    live 2026-09-08 (slot 2): with 119 warnings the kept 2,000 characters were entirely the
    warnings block plus the FAILED line, so the coder had the test's name and nothing about why.
    It re-ran the suite four ways, all green, and reported that it could not reproduce a failure
    it was never shown — 3 h 28 m and $16.26, and two of the fourteen named quantities lost.

    Under the cap the output is returned whole; over it, two thirds from the front and one third
    from the back, with a marker naming what was dropped so nothing looks complete when it is not.
    """
    cap = _layer_b_output_cap()
    if len(out) <= cap:
        return out
    head, tail = (cap * 2) // 3, cap // 3
    dropped = len(out) - head - tail
    return f"{out[:head]}\n\n[... {dropped} characters of pytest output elided ...]\n\n{out[-tail:]}"


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
        return False, _bounded_pytest_output(out1)
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
    worktree: Path, test_paths: list[str], *, timeout_s: int = 900,
    returns_tools: dict[str, tuple[str, str]] | None = None, masked: Path | None = None,
    environment: Any | None = None,
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

    ``environment`` (B3c-2): a pipeline environment the tested package names, built from the judged
    definition. Its Python runs the tests, its activation sourced inside the jail
    (:func:`envs.in_environment`, never in this process: it is coder-written and not yet reviewed),
    its prefix and activation copy bound read-only. ``None``: the analysis environment, as before.
    """
    import os

    from auto_research.sandbox.runner import with_private_param_dir

    cfg = _orchestration_config()
    from auto_research.env import analysis_environment

    analysis = analysis_environment(cfg)
    python_exec = str(analysis.python) if analysis is not None else "python"

    if analysis is not None:
        base_env = analysis.activation_env()
    elif cfg.get("analyze_ini_sh"):  # an init script with no prefix: sourced clean, as before
        from auto_research.sandbox.runner import _source_ini_env

        base_env = dict(_source_ini_env(cfg["analyze_ini_sh"]))
    else:
        base_env = dict(os.environ)
    existing = base_env.get("PYTHONPATH", "")
    env = {**base_env, "PYTHONPATH": str(worktree) + (":" + existing if existing else "")}
    extra_ro: tuple[str, ...] = ()
    if environment is not None:
        python_exec = str(environment.python)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
        extra_ro = tuple(str(p) for p in (environment.prefix,
                                          *((environment.activate.parent,) if environment.activate else ())))

    def _run(junit_path: str) -> tuple[int, str, dict[str, str]]:
        # The return check's recorder (G15) loads in both runs, so the two argvs stay identical for
        # the determinism compare; each run records into its own directory beside its JUnit file.
        plugin_args, run_env = returns.install(
            Path(junit_path).with_suffix(".returns"), returns_tools or {}, env,
        )
        argv = [python_exec, "-m", "pytest", "-p", "no:cacheprovider", *plugin_args, "-q",
                f"--junitxml={junit_path}", *test_paths]
        if environment is not None:
            # A clean start in the environment, inside the jail below (the shared launcher).
            import shlex

            from auto_research.sandbox.envs import in_environment

            script = in_environment(environment, f'{shlex.quote(python_exec)} "$@"', worktree, pass_args=True,
                                    keep=("AR_RETURNS_OUT", "AR_RETURNS_TOOLS", "AR_RETURNS_COPY_CAP", "PYTHONPATH"))
            argv = ["/bin/bash", "--noprofile", "--norc", "-c", script, "_", *argv[1:]]
        # The OS jail, with **no network** (§12): a mergeability verdict that can reach the network
        # is a verdict that depends on something other than the data on disk, and §9.4's premise is
        # real local input. Verified against all 30 existing Layer-B suites before being turned on.
        run_env = {**run_env, "PYTHONNOUSERSITE": "1"}
        unjailed = list(argv)
        argv = _jailed_pytest(argv, junit_path=junit_path, worktree=worktree, masked=masked, extra_ro=extra_ro)
        # Its own HEASoft parameter folder (F23), shared by both runs like the private /tmp.
        private = _gate_private_tmp(junit_path)
        jailed = argv != unjailed
        run_env = with_private_param_dir(
            run_env, "/tmp/pfiles" if jailed else private / "pfiles",
            create=private / "pfiles",
        )
        if environment is not None:
            # The launcher puts this writable half back after the activation (HEASoft's init points
            # PFILES at the real home's folder, F23).
            (private / "pfiles").mkdir(parents=True, exist_ok=True)
            run_env["PFILES"] = "/tmp/pfiles" if jailed else str(private / "pfiles")
        # One pytest scratch folder for both runs, which pytest wipes when each run starts: run 1's
        # scratch is gone before run 2 with the jail on or off, whatever TMPDIR says.
        basetemp = "/tmp/pytest-gate" if jailed else str(private / "pytest-gate")
        argv = [*argv, f"--basetemp={basetemp}"]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(worktree), capture_output=True, text=True, env=run_env, timeout=timeout_s,
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
        # Run 1's pytest scratch goes before run 2 (human ruling 2026-10-06): a toolkit suite can
        # leave tens of GB there (measured: 65 GB in 10 min), and two runs' worth filled the disk.
        for scratch in _gate_private_tmp(str(Path(td) / "junit1.xml")).glob("pytest-of-*"):
            shutil.rmtree(scratch, ignore_errors=True)
        _rc2, _out2, map2 = _run(str(Path(td) / "junit2.xml"))
        return_checks, return_findings = (
            returns.judge(Path(td) / "junit1.returns", returns_tools) if returns_tools else ([], [])
        )

    passed, detail = _layer_b_verdict(rc1, map1, out1)
    results = [CheckResult(name="layer_b.pytest", layer="B", passed=passed, detail=detail)]
    results.extend(return_checks)
    deterministic, detail = _outcomes_determinism(map1, map2)
    return {
        "results": results,
        "findings": return_findings,
        "deterministic": deterministic,
        "determinism_detail": detail,
    }


def _gate_private_tmp(junit_path: str) -> Path:
    """The Layer-B run's private ``/tmp``: inside the JUnit ``TemporaryDirectory``, removed with it.

    Both determinism runs share it (they share the JUnit directory), which is what lets a test that
    leaves state behind in ``/tmp`` show up as an outcome flip rather than be hidden by a fresh one.
    """
    path = Path(junit_path).parent / "tmp"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _jailed_pytest(
    argv: list[str], *, junit_path: str, worktree: Path | None = None, masked: Path | None = None,
    extra_ro: tuple[str, ...] = (),
) -> list[str]:
    """Wrap the Layer-B pytest invocation in the OS jail, network denied.

    The JUnit report is written to a throwaway directory outside the worktree, so that directory
    has to be writable inside the jail too — otherwise the run succeeds and its outcome map comes
    back empty, which the determinism compare would read as "no tests ran". The worktree under test
    is bound writable explicitly: a benchmark trial's tree lives under the host ``/tmp``, which the
    private ``/tmp`` below no longer exposes.
    """
    from auto_research.sandbox.jail import JAIL_HOME_AT, JailPolicy, policy_from_config, wrap

    base = policy_from_config(network=False, label="gate")
    # Its own home, inside the JUnit temporary folder and removed with it: a shared one let a
    # coder-written test leave a `.pth` or an `Xspec.init` for every later gate (F22 review).
    home = Path(junit_path).parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    host_tmp = Path("/tmp").resolve()
    tree = Path(worktree).resolve() if worktree else None
    # Writable: the worktree under test, the JUnit folder and its own home -- nothing else. This
    # process runs coder-authored code nobody has reviewed yet, so every other allow-listed root
    # (the observation data, every run's folders and authoring control files) is read-only to it
    # (F24, 2026-10-06), and the host /tmp is replaced by a private one (F20 review): it holds other
    # sessions' trees and credential files. The JUnit folder, usually under the host /tmp, is still
    # bound on top of the private one.
    others = tuple(
        e for e in (*base.ro, *base.rw)
        if Path(e).resolve() != host_tmp and (tree is None or Path(e).resolve() != tree)
    )
    policy = JailPolicy(
        ro=(*others, *extra_ro),
        rw=(str(Path(junit_path).parent), *((str(worktree),) if worktree else ()), str(home)),
        network=False,
        home=str(home),
        label="gate",
        tmp=str(_gate_private_tmp(junit_path)),
        home_at=JAIL_HOME_AT,
        # The real worktree, when the tests run on an export of it: hidden, so a test cannot
        # import an unjudged file from it by absolute path (B3c-1 review 5).
        masked=(str(masked),) if masked is not None else (),
    )
    return wrap(argv, policy)


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


def _orchestration_config() -> dict:
    """Load and cache the configured ``orchestration.yaml`` (best-effort).

    Cached **on the resolved path** rather than on nothing: an override is a per-process decision
    set before the process starts, so in a real run the key never changes, but keying on it means a
    test that points the process elsewhere is not served the previous directory's answer.
    """
    return _load_yaml(orchestration_yaml())


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
    # Enumerated once, here, BEFORE Layer B runs: an artifact a tool's own test writes into the
    # worktree must not be able to fail `changes_allowed` in the same gate run (F2).
    all_changes = collect_all_changes(worktree)
    manifest = changeset_manifest(worktree)
    base = _git(worktree, "rev-parse", "HEAD").strip()

    watched = None
    if test_runner is None:
        # Bind the per-package timeout into the default runner only; an injected runner keeps
        # its (worktree, test_paths) signature untouched. Derived from the touched changeset
        # (like every other check), so it no longer depends on the brief's advisory field.
        watched = returns.watched_tools(worktree, targets, partial(_head_file, worktree))
        test_runner = partial(
            _default_test_runner, timeout_s=_gate_test_timeout(worktree, targets), masked=worktree,
            returns_tools=watched,
        )

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
        if rel.endswith(".py") and re.search(r"^[ \t\f]*#.*?coding[:=][ \t]*(?!utf-?8\b)[-\w.]+", "\n".join(source.split("\n")[:2]), re.I | re.M):
            # Python decodes a source by its PEP 263 cookie at import, the scans read UTF-8: under
            # `utf-7` a "comment" is code (B3c-1 review 31). UTF-8 only.
            layer_a.append(CheckResult(
                name="no_fabrication.encoding", layer="A", passed=False,
                detail=f"{rel} — a source-encoding declaration other than utf-8; write UTF-8 and drop it",
            ))
            continue
        if re.search(r"\r(?!\n)", source):
            # Python reads a lone CR as a line break, git does not: every scan scoped to git's
            # added lines would then look at the wrong ones (B3c-1 review 27). No source needs one.
            layer_a.append(CheckResult(
                name="no_fabrication.line_endings", layer="A", passed=False,
                detail=f"{rel} — a carriage return without a line feed; use \\n line endings",
            ))
            continue
        if _ENV_PINNED_RE.match(rel):
            # A pinned list of URLs and hashes, checked line by line in A.3: only its comment
            # lines are prose (a package named like a banned word, `mock`, is not).
            source = "\n".join(ln if ln.lstrip().startswith("#") else "" for ln in source.split("\n"))
        try:
            if rel.endswith(".py") and not _ENV_PINNED_RE.match(rel):
                hard, finds = scan_python_source(rel, source, added=added, config=cfg)
                layer_a.extend(hard)
                findings.extend(finds)
                if not _TEST_RE.search(rel):   # a test's figure dicts are fixtures, not tool returns
                    layer_a.extend(scan_figure_shape(rel, source, added=added))
            else:
                hard, finds = scan_text_source(rel, source, added=added, config=cfg)
                layer_a.extend(hard)
                findings.extend(finds)
        except (RecursionError, MemoryError):
            # A file nested too deeply to parse or walk is a failed check, never a crash that ends
            # the authoring (B3c-1 review 29).
            layer_a.append(CheckResult(
                name="no_fabrication.syntax", layer="A", passed=False,
                detail=f"{rel} — nested too deeply to scan; write it as plain, shallow code",
            ))

    # A.3 structural.
    structural = check_structural(worktree, tool_spec, targets, all_changes)
    layer_a.extend(structural)

    # Summarising no-fabrication/provenance as explicit PASS checks aids the feedback msg.
    if not any(c.name.startswith("no_fabrication") and not c.passed for c in layer_a):
        layer_a.append(CheckResult(name="no_fabrication.scan", layer="A", passed=True, detail="clean"))

    # Layer B + determinism — only when structure is sound (else the run is moot).
    layer_b: list[CheckResult] = []
    structural_ok = all(c.passed for c in structural)
    if structural_ok:
        test_paths = sorted(str(p.relative_to(worktree)) for p in targets if _TEST_RE.search(str(p)))
        export_root = Path(tempfile.mkdtemp(prefix="gate-export-"))
        try:
            export = export_root / worktree.name
            try:
                _export_judged(worktree, manifest, export, base=base)
            except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
                outcome = {"results": [CheckResult(
                    name="layer_b.export", layer="B", passed=False,
                    detail=f"the judged files could not be exported for the tests: {exc}",
                )], "deterministic": True}
            else:
                outcome = await _run_layer_b(test_runner, export, test_paths, watched=watched)
        finally:
            shutil.rmtree(export_root, ignore_errors=True)
        # What is judged is what was scanned: a test that writes into the worktree would change
        # the changeset after the scans ([G3]).
        after = changeset_manifest(worktree)
        moved = sorted(k for k in set(manifest) | set(after) if manifest.get(k) != after.get(k))
        moved += sorted(r for r in set(collect_all_changes(worktree)) - set(all_changes) - set(moved)
                        if not _interpreter_cache(r))
        # (The tests run on an export with the worktree hidden; this catches the rest — the jail
        # off, or a process editing the worktree while the gate ran.)
        layer_a.append(CheckResult(
            name="structural.tests_left_tree_unchanged", layer="A", passed=not moved,
            detail="" if not moved else (
                f"the changeset changed while the gate ran: {moved[:10]}. The gate judges the files "
                "as submitted; leave them untouched until it reports, and write test output under "
                "pytest's tmp_path."
            ),
        ))
        layer_b.extend(outcome.get("results", []))
        findings.extend(outcome.get("findings", []))
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
        reason=reason, report_hash=_report_hash(layer_a, layer_b), manifest=manifest, base=base,
    )


async def _run_layer_b(
    test_runner: TestRunner, export: Path, test_paths: list[str], *, watched: dict | None = None,
) -> dict:
    """Layer B per environment (B3c-2): each package's tests run in the environment its
    ``tool_config.yaml`` names, built from the judged definition in ``export`` into the gate's own
    conda dir (``agent_conda_dir()/gate``: never listed, reused while the definition is unchanged);
    tests of a package naming none run as before. The groups' outcomes are merged into one. With
    ``watched`` (the default runner's return recorder), each group watches only its own
    environment's tools: another environment's may not even import there."""
    groups: dict[str | None, list[str]] = {}
    for path in test_paths:
        env = _package_env(export / path.split("/", 1)[0])
        groups.setdefault(env, []).append(path)
    if list(groups) == [None]:
        return await test_runner(export, test_paths)

    def own(env_id):
        if watched is None:
            return {}
        return {"returns_tools": {k: v for k, v in watched.items()
                                  if _package_env(export / v[0].split(".", 1)[0]) == env_id}}
    merged: dict = {"results": [], "findings": [], "deterministic": True, "determinism_detail": ""}
    for env_id, paths in groups.items():
        if env_id is None:
            outcome = await test_runner(export, paths, **own(None))
        else:
            environment, error = await _gate_environment(export, env_id)
            if environment is None:
                outcome = {"results": [CheckResult(
                    name=f"layer_b.env.{env_id}", layer="B", passed=False,
                    detail=f"envs/{env_id} did not build, so its package's tests could not run: {error}",
                )], "deterministic": True}
            else:
                outcome = await test_runner(export, paths, environment=environment, **own(env_id))
        merged["results"] += outcome.get("results", [])
        merged["findings"] += outcome.get("findings", [])
        if not outcome.get("deterministic", True):
            merged["deterministic"] = False
            detail = f"[{f'env {env_id}' if env_id else 'analysis env'}] {outcome.get('determinism_detail', '')}"
            merged["determinism_detail"] = "; ".join(filter(None, (merged["determinism_detail"], detail)))
    return merged


async def _gate_environment(export: Path, env_id: str) -> tuple[Any | None, str]:
    """The environment ``envs/<env_id>`` of the judged files, built for the gate; or why not."""
    from auto_research.env import agent_conda_dir, analysis_environment, conda_executable, declared_externals
    from auto_research.sandbox.env_build import StoppableRunner, build, read_definition
    from auto_research.sandbox.jail import jail_enabled

    import time

    from auto_research.sandbox.env_build import _remove_tree

    human = analysis_environment()
    runner = StoppableRunner()
    gate_root = agent_conda_dir() / "gate"

    def _build():
        import fcntl

        definition = read_definition(export / "envs" / env_id)
        conda_dir = gate_root / definition.digest()[:16]
        gate_root.mkdir(parents=True, exist_ok=True)
        # One folder per definition digest: concurrent gates (a benchmark's trials) never move one
        # another's build while its tests run; one unused for a day is removed, under one lock so
        # two gates never prune and rebuild the same folder at once.
        with open(gate_root / ".prune.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            for old in gate_root.iterdir():
                try:
                    if old.is_dir() and old != conda_dir and time.time() - old.stat().st_mtime > 86400:
                        _remove_tree(old)
                except OSError:
                    pass
            conda_dir.mkdir(parents=True, exist_ok=True)
            os.utime(conda_dir)
        return build(definition, conda_dir=conda_dir,
                     conda_exe=conda_executable(), base_envs={human.name: human} if human else {},
                     externals=declared_externals(), jailed=jail_enabled(), runner=runner), conda_dir
    def _reachable(built):
        # A derived environment with no activation of its own sources its base's, the human's init
        # script, which may lie where the jail binds nothing (this repository): a copy inside this
        # build's own folder (pruned with it), bound with it, written whole before it is used.
        import dataclasses
        import uuid

        environment, conda_dir = built
        if environment.activate is None or environment.activate.resolve().is_relative_to(conda_dir.resolve()):
            return environment
        data = environment.activate.read_bytes()
        copy = conda_dir / "activation-base" / hashlib.sha256(data).hexdigest()[:16] / environment.activate.name
        if not copy.is_file() or copy.read_bytes() != data:
            copy.parent.mkdir(parents=True, exist_ok=True)
            temp = copy.with_name(f".{copy.name}.{uuid.uuid4().hex}")
            temp.write_bytes(data)
            os.replace(temp, copy)
        return dataclasses.replace(environment, activate=copy)
    try:
        return _reachable(await asyncio.to_thread(_build)), ""
    except asyncio.CancelledError:
        runner.stop()
        raise
    except Exception as exc:  # noqa: BLE001 — a build failure is a failed Layer B, said to the coder
        return None, f"{type(exc).__name__}: {str(exc)[-800:]}"


def _failure_reason(layer_a: list[CheckResult], layer_b: list[CheckResult]) -> str:
    failed = [c for c in (*layer_a, *layer_b) if not c.passed]
    if not failed:
        return "no Layer-B tests ran (untested tool is not mergeable)"
    return "; ".join(f"{c.name}: {c.detail}" for c in failed[:6])
