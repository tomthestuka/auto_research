"""The frozen question every trial is posed, and the fourteen quantities it must report.

**Recovered, not written.** The question is the one the pipeline was actually given on 2026-07-10,
lifted verbatim out of the run cell that posed it, so a trial here is the same task that run
attempted and that run becomes a reference point rather than a different experiment. Writing a
question for the benchmark would measure a question tuned to the benchmark.

**Same task means the same starting state, not just the same words** -- and that took a live run to
get right. The first build gave each trial an archive-only copy of the observations, which is
*cleaner* than what the 2026-07-10 run saw and therefore a different, harder task: the toolkit's
loader at the pinned base finds only the background-rejected event lists that the group's own
preprocessing script leaves beside the archive files, so a pristine copy made it fail outright. The
clone now carries those, and carries no measurement of any kind, which is the state that run began
from.

**One addition, and it names the task rather than the answer.** A research report has no numeric
slots -- it is prose, findings, figures and references -- and each tree authors its own tool chain,
so the artifacts differ from trial to trial and nothing about their contents can be assumed. Left
alone, reading the fourteen quantities back out would be prose mining, with an extractor guessing
which energy band a sentence meant. So the question gains one step listing the fourteen quantities
and their units, and asking for each as a value with its error and the artifact it came from. The
published values are public and the tree is required to read them anyway -- that is what makes this
a reproduction -- so naming *which* quantities to report gives away nothing. The step is explicit
that a quantity this run did not produce must be reported as not measured, with the reason: an
honest gap is a correct answer, and it is the alternative to inventing one.

The artifact each quantity came from is what separates a number this run **measured** from a number
it **transcribed out of the paper**, which is a distinction no amount of prose reading recovers.

**Frozen and hashed before anything spends.** The bytes live in a JSON file beside this module and
:func:`request_sha256` is stamped into every batch, because an instrument revised in the light of
its own results absorbs the variance it exists to detect. If the question has to change, the hash
changes with it and the earlier trials are visibly a different measurement.

The **answer key is deliberately elsewhere** -- in the manuscript's own data directory, which is
not among the roots any actor is allowed to read. Not a secret (the paper is published, and the
tree is told to read it), but tamper hygiene: the values a trial is scored against are never on a
path the trial can reach.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REQUEST_PATH = Path(__file__).with_name("request_cygx1_krawczynski2022.json")


@lru_cache(maxsize=1)
def _frozen() -> dict[str, Any]:
    return json.loads(REQUEST_PATH.read_text(encoding="utf-8"))


def load_request() -> Any:
    """The question, context and constraints every trial is posed, as the pipeline's own type."""
    from auto_research.schemas import ResearchRequest

    return ResearchRequest(**_frozen()["request"])


def request_provenance() -> dict[str, Any]:
    """Where the question came from, what was added to it, and the run it is comparable to."""
    return dict(_frozen()["provenance"])


def quantities() -> tuple[dict[str, str], ...]:
    """The fourteen quantities, in the order the question lists them.

    The labels are the answer key's own, so extraction is a keyed lookup rather than a match
    between two lists that could drift apart.
    """
    return tuple(dict(entry) for entry in _frozen()["quantities"])


def request_sha256() -> str:
    """The hash a batch stamps, over the exact bytes a trial is posed."""
    payload = json.dumps(_frozen()["request"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "REQUEST_PATH",
    "load_request",
    "quantities",
    "request_provenance",
    "request_sha256",
]
