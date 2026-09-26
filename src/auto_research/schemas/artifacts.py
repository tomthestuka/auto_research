"""Receipts: the runtime's record of every artifact a claim can rest on.

A receipt is minted by the runtime, never declared by a tool or a model: the runtime hashes the
bytes it stored, stamps who produced them, and records what they were derived from. Every
artifact kind shares one shape and one handle scheme, ``<source>:<letter><n>``, so a report can
cite a figure, a tool result, a data file, a paper or a human answer the same way.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef


NonEmptyStr = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

ArtifactKind = Literal["figure", "result", "data", "paper", "web", "human", "commit"]

#: One letter per kind; the letter is part of every handle, so a handle names its kind.
KIND_LETTER: dict[str, str] = {
    "figure": "f", "result": "r", "data": "d", "paper": "p", "web": "w", "human": "h",
    "commit": "c",
}
LETTER_KIND: dict[str, str] = {letter: kind for kind, letter in KIND_LETTER.items()}

#: ``<source>:<letter><n>``, n from 1. The source is the producing agent's key (``lib``,
#: ``analyst_worker_0``, …); ``self`` is accepted in prose as an alias for the citing agent's own.
HANDLE_PATTERN = r"[A-Za-z][A-Za-z0-9_\-]*:[frdpwhc][1-9][0-9]*"
Handle = Annotated[str, Field(pattern=rf"^{HANDLE_PATTERN}$")]

#: A receipt carrying either flag cannot ground a number in a claim. Both propagate to descendants.
ReceiptFlag = Literal["failed", "synthetic"]


class InputFile(MessageModel):
    """One file (or directory) a tool call read, hashed before the call ran."""

    path: Path
    sha256: Sha256
    basis: Literal["content", "listing"] = Field(
        default="content",
        description="'content' hashes the bytes; 'listing' hashes a directory's (name, size, mtime) listing.",
    )
    receipt: Handle | None = Field(
        default=None, description="The in-run receipt whose bytes these are, when one exists."
    )


class Receipt(MessageModel):
    """The runtime's record of one artifact (CLAUDE.md §2b, §7)."""

    handle: Handle
    kind: ArtifactKind
    path: Path = Field(description="Stored bytes. For result/web/paper/human: the text blob a quote is checked against.")
    sha256: Sha256 = Field(description="Computed by the runtime over the bytes at `path`.")
    produced_by: NonEmptyStr = Field(description="Tool kind, 'web_fetch', 'ask_human', 'coder', …")
    created_at: datetime
    call_id: str | None = None
    toolkit_commit: str | None = None
    args_sha256: Sha256 | None = None
    inputs: list[InputFile] = Field(default_factory=list)
    parents: list[Handle] = Field(default_factory=list)
    flags: list[ReceiptFlag] = Field(default_factory=list)
    arg_numbers: list[str] = Field(
        default_factory=list,
        description="Legacy (before 2026-09-25): the numbers typed into a shell call. Kept so old stores replay; "
        "the shell grounds nothing now (`provenance.store.is_evidence`).",
    )
    # figure
    caption: str | None = None
    mime_type: str | None = None
    # paper / web
    title: str | None = None
    ids: dict[str, str] = Field(
        default_factory=dict, description="Persistent identifiers: bibcode, arxiv, doi, url."
    )
    source_sha256: Sha256 | None = Field(
        default=None, description="Hash of the original PDF/HTML bytes; `sha256` is the extracted text."
    )
    source_path: Path | None = Field(
        default=None,
        description="The original file (the PDF) whose bytes `source_sha256` hashes; `path` stays the checked text.",
    )
    parser: str | None = Field(default=None, description="Text extractor and version, for paper/web.")
    # commit
    commit_sha: str | None = None

    @model_validator(mode="after")
    def _kind_matches_handle(self) -> "Receipt":
        letter = self.handle.split(":", 1)[1][0]
        if LETTER_KIND[letter] != self.kind:
            raise ValueError(f"handle {self.handle!r} names kind {LETTER_KIND[letter]!r}, not {self.kind!r}")
        if self.kind == "figure" and not (self.caption and self.mime_type):
            raise ValueError("a figure receipt needs caption and mime_type")
        return self

    @property
    def source(self) -> str:
        return self.handle.split(":", 1)[0]

    def to_figure_ref(self) -> FigureRef:
        """The multimodal-boundary view of a figure receipt (bytes still load only at the call)."""
        if self.kind != "figure":
            raise ValueError(f"{self.handle} is a {self.kind}, not a figure")
        return FigureRef(
            path=self.path, caption=self.caption or "", mime_type=self.mime_type or "",
            produced_by=self.produced_by, created_at=self.created_at, sha256=self.sha256,
        )


class PaperAttribution(MessageModel):
    """One value a report attributes to a paper, for the verify-against-the-PDF list.

    Papers are cited, never text-checked (human ruling 2026-09-24), so every paper value a report
    states is recorded here and nothing verifies it but a person. One shape for every role's report;
    the paper's title and identifiers are resolved from its receipt when the list is rendered.
    """

    handle: Handle = Field(description="The paper receipt the value is attributed to.")
    value: NonEmptyStr = Field(description="The value as the report states it.")
    locator: NonEmptyStr = Field(description="Where in the paper: a table, figure, section or page.")

