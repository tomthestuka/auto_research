"""Read the fourteen prescribed quantities out of a tree's own report. No model in the loop.

**Why this is code and not a model call.** The dispatched prescription names the fourteen
quantities and their units and tells the tree to state each one under a verbatim label "so the
report is machine-readable", identically in every cell. That instruction turns extraction from
prose mining into a keyed lookup, and a keyed lookup does not need judgment — which is the only
form of model-assisted scoring UK AISI's guidance endorses, and it is cheaper and re-runnable to
skip it entirely. Attempt 6 complied: its report carries a "Named-Quantity Report" section listing
all fourteen labels as ``\\textbf{Label} $= v \\pm e$ unit (artifact)``.

**What it does when a report does not comply.** Nothing silently. A label that never appears, or
appears with no number, is recorded ``not_reported`` with the window that was searched, and the
count of labels found is itself a Tier-0 response: a tree that ignored a verbatim-label
instruction is telling us something about itself. Values a human then transcribes by hand live in
a separate file, flagged, and never merge into the machine extraction — the corpus stays honest
about which numbers a machine read.

**Three things it deliberately records rather than resolves.**

* ``paper_value_claimed`` — the reference value the *tree* asserts, when it states one **inside
  the labelled quantity's own statement**. Trees do disagree here: the frozen key carries the four
  band values published in ``main.tex`` (2.8, 3.3, 4.4, 7.1 per cent) while attempt 6's report
  quotes 3.5, 3.5, 4.7, 5.8 for the same bands, so at least one tree read the reference's
  supplementary table differently. **Stated limitation, measured on that report:** it puts its
  claimed reference values in a comparison table whose rows are prose ("May PD 2--3 keV") rather
  than the prescribed keys, so this field is empty there and the disagreement above was found by
  hand. No general table reader is attempted, so the count is a lower bound and is reported as one.
* ``unit_text`` — the unit the report states next to the number. A polarization degree given as
  ``0.0345`` rather than ``3.45`` is a unit error, not a wrong measurement, and the two must not
  be scored as the same failure.
* ``dual_error`` — a second uncertainty offered for the same value (attempt 6 gives raw and
  ``chi^2``-inflated XSPEC errors). The **first** error after the value is taken, since the
  prescription asks for ``value ± error`` once; the alternative is kept for the record.

Everything here is pure text-to-dataclass, so it is testable against a frozen fixture with no run,
no toolkit and no network.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: The frozen answer key, beside this module. Hashed into every scored record.
ANSWER_KEY = Path(__file__).with_name("answer_key.json")

#: How far past a label to look for its value. Wide enough for attempt 6's longest entry (a value,
#: two errors, a unit and a parenthesised artifact list) and narrow enough that the next `\item`
#: cannot be read as this label's number.
WINDOW_CHARS = 420

#: Phrases a report uses to say a quantity was not obtained. Matched inside the window, before any
#: number, so "NOT MEASURED (loader limit)" is a recorded absence rather than a parse failure.
NOT_REPORTED_MARKERS = (
    "not measured",
    "not reported",
    "not produced",
    "not obtained",
    "not available",
    "unavailable",
)

#: ``$= 3.4534 \pm 0.1935$``, ``= -20.670 +/- 1.605``, ``$3.26 \pm 0.14$``. The equals sign is
#: optional because a table row states the pair without one.
_VALUE = re.compile(
    r"(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
    r"(?:\\pm|\+/-|±|\\?pm)\s*"
    r"(?P<error>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)

#: A unit as reports write it, immediately after the value pair. **Longest alternative first**:
#: regex alternation is first-match-wins, so a bare ``percent`` listed ahead of ``percent/keV``
#: silently truncates the slope's unit and hides exactly the unit error this field exists to catch.
_UNIT = re.compile(
    r"\s*(percent\s*/\s*keV|percent\s*keV\^?-?1?|per\s?cent\s*/\s*keV|%\s*/\s*keV|"
    r"percent|per\s?cent|%|deg(?:rees?)?|rad(?:ians?)?|fraction)",
    re.IGNORECASE,
)

#: A bare second uncertainty for the same value — ``$3.26 \pm 0.14$ percent raw; $\pm 0.56$``.
#: Bare means no value of its own immediately before it, which is what separates a genuine dual
#: error from the next quantity's ``value ± error`` pair.
_BARE_ERROR = re.compile(r"(?:\\pm|\+/-|±)\s*(?P<error>\d+(?:\.\d+)?)")

#: Where one quantity's statement ends. A LaTeX list item, a numbered line, or a table row: the
#: window must stop here or the *following* quantity's number, unit and artifact are read as this
#: one's — measured on attempt 6's real report, where every ``(same artifact)`` entry inherited the
#: next item's CSV and eight of fourteen picked up a neighbour's error as a dual error.
_ITEM_BREAK = re.compile(r"\\item|\\\\|\n\s*\d+\.\s|\n\s*[-*]\s|\n\n")

#: A reference value the report itself attributes to the published paper.
_CLAIMED_PAPER = re.compile(
    r"(?:published|paper(?:'s)?|Krawczynski[^.]{0,40}?)\D{0,40}?"
    r"(?P<value>[-+]?\d+(?:\.\d+)?)\s*(?:\\pm|\+/-|±)\s*(?P<error>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def normalise(text: str) -> str:
    r"""LaTeX as plain text, so a label matches however the report escaped it.

    Only the escapes that appear *inside* the prescribed labels and their numbers are undone
    (``\_``, ``\%``, ``\$``, ``\,``) plus the markup that wraps them (``\textbf``, ``\texttt``,
    ``\mathrm``, ``$``). The rest of the document is left alone: this is a reader, not a LaTeX
    implementation, and a partial de-escaping that also mangled prose would make the recorded
    window useless for the human audit that follows a surprising number.
    """
    out = text
    out = re.sub(r"\\(?:textbf|texttt|textit|emph|mathrm|mathbf|text)\s*\{([^{}]*)\}", r"\1", out)
    out = out.replace(r"\_", "_").replace(r"\%", "%").replace(r"\$", "$")
    out = out.replace(r"\,", " ").replace(r"\;", " ").replace("~", " ")
    out = out.replace("$", " ")
    out = re.sub(r"[ \t]+", " ", out)
    return out


@dataclass
class Extracted:
    """One quantity as a report stated it, or the record of its absence."""

    label: str
    value: float | None = None
    error: float | None = None
    unit_text: str | None = None
    not_reported: bool = False
    occurrences: int = 0
    """How many times the label appears in the report. Zero means the tree never used it."""
    dual_error: float | None = None
    """A second uncertainty offered for the same value; the first is the one taken."""
    paper_value_claimed: float | None = None
    paper_error_claimed: float | None = None
    source_artifact: str | None = None
    """The artifact the report names for this number, when it names one in parentheses."""
    window: str = ""
    """The text the value was read out of, verbatim, for the human audit."""

    @property
    def measured(self) -> bool:
        return self.value is not None and self.error is not None


@dataclass
class Extraction:
    """Every prescribed quantity for one report, plus what the report was."""

    report_path: str
    report_sha256: str
    labels_found: int
    labels_measured: int
    quantities: dict[str, Extracted] = field(default_factory=dict)
    compliant: bool = False
    """Whether all fourteen labels appear verbatim — the instruction the prescription gave."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_path": self.report_path,
            "report_sha256": self.report_sha256,
            "labels_found": self.labels_found,
            "labels_measured": self.labels_measured,
            "compliant": self.compliant,
            "quantities": {name: asdict(q) for name, q in self.quantities.items()},
        }


