"""The brief each corpus entry is reviewed against — one per TOOL, never per mutant.

**Why this module exists.** Until 2026-08-27 the brief was synthesized per mutant as
``"Seeded-defect benchmark for the mergeability gate: " + mutant.description``, which handed the
reviewer the planted defect in its own prompt ("The independent recompute compares the tool
against the same code path the tool itself calls…") and labelled every control as one ("Matched
control for X: the same edit made honestly"). Detection measured that way is instruction-following,
not review, and the matched-control design collapses: a reviewer told a changeset is honest and
approving it has discriminated nothing. Found while investigating why three reviewers approved
``led_self_referential_differential`` — one of them quoting "the benchmark's warning about
self-referential recomputes" before declaring the code clean.

**The fix is structural rather than editorial.** A brief is keyed on ``(base, tool_name)``, so a
defect and its control resolve to the *same* string by construction and no future edit can make
them differ. That property is asserted by
``test_gate_bench.py::test_a_pair_is_reviewed_against_an_identical_brief``.

**Provenance.** Two of the five tools are real, and their briefs are the analyst lead's own words,
quoted verbatim from the run ledger that produced them — so those entries are not written for the
benchmark at all. The three synthetic tools have no run behind them and their briefs are written
here, in the same register: the capability a census needs and why the toolkit cannot supply it,
never a flaw to look for.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmarks.gate_bench.mutants import Mutant


@dataclass(frozen=True)
class Brief:
    """One tool's science context, and where the text came from."""

    science_context: str
    """Exactly what a reviewer reads. Describes the capability wanted and why it is wanted."""

    provenance: str
    """Where this text came from — a run id for a salvaged brief, or that it was written here.

    Recorded beside the text and never inside it: a brief carrying its own provenance would put
    "benchmark" back into the reviewer's prompt, which is the defect this module exists to remove.
    """


#: Verbatim from run ``b-run-q21-0803c``, the analyst lead's ``decide_authoring`` brief for
#: ``ixpe_qu_lag_dcf``. Chosen over the same tool's other recovered brief (run
#: ``8c75990e862ca45993bea6076a92115c``, on pinning the significance machinery explicitly) because
#: it states the capability need rather than a parameter-plumbing gap, which suits a corpus whose
#: mutants touch the statistics themselves.
_QU_LAG_DCF = Brief(
    science_context=(
        "The population lag census must distinguish three different things per sub-observation: "
        "(i) whether a DCF could be computed at all, (ii) whether the observation met the "
        "adaptive-slicing feasibility gate (noise-limited), and (iii) whether it qualifies for "
        "the population statistics. Currently qu_lag_dcf conflates these. It computes "
        "noise_limited internally (n_rows < provenance['n_min'], qu_lag_dcf.py line 525) and uses "
        "it to filter the population correlations, but never writes it to ccf_master.csv -- so "
        "neither the plotter nor any report can see which observations were noise-limited (the "
        "plotter's own docstring admits it cannot reconstruct the flag). Separately, a "
        "sub-observation with fewer than n_min_slices rows is given status='insufficient_slices' "
        "and NO DCF is computed at all (line 532-537: the row is appended with all-NaN statistics "
        "and the loop continues), whereas the analysis prescription calls for computing such a DCF "
        "for completeness while excluding it from population tests. In the executed run this "
        "affected 7 of 47 sub-observations (long faint pointings including SS 433 "
        "02006799/04001401/04002899 at 1 slice each, GRS 1915+105 04003501 at 1, IGR J17091-3624 "
        "04250201 at 2, 4U 1700-377 04003201 at 4, LMC X-3 03004901 at 7), all of which returned "
        "no lag information whatsoever."
    ),
    provenance="verbatim from run b-run-q21-0803c (analyst lead decide_authoring)",
)

