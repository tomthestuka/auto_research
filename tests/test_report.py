"""Deterministic tests for the report composition module (step-13)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_research.report import (
    build_references,
    compile_latex,
    stage_figures,
    validate_latex_refs,
    write_report_jsonl,
    write_report_latex,
)
from auto_research.schemas import FigureRef
from auto_research.schemas.report import (
    LatexReport,
    Reference,
    ReportSection,
    ResearchReport,
)
from auto_research.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fig(tmp_path: Path, name: str) -> FigureRef:
    p = tmp_path / f"{name}.png"
    p.write_bytes(b"\x89PNG\r\n")
    import hashlib
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return FigureRef(
        path=p,
        caption=name,
        mime_type="image/png",
        produced_by="test",
        created_at=datetime.now(UTC),
        sha256=sha,
    )


def _make_report(run_id: str = "run123", figures: list[FigureRef] | None = None) -> ResearchReport:
    return ResearchReport(
        run_id=run_id,
        request_question="What is the spin of Cyg X-1?",
        title="Broadband spectral analysis of Cyg X-1",
        abstract="We fit the joint IXPE+NuSTAR+NICER spectrum.",
        sections=[ReportSection(heading="Results", body="Spin is high.")],
        key_findings=["High spin confirmed."],
        figures=figures or [],
        references=[],
        verdict_rationale="Accepted.",
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# build_references
# ---------------------------------------------------------------------------

def test_build_references_maps_stored_papers(tmp_path):
    """Papers in storage are matched by local_path; cite keys are derived from metadata."""
    pdf = tmp_path / "arxiv_2603.10870v1.pdf"
    pdf.write_bytes(b"%PDF")

    storage = Storage(tmp_path / "db.sqlite")
    from auto_research.storage.database import PaperRecord
    paper = PaperRecord(
        source="arxiv",
        source_id="2603.10870",
        title="Soft-state spectropolarimetry of Cyg X-1",
        authors=["Niedźwiecki, A.", "Smith, B."],
        year=2026,
        local_path=pdf,
    )
    asyncio.run(storage.initialize())
    asyncio.run(storage.upsert_paper(paper))

    refs = asyncio.run(build_references(storage, [pdf]))
    assert len(refs) == 1
    assert refs[0].title == paper.title
    assert refs[0].year == 2026
    assert "2026" in refs[0].key
    assert refs[0].local_path == pdf


def test_build_references_fallback_to_stem(tmp_path):
    """Papers not in storage fall back to the filename stem."""
    pdf = tmp_path / "mystery_paper.pdf"
    pdf.write_bytes(b"%PDF")

    storage = Storage(tmp_path / "db.sqlite")
    asyncio.run(storage.initialize())

    refs = asyncio.run(build_references(storage, [pdf]))
    assert len(refs) == 1
    assert refs[0].title == "mystery_paper"
    assert refs[0].key  # non-empty key derived from stem


def test_build_references_deduplicates_keys(tmp_path):
    """Duplicate cite keys get letter suffixes."""
    pdfs = []
    for i in range(3):
        p = tmp_path / f"paper_{i}.pdf"
        p.write_bytes(b"%PDF")
        pdfs.append(p)

    storage = Storage(tmp_path / "db.sqlite")
    asyncio.run(storage.initialize())
    from auto_research.storage.database import PaperRecord
    for i, p in enumerate(pdfs):
        asyncio.run(storage.upsert_paper(PaperRecord(
            source="arxiv", source_id=f"x{i}", title=f"Title {i}",
            authors=["Smith, J."], year=2026, local_path=p,
        )))

    refs = asyncio.run(build_references(storage, pdfs))
    keys = [r.key for r in refs]
    assert len(keys) == len(set(keys)), f"Duplicate keys: {keys}"


# ---------------------------------------------------------------------------
# stage_figures
# ---------------------------------------------------------------------------

def test_stage_figures_creates_symlinks(tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    fig = _make_fig(tmp_path / "src", "fit_plot")

    rdir = tmp_path / "report"
    staged = stage_figures([fig], rdir)

    assert len(staged) == 1
    figref, relpath = staged[0]
    assert relpath == f"figures/{Path(fig.path).name}"
    link = rdir / relpath
    assert link.is_symlink()
    assert link.resolve() == Path(fig.path).resolve()


def test_stage_figures_replaces_stale_symlink(tmp_path):
    """A stale symlink is replaced without error."""
    src = tmp_path / "src"
    src.mkdir()
    fig = _make_fig(src, "plot")
    rdir = tmp_path / "report"
    stage_figures([fig], rdir)
    # Call again — should not raise
    staged = stage_figures([fig], rdir)
    assert len(staged) == 1


# ---------------------------------------------------------------------------
# write_report_jsonl
# ---------------------------------------------------------------------------

def test_write_report_jsonl_round_trips(tmp_path):
    report = _make_report()
    path = write_report_jsonl(report, tmp_path / "report.jsonl")
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    restored = ResearchReport.model_validate_json(lines[0])
    assert restored.run_id == report.run_id
    assert restored.title == report.title


def test_write_report_jsonl_figures_by_reference(tmp_path):
    """Figures are stored as FigureRef dicts (path/sha256), not bytes."""
    fig = _make_fig(tmp_path, "plot")
    report = _make_report(figures=[fig])
    path = write_report_jsonl(report, tmp_path / "report.jsonl")
    raw = json.loads(path.read_text())
    assert "figures" in raw
    assert raw["figures"][0]["sha256"] == fig.sha256
    assert "thumbnail_b64" in raw["figures"][0]  # field present, may be null


# ---------------------------------------------------------------------------
# validate_latex_refs
# ---------------------------------------------------------------------------

_GOOD_TEX = r"""
\documentclass{article}
\begin{document}
\includegraphics{figures/fit.png}
\cite{smith2026}
\end{document}
"""

_BAD_TEX = r"""
\documentclass{article}
\begin{document}
\includegraphics{figures/missing.png}
\cite{badkey}
\end{document}
"""


def test_validate_latex_refs_passes_clean(tmp_path):
    fig = _make_fig(tmp_path, "fit")
    ref = Reference(key="smith2026", title="A paper", authors=["Smith"], year=2026)
    staged = [(fig, "figures/fit.png")]
    errs = validate_latex_refs(_GOOD_TEX, staged, [ref])
    assert errs == []


def test_validate_latex_refs_flags_missing(tmp_path):
    fig = _make_fig(tmp_path, "fit")
    ref = Reference(key="smith2026", title="A paper", authors=["Smith"], year=2026)
    staged = [(fig, "figures/fit.png")]
    errs = validate_latex_refs(_BAD_TEX, staged, [ref])
    assert any("missing.png" in e for e in errs)
    assert any("badkey" in e for e in errs)


# ---------------------------------------------------------------------------
# compile_latex
# ---------------------------------------------------------------------------

_MINIMAL_TEX = r"""
\documentclass{article}
\begin{document}
Hello world.
\end{document}
"""

_BROKEN_TEX = r"""
\documentclass{article}
\begin{document}
\unknowncommand{broken
\end{document}
"""


@pytest.mark.skipif(
    not __import__("shutil").which("pdflatex"),
    reason="pdflatex not available",
)
def test_compile_latex_clean(tmp_path):
    (tmp_path / "report.tex").write_text(_MINIMAL_TEX)
    ok, errs = compile_latex(tmp_path)
    assert ok
    assert errs == []
    assert (tmp_path / "report.pdf").exists()


def test_compile_latex_no_compiler_returns_ok(tmp_path):
    """When compiler is absent, returns (True, []) to degrade gracefully."""
    (tmp_path / "report.tex").write_text(_BROKEN_TEX)
    with patch("shutil.which", return_value=None):
        ok, errs = compile_latex(tmp_path)
    assert ok
    assert errs == []


# ---------------------------------------------------------------------------
# report_formats gating (node-level)
# ---------------------------------------------------------------------------

class FakeAnalystReport:
    figures: list = []
    summary: str = "analysis done"
    findings: list = []
    uncertainty: str = ""

    def model_dump(self, **kw):
        return {"summary": self.summary, "findings": [], "uncertainty": ""}


class FakeVerdict:
    accepted = True
    rationale = "Test accepted."
    needs_more_literature = False
    needs_analysis_rework = False
    follow_up = None


class FakeReviewerForReport:
    """Canned reviewer for report composition tests."""

    def __init__(self, *, latex_attempts: int = 1):
        self._attempt = 0
        self._latex_attempts = latex_attempts

    async def compose_machine_report(self, request, report, references, verdict, run_id):
        return _make_report(run_id=run_id)

    async def compose_latex_report(self, request, report, staged, references, verdict, *, feedback=None):
        self._attempt += 1
        if self._attempt < self._latex_attempts:
            # First attempts return broken LaTeX to trigger feedback
            return r"\documentclass{article}\begin{document}\cite{badkey}\end{document}"
        return _MINIMAL_TEX


def test_report_jsonl_only(tmp_path):
    """report_formats=['jsonl'] writes only JSONL; no .tex file."""
    from auto_research.report.figures import stage_figures as sf
    from auto_research.report.references import build_references as br
    from auto_research.report.writers import write_report_jsonl as wrj

    storage = Storage(tmp_path / "db.sqlite")
    asyncio.run(storage.initialize())
    reviewer = FakeReviewerForReport()
    rdir = tmp_path / "report"
    rdir.mkdir()

    refs = asyncio.run(br(storage, []))
    machine = asyncio.run(
        reviewer.compose_machine_report(
            type("R", (), {"question": "Q", "model_dump": lambda s, **k: {"question": "Q"}})(),
            FakeAnalystReport(),
            refs,
            FakeVerdict(),
            "run1",
        )
    )
    path = wrj(machine, rdir / "report.jsonl")
    assert path.exists()
    assert not (rdir / "report.tex").exists()


def test_report_formats_empty_skips_report(tmp_path):
    """report_formats=[] means the compose_report node is not reached (route returns END)."""
    from auto_research.graph.runtime import _normalize_report_formats
    resolved = _normalize_report_formats([], ["jsonl", "tex"])
    assert resolved == []


def test_report_formats_normalization():
    from auto_research.graph.runtime import _normalize_report_formats
    assert _normalize_report_formats(None, ["jsonl"]) == ["jsonl"]
    assert _normalize_report_formats("tex", ["jsonl"]) == ["tex"]
    assert _normalize_report_formats(["jsonl", "tex"], []) == ["jsonl", "tex"]
    with pytest.raises(ValueError, match="Unknown report_formats"):
        _normalize_report_formats(["pdf"], [])


def test_error_feedback_loop(tmp_path):
    """Reviewer that returns broken LaTeX on attempt 1 is retried."""
    reviewer = FakeReviewerForReport(latex_attempts=2)
    storage = Storage(tmp_path / "db.sqlite")
    asyncio.run(storage.initialize())
    refs = asyncio.run(build_references(storage, []))
    analyst = FakeAnalystReport()
    verdict = FakeVerdict()
    rdir = tmp_path / "report"
    rdir.mkdir()

    async def _run():
        staged = stage_figures(analyst.figures, rdir)
        feedback = None
        for attempt in range(2):
            latex = await reviewer.compose_latex_report(
                None, analyst, staged, refs, verdict, feedback=feedback
            )
            write_report_latex(latex, rdir / "report.tex")
            errs = validate_latex_refs(latex, staged, refs)
            if not errs:
                return latex, errs
            feedback = errs
        return latex, errs

    latex, errs = asyncio.run(_run())
    assert errs == [], f"Expected clean after retry, got: {errs}"
    assert "Hello world" in latex
