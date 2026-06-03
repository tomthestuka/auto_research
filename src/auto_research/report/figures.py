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
