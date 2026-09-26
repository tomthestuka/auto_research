"""The report the reviewer files once per turn; the graph routes on its ``assign_to``."""

from typing import Annotated, Literal

from pydantic import Field

from auto_research.schemas.artifacts import Handle
from auto_research.schemas.base import MessageModel



class ReviewerSection(MessageModel):
    """One section of the reviewer's report: a heading and its prose."""

    heading: str = Field(description="The section heading, in plain text.")
    body: str = Field(
        description=(
            "The section's prose, in plain text. Separate paragraphs with a blank line and start a "
            "bullet line with '- '. Write symbols as Unicode (χ², ±, σ, ≲). Put every number "
            'inside a quote of the receipt it comes from, {{handle "exact text"}}, or in text '
            "that cites that receipt as [handle]; attribute a paper's value with where it is, "
            '{{lib:p4 "value" Table 2}}. Refer to the k-th entry of `figures` as '
            "'Figure k' and the k-th entry of `tables` as 'Table k'."
        ),
    )


class ReviewerTable(MessageModel):
    """One table in the reviewer's report: either a receipt typeset whole, or rows you assemble.

    Fill exactly one form. A table a tool computed is cited by its receipt and typeset from the
    receipt's stored bytes; a table you build yourself (this work against a paper, say) is written
    as columns and rows, with every numeric cell a quote.
    """

    caption: str = Field(description="The table caption, in plain text.")
    receipt: Handle | None = Field(
        default=None,
        description=(
            "For a table a tool computed: the handle of the data or result receipt holding it as "
            "delimited text (CSV or TSV). The harness typesets those bytes as they are. Leave "
            "columns and rows empty in this form."
        ),
    )
    columns: list[str] = Field(
        default_factory=list,
        description="For a table you build: the column headings, in plain text.",
    )
    rows: list[list[str]] = Field(
        default_factory=list,
        description=(
            "For a table you build: one list of cells per row, as many cells as there are columns. "
            'Write every numeric cell as a quote, {{handle "exact text"}}, or a paper\'s value as '
            '{{lib:p4 "value" Table 2}}.'
        ),
    )


#: Where the reviewer sends its report (human 2026-09-25). The reviewer is the sole I/O: every
#: team's work starts from a reviewer turn and returns to one.
Assignee = Literal["librarian", "analysts", "human"]


class ReviewerReport(MessageModel):
    """The one report the reviewer files per turn, through ``edit_report`` and ``turn_end``.

    The same form serves every decision, and what the content means follows ``assign_to`` (the
    webform shape, human 2026-09-24): to the librarian or the analysts, ``rationale`` and
    ``follow_up`` are the verdict so far and the task handed to that team; to the human, the
    header, sections, findings, figures and tables are the report the harness renders and sends.
    Every prose field is checked against receipts either way.

    The schema is **permissive** so a partial write mid-loop is legal; what each decision requires
    is ``turn_end``'s job (``agents/reviewer_tools.reviewer_turn_problems``). There is no
    references field: the runtime builds the bibliography from the paper and web receipts the
    report cites, which carry each document's title and persistent identifiers.
    """

    accepted: bool = Field(
        default=False,
        description=(
            "True when the analysis answers the request and agrees with the literature. False when "
            "it does not yet, or when an honest limitation stands; say which in rationale."
        ),
    )
    rationale: str = Field(
        default="",
        description="The evidence-grounded basis for the decision, citing the receipts it rests on.",
    )
    assign_to: Assignee | None = Field(
        default=None,
        description=(
            "Who receives this report. 'librarian': follow_up is the literature questions for a "
            "search. 'analysts': follow_up is the analysis task, or the revision to make. 'human': "
            "the title, abstract, sections, findings, figures and tables are the report the human "
            "receives and discusses with you."
        ),
    )
    follow_up: str = Field(
        default="",
        description=(
            "For the librarian or the analysts: the task handed to them verbatim — the literature "
            "questions to answer, or the analysis steps to run with the targets and the "
            "data-source priority. For a capability the toolkit lacks, state what to measure and "
            "on which data, and leave the build choice to the analyst team."
        ),
    )
    title: str = Field(default="", description="The final report's title, in plain text.")
    abstract: str = Field(
        default="",
        description=(
            "The final report's abstract: the question, what was done, what was found, and what "
            "limits it, in plain prose with every number grounded in a receipt."
        ),
    )
    sections: list[ReviewerSection] = Field(
        default_factory=list,
        description="The final report's body, in reading order.",
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description=(
            "The final report's findings, one self-contained statement each. For an incomplete "
            "analysis, state what was attempted and what blocked it, and keep only partial "
            "results that genuinely hold."
        ),
    )
    figures: list[Handle] = Field(
        default_factory=list,
        description=(
            "Figure receipts to place in the final report, in the order the prose refers to them "
            "as 'Figure 1', 'Figure 2', …. Pick them from the figure handles in your inputs."
        ),
    )
    tables: list[ReviewerTable] = Field(
        default_factory=list,
        description="Tables for the final report, referred to in the prose as 'Table 1', 'Table 2', ….",
    )

    def prose_fields(self) -> dict[str, str | list[str]]:
        """Every piece of prose in the report, keyed by the label a problem names it with.

        The one enumeration the quote check and the renderer both walk, in reading order, so a
        field cannot be rendered without having been checked. Table cells are joined per row with
        ``" | "``, which keeps each cell's bindings intact and names the row in a problem.
        """
        fields: dict[str, str | list[str]] = {
            "rationale": self.rationale,
            "follow_up": self.follow_up,
            "title": self.title,
            "abstract": self.abstract,
        }
        for i, section in enumerate(self.sections, 1):
            fields[f"section {i} heading"] = section.heading
            fields[f"section {i}"] = section.body
        fields["key_findings"] = list(self.key_findings)
        for i, table in enumerate(self.tables, 1):
            fields[f"table {i} caption"] = table.caption
            if table.columns:
                fields[f"table {i} columns"] = " | ".join(table.columns)
            for j, row in enumerate(table.rows, 1):
                fields[f"table {i} row {j}"] = " | ".join(row)
        return {name: value for name, value in fields.items() if value}
