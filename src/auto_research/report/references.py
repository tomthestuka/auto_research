"""Build Reference lists from run literature paths and Storage metadata."""

from __future__ import annotations

import re
from pathlib import Path

from auto_research.schemas.report import Reference
from auto_research.storage import Storage


def _stable_key(authors: list[str], year: int | None, stem: str) -> str:
    """Derive a stable BibTeX-style cite key from first author surname + year."""
    if authors:
        surname = re.sub(r"[^a-zA-Z]", "", authors[0].split()[-1]).lower()
    else:
        surname = re.sub(r"[^a-zA-Z0-9]", "", stem[:20]).lower()
    return (surname + str(year)) if year else (surname or stem[:20])


async def build_references(
    storage: Storage,
    literature_paths: list[Path],
) -> list[Reference]:
    """Map run literature paths to Reference objects via storage metadata.

    Papers not found in storage fall back to the file stem for title/key.
    Duplicate cite keys are disambiguated with letter suffixes (a, b, …).
    """
    await storage.initialize()
    papers = await storage.list_papers()
    by_path: dict[str, object] = {
        str(Path(p.local_path).resolve()): p
        for p in papers
        if p.local_path
    }

    refs: list[Reference] = []
    seen_keys: set[str] = set()

    for lit_path in literature_paths:
        paper = by_path.get(str(lit_path.resolve()))
        if paper:
            base_key = _stable_key(paper.authors, paper.year, lit_path.stem)
        else:
            base_key = re.sub(r"[^a-zA-Z0-9]", "", lit_path.stem[:20]).lower() or "ref"

        key = base_key
        suffix = ord("a")
        while key in seen_keys:
            key = base_key + chr(suffix)
            suffix += 1
        seen_keys.add(key)

        refs.append(
            Reference(
                key=key,
                title=paper.title if paper else lit_path.stem,
                authors=paper.authors if paper else [],
                year=paper.year if paper else None,
                source=paper.source if paper else None,
                local_path=lit_path,
            )
        )
    return refs
