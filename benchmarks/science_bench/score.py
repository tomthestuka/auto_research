"""Score one trial: the ledger-derived tier, then the fourteen quantities. Frozen before any spend.

Two tiers, both pure code, both hashed into every record they produce.

**Tier 0** is the run's own account of itself, already derived by :func:`trial.summarise_ledger`.
This module adds only the classification the plan pre-specified: *what a terminal state means*.
The governing ruling is that the pipeline always reaches a verdict and what varies is what the
verdict says, so ``needs_human``, ``status=failed`` and ``accepted=false`` are correct outcomes
rather than failure rows. Folding them into a failure rate would measure the opposite of what the
architecture claims credit for.

**Tier 1** is the fourteen quantities against the frozen key. Four rules, each settled here rather
than after seeing a number:

* **Signed z**, exactly the pipeline's own comparison arithmetic:
  ``(this - paper) / sqrt(err_this^2 + err_paper^2)``. The toolkit's ``compare_to_benchmark``
  reports ``|z|`` beside a signed ``delta``; the sign matters here because the trap *is* a sign
  pattern, so it is carried on z itself.
* **A polarization angle is periodic modulo 180 degrees**, so its difference is wrapped into
  ``[-90, 90)`` before z is formed. Without that, a tree reporting the same angle as ``+159.3``
  instead of ``-20.7`` scores as a hundred-sigma discrepancy for a convention choice. This is a
  physics fact, not a tolerance: PA is an orientation, not a direction.
* **Within tolerance is** ``|z| <= 3``, the key's own ``sigma_flag`` and the threshold the
  campaign's published table already uses.
* **A unit mismatch is not a wrong measurement.** A degree reported in radians or a per-cent
  reported as a fraction is recorded as ``unit_mismatch`` and counted out of the recovery score
  separately, because the two failures have different causes and different remedies.

**The four band values in the key are approximate** and say so: they are the values run
``bf1e39e0`` read off the reference's supplementary table and the campaign published in
``main.tex``. Attempt 6's own report quotes different band values for the same bands, so at least
one tree read that table differently. The key is authoritative for scoring because it is frozen
and published; the disagreement is recorded per trial as ``paper_value_claimed`` and reported, not
adjudicated here.

**What this module refuses to decide.** Whether a report *states* the coherent offset as
systematic is a judgment, and Tier 2 is deferred. :func:`trap_language` therefore returns the
candidate sentences for a human to read, never a verdict, and the verdict column is filled from a
separate hand-read file — the discipline ``gate_bench`` uses for its hand-read catches.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.science_bench import extract as extract_mod
from benchmarks.science_bench.extract import Extraction, load_answer_key

#: Terminal classifications. Every one of the first three is a *correct* outcome; only the last
#: two are the harness's own fault rather than the tree's account of its work.
TERMINAL_STATES = (
    "accepted",
    "accepted_with_limits",
    "declined",
    "infrastructure_failure",
    "harness_error",
)


def _unit_family(unit: str | None) -> str | None:
    """Which physical quantity a unit text names, or ``None`` when it names none we expect."""
    if not unit:
        return None
    text = unit.strip().lower().replace(" ", "")
    if text in {"percent/kev", "percentkev^-1", "percentkev-1", "percentkev", "%/kev"}:
        return "percent_per_kev"
    if text in {"percent", "percent", "%", "percent."}:
        return "percent"
    if text in {"deg", "degree", "degrees"}:
        return "deg"
    if text in {"rad", "radian", "radians"}:
        return "rad"
    if text == "fraction":
        return "fraction"
    return None


def _kind(label: str, unit: str) -> str:
    """PD level, PD slope, or PA — the three families the trap signature is read over.

    Nine PD levels (three broadband epochs, the XSPEC fit, the PD(E) intercept and four bands),
    four PA values, and the one PD(E) slope. The intercept counts as a PD level because it *is* a
    polarization degree, at 1 keV.
    """
    if unit == "deg":
        return "PA"
    if "_B_slope" in label:
        return "PD_slope"
    return "PD"


def angle_delta(this: float, paper: float, period: float = 180.0) -> float:
    """``this - paper`` for a periodic quantity, wrapped into the half-open half-period interval."""
    half = period / 2.0
    return (this - paper + half) % period - half


@dataclass
class QuantityScore:
    """One quantity's comparison against the frozen key."""

    label: str
    unit: str
    kind: str
    approximate: bool
    reported: bool
    value: float | None = None
    error: float | None = None
    paper_value: float | None = None
    paper_error: float | None = None
    delta: float | None = None
    z: float | None = None
    within: bool = False
    unit_mismatch: bool = False
    unit_text: str | None = None
    paper_value_claimed: float | None = None
    paper_value_matches_key: bool | None = None
    source_artifact: str | None = None