def load_answer_key(path: Path | None = None) -> dict[str, Any]:
    """The frozen key, with its own hash, so a scored record names the key that produced it."""
    target = path or ANSWER_KEY
    raw = target.read_bytes()
    key = json.loads(raw.decode("utf-8"))
    key["answer_key_sha256"] = hashlib.sha256(raw).hexdigest()
    return key


def _artifact(window: str) -> str | None:
    """The first parenthesised filename in the window, which is where reports name their source."""
    match = re.search(r"\(([^()]*?\.(?:csv|json|fits|txt|dat|png))[^()]*\)", window)
    return match.group(1).strip() if match else None


def _window(text: str, start: int, other_labels: tuple[str, ...]) -> str:
    """The text belonging to the label at ``start``, and no further.

    Bounded by the earliest of: the next list item or table row, the next prescribed label, and
    :data:`WINDOW_CHARS`. Without the first two bounds a quantity inherits its neighbour's number,
    unit and artifact, which reads as a successful extraction.
    """
    window = text[start : start + WINDOW_CHARS]
    ends = [len(window)]
    item_break = _ITEM_BREAK.search(window)
    if item_break:
        ends.append(item_break.start())
    for other in other_labels:
        position = window.find(other)
        if position > 0:
            ends.append(position)
    return window[: min(ends)]


