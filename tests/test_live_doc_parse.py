"""Opt-in check that Docling actually recovers a real scientific paper's figures.

    AUTO_RESEARCH_LIVE_DOC_PARSE=1 pytest tests/test_live_doc_parse.py -s

Costs no provider tokens — it is gated because it needs the Docling dependency, ~400 MB of cached
weights, and about 90 seconds of CPU, none of which belong in the default suite.

**Why a live test rather than a fixture.** The claim this module rests on is empirical and about
*real documents*: that rendering-and-cropping recovers a scientific figure where extracting
embedded rasters recovers nothing, because plots are vector drawing commands. A synthetic PDF
would not test that — it would test whatever the fixture happened to embed. Measured on the paper
the science sweep reproduces (Krawczynski et al. 2022, `2022Sci...378..650K`): pypdf finds **zero**
embedded images on 11 of its 15 figure pages, and Docling recovers **all 15** with 14 captions
correctly paired.

Assertions stay on properties that must hold rather than on exact counts, since a Docling upgrade
may legitimately shift a crop or a caption by one. The exact numbers observed on 2026-09-03 are
printed for a human to compare against, which is what makes a silent regression visible.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from auto_research.models.providers import _doc_parse
from auto_research.schemas import DocumentRef

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_DOC_PARSE") != "1",
    reason="set AUTO_RESEARCH_LIVE_DOC_PARSE=1 to spend ~90s of CPU on a real Docling parse",
)

#: A real paper with vector figures. Any scientific PDF exercises the same property; this one is
#: the science sweep's own target, so a regression here is a regression in the thing being measured.
PAPER = Path(
    "/data/fzhou/ar_runtime/ws/fbb93f2600d620b26baa9c71dc5e53d7/runtime/runs/"
    "fbb93f2600d620b26baa9c71dc5e53d7/reviewer/report/papers/nasa_ads_2022Sci...378..650K.pdf"
)


def _document(path: Path) -> DocumentRef:
    return DocumentRef(
        path=path,
        title="Krawczynski et al. 2022",
        mime_type="application/pdf",
        created_at=datetime.now(UTC),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    if not PAPER.is_file():
        pytest.skip(f"reference paper not on this host: {PAPER}")
    root = tmp_path_factory.mktemp("parsed_docs")
    started = time.time()
    result = _doc_parse.parse_document(_document(PAPER), root=root)
    elapsed = time.time() - started
    assert result is not None, "the reference paper must parse"
    print(
        f"\nparsed in {elapsed:.1f}s: {len(result.text):,} chars, "
        f"{len(result.figures)} figures (2026-09-03: 86.0s, 91,006 chars, 15 figures)"
    )
    return root, result


def test_the_vector_figures_are_recovered(parsed):
    """The whole point. pypdf finds embedded rasters on 4 of 15 figure pages; a render-and-crop
    parser must do far better than that or this dependency is not earning its place."""
    _root, result = parsed
    assert len(result.figures) >= 10, (
        "far fewer figures than the 15 observed — extraction has regressed toward the "
        "embedded-raster behaviour this module exists to avoid"
    )
    for figure in result.figures:
        assert figure.path.is_file()
        assert figure.path.stat().st_size > 5_000, f"{figure.path.name} is too small to be a plot"


def test_most_figures_carry_their_real_caption(parsed):
    """Docling links a picture to its caption in the document graph, so a crop should arrive
    labelled rather than matched by proximity afterwards. Not all: caption detection is
    heuristic at the edges, and one figure went unlabelled on the reference paper."""
    _root, result = parsed
    labelled = [f for f in result.figures if "no caption detected" not in f.caption]
    assert len(labelled) >= len(result.figures) - 3
    assert any(
        f.caption.lower().startswith(("figure", "fig.")) for f in labelled
    ), "captions should be the paper's own figure captions"


def test_the_text_carries_the_paper_headline_number(parsed):
    """A reviewer compares measured values against the paper's, so the numbers have to survive
    extraction — this is the one that would make the whole substitution useless if it failed."""
    _root, result = parsed
    condensed = result.text.replace(" ", "")
    assert "4.01" in condensed and "0.20" in condensed, (
        "the published 2-8 keV polarization degree must survive text extraction"
    )


def test_a_second_parse_is_served_from_cache(parsed):
    """The cache is what makes this affordable in a ReAct loop that re-sends its attachments
    every turn: a ~90s parse must happen once per document, not once per turn."""
    root, first = parsed
    started = time.time()
    again = _doc_parse.parse_document(_document(PAPER), root=root)
    elapsed = time.time() - started
    assert again is not None
    assert elapsed < 5.0, f"cache hit took {elapsed:.1f}s; it should be a file read"
    assert len(again.figures) == len(first.figures)
    assert again.text == first.text


def test_the_manifest_stamps_what_produced_the_crops(parsed):
    """A benchmark reads these artifacts long after the run. A figure whose provenance is
    'some version of some parser' is not evidence."""
    import json

    root, _result = parsed
    manifest = json.loads(
        next(root.glob("*/manifest.json")).read_text(encoding="utf-8")
    )
    assert manifest["parser"].startswith("docling")
    assert manifest["images_scale"] == _doc_parse.IMAGES_SCALE
    assert manifest["sha256"] and manifest["figures"]
