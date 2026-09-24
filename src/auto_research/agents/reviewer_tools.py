"""The reviewer's report surface and the check its ``turn_end`` runs.

The reviewer files one :class:`~auto_research.schemas.review.ReviewerReport` per review through the
shared ``edit_report`` / ``review_report`` / ``turn_end`` tools, like every other unit. What is the
reviewer's own lives here: which fields its ``edit_report`` declares, what each decision requires
before the turn may end, and the render-and-compile step that makes the ``.tex`` part of the check
rather than a step after it (CLAUDE.md §2b: a role's artifact is validated at its edge).

What ``turn_end`` refuses, and why each is here:

* **No report.** Nothing is composed on the reviewer's behalf.
* **A decision with nothing to hand on.** A rework or dispatch report is handed verbatim to the next
  team, so an empty ``follow_up`` is not a decision.
* **A switch that does nothing at this stage.** The graph reads ``needs_more_literature`` only after
  the literature review and ``needs_analysis_rework`` only after the analysis review; a flag set at
  the other stage used to be silently ignored.
* **A rework with no iterations left**, naming the cap, which is Python's.
* **A final report missing its parts** (title, abstract, a section, a finding).
* **A number no receipt grounds**, anywhere in the report, ``follow_up`` included (human
  2026-09-24: a request for more work is a report too). Any receipt the reviewer can read grounds
  any field, except an input (the request, a dispatched task), which grounds a text only when that
  text cites it (:func:`reviewer_scope`).
* **A report the renderer cannot place** (an unparseable table receipt, a figure handle that is not
  a figure) and a character the font cannot typeset. Any other compile failure is the renderer's
  bug: it is recorded on :class:`ReviewerRender` for the graph to report loudly, and it does not
  hold the model in a loop it cannot leave.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from auto_research.provenance import ReceiptLookup, check_report_fields
from auto_research.report.render import CompileResult, RenderResult, compile_report, render_report
from auto_research.schemas.figures import FigureRef
from auto_research.schemas.review import ReviewerReport

#: The reviewer's receipt source key: its own opened papers and its rendered reports are ``rev:``.
REVIEWER_SOURCE = "rev"

#: The fields the reviewer's ``edit_report`` declares, every one a real ``ReviewerReport`` field
#: whose prose the tool reads from that model's own ``Field(description=...)``.
REVIEWER_REPORT_FIELDS: tuple[str, ...] = (
    "accepted", "rationale", "needs_more_literature", "needs_analysis_rework", "follow_up",
    "title", "abstract", "sections", "key_findings", "figures", "tables",
)
#: The reviewer files no ``status``: its decision is the verdict fields themselves.
REVIEWER_STATUS_VALUES: tuple[str, ...] = ()

ReviewStage = Literal["literature", "analysis"]


def reviewer_scope(lookup: ReceiptLookup) -> list[str]:
    """The receipts that ground the reviewer's numbers without being cited in the same text.

    Everything the reviewer can read, except its inputs (the request, a dispatched task): an input
    grounds a text only when that text cites ``[input:hN]`` (human 2026-09-24, the analysts' rule).
    A reproduction question carries the published values, so without this a value from the question
    could reappear as a finding with nothing marking where it came from.
    """
    handles = getattr(lookup, "handles", None)
    return [h for h in (handles() if callable(handles) else []) if not h.startswith("input:")]


@dataclass
class ReviewerRender:
    """Renders a finalizing report inside ``turn_end`` and keeps what the graph needs afterwards.

    ``formats`` is the run's ``report_formats``; only ``tex`` renders. ``render`` and ``compile``
    hold the last attempt, so after the loop the graph reads the ``.tex``, the PDF and any renderer
    error from here rather than rendering a second time.
    """

    report_dir: Path
    run_id: str
    formats: Sequence[str] = ("tex",)
    render: RenderResult | None = None
    compile: CompileResult | None = None
    attempts: int = 0

    def __call__(self, report: ReviewerReport, lookup: ReceiptLookup) -> list[str]:
        if "tex" not in self.formats:
            return []
        self.attempts += 1
        self.compile = None
        self.render = render_report(report, lookup, self.report_dir, run_id=self.run_id,
                                    own_source=REVIEWER_SOURCE)
        if self.render.problems:
            return list(self.render.problems)
        self.compile = compile_report(self.report_dir)
        return list(self.compile.glyph_problems)

    @property
    def renderer_errors(self) -> list[str]:
        return list(self.compile.errors) if self.compile is not None else []


def reviewer_turn_problems(
    report: ReviewerReport | None,
    *,
    lookup: ReceiptLookup,
    scope: Sequence[str],
    stage: ReviewStage,
    iterations_left: int,
    render: ReviewerRender | None = None,
) -> list[str]:
    """Everything that stands between the reviewer's report and the end of its turn.

    ``scope`` is every handle the reviewer can read (``ScopedLookup.handles()``); ``iterations_left``
    is the graph's remaining budget for the loop edge this stage controls. ``render`` runs only for
    a finalizing analysis review, and only once every other problem is fixed.
    """
    if report is None:
        return [
            "No report has been written. Call edit_report to write it — that call is the only way "
            "your report is saved, and nothing is composed on your behalf."
        ]
    problems: list[str] = []
    if not report.rationale.strip():
        problems.append("rationale is empty. State the evidence the decision rests on.")

    if stage == "literature":
        if report.needs_analysis_rework:
            problems.append(
                "needs_analysis_rework applies at the analysis review. At the literature review, "
                "set needs_more_literature for another search, or leave both false to dispatch the "
                "analysts with follow_up as their task."
            )
        if report.needs_more_literature and iterations_left <= 0:
            problems.append(
                "No literature passes remain, so another search cannot be granted. Leave "
                "needs_more_literature false, dispatch the analysts, and carry the literature gap "
                "as a stated limitation."
            )
        if not report.follow_up.strip():
            problems.append(
                "follow_up is empty. It is handed verbatim to the next team: the literature "
                "questions for another search, or the analysts' task."
            )
        finalizing = False
    else:
        if report.needs_more_literature:
            problems.append(
                "needs_more_literature applies at the literature review; after the analysis the run "
                "has no further literature pass. State the gap in the report as a limitation, or "
                "set needs_analysis_rework to send the analysts back."
            )
        if report.needs_analysis_rework:
            if not report.follow_up.strip():
                problems.append(
                    "Your report asks for analysis rework but follow_up is empty. That text is "
                    "handed to the analyst team verbatim as their next task."
                )
            if iterations_left <= 0:
                problems.append(
                    "No analysis iterations remain, so rework cannot be granted. Finalize on the "
                    "evidence you have (needs_analysis_rework=false) — an honest 'this is not "
                    "settled' is a correct outcome."
                )
        finalizing = not report.needs_analysis_rework
        if finalizing:
            for name, present in (
                ("title", report.title.strip()),
                ("abstract", report.abstract.strip()),
                ("sections", report.sections),
                ("key_findings", report.key_findings),
            ):
                if not present:
                    problems.append(
                        f"You are finalizing, so this report is the run's report, and {name} is "
                        "empty. Write it — for an incomplete analysis, say what was attempted and "
                        "what blocked it."
                    )

    checked = check_report_fields(
        report.prose_fields(), lookup, own_source=REVIEWER_SOURCE, itemised=(), scope=scope,
    )
    problems.extend(checked.problems)
    if finalizing and render is not None and not problems:
        problems.extend(render(report, lookup))
    return problems


# -- the reviewer's own tools -----------------------------------------------------------------

#: The reviewer reads papers through the shared reader (``agents/paper_tools.PaperReader``), the
#: librarian's path: the PDF itself attached, quotes checked against its text layer.
READ_PAPER_TOOL = "read_paper"

#: The description the reviewer's ``edit_report`` carries; its field prose comes from the model.
REVIEWER_EDIT_DESCRIPTION = (
    "Write (or rewrite) your report — this is the ONLY way it is saved. Provide the whole report "
    "each call; a call overwrites the last, so refine across calls. Write plain text: the harness "
    "typesets the final report from it. Every number sits in a quote of the receipt it comes from, "
    '{{handle "exact text"}}, or in text that cites that receipt as [handle]; any receipt you can '
    "read may ground any field. Place figures by their receipt handle in `figures`. Free: costs no "
    "tool budget."
)

#: Rendered pages shown per draft preview; the media guard bounds a request, this bounds the draft.
MAX_PREVIEW_PAGES = 12
PREVIEW_DPI = 80


def reviewer_draft_view(
    report_dir: Path,
    run_id: str,
    lookup: ReceiptLookup,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Callable[[ReviewerReport], tuple[str, list[FigureRef]]]:
    """``review_report``'s view of the reviewer's own draft: the rendered pages, as images.

    The polishing pass (human 2026-09-24) happens at the source: the reviewer sees what its report
    will look like and edits the report again, never the ``.tex``. The draft renders into
    ``report_dir/draft/``, apart from the final report, and a render problem is shown in place of
    pages so the view never fails the call.
    """
    draft_dir = report_dir / "draft"

    def view(report: ReviewerReport) -> tuple[str, list[FigureRef]]:
        shutil.rmtree(draft_dir, ignore_errors=True)
        rendered = render_report(report, lookup, draft_dir, run_id=run_id, own_source=REVIEWER_SOURCE)
        if rendered.problems:
            return ("Your draft cannot be rendered yet:\n" + "\n".join(f"  - {p}" for p in rendered.problems), [])
        compiled = compile_report(draft_dir, run=run)
        notes = [f"  - {p}" for p in compiled.glyph_problems]
        if compiled.pdf_path is None:
            detail = "; ".join(compiled.errors) or "no PDF was produced"
            return (f"Your draft rendered but did not compile ({detail}).\n" + "\n".join(notes), [])
        pages = _rasterize(compiled.pdf_path, draft_dir, run=run)
        text = (
            f"Your draft as it will be typeset: {len(pages)} page image(s) attached"
            + (f" (first {MAX_PREVIEW_PAGES} shown)" if len(pages) == MAX_PREVIEW_PAGES else "")
            + ". Read them for wording, section order and figure/table placement, then edit_report "
            "again to polish."
        )
        return ("\n".join([text, *notes]), pages)

    return view


def reviewer_text_view(lookup: ReceiptLookup) -> Callable[[ReviewerReport], tuple[str, list[FigureRef]]]:
    """``review_report``'s view of the draft when no ``.tex`` is configured: its text and figures.

    A reviewer report places figures by receipt handle, so the analysts' figure-record view cannot
    show it; this reads each figure's receipt instead.
    """

    def view(report: ReviewerReport) -> tuple[str, list[FigureRef]]:
        lines = [f"{label}: {value}" if isinstance(value, str) else f"{label}:\n" + "\n".join(
            f"  - {item}" for item in value) for label, value in report.prose_fields().items()]
        figures: list[FigureRef] = []
        for number, handle in enumerate(report.figures, 1):
            receipt = lookup.get(handle)
            if receipt is not None and receipt.kind == "figure":
                figures.append(receipt.to_figure_ref().model_copy(
                    update={"caption": f"Figure {number} — {receipt.caption}"}))
                lines.append(f"Figure {number}: [{handle}] {receipt.caption}")
            else:
                lines.append(f"Figure {number}: {handle} is not a figure receipt you can place.")
        return ("Your current draft:\n\n" + "\n".join(lines), figures)

    return view


def _rasterize(
    pdf: Path, out_dir: Path, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> list[FigureRef]:
    if not shutil.which("pdftoppm"):
        return []
    prefix = out_dir / "page"
    run(
        ["pdftoppm", "-r", str(PREVIEW_DPI), "-png", "-l", str(MAX_PREVIEW_PAGES), str(pdf), str(prefix)],
        capture_output=True, timeout=120,
    )
    now = datetime.now(timezone.utc)
    pages: list[FigureRef] = []
    for number, png in enumerate(sorted(out_dir.glob("page-*.png")), 1):
        pages.append(FigureRef(
            path=png, caption=f"Draft page {number}", mime_type="image/png",
            produced_by="report_renderer", created_at=now,
            sha256=hashlib.sha256(png.read_bytes()).hexdigest(),
        ))
    return pages
