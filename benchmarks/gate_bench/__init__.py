"""Seeded-defect catch-rate benchmark for the Phase-2 mergeability gate (CLAUDE.md §9.4).

Plants a corpus of defects as complete, uncommitted changesets in throwaway clones and runs
the **production** gate (and, opt-in, the live cross-provider review) over each one, so the
paper's flagship claim — the stack catches fabrication and defective tool code — is measured
rather than asserted.
"""

from benchmarks.gate_bench.mutants import Mutant, corpus
from benchmarks.gate_bench.score import Record, build_matrix, score_record

__all__ = ["Mutant", "Record", "build_matrix", "corpus", "score_record"]