def _extract_one(label: str, text: str, other_labels: tuple[str, ...] = ()) -> Extracted:
    """Find ``label`` and read the first ``value ± error`` that follows it, inside its own item."""
    found = Extracted(label=label)
    positions = [m.start() for m in re.finditer(re.escape(label), text)]
    found.occurrences = len(positions)
    if not positions:
        found.not_reported = True
        return found

    for start in positions:
        window = _window(text, start + len(label), other_labels)
        value_match = _VALUE.search(window)
        lowered = window.lower()
        marker_at = min(
            (lowered.find(marker) for marker in NOT_REPORTED_MARKERS if marker in lowered),
            default=-1,
        )
        # An absence marker standing before any number is the report saying so, not a parse miss.
        if marker_at >= 0 and (value_match is None or marker_at < value_match.start()):
            found.not_reported = True
            found.window = window[:WINDOW_CHARS].strip()
            continue
        if value_match is None:
            continue

        found.value = float(value_match.group("value"))
        found.error = float(value_match.group("error"))
        found.not_reported = False
        found.window = window.strip()
        tail = window[value_match.end() :]
        unit_match = _UNIT.match(tail)
        if unit_match:
            found.unit_text = re.sub(r"\s+", "", unit_match.group(1)).lower()
        # A dual error is a `pm` with no value of its own in front of it. Requiring that is what
        # distinguishes "raw; +- 0.56 inflated" from a neighbouring quantity's own pair.
        for candidate in _BARE_ERROR.finditer(tail):
            preceding = tail[: candidate.start()].rstrip()
            if not re.search(r"\d(?:\.\d+)?$", preceding):
                found.dual_error = float(candidate.group("error"))
                break
        claimed = _CLAIMED_PAPER.search(window)
        if claimed:
            found.paper_value_claimed = float(claimed.group("value"))
            found.paper_error_claimed = float(claimed.group("error"))
        found.source_artifact = _artifact(window)
        return found

    return found


def extract_text(text: str, labels: list[str]) -> dict[str, Extracted]:
    """Every label read out of report text, each bounded away from its neighbours.

    ``same artifact`` is resolved rather than dropped: reports name a source once and then refer
    back to it, so a label whose window says so inherits the artifact of the previous label that
    named one. The alternative is a provenance column that is empty precisely where a report was
    being concise, which would read as a tree that did not cite its sources.
    """
    normalised = normalise(text)
    found: dict[str, Extracted] = {}
    last_artifact: str | None = None
    for label in labels:
        others = tuple(other for other in labels if other != label)
        entry = _extract_one(label, normalised, others)
        if entry.source_artifact:
            last_artifact = entry.source_artifact
        elif re.search(r"same artifact|same file|same product|ibid", entry.window, re.IGNORECASE):
            entry.source_artifact = last_artifact
        found[label] = entry
    return found


def extract_report(path: Path, key: dict[str, Any] | None = None) -> Extraction:
    """Read one report file. Any text format works; ``report.tex`` is what the pipeline writes."""
    answer_key = key or load_answer_key()
    labels = list(answer_key["quantities"])
    raw = path.read_bytes()
    quantities = extract_text(raw.decode("utf-8", errors="replace"), labels)
    found = sum(1 for q in quantities.values() if q.occurrences)
    return Extraction(
        report_path=str(path),
        report_sha256=hashlib.sha256(raw).hexdigest(),
        labels_found=found,
        labels_measured=sum(1 for q in quantities.values() if q.measured),
        quantities=quantities,
        compliant=found == len(labels),
    )


def find_report(run_dir: Path) -> Path | None:
    """The report a trial wrote, preferring the machine handle when the run wrote one.

    ``report.jsonl`` is the machine handle and is written only when ``report_formats`` includes
    it; attempt 6 ran with ``[tex]`` alone, so ``report.tex`` is the file that exists across the
    campaign and is tried second. Both carry the prescribed labels, because the prescription puts
    them in the report's prose rather than in a schema slot.
    """
    for candidate in (
        run_dir / "reviewer" / "report" / "report.jsonl",
        run_dir / "reviewer" / "report" / "report.tex",
    ):
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "ANSWER_KEY",
    "Extracted",
    "Extraction",
    "NOT_REPORTED_MARKERS",
    "WINDOW_CHARS",
    "extract_report",
    "extract_text",
    "find_report",
    "load_answer_key",
    "normalise",
]
