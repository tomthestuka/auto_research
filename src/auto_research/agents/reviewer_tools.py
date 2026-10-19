"""The reviewer's report surface and the check its ``turn_end`` runs.

The reviewer files one :class:`~auto_research.schemas.review.ReviewerReport` per review through the
shared ``write_report`` / ``edit_report`` / ``turn_end`` tools, like every other unit. What is the
reviewer's own lives here: which fields its ``edit_report`` declares, what each decision requires
before the turn may end, and the render-and-compile step that makes the ``.tex`` part of the check
rather than a step after it (CLAUDE.md §2b: a role's artifact is validated at its edge).

What ``turn_end`` refuses, and why each is here:

* **No report.** Nothing is composed on the reviewer's behalf.
* **No assignee.** The graph routes on ``assign_to``; a report that names no team goes nowhere.
* **A task with nothing to hand on.** A report to the librarian or the analysts is handed to that
  team verbatim, so an empty ``follow_up`` is not a decision.
* **An assignment with no passes left**, naming the cap, which is Python's.
* **A report to the human missing its parts** (title, abstract, a section, a finding).
* **A number no evidence grounds**, anywhere in the report except the brief (``follow_up``: a
  prescription, not a claim, human 2026-09-25). Evidence is what the analysis produced — toolkit
  results, commits, papers by attribution — never what a role read (``is_evidence``). Any receipt the reviewer can read grounds
  any field, except an input (the request, a dispatched task, a file the human sent), which grounds
  a text only when that text cites it (:func:`reviewer_scope`). The human's reply text is a nudge,
  never a receipt, so it grounds nothing.
* **A citation of anything that is not evidence** — a file read, a search, a web page, the shell,
  a brief, a file the human sent, an earlier published report — named by what it is.
* **A report the renderer cannot place** (an unparseable table receipt, a figure handle that is not
  a figure) and a character the font cannot typeset. Any other compile failure is the renderer's
  bug: it is recorded on :class:`ReviewerRender` for the graph to report loudly, and it does not
  hold the model in a loop it cannot leave.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from auto_research.provenance import ReceiptLookup, check_report_fields
from auto_research.report.html import HtmlResult, render_html
from auto_research.report.render import CompileResult, RenderResult, compile_report, render_report
from auto_research.schemas.review import ReviewerReport

#: The reviewer's receipt source key: its own opened papers and its rendered reports are ``rev:``.
REVIEWER_SOURCE = "rev"

#: The fields the reviewer's report document holds, every one a real ``ReviewerReport`` field with a
#: block in ``report_edit.REVIEWER_BLOCKS`` (whose hints the tool descriptions list).
REVIEWER_REPORT_FIELDS: tuple[str, ...] = (
    "accepted", "rationale", "assign_to", "follow_up",
    "title", "abstract", "sections", "key_findings", "figures", "tables",
)
#: The reviewer files no ``status``: its decision is the verdict fields themselves.
REVIEWER_STATUS_VALUES: tuple[str, ...] = ()

#: What each team's pass is called when its cap is named in a refusal.
_PASS_NAMES = {"librarian": "research passes", "analysts": "analysis passes"}


def reviewer_scope(lookup: ReceiptLookup) -> list[str]:
    """The receipts that ground the reviewer's numbers without being cited in the same text.

    Everything the reviewer can read, except its inputs (the request, a dispatched task): an input
    grounds a text only when that text cites ``[input:hN]`` (human 2026-09-24, the analysts' rule).
    A reproduction question carries the published values, so without this a value from the question
    could reappear as a finding with nothing marking where it came from.
    """
    handles = getattr(lookup, "handles", None)
    return [h for h in (handles() if callable(handles) else []) if not h.startswith("input:")]


#: The reviewer's brief to the next team (human ruling 2026-09-25): a prescription, not a claim,
#: so its numbers are not grounded — and, dispatched, it grounds nothing downstream
#: (``provenance.store.is_evidence``: a ``dispatch`` input is not evidence).
BRIEF_FIELDS = frozenset({"follow_up"})


def reviewer_claims(report: ReviewerReport) -> dict[str, str | list[str]]:
    """The report's prose the grounding check reads: every field but the brief."""
    return {k: v for k, v in report.prose_fields().items() if k not in BRIEF_FIELDS}


@dataclass
class ReviewerRender:
    """Renders a finalizing report inside ``turn_end`` and keeps what the graph needs afterwards.

    ``formats`` is the run's ``report_formats``: each of ``tex`` (LaTeX + PDF) and ``html`` (the page
    with provenance popovers) runs its own path only when listed, and one format's fault never
    stops another's. ``render``, ``compile`` and ``html`` hold the last attempt, so after the loop
    the graph reads the files and any renderer error from here rather than rendering again.
    ``ledger`` is the run's ``monitor.jsonl``, from which the page rebuilds each call's arguments.
    """

    report_dir: Path
    run_id: str
    formats: Sequence[str] = ("tex",)
    ledger: Path | None = None
    render: RenderResult | None = None
    compile: CompileResult | None = None
    html: HtmlResult | None = None
    attempts: int = 0

    def __call__(
        self, report: ReviewerReport, lookup: ReceiptLookup, scope: Sequence[str] = (),
        numbers: Mapping[str, list] | None = None,
    ) -> list[str]:
        """``numbers``: the validator's own per-number answer (``check_report_fields(...,
        sources=True).numbers``), computed here over ``scope`` when not given."""
        if not {"tex", "html"} & set(self.formats):
            return []
        self.attempts += 1
        problems: list[str] = []
        if "tex" in self.formats:
            self.compile = None
            try:
                self.render = render_report(report, lookup, self.report_dir, run_id=self.run_id,
                                            own_source=REVIEWER_SOURCE)
                if self.render.problems:
                    problems.extend(self.render.problems)
                else:
                    self.compile = compile_report(self.report_dir)
                    problems.extend(self.compile.glyph_problems)
            except Exception as exc:  # noqa: BLE001 — a renderer fault is recorded, never the model's
                self.render = None
                self.compile = CompileResult(pdf_path=None, errors=[f"the LaTeX renderer raised {type(exc).__name__}: {exc}"])
        if "html" in self.formats:
            try:
                if numbers is None:
                    numbers = check_report_fields(
                        reviewer_claims(report), lookup, own_source=REVIEWER_SOURCE, itemised=(),
                        scope=scope, sources=True,
                    ).numbers
                # Written only when no format has a problem this attempt: a page is never left for
                # a report the turn has not accepted.
                self.html = render_html(report, lookup, self.report_dir, run_id=self.run_id,
                                        numbers=numbers, ledger=self.ledger, own_source=REVIEWER_SOURCE,
                                        write=not problems)
                problems.extend(self.html.problems)
            except Exception as exc:  # noqa: BLE001 — a renderer fault is recorded, never the model's
                self.html = HtmlResult(html_path=None, errors=[f"the HTML renderer raised {type(exc).__name__}: {exc}"])
        return list(dict.fromkeys(problems))

    @property
    def renderer_errors(self) -> list[str]:
        """Every renderer fault of the last attempt (never the model's to fix)."""
        return [*(self.compile.errors if self.compile is not None else []), *self.html_errors]

    @property
    def html_errors(self) -> list[str]:
        return list(self.html.errors) if self.html is not None else []


def reviewer_turn_problems(
    report: ReviewerReport | None,
    *,
    lookup: ReceiptLookup,
    scope: Sequence[str],
    budgets: Mapping[str, int],
    render: ReviewerRender | None = None,
) -> list[str]:
    """Everything that stands between the reviewer's report and the end of its turn.

    ``scope`` is every handle the reviewer can read (``ScopedLookup.handles()``); ``budgets`` is
    how many passes remain for each team the graph caps (``librarian``, ``analysts``). ``render``
    runs only for a report to the human, and only once every other problem is fixed.
    """
    if report is None:
        return [
            "No report has been written. Write it with write_report (or fill the empty draft "
            "with edit_report) — those calls are the only way your report is saved, and nothing "
            "is composed on your behalf."
        ]
    problems: list[str] = []
    if not report.rationale.strip():
        problems.append("rationale is empty. State the evidence the decision rests on.")

    to_human = report.assign_to == "human"
    if report.assign_to is None:
        problems.append(
            "assign_to is empty. Set it to 'librarian' (follow_up is the research questions), "
            "'analysts' (follow_up is the analysis task) or 'human' (the report is sent to the "
            "human to discuss)."
        )
    elif not to_human:
        team = report.assign_to
        if not report.follow_up.strip():
            problems.append(
                f"follow_up is empty. It is handed to the {team} verbatim as their task: write the "
                "questions to answer or the steps to run."
            )
        if budgets.get(team, 0) <= 0:
            problems.append(
                f"No {_PASS_NAMES[team]} remain in this run, so the {team} cannot be assigned. "
                "Assign another team, or send the report to the human with the open question "
                "stated as a limitation."
            )
    else:
        for name, present in (
            ("title", report.title.strip()),
            ("abstract", report.abstract.strip()),
            ("sections", report.sections),
            ("key_findings", report.key_findings),
        ):
            if not present:
                problems.append(
                    f"This report goes to the human, and {name} is empty. Write it — for an "
                    "incomplete analysis, say what was attempted and what blocked it."
                )

    page = render is not None and "html" in render.formats
    checked = check_report_fields(
        reviewer_claims(report), lookup, own_source=REVIEWER_SOURCE, itemised=(), scope=scope,
        sources=to_human and page,
    )
    problems.extend(checked.problems)
    if to_human and render is not None and not problems:
        problems.extend(render(report, lookup, scope, checked.numbers if page else None))
    return problems


# -- the reviewer's own tools -----------------------------------------------------------------

#: The reviewer reads papers through the shared reader (``agents/paper_tools.PaperReader``), the
#: librarian's path: the PDF itself attached, quotes checked against its text layer.
READ_PAPER_TOOL = "read_paper"

#: The description the reviewer's ``edit_report`` carries; its field prose comes from the model.
REVIEWER_EDIT_DESCRIPTION = (
    "Write plain text: the harness typesets the final report from it. Every number sits in a quote "
    'of the receipt it comes from, {{handle "exact text"}}, or in text that cites that receipt as '
    "[handle]; any receipt you can read may ground any field. Place figures by their receipt "
    "handle, one '- handle' line under # figures."
)

