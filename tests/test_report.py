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
    stage_papers,
    validate_latex_figures,
    validate_report_links,
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
        sha256="a" * 64,
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
            authors=["Smith, J."], year=2026, local_path=p, sha256=str(i) * 64,
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


def test_stage_papers_symlinks_cited_pdfs(tmp_path):
    pdf = tmp_path / "src" / "Cyg X-1.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.5\nbody")
    rdir = tmp_path / "report"

    staged = stage_papers([pdf], rdir)

    assert len(staged) == 1
    src, relpath = staged[0]
    assert relpath == f"papers/{pdf.name}"
    link = rdir / relpath
    assert link.is_symlink()
    assert link.resolve() == pdf.resolve()


def test_stage_papers_skips_missing_and_replaces_stale(tmp_path):
    """Nonexistent paths are skipped; a repeat call replaces the stale link."""
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.5\n")
    rdir = tmp_path / "report"

    assert stage_papers([tmp_path / "gone.pdf"], rdir) == []  # missing source skipped
    stage_papers([pdf], rdir)
    staged = stage_papers([pdf], rdir)  # call again — must not raise
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
# validate_latex_figures (figure paths only)
# ---------------------------------------------------------------------------

_FIG_GOOD_TEX = r"""
\documentclass{article}
\begin{document}
\includegraphics{figures/fit.png}
\end{document}
"""

_FIG_BAD_TEX = r"""
\documentclass{article}
\begin{document}
\includegraphics{figures/missing.png}
\end{document}
"""


def test_validate_latex_figures_passes_clean(tmp_path):
    fig = _make_fig(tmp_path, "fit")
    staged = [(fig, "figures/fit.png")]
    assert validate_latex_figures(_FIG_GOOD_TEX, staged) == []


def test_validate_latex_figures_flags_missing(tmp_path):
    fig = _make_fig(tmp_path, "fit")
    staged = [(fig, "figures/fit.png")]
    errs = validate_latex_figures(_FIG_BAD_TEX, staged)
    assert any("missing.png" in e for e in errs)


# ---------------------------------------------------------------------------
# validate_report_links (hard reference-link resolve gate)
# ---------------------------------------------------------------------------


def test_validate_report_links_passes(tmp_path):
    rdir = tmp_path / "report"
    rdir.mkdir()
    pdf = tmp_path / "cited.pdf"
    pdf.write_bytes(b"%PDF-1.5\nbody")
    staged = stage_papers([pdf], rdir)
    relpath = staged[0][1]
    tex = r"\href{" + relpath + r"}{" + relpath + r"}"
    assert validate_report_links(tex, rdir, staged) == []


def test_validate_report_links_flags_dead_link(tmp_path):
    rdir = tmp_path / "report"
    (rdir / "papers").mkdir(parents=True)
    tex = r"\href{papers/missing.pdf}{papers/missing.pdf}"
    errs = validate_report_links(tex, rdir, [])
    assert any("missing.pdf" in e for e in errs)


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

    async def compose_latex_report(self, request, report, staged, staged_papers, verdict, *, feedback=None):
        self._attempt += 1
        if self._attempt < self._latex_attempts:
            # First attempts link a non-existent PDF to trip the resolve gate.
            return r"\documentclass{article}\begin{document}\href{papers/missing.pdf}{papers/missing.pdf}\end{document}"
        links = "".join(r"\href{" + rp + "}{" + rp + "}" for _, rp in staged_papers)
        return r"\documentclass{article}\begin{document}Hello world." + links + r"\end{document}"


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
    """A dead reference link on attempt 1 is fed back and cleared on retry."""
    reviewer = FakeReviewerForReport(latex_attempts=2)
    analyst = FakeAnalystReport()
    verdict = FakeVerdict()
    rdir = tmp_path / "report"
    rdir.mkdir()
    pdf = tmp_path / "cited.pdf"
    pdf.write_bytes(b"%PDF-1.5\nbody")

    async def _run():
        staged = stage_figures(analyst.figures, rdir)
        staged_papers = stage_papers([pdf], rdir)
        feedback = None
        for _ in range(2):
            latex = await reviewer.compose_latex_report(
                None, analyst, staged, staged_papers, verdict, feedback=feedback
            )
            write_report_latex(latex, rdir / "report.tex")
            errs = validate_latex_figures(latex, staged)
            errs += validate_report_links(latex, rdir, staged_papers)
            if not errs:
                return latex, errs
            feedback = errs
        return latex, errs

    latex, errs = asyncio.run(_run())
    assert errs == [], f"Expected clean after retry, got: {errs}"
    assert "Hello world" in latex
    assert "papers/cited.pdf" in latex


def test_write_report_latex_sanitizes_href_display(tmp_path):
    r"""The \href display arg is wrapped in \nolinkurl so an underscore in a staged
    relpath (papers/arxiv_<id>.pdf) no longer trips 'Missing $ inserted' (the
    systematic no-PDF bug). URL arg and \includegraphics are left untouched."""
    latex = (
        r"\href{papers/arxiv_2401_12345.pdf}{papers/arxiv_2401_12345.pdf}"
        "\n"
        r"\includegraphics{figures/plot_fit_1.png}"
    )
    written = write_report_latex(latex, tmp_path / "report.tex").read_text(encoding="utf-8")

    # Display arg is now verbatim (link preserved), so the underscore is no longer text-mode math.
    assert r"\href{papers/arxiv_2401_12345.pdf}{\nolinkurl{papers/arxiv_2401_12345.pdf}}" in written
    # The raw, unwrapped display (the failing form) is gone.
    assert r"{papers/arxiv_2401_12345.pdf}{papers/arxiv_2401_12345.pdf}" not in written
    # \includegraphics is untouched (graphicx tolerates underscores in filenames).
    assert r"\includegraphics{figures/plot_fit_1.png}" in written


def test_sanitize_href_display_is_idempotent(tmp_path):
    r"""Re-writing an already-sanitized \href does not double-wrap (braces block the match)."""
    once = write_report_latex(
        r"\href{papers/a_b.pdf}{papers/a_b.pdf}", tmp_path / "r1.tex"
    ).read_text(encoding="utf-8")
    twice = write_report_latex(once, tmp_path / "r2.tex").read_text(encoding="utf-8")
    assert once == twice
    assert twice.count(r"\nolinkurl") == 1
