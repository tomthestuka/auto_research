"""Stage figures as symlinks in the report directory."""

from __future__ import annotations

from pathlib import Path

from auto_research.schemas.figures import FigureRef


def stage_figures(
    figures: list[FigureRef],
    report_dir: Path,
) -> list[tuple[FigureRef, str]]:
    """Symlink each figure into report_dir/figures/; return (figref, relpath) pairs.

    Replaces any stale link with the same name. The relpath is relative to
    report_dir and suitable for use in \\includegraphics{<relpath>}.
    """
    figs_dir = report_dir / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[FigureRef, str]] = []
    for figref in figures:
        src = Path(figref.path).resolve()
        link = figs_dir / src.name
        if link.is_symlink() or link.exists():
            link.unlink(missing_ok=True)
        link.symlink_to(src)
        staged.append((figref, f"figures/{link.name}"))
    return staged


def stage_papers(
    cited_paths: list[Path],
    report_dir: Path,
) -> list[tuple[Path, str]]:
    """Symlink each cited PDF into report_dir/papers/; return (path, relpath) pairs.

    Mirrors :func:`stage_figures`. The relpath (e.g. ``papers/<name>.pdf``) is what the
    reviewer hyperlinks in the report's References section; the resolve gate
    (``validate_report_links``) checks each ``\\href`` target points at one of these files.
    Only existing files are staged, so a dangling link is never offered as valid.
    """
    papers_dir = report_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[Path, str]] = []
    for cited in cited_paths:
        src = Path(cited).resolve()
        if not src.is_file():
            continue
        link = papers_dir / src.name
        if link.is_symlink() or link.exists():
            link.unlink(missing_ok=True)
        link.symlink_to(src)
        staged.append((src, f"papers/{link.name}"))
    return staged
