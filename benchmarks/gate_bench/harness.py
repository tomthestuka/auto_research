"""Per-mutant execution: prepare a throwaway worktree, plant the changeset, run the
**production** gate over it, and (opt-in) the live cross-provider review.

Nothing here reimplements a defence. The gate is ``auto_research.sandbox.gate.run_gate`` with
its real default test runner (the analysis interpreter, the tests run twice), the review is
``auto_research.sandbox.review.make_review_hook``, and the commit sweep calls the runtime's own
``approve_edit`` — inside a throwaway clone, never the live checkout. The benchmark measures
the defences the runtime actually ships; a copy of them would measure nothing.

Sequencing mirrors production: the review runs only on a gate pass, because that is when the
runtime asks for one (``propose_and_pr`` loops the gate first).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auto_research.sandbox.gate import collect_targets, run_gate
from auto_research.schemas import ToolSpec, ValidationDataset

from benchmarks.gate_bench.briefs import brief_for
from benchmarks.gate_bench.mutants import Mutant

REPO_ROOT = Path(__file__).resolve().parents[2]

# The corpus is pinned to its own toolkit branch rather than the branch the runtime commits
# to, so a live authoring run cannot move the code a mutant's anchors are written against.
DEFAULT_TOOLKIT_REF = "gate-bench-base"


@dataclass
class RunConfig:
    """Everything a benchmark run needs that is not the corpus itself."""

    toolkit_root: Path
    toolkit_ref: str = DEFAULT_TOOLKIT_REF
    review: bool = False
    keep_worktrees: bool = False
    review_hook: Any | None = None  # injectable; built from config on first use when None
    stamps: dict[str, str] = field(default_factory=dict)
    # Layer ablations (paper item 2). Both are production parameters of ``run_gate``, so a
    # disabled defence needs no edit to the pipeline: ``gate_config`` replaces the scan lists and
    # ``test_runner`` replaces the Layer-B/determinism run.
    gate_config: dict | None = None
    test_runner: Any | None = None
    ablation: str = "none"
    reviewer: str = "gpt_5_6_sol"  # roster member driving the review leg


# ----------------------------------------------------------------------------
# Ablations — one defence disabled at a time, over the identical corpus
# ----------------------------------------------------------------------------

# Emptying the four scan lists is a supported configuration (an emptied section has meant
# "nothing to flag" since 2026-07-16). This does NOT disable the whole mechanical scan: the scan
# has a config-driven tier (vocabulary and code patterns) and a **hardcoded** tier that no
# configuration can switch off. Measured 2026-08-17: with every list emptied,
# ``no_fabrication.binary`` and ``no_fabrication.placeholder_body`` still fire — both are
# structural facts about the code rather than words in it. "No mechanical scan" in the ablation
# therefore means "no vocabulary or code-pattern lists", which the table must say.
SCAN_OFF_CONFIG: dict = {
    "hard_fail_vocabulary": [],
    "hard_fail_code_patterns": [],
    "finding_vocabulary": [],
    "finding_code_patterns": [],
    "allowlist_obsid_tokens": [],
}


async def _tests_off_runner(worktree: Path, test_paths: list[str]) -> dict:
    """Stand in for a tool-local test layer that verifies nothing.

    Returning no results is not the right model: ``run_gate`` requires a non-empty Layer B, so
    an empty result set fails every changeset ("an untested tool is not mergeable", §9.4) rather
    than ablating the layer. A layer that runs and always passes is what "tests removed" means.

    Determinism goes with it, unavoidably: the gate derives it from this same double test run, so
    it cannot be ablated separately — removing the tool-local tests removes the determinism
    comparison too.
    """
    from auto_research.schemas import CheckResult

    return {
        "results": [CheckResult(
            name="layer_b.pytest", layer="B", passed=True,
            detail="ablation: tool-local tests disabled (no verification performed)",
        )],
        "deterministic": True,
        "determinism_detail": "ablation: not compared (derived from the disabled test run)",
    }


ABLATIONS = {
    "none": {},
    "scan": {"gate_config": SCAN_OFF_CONFIG},
    "layer_b": {"test_runner": _tests_off_runner},
}


# ----------------------------------------------------------------------------
# Reviewer roster — the model-swap leg
# ----------------------------------------------------------------------------

REVIEWERS_CONFIG = Path(__file__).resolve().parent / "reviewers.yaml"

# There is no author in this corpus: the defects are planted by hand, so no provider is
# correlated with any other and the §9.3 author↔reviewer rule has nothing to bind. Declaring the
# author provider as absent is the truthful way to say that; naming a provider we do not use
# would misstate the trust boundary in order to satisfy a guard that does not apply.
NO_AUTHOR = "(no author: defects are planted by hand)"


def reviewer_handles() -> dict:
    """`{member name: ModelHandle}` for the swap roster, harmonised per `reviewers.yaml`."""
    import yaml

    from auto_research.models.registry import ModelRegistry
    from auto_research.models.types import ModelsConfig

    data = yaml.safe_load(REVIEWERS_CONFIG.read_text(encoding="utf-8"))
    # ``ModelRegistry`` directly, not ``from_file``: the latter enforces the production
    # required-role set, which a reviewer-only roster has no reason to carry.
    group = ModelRegistry(ModelsConfig.model_validate(data)).resolve("bench_reviewer")
    return {handle.config.name: handle for handle in group.handles}


def review_hook_for(reviewer: str, *, monitor=None):
    """Build the production review hook driven by one named roster member."""
    from auto_research.agents import AnalystColeadAgent
    from auto_research.sandbox.review import make_review_hook

    handles = reviewer_handles()
    if reviewer not in handles:
        raise SystemExit(
            f"unknown reviewer {reviewer!r}; roster: {', '.join(sorted(handles))}"
        )
    agent = AnalystColeadAgent(model=handles[reviewer], monitor=monitor)
    return make_review_hook(agent, author_provider=NO_AUTHOR)


# ----------------------------------------------------------------------------
# Provenance stamps — the matrix is only reproducible against the defences it measured
# ----------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def resolve_toolkit_ref(toolkit_root: Path, ref: str) -> str:
    """Resolve ``ref`` in the toolkit to a concrete SHA, failing with the fix if it is absent."""
    try:
        return _git(Path(toolkit_root), "rev-parse", "--verify", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"toolkit ref {ref!r} does not resolve in {toolkit_root}.\n"
            f"The corpus is pinned to a branch of its own so a live authoring run cannot move\n"
            f"the code its anchors are written against. Create it once (a human runs all git):\n"
            f"    git -C {toolkit_root} branch {ref} <sha>\n"
            f"or pass --toolkit-ref <branch|sha> to measure against a different base."
        ) from None


def collect_stamps(
    toolkit_root: Path, toolkit_ref: str, *, resolve_toolkit: bool = True
) -> dict[str, str]:
    """Orchestration commit, toolkit commit, and the hash of the scan configuration.

    ``resolve_toolkit=False`` skips the toolkit lookup for a synthetic-only selection, which
    needs no clone and must not require the pinned branch to exist yet.
    """
    scan_config = REPO_ROOT / "config" / "code_gate.yaml"
    return {
        "auto_research_sha": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "auto_research_dirty": str(bool(_git(REPO_ROOT, "status", "--porcelain"))),
        "toolkit_ref": toolkit_ref if resolve_toolkit else "(no real mutant selected)",
        "toolkit_sha": (
            resolve_toolkit_ref(toolkit_root, toolkit_ref) if resolve_toolkit
            else "(no real mutant selected)"
        ),
        "code_gate_sha256": hashlib.sha256(scan_config.read_bytes()).hexdigest(),
    }


# ----------------------------------------------------------------------------
# Worktree preparation
# ----------------------------------------------------------------------------


def prepare_worktree(mutant: Mutant, dest: Path, cfg: RunConfig) -> Path:
    """Prepare the base the mutant plants into: a fresh temp repo, or a toolkit clone."""
    from benchmarks.gate_bench.basepkg import make_synthetic_repo

    if mutant.base == "synthetic":
        return make_synthetic_repo(dest)
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(cfg.toolkit_root), str(dest)],
        check=True, capture_output=True, text=True,
    )
    _git(dest, "checkout", "--quiet", "-b", "gate-bench", cfg.stamps["toolkit_sha"])
    return dest


# The synthetic package reads a fixture product whose rows carry marked identifiers (§9.5), so
# its brief must name that fixture and set ``synthetic=True``. Naming a real observation here
# instead told the co-lead the package was validated against data it never opens, which is a
# defect in the brief the co-lead then correctly rejected — on clean controls as much as on
# planted defects, making every synthetic review verdict unattributable.
SYNTHETIC_VALIDATION = ValidationDataset(
    mission="IXPE", obsid="synth260817-4d1f7a", synthetic=True
)
REAL_VALIDATION = ValidationDataset(mission="IXPE", obsid="01002901")


def tool_spec_for(mutant: Mutant) -> ToolSpec:
    """The brief the gate is handed — the same shape the lead writes for a real authoring."""
    # The id is opaque on purpose. It used to be `gate-bench-{mutant.id}`, which put the defect's
    # own slug into the reviewer's prompt (`gate-bench-led_self_referential_differential`) and
    # marked every control with a `ctl_` prefix. `ToolSpec.id` is field 1 of the dump the co-lead
    # reads (`roles.py`), so that was a second answer leak beside the science_context one. A hash
    # keeps entries distinct — as two real changesets would be — while carrying no class.
    opaque = hashlib.sha256(mutant.id.encode("utf-8")).hexdigest()[:10]
    return ToolSpec(
        id=f"{mutant.tool_name}-{opaque}",
        # Keyed on the TOOL, so a defect and its matched control are reviewed against byte-identical
        # briefs. See `briefs.py` for why this is structural rather than a wording fix.
        science_context=brief_for(mutant).science_context,
        expected_behavior=mutant.spec_behavior or (
            "The tool computes its stated quantity from the named validation input and "
            "commits a test file beside it."
        ),
        proposed_tool_name=mutant.tool_name,
        target_domain=mutant.target_domain,
        disposition="extend_existing" if mutant.base == "real" else "new_package",
        validation_data=[
            REAL_VALIDATION if mutant.base == "real" else SYNTHETIC_VALIDATION
        ],
    )


# ----------------------------------------------------------------------------
# The G3 probe — what a commit would sweep in that the gate never scanned
# ----------------------------------------------------------------------------


def commit_sweep(worktree: Path, scanned: set[str], message: str) -> dict:
    """Commit through the runtime's own ``approve_edit`` and report what it staged that the
    gate had not enumerated (CLAUDE.md §9.9 / handoff [G3]).

    The gate enumerates its targets once, before Layer B runs; ``approve_edit`` re-enumerates
    at commit time, so any file a test wrote in between is staged without ever having been
    scanned. Runs inside the throwaway clone only.
    """
    from auto_research.sandbox.merge_flow import approve_edit

    sha = approve_edit(worktree, message=message)
    committed = set(_git(worktree, "show", "--name-only", "--pretty=format:", sha).split())
    unscanned = sorted(committed - scanned)
    return {
        "ran": True,
        "commit_sha": sha,
        "n_committed": len(committed),
        "unscanned_paths": unscanned,
        "caught": bool(unscanned),
    }


# ----------------------------------------------------------------------------
# One mutant
# ----------------------------------------------------------------------------


def _check_dicts(checks) -> list[dict]:
    return [{"name": c.name, "passed": c.passed, "detail": c.detail[:400]} for c in checks]


def _cleanup_markers() -> None:
    """Remove the temp markers a deliberately nondeterministic test leaves behind."""
    for path in Path(tempfile.gettempdir()).glob("gate_bench_seen_*"):
        try:
            path.unlink()
        except OSError:
            pass


async def run_mutant(mutant: Mutant, cfg: RunConfig, *, meter: Any | None = None) -> dict:
    """Build, gate, optionally review and sweep one mutant; return its JSONL record."""
    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix=f"gate_bench_{mutant.id}_"))
    spend_before = getattr(meter, "total_usd", 0.0)
    record: dict = {
        "id": mutant.id,
        "family": mutant.family,
        "base": mutant.base,
        "description": mutant.description,
        "expected": mutant.expected,
        "known_hole": mutant.known_hole,
        "notes": mutant.notes,
        "pairs_with": mutant.pairs_with,
        "ablation": cfg.ablation,
        **cfg.stamps,
    }
    try:
        worktree = prepare_worktree(mutant, workdir / "repo", cfg)
        mutant.build(worktree)
        spec = tool_spec_for(mutant)

        scanned = {str(p.relative_to(worktree)) for p in collect_targets(worktree)}
        record["n_changed_files"] = len(scanned)

        gate = await run_gate(
            worktree, spec, test_runner=cfg.test_runner, config=cfg.gate_config
        )
        record["gate"] = {
            "passed": gate.passed,
            "reason": gate.reason[:600],
            "report_hash": gate.report_hash,
            "layer_a": _check_dicts(gate.layer_a),
            "layer_b": _check_dicts(gate.layer_b),
            "findings": _check_dicts(gate.findings),
        }

        record["review"] = None
        if gate.passed and cfg.review:
            hook = cfg.review_hook
            if hook is None:
                hook = review_hook_for(cfg.reviewer)
                cfg.review_hook = hook
            verdict = await hook(worktree, spec, gate)
            record["review"] = {
                "ran": True,
                "reviewer": cfg.reviewer,
                "approved": verdict.approved,
                "checks_adequate": verdict.checks_adequate,
                # Verbatim, not truncated: these records are the audit trail for the hand-read
                # attribution, and a judgement cannot rest on a head of the text.
                "issues": list(verdict.issues),
                "rationale": verdict.rationale,
            }

        record["commit_sweep"] = None
        if gate.passed and mutant.expected == "commit_sweep":
            record["commit_sweep"] = commit_sweep(worktree, scanned, f"gate-bench {mutant.id}")

        if cfg.keep_worktrees:
            record["worktree"] = str(worktree)
    except Exception as exc:  # noqa: BLE001 — one broken mutant must not end the run
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if mutant.cleanup_markers:
            _cleanup_markers()
        if not cfg.keep_worktrees:
            shutil.rmtree(workdir, ignore_errors=True)

    record["duration_s"] = round(time.time() - started, 2)
    record["cost_usd"] = round(getattr(meter, "total_usd", 0.0) - spend_before, 4)
    return record