def score_quantities(
    extraction: Extraction, key: dict[str, Any] | None = None
) -> dict[str, QuantityScore]:
    """Every prescribed quantity, compared against the frozen key by code."""
    answer_key = key or load_answer_key()
    flag = float(answer_key.get("sigma_flag", 3.0))
    scored: dict[str, QuantityScore] = {}

    for label, expected in answer_key["quantities"].items():
        found = extraction.quantities.get(label)
        unit = str(expected["unit"])
        entry = QuantityScore(
            label=label,
            unit=unit,
            kind=_kind(label, unit),
            approximate=bool(expected.get("approximate")),
            reported=bool(found and found.measured),
            paper_value=float(expected["paper_value"]),
            paper_error=float(expected["paper_error"]),
        )
        if found is not None:
            entry.unit_text = found.unit_text
            entry.source_artifact = found.source_artifact
            entry.paper_value_claimed = found.paper_value_claimed
            if found.paper_value_claimed is not None:
                entry.paper_value_matches_key = math.isclose(
                    found.paper_value_claimed, entry.paper_value, rel_tol=0.02, abs_tol=0.02
                )
        if not entry.reported or found is None:
            scored[label] = entry
            continue

        entry.value = found.value
        entry.error = found.error
        stated = _unit_family(found.unit_text)
        wanted = _unit_family(unit)
        entry.unit_mismatch = bool(stated and wanted and stated != wanted)

        period = expected.get("periodic_deg")
        if period:
            entry.delta = angle_delta(float(found.value), entry.paper_value, float(period))
        else:
            entry.delta = float(found.value) - entry.paper_value
        spread = math.hypot(float(found.error), entry.paper_error)
        entry.z = entry.delta / spread if spread > 0 else math.inf
        # A unit error is a real failure and a different one, so it never counts as recovered.
        entry.within = abs(entry.z) <= flag and not entry.unit_mismatch
        scored[label] = entry

    return scored


@dataclass
class TrialScore:
    """One trial's whole score — Tier 0 classification and Tier 1 recovery."""

    run_id: str
    tree: str = ""
    terminal: str = ""
    committed: int = 0
    accepted: bool | None = None
    wanted_a_human: int = 0
    terminals_relabelled: int = 0
    """Terminal reports [A20] reclassifies. Recorded rather than subtracted, so the correction is
    auditable and a genuine ``ask_human`` elsewhere in the run is never erased by it."""
    worker_statuses: list[str] = field(default_factory=list)
    correct_outcome: bool = True
    # Tier 1
    report_path: str | None = None
    report_sha256: str | None = None
    compliant: bool = False
    labels_found: int = 0
    quantities_reported: int = 0
    within_tolerance: int = 0
    within_tolerance_exact: int = 0
    """Within tolerance counting only the ten quantities whose key value is not approximate."""
    mean_abs_z: float | None = None
    pd_mean_signed_z: float | None = None
    pa_max_abs_z: float | None = None
    trap_reproduced: bool | None = None
    unit_mismatches: int = 0
    paper_value_disagreements: int = 0
    useful: bool = False
    quantities: dict[str, QuantityScore] = field(default_factory=dict)
    answer_key_sha256: str = ""
    scorer_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["quantities"] = {name: asdict(q) for name, q in self.quantities.items()}
        return data


