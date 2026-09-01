"""Science-half provider sweep: does the tree reproduce a published measurement (paper item 7).

``author_bench`` measures the coding half of the pipeline; this measures the science half, and the
two are presented beside each other. One trial is a whole research run against a question with a
published answer, so what a trial varies is the reviewer, the analyst lead and the analyst worker
pair, and what it holds fixed is the author and the tool reviewer the coding sweep already measured.

Development code: nothing in the pipeline imports it and it never runs during a research run.
"""

from benchmarks.science_bench.arms import (
    BLOCK0,
    BLOCK1,
    BLOCK2_UNASSIGNED,
    CODER,
    COLEAD,
    LEADS,
    REVIEWERS,
    WORKER_PAIRS,
    Arm,
    RunSlot,
    Seat,
    WorkerPair,
    admissible,
    arm,
    campaign,
    confirm_block,
    run_id,
    schedule,
    vendor_ceilings,
    vendor_load,
)

__all__ = [
    "BLOCK0",
    "BLOCK1",
    "BLOCK2_UNASSIGNED",
    "CODER",
    "COLEAD",
    "LEADS",
    "REVIEWERS",
    "WORKER_PAIRS",
    "Arm",
    "RunSlot",
    "Seat",
    "WorkerPair",
    "admissible",
    "arm",
    "campaign",
    "confirm_block",
    "run_id",
    "schedule",
    "vendor_ceilings",
    "vendor_load",
]