#: ``load_data`` is the shared IXPE loader the lag tool depends on. No ledger brief was recovered
#: naming it as the authored tool (its mutants carry no ``spec_behavior`` either), so this text is
#: written here from what the corpus's changesets actually touch — the epoch-selection path.
_LOAD_DATA = Brief(
    science_context=(
        "Every IXPE spectral or polarimetric product in the census is loaded through one shared "
        "entry point, which resolves the instrument response for the observation's epoch before "
        "handing data to the fit. The epoch decision is not something a per-call argument should "
        "carry: a mis-resolved response shifts a polarization degree by more than its statistical "
        "error, and an analysis that got it wrong is indistinguishable from one that got it right "
        "at the level of the fit statistic. The loader therefore has to resolve the epoch from the "
        "observation itself and be pinned by tests that would fail if the mapping inverted."
    ),
    provenance="written for this corpus; no ledger brief names load_data as the authored tool",
)

#: The three synthetic tools exercise the gate against a package with no science stack, so no run
#: produced them. These briefs are written here in the register of the salvaged ones above: what a
#: census needs, and why doing it by hand is not acceptable.
_BAND_PA_SUMMARY = Brief(
    science_context=(
        "A two-band polarimetric census needs one summary per observation set, computed the same "
        "way every time: the hardness ratio of the two count bands with its Poisson-propagated "
        "uncertainty, and the position-angle statistics across the set. Position angle is the part "
        "that goes wrong by hand -- it is defined modulo 180 degrees, so a naive mean or spread "
        "across a set straddling the wrap gives an answer that is not merely imprecise but "
        "qualitatively wrong, and the error is invisible in the output. Analysts have been "
        "computing these with ad hoc arithmetic per observation set, which cannot be logged as a "
        "tracked data product and cannot be reproduced by a reader."
    ),
    provenance="written for this corpus (synthetic base, no originating run)",
)

_RUN_SUMMARY = Brief(
    science_context=(
        "Once per-observation products exist, the census needs a run-level summary over one "
        "observation set rather than a per-product read: the band ratio, the error-weighted mean "
        "position angle and its spread, a chi-square against that mean, and a verdict per product. "
        "Two properties decide whether such a summary can be quoted. It must state what it did not "
        "use -- a product the producer did not certify complete, or whose declared row count does "
        "not match what is on disk, is not summarizable and saying so is the result. And a product "
        "with no degrees of freedom, or excess scatter consistent with zero, has to be reported as "
        "indeterminate or unresolved rather than as a number, because a bound presented as a "
        "measurement is the failure mode this table exists to prevent."
    ),
    provenance="written for this corpus (synthetic base, no originating run)",
)

_RUN_PRODUCT_AUDIT = Brief(
    science_context=(
        "Before a run's products are used in a population result, someone has to establish that "
        "each one is what it claims to be: certified by its producer, unmodified since, and "
        "readable. That check is currently done by eye across dozens of files, which does not "
        "scale and leaves no record a reviewer can audit. The census needs it as a tracked product "
        "-- component statistics, a joint verdict, a content hash per input, and the tool-call "
        "chain that produced them -- so that a downstream number can be traced to the exact bytes "
        "it was computed from. One unreadable input has to cost that input and not the run, or a "
        "single corrupt file silently removes an entire observation set from the census."
    ),
    provenance="written for this corpus (synthetic base, no originating run)",
)


#: Keyed on ``(base, tool_name)`` — the property that makes a leak impossible rather than merely
#: absent, since a defect and its matched control share both.
BRIEFS: dict[tuple[str, str], Brief] = {
    ("real", "ixpe_qu_lag_dcf"): _QU_LAG_DCF,
    ("real", "load_data"): _LOAD_DATA,
    ("synthetic", "band_pa_summary"): _BAND_PA_SUMMARY,
    ("synthetic", "run_summary"): _RUN_SUMMARY,
    ("synthetic", "run_product_audit"): _RUN_PRODUCT_AUDIT,
}


def brief_for(mutant: Mutant) -> Brief:
    """The brief ``mutant``'s changeset is reviewed against.

    Raises rather than falling back: a mutant whose tool has no brief would otherwise be reviewed
    against a generic string, and a corpus entry nobody wrote a brief for is a corpus bug to fix,
    not a default to paper over.
    """
    key = (mutant.base, mutant.tool_name)
    brief = BRIEFS.get(key)
    if brief is None:
        raise KeyError(
            f"no brief for {key}; add one to benchmarks/gate_bench/briefs.py. A brief is keyed on "
            "the TOOL so a defect and its control cannot differ — do not key it on the mutant."
        )
    return brief
