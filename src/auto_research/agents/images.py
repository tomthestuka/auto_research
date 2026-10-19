"""The image budget at a turn boundary (B4, human rulings B3/B6/B8 of 2026-10-18).

A figure stays in a conversation at the turn that produced it, and within a turn the conversation
only grows at its end (ruling B6): every figure a turn made rides every later call of that turn,
bounded only by the provider's own ceilings (count, size, bytes — `models/providers/_media.py`) and
the turn's compaction. At a turn boundary the next turn's opening transcript carries the newest
figure sets of the earlier turns (:func:`keep_newest`):

* each image costed at ``⌈w/28⌉·⌈h/28⌉`` tokens after the provider's own downscale, at most
  :data:`MAX_IMAGE_TOKENS` (platform.claude.com/docs/en/build-with-claude/vision, "Resolution and
  token cost");
* every set while they fit :data:`IMAGE_BUDGET_TOKENS`; past it, the oldest sets go until the rest
  fit :data:`IMAGE_LOW_WATER_TOKENS` (ruling B8), no set exempt (ruling B3), sets kept or dropped
  whole; the same with :data:`IMAGE_BUDGET_COUNT` / :data:`IMAGE_LOW_WATER_COUNT` images. A
  dropped figure is named by its handle in the transcript's index lines, and ``view_receipt``
  shows it again.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from auto_research.schemas import FigureRef

#: Image tokens a request carries at most, newest sets first (master plan B4).
IMAGE_BUDGET_TOKENS = 40_000
#: Where an eviction stops: the kept total after evicting. Below the budget so each break of the
#: cached prefix buys back about half of it (human ruling, board B8).
IMAGE_LOW_WATER_TOKENS = 20_000
#: Images a kept set holds at most, and where a count eviction stops (R7 review): small images cost
#: few tokens, so the token budget alone let a set crowd a provider's per-request image count.
IMAGE_BUDGET_COUNT = 40
IMAGE_LOW_WATER_COUNT = 20
#: Cost assumed for an image whose size cannot be read (a missing or unreadable file).
UNREADABLE_IMAGE_TOKENS = 1_600
#: The largest image a provider processes: Anthropic's high-resolution tier, "Claude 4.7 and later
#: models: max long edge 2576 px, max 4784 visual tokens"; a larger image is downscaled before it
#: is processed (platform.claude.com/docs/en/build-with-claude/vision, "Resolution and token cost").
#: The upper bound of what one image can cost, whichever vendor serves it.
MAX_LONG_EDGE = 2576
MAX_IMAGE_TOKENS = 4784


def image_tokens(path: Path | str) -> int:
    """``⌈w/28⌉·⌈h/28⌉`` for the image at ``path`` after the provider's own downscale (its header
    only is read), at most :data:`MAX_IMAGE_TOKENS`."""
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return UNREADABLE_IMAGE_TOKENS
    return _tokens_cached(str(target), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4096)
def _tokens_cached(path: str, mtime_ns: int, size: int) -> int:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 — an unreadable figure is costed, never fatal
        return UNREADABLE_IMAGE_TOKENS
    scale = min(1.0, MAX_LONG_EDGE / max(width, height, 1))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    return min(MAX_IMAGE_TOKENS, math.ceil(width / 28) * math.ceil(height / 28))


def _cost(figures: Sequence[FigureRef]) -> int:
    return sum(image_tokens(f.path) for f in figures)


def keep_newest(sets: Sequence[Sequence[FigureRef]]) -> list[FigureRef]:
    """At a turn boundary (ruling B6.3): the figures of the newest sets the next turn carries.

    All of them while they fit :data:`IMAGE_BUDGET_TOKENS`; past it, the oldest sets go until the
    rest fit :data:`IMAGE_LOW_WATER_TOKENS` (ruling B8). Sets are kept or dropped whole.
    """
    costs = [_cost(figs) for figs in sets]
    total = sum(costs)
    count = sum(len(figs) for figs in sets)
    start = 0
    if total > IMAGE_BUDGET_TOKENS or count > IMAGE_BUDGET_COUNT:
        while start < len(sets) and (
            total > IMAGE_LOW_WATER_TOKENS or count > IMAGE_LOW_WATER_COUNT
        ):
            total -= costs[start]
            count -= len(sets[start])
            start += 1
    return [figure for figs in sets[start:] for figure in figs]
