"""Stage cited figure receipts into the report directory."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from auto_research.provenance import ReceiptLookup
from auto_research.schemas.artifacts import Receipt


def stage_figure_receipts(
    handles: Sequence[str],
    lookup: ReceiptLookup,
    report_dir: Path,
) -> tuple[list[tuple[Receipt, str]], list[str]]:
    """Copy each cited figure receipt into ``report_dir/figures/<sha12><ext>``, re-hashed first.

    Returns ``(staged, problems)``: ``(receipt, relpath)`` pairs in citation order, and one sentence
    per handle that cannot be placed. Two fixes in one ([D4]+[D5]): the staged name is the content
    hash, so two figures sharing a basename never collide, and the bytes are verified against the
    receipt before they are copied, so a figure whose file changed or vanished after it was produced
    never reaches the report. A figure cited twice is placed once.
    """
    figs_dir = report_dir / "figures"
    staged: list[tuple[Receipt, str]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for handle in handles:
        if handle in seen:
            continue
        seen.add(handle)
        receipt = lookup.get(handle)
        if receipt is None:
            problems.append(f"figures lists {handle}, which is not a receipt you can cite.")
            continue
        if receipt.kind != "figure":
            problems.append(
                f"figures lists {handle}, a {receipt.kind}, not a figure. List figure receipts only."
            )
            continue
        mismatch = lookup.verify(handle)
        if mismatch:
            problems.append(f"figures lists {handle}: {mismatch} Cite a different figure.")
            continue
        source = Path(receipt.path)
        target = figs_dir / f"{receipt.sha256[:12]}{source.suffix.lower()}"
        if not target.is_file():
            figs_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        staged.append((receipt, f"figures/{target.name}"))
    return staged, problems