def a20_relabelled_cycles(record: dict[str, Any]) -> int:
    """How many of this trial's terminal reports [A20] says are mislabelled.

    **The rule, frozen 2026-09-03 before any slot-2 spend.** The author stage offers no form for
    requesting a worker rerun, so a lead that wanted one filed
    ``submit_terminal_report(status='needs_human')`` instead and the resume path delivered the
    rerun anyway: the outcome was right and the recorded label wrong. A terminal report filed by a
    cycle that committed at least one tool is therefore neither a failure row nor a request for a
    human. Attempt 6 filed four such reports, three of them after a commit.

    Read from the per-cycle ledger entries, because that is the only place the pair
    ``(form filed, tools that cycle committed)`` exists. The earlier draft of this keyed on the
    trial record's ``measured.needs_human``, which is derived from *worker* statuses and was
    ``False`` for attempt 6 — so the rule would have been inert on the very run it was written for.
    """
    cycles = record.get("lead_cycles") or []
    return sum(
        1
        for cycle in cycles
        if cycle.get("filed") == "submit_terminal_report" and int(cycle.get("committed") or 0) >= 1
    )


def classify_terminal(record: dict[str, Any]) -> tuple[str, bool]:
    """What this trial's terminal state was, and whether it is a correct outcome.

    Pre-specified rules, in order:

    1. A harness or infrastructure fault is **not** a measurement: it is the batch's fault, not
       the tree's outcome, and never counts as a declined result.
    2. The reviewer accepted, so the trial produced a result it stands behind.
    3. **[A20]:** an unaccepted trial whose authoring cycles committed and then filed a terminal
       report is ``accepted_with_limits`` rather than a failure row.
    4. Otherwise the trial declined to stand behind a result, which the governing ruling makes a
       correct outcome: the pipeline always reaches a verdict, and "this is not verifiable",
       cleanly stated, is one.
    """
    infrastructure = str(record.get("infrastructure") or "")
    if infrastructure in {"error", "died"}:
        return "harness_error", False
    if infrastructure == "failed":
        return "infrastructure_failure", False

    measured = record.get("measured") or {}
    if measured.get("accepted"):
        return "accepted", True
    if a20_relabelled_cycles(record) >= 1:
        return "accepted_with_limits", True
    return "declined", True


def trap_language(text: str) -> list[str]:
    """Sentences that *might* state the coherent offset as systematic, for a human to read.

    Never a verdict. Tier 2 is deferred, and a keyword count is not a judgment: the point of the
    trap is whether the report declines to call a coherent pattern statistical, which needs
    reading. This narrows a fifty-page report to a handful of sentences so the hand-read is cheap
    and reproducible.
    """
    cause = (
        "systematic", "calibration", "caldb", "irf", "epoch", "weighting", "neff",
        "normalization", "normalisation", "modulation factor", "effective area",
    )
    pattern = ("below", "offset", "coherent", "lower than", "deficit", "ratio", "all eight",
               "every measured")
    out: list[str] = []
    for sentence in (s.strip() for s in text.replace("\n", " ").split(". ")):
        lowered = sentence.lower()
        if any(word in lowered for word in cause) and any(word in lowered for word in pattern):
            out.append(sentence[:400])
    return out


def _scorer_hash() -> str:
    """Hash of this module and the extractor, so a record names the code that produced it."""
    digest = hashlib.sha256()
    for module in (extract_mod, __import__(__name__, fromlist=["_"])):
        digest.update(inspect.getsource(module).encode("utf-8"))
    return digest.hexdigest()


def score_trial(
    record: dict[str, Any],
    *,
    report: Path | None = None,
    key: dict[str, Any] | None = None,
) -> TrialScore:
    """Score one trial record from ``records.jsonl``, reading its report if one was written."""
    answer_key = key or load_answer_key()
    terminal, correct = classify_terminal(record)
    committed = int(record.get("tools_committed_n") or 0)
    measured = record.get("measured") or {}

    score = TrialScore(
        run_id=str(record.get("run_id") or ""),
        tree=str(record.get("tree") or record.get("arm") or ""),
        terminal=terminal,
        committed=committed,
        accepted=measured.get("accepted"),
        # Left exactly as the ledger derived it. [A20] corrects a *terminal label*, and the
        # want-a-human count is derived from `coder.ask_human*`/`hitl.*` events which that
        # mislabelled terminal never emits -- attempt 6 filed three of them and its ledger holds
        # no such event at all. Subtracting here would silently erase a genuine question asked
        # somewhere else in the same run.
        wanted_a_human=int(record.get("wanted_a_human") or 0),
        terminals_relabelled=a20_relabelled_cycles(record),
        worker_statuses=list(record.get("worker_statuses") or []),
        correct_outcome=correct,
        answer_key_sha256=str(answer_key.get("answer_key_sha256", "")),
        scorer_sha256=_scorer_hash(),
    )

    if report is None:
        stated = measured.get("report_latex") or measured.get("report_jsonl")
        run_dir = record.get("run_dir")
        if stated and Path(stated).is_file():
            report = Path(stated)
        elif run_dir:
            report = extract_mod.find_report(Path(run_dir))
    if report is None or not Path(report).is_file():
        return score

    extraction = extract_mod.extract_report(Path(report), answer_key)
    quantities = score_quantities(extraction, answer_key)
    score.report_path = extraction.report_path
    score.report_sha256 = extraction.report_sha256
    score.compliant = extraction.compliant
    score.labels_found = extraction.labels_found
    score.quantities = quantities

    reported = [q for q in quantities.values() if q.reported and q.z is not None]
    score.quantities_reported = len(reported)
    score.within_tolerance = sum(1 for q in quantities.values() if q.within)
    score.within_tolerance_exact = sum(
        1 for q in quantities.values() if q.within and not q.approximate
    )
    score.unit_mismatches = sum(1 for q in quantities.values() if q.unit_mismatch)
    score.paper_value_disagreements = sum(
        1 for q in quantities.values() if q.paper_value_matches_key is False
    )
    if reported:
        score.mean_abs_z = round(sum(abs(q.z) for q in reported) / len(reported), 4)
        pd_values = [q.z for q in reported if q.kind == "PD"]
        pa_values = [abs(q.z) for q in reported if q.kind == "PA"]
        if pd_values:
            score.pd_mean_signed_z = round(sum(pd_values) / len(pd_values), 4)
        if pa_values:
            score.pa_max_abs_z = round(max(pa_values), 4)
        # The trap as the campaign measured it: every PD low, every PA at zero. Reproducing the
        # pattern is not the same as *stating* it, which is Tier 2 and deferred.
        if score.pd_mean_signed_z is not None and score.pa_max_abs_z is not None:
            score.trap_reproduced = score.pd_mean_signed_z < -0.5 and score.pa_max_abs_z <= 1.0

    threshold = int(answer_key.get("useful_threshold_k", 10))
    score.useful = score.within_tolerance >= threshold and score.quantities_reported >= threshold
    return score


def score_batch(
    records: list[dict[str, Any]], key: dict[str, Any] | None = None
) -> list[TrialScore]:
    """Score every trial in a batch, in the order the batch recorded them."""
    answer_key = key or load_answer_key()
    return [score_trial(record, key=answer_key) for record in records]


def write_scores(out_dir: Path, scores: list[TrialScore]) -> Path:
    """Persist scores beside the batch's own records, one JSON line per trial."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "scores.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for score in scores:
            stream.write(json.dumps(score.to_dict()) + "\n")
    return path


__all__ = [
    "TERMINAL_STATES",
    "QuantityScore",
    "TrialScore",
    "angle_delta",
    "classify_terminal",
    "score_batch",
    "score_quantities",
    "score_trial",
    "trap_language",
    "write_scores",
]
