"""Opt-in LIVE probe: can each demonstration seat actually hold its seat?

    AUTO_RESEARCH_LIVE_SEAT_PROBE=1 pytest tests/test_live_seat_probe.py -s

**Why this exists.** The demonstration seats three members no configuration names — a
``gpt-5.6-terra`` lead, a ``qwen3.8-max`` reviewer and a ``qwen3.7-plus`` worker — and each is one
tier below the incumbent on every independent index. Two things could go wrong, and only one of
them is worth measuring. A model that reasons worse is *the point*: the demonstration exists to
show what the harness does when its models are weaker. A model that cannot receive a figure, or
cannot receive a paper, or cannot drive a tool loop at all, is **broken plumbing wearing the
costume of a weak model**, and it would corrupt three trials before anyone noticed. This separates
those before the batch spends anything.

The 2026-08-20 lesson binds the design: a probe passed while the coder could not execute a single
command, because every *stage* was reachable while one *actor* was hobbled. So each cell names the
capability that seat uses and checks that seat did it — reviewer reads a paper and its figures,
lead sustains a loop and files a form, worker calls a tool and cites a figure.

**Seats arrive through the configuration pointer, not by hand-built handles.** ``tests.seats``
writes a whole configuration directory with ``models.yaml`` overridden and every other file copied
byte for byte, so each role resolves its member through the ordinary production path with the media
guard, key rotation, retry and prompt loading all in front of it. The pinned reasoning effort comes
from the sweep's own roster, so this probes the batch's request rather than a vendor default.

**Assertions stay on our side of the boundary** (CLAUDE.md §10): what the request carried, that a
loop terminated, that a form validates, that a discriminator only the real channel could supply
came back. Answer *quality* is never asserted — every cell lands under ``logs/dev/seat-probe/``
for a human to read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from tests.dev_logs import dev_log_path
from tests.seats import from_seat, seated

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_SEAT_PROBE") != "1",
    reason="set AUTO_RESEARCH_LIVE_SEAT_PROBE=1 to spend provider tokens on the seat probe",
)

load_dotenv()

#: The paper the reviewer must read: the reference the sweep reproduces, with vector figures.
PAPER = Path(
    "/data/fzhou/ar_runtime/ws/fbb93f2600d620b26baa9c71dc5e53d7/runtime/runs/"
    "fbb93f2600d620b26baa9c71dc5e53d7/reviewer/report/papers/nasa_ads_2022Sci...378..650K.pdf"
)

#: Real tool output, 2683 px wide — above Anthropic's many-image dimension threshold, so the media
#: guard is genuinely exercised rather than merely passed by.
FIGURE_SOURCE = Path("/data/fzhou/ar_runtime/test_runtime/runs/8c75990e862ca45993bea6076a92115c")


def _record(cell: str, payload: dict[str, Any]) -> Path:
    path = dev_log_path("seat-probe", cell, ".json")
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\n[seat-probe] {cell} -> {path}")
    return path


def _figures(limit: int = 2) -> list:
    from auto_research.schemas import FigureRef

    paths = sorted(FIGURE_SOURCE.glob("analyst_worker_*/figures/*.png"))[:limit]
    if not paths:
        pytest.skip(f"no real figures under {FIGURE_SOURCE}")
    return [
        FigureRef(
            path=str(path),
            caption=f"{path.stem} (real tool output)",
            mime_type="image/png",
            produced_by="xspec_utils.lib.qu_lag_dcf_plot.qu_lag_dcf_plot",
            created_at=datetime.now(UTC),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in paths
    ]


def _document():
    from auto_research.schemas import DocumentRef

    if not PAPER.is_file():
        pytest.skip(f"reference paper not on this host: {PAPER}")
    return DocumentRef(
        path=PAPER,
        title="Krawczynski et al. 2022",
        mime_type="application/pdf",
        sha256=hashlib.sha256(PAPER.read_bytes()).hexdigest(),
        created_at=datetime.now(UTC),
    )


def _demo_seats():
    """The demonstration's contrast tree, straight off the sweep roster."""
    from benchmarks.science_bench import arms

    lead, reviewer, workers, _ = arms.DEMO_TREES[1]
    return (
        arms.BY_LEAD[lead],
        arms.BY_REVIEWER[reviewer],
        arms.BY_WORKER_PAIR[workers],
    )


class _Recording:
    """Forwards to a real handle and records what each request carried.

    The attachment checks have to be about the **request**: a test reading only the response cannot
    see a parameter silently dropped. The guard runs below here, so a call that returns at all is
    one the guard accepted.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self.config = getattr(handle, "config", None)
        self.role = getattr(handle, "role", "")
        self.requests: list[dict] = []

    async def call(self, messages, **kwargs):
        self.requests.append(
            {
                "images": len(kwargs.get("images") or ()),
                "documents": len(kwargs.get("documents") or ()),
                "messages": len(messages),
                "tools": [t["name"] for t in (kwargs.get("tools") or [])],
                "schema": getattr(kwargs.get("schema"), "__name__", None),
            }
        )
        return await self._handle.call(messages, **kwargs)


# ── cell 1: the reviewer reads a paper and sees its figures ─────────────────────────────


class PaperRead(BaseModel):
    """What a reviewer must be able to recover from a paper it was handed."""

    received_text: bool
    received_images: bool
    image_count: int
    published_polarization_degree: str
    """Exists only in the extracted text."""
    first_figure_chart_type: str
    first_figure_colors: str
    """Exists only in the pixels: the captions never name the colours, and the figure is a vector
    polar plot that yields no embedded raster, so an answer here requires a rendered crop."""
    scientific_critique: str
    """Free text. Recorded for a human, never asserted — this is the ability the seat is chosen
    for and the one thing a test must not grade."""


def test_the_reviewer_seat_receives_a_paper_as_text_and_figures():
    """Alibaba publishes no document channel this project can use, so the reviewer's paper is
    assembled locally into the two channels Anthropic and OpenAI build internally. If that glue is
    broken, four trials review a paper they never read — and it would look like a weak reviewer.
    """
    from auto_research.models import load_registry

    _, reviewer, _ = _demo_seats()
    document = _document()

    with seated(reviewer=from_seat(reviewer)):
        handle = load_registry().resolve("reviewer").handles[0]
        assert handle.config.model == reviewer.model
        recording = _Recording(handle)
        ask = (
            "You are handed one scientific paper. Set received_images true ONLY if actual images "
            "reached you, and received_text true ONLY if extracted prose reached you. Report "
            "image_count. For the FIRST figure give its chart type and the colours of its data "
            "series. Separately quote the published 2-8 keV polarization degree with its "
            "uncertainty exactly as the text gives it. Then give a short scientific critique of "
            "the paper's central measurement. Ground every field in what you actually received, "
            "and say so plainly where something is absent."
        )
        from auto_research.models.types import ModelMessage

        response = asyncio.run(
            recording.call(
                [ModelMessage(role="user", content=ask)],
                schema=PaperRead,
                documents=[document],
            )
        )

    read = PaperRead.model_validate(response.parsed)
    _record(
        "reviewer_paper",
        {"member": reviewer.model, "requests": recording.requests, "read": read.model_dump()},
    )

    assert recording.requests[0]["documents"] == 1, "the document never reached the request"
    # The text channel: a number that exists only in the extracted prose.
    assert "4.0" in read.published_polarization_degree, (
        f"the paper's headline value did not survive into the model's view: "
        f"{read.published_polarization_degree!r}"
    )
    # The image channel: colours no caption names, on a vector figure with no embedded raster.
    assert read.received_images, "the model reports receiving no images"
    colours = read.first_figure_colors.lower()
    assert sum(c in colours for c in ("red", "green", "blue", "orange", "black")) >= 2, (
        f"figure colours not recovered, so the crops did not land: {read.first_figure_colors!r}"
    )
    assert read.scientific_critique.strip(), "no critique returned"


# ── cell 2: the lead sustains a real cycle and files a form ─────────────────────────────


def test_the_lead_seat_sustains_a_real_cycle_and_files_a_form(tmp_path, monkeypatch):
    """The lead's loop is the longest in the pipeline and the one a weak agentic model fails at.

    Driven through the **real** ``run_cycle`` on the real prompt with the real frozen stimulus, so
    what is exercised is the loop this seat will actually run. The coding session is stubbed: a
    real chain link costs $27-45 and the loop above it is what is under test.
    """
    from auto_research.agents.react import AUTHOR_CHAIN_TOOL, HOLD_AUTHORING_TOOL
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.graph.runtime import readonly_roots_for, readonly_tools_for
    from auto_research.models import load_registry
    from auto_research.schemas import AnalysisTask, AnalystReport, MergeFlowResult

    fixture = Path(__file__).parent / "fixtures" / "lead_cycle_reports.json"
    if not fixture.is_file():
        pytest.skip(f"the frozen lead stimulus is not on this host: {fixture}")
    reports = [
        AnalystReport.model_validate(row["report"])
        for row in json.loads(fixture.read_text(encoding="utf-8"))["reports"]
    ]

    driven: list = []

    async def _fake_link(self, messages, provider, spec, **kwargs):
        driven.append(spec)
        return MergeFlowResult(
            tool_spec_id=spec.id,
            status="committed",
            branch="ai",
            commit_sha="0123456789ab",
            reason="stubbed coding session (the loop above it is under test)",
        )

    monkeypatch.setattr(AnalystLeadAgent, "_drive_one_link", _fake_link)

    lead_seat, _, _ = _demo_seats()
    figures = _figures()

    with seated(analyst_lead=from_seat(lead_seat)):
        handle = load_registry().resolve("analyst_lead").handles[0]
        assert handle.config.model == lead_seat.model
        recording = _Recording(handle)
        # The role, its prompt and its tool loop all come from production. `AnalystLeadAgent()`
        # loads `prompts/analyst_lead.md` itself, and `readonly_tools_for` is the same call
        # `run_research` makes — a hand-built provider here differed from it by five arguments.
        lead = AnalystLeadAgent(model=recording)
        lead.agent_key = "analyst_lead"
        lead.report_dir = tmp_path
        toolkit_root = configured_toolkit_root()
        lead.tools = readonly_tools_for(
            "analyst_lead",
            roots=[tmp_path, *readonly_roots_for("seat-probe", toolkit_root)[1:]],
            retrieval=None,
            web_source=None,
            human_handler=None,
        )
        lead._figure_catalog["analyst_worker_0"] = list(figures)
        outcome = asyncio.run(
            lead.run_cycle(
                AnalysisTask(
                    question=(
                        "Reproduce the standard IXPE polarimetry result for Cyg X-1 from the "
                        "local observations, per-DU and combined."
                    ),
                    instructions="The workers have reported. Settle the toolkit question, then judge.",
                ),
                reports,
                sources=["analyst_worker_0", "analyst_worker_1"],
                rounds_left=2,
                author_budget=3,
                toolkit_root=toolkit_root,
                worktree_base=toolkit_root,
                direct_branch="ai",
            )
        )

    _record(
        "lead_cycle",
        {
            "member": lead_seat.model,
            "requests": recording.requests,
            "filed": outcome.filed,
            "links_driven": len(driven),
            "tool_calls": len(recording.requests),
            "decision": outcome.decision.model_dump() if outcome.decision else None,
        },
    )

    assert outcome.filed is not None, (
        "the lead ended its cycle without filing any form — this seat cannot drive the loop"
    )
    assert outcome.filed in (
        AUTHOR_CHAIN_TOOL,
        HOLD_AUTHORING_TOOL,
        "submit_terminal_report",
        "request_another_round",
        "submit_adjudication",
    )
    # It must have used the loop, not answered from the seed turn alone.
    assert len(recording.requests) >= 2, "the lead filed on its first turn without investigating"
    # The figures must have reached a request: this seat is on a figure-bearing axis.
    assert any(request["images"] for request in recording.requests), (
        "no request carried a figure, so the lead judged its workers' plots blind"
    )
    if outcome.filed == AUTHOR_CHAIN_TOOL:
        assert outcome.decision is not None and outcome.decision.tool_specs
        for spec in outcome.decision.tool_specs:
            assert spec.target_domain and spec.disposition and spec.validation_data


# ── cell 3: the worker calls a real tool and sees a figure ──────────────────────────────


def test_the_worker_seat_drives_a_tool_loop_and_sees_a_figure(tmp_path):
    """The smallest member of the tree, in the seat that does the reduction and reads the plots.

    ``qwen3.7-plus`` has no current independent agentic benchmark, so whether it sustains a ReAct
    loop with the real toolkit catalogue resident is unmeasured — and that is exactly the failure
    that would read as a weak worker rather than as an unusable one. Alibaba has no provider-side
    tool search, so its whole catalogue is resident, which is a stated difference from the
    incumbent pair rather than a defect.
    """
    from auto_research.agents.react import EDIT_REPORT_TOOL, TURN_END_TOOL
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.graph.runtime import attach_worker_runtime
    from auto_research.models import load_registry
    from auto_research.sandbox.mcp_client import MCPToolClient
    from auto_research.sandbox.registry import ToolkitRegistry
    from auto_research.sandbox.workspace import create_run_workspace
    from auto_research.schemas import AnalysisTask, AnalystReport

    _, _, pair = _demo_seats()
    worker_seat = next(m for m in pair.members if m.provider == "dashscope")
    figures = _figures(limit=1)

    toolkit_root = configured_toolkit_root()
    registry = ToolkitRegistry(toolkit_root)
    try:
        registry.load()
    except Exception as exc:  # noqa: BLE001 — a science-stack absence is not this probe's subject
        pytest.skip(f"toolkit registry did not load on this host: {type(exc).__name__}: {exc}")

    with seated(analyst_worker=from_seat(worker_seat)):
        handle = load_registry().resolve("analyst_worker").handles[0]
        assert handle.config.model == worker_seat.model
        recording = _Recording(handle)
        # Production's own wiring, not a look-alike: `AnalystWorkerAgent.analyze` runs its inner
        # ReAct tool loop ONLY when a sandbox and a registry are attached, and otherwise falls
        # back to a plain structured call. A hand-built worker therefore never calls a tool, and a
        # probe on one cannot tell a model that will not drive a loop from a model never given one.
        # `attach_worker_runtime` is the same call `run_research` makes.
        run_id = "seat-probe"
        create_run_workspace(run_id, ["analyst_worker_0"])
        worker = AnalystWorkerAgent(model=recording, worker_index=0)
        mcp_client = MCPToolClient(servers=[])
        # `sandbox_settings` defaults to the configured ones, so the REPL this worker gets is the
        # REPL production gives it. Passing nothing here previously built a sandbox that refused
        # to start, which is the right failure and the reason the settings live in the factory.
        sandboxes = attach_worker_runtime(
            [worker], run_id=run_id, registry=registry, toolkit_root=toolkit_root,
            mcp_client=mcp_client,
        )
        assert worker.sandbox is not None and worker.registry is not None, (
            "the production wiring did not attach a sandbox and a registry"
        )
        task = AnalysisTask(
            question=(
                "Before any reduction, inventory what is available: list the IXPE observation "
                "directories the data catalogue knows about, and state which tool you would call "
                "to produce a 2-8 keV polarization cube and what arguments it takes. Then look at "
                "the attached figure from a previous run and say what it plots."
            ),
            instructions=(
                "Use your read-only tools to answer from what is on disk. Report honestly if a "
                "capability is missing."
            ),
        )
        # A peer's figures reach a worker on that peer's *report*, which is the production
        # channel: `analyze` pulls `report.figures` off every peer report it is handed. Passing
        # them any other way would exercise a path production does not have.
        peer = AnalystReport(
            summary="A previous round produced this diagnostic plot for the same source.",
            figures=list(figures),
            status="succeeded",
        )
        try:
            report = asyncio.run(worker.analyze(task, peer_reports=[peer]))
        finally:
            for sandbox in sandboxes:
                try:
                    asyncio.run(sandbox.shutdown())
                except Exception:  # noqa: BLE001 — teardown never fails a measured cell
                    pass

    _record(
        "worker_loop",
        {
            "member": worker_seat.model,
            "requests": recording.requests,
            "status": report.status,
            "summary": report.summary,
            "findings": report.findings,
            "tools_declared": recording.requests[0]["tools"] if recording.requests else [],
        },
    )

    # Per-actor capability, not stage coverage: this seat must have called a tool.
    assert len(recording.requests) >= 2, (
        "the worker answered from the seed turn without calling a single tool"
    )
    assert recording.requests[0]["tools"], "no tools were declared to the worker"
    assert any(request["images"] for request in recording.requests), (
        "no request carried the peer figure, so this seat would judge plots blind"
    )
    assert report.status in ("succeeded", "failed")  # F9: agent-declared, two values
    assert report.summary.strip(), "the worker produced no report body"

    # ── F5c/F5d/F9, added 2026-09-09 ────────────────────────────────────────
    #
    # `turn_end` must be OFFERED (F5c): it is how a worker's loop ends now, and a loop with no
    # terminal tool has no way to finish except by falling silent.
    assert TURN_END_TOOL in recording.requests[0]["tools"], (
        f"turn_end was not declared to the worker; it had {recording.requests[0]['tools']}"
    )
    assert EDIT_REPORT_TOOL in recording.requests[0]["tools"]

    # **NO REQUEST CARRIED AN OUTPUT SCHEMA** — this is the F5d assertion, and it is the precise
    # one. The deleted fallback compose was a structured call, so a schema on any request means
    # the harness composed a report on the model's behalf. That path was the only one that sent a
    # worker the runtime-owned `figures`/`FigureRef` tree, which `_sanitize_strict` promoted to
    # *required*, which is how four invented `placeholder_*` digests killed slot 5 one line before
    # they would have been discarded. If it is gone, no request has a schema.
    schemas = [r["schema"] for r in recording.requests if r["schema"]]
    assert not schemas, f"a report was composed on the model's behalf via schema(s) {schemas}"

    # Whether this seat actually ends on the terminal is model behaviour, so it is RECORDED and
    # only its consequence is asserted: a report exists iff the model wrote one with edit_report,
    # because nothing writes one for it. A worker that never wrote one gets the honest `failed`.
    _record("worker_turn_end", {
        "member": worker_seat.model,
        "turn_end_declared": TURN_END_TOOL in recording.requests[0]["tools"],
        "status": report.status,
        "summary_chars": len(report.summary or ""),
        "figures_cited": len(report.figures or []),
        "requests": len(recording.requests),
    })
    if report.status == "failed" and not (report.figures or []):
        # Permitted and informative: the seat did not complete the work, which under F9 is a
        # correct outcome it declares for itself rather than one the harness infers.
        assert report.summary.strip(), "an honest failure must still say what blocked it"


def test_the_reviewer_seat_authors_a_compiling_tex_by_editing_it(tmp_path):
    """F5e: the reviewer drafts `report.tex` with `file_edit` and ends on a validating `turn_end`.

    This replaced a recompose loop that asked for a whole `LatexReport` through structured output
    on every attempt and fed the previous attempt's compile errors back as prose. Two reasons it
    needs a live cell rather than a deterministic one:

    1. **Model behaviour.** Whether a seat can draft valid LaTeX *by editing a file* — rather than
       by emitting a document in one shot — is unmeasured, and it is the reviewer's whole job now.
    2. **The contrast arm's reviewer is the model recorded as failing the path this removes.**
       `ResearchReport`/`LatexReport` failed on `qwen3.8-max` at every reasoning effort
       (2026-08-24: unresolved `$ref` in `list[Model]` fields, so it invented a shape and returned
       HTTP 200). If the editing path works on that seat, the change earns its place on evidence.

    The validator here is the same closure `graph.builder.compose_report_node` builds, and the
    provider is built by `readonly_tools_for` — production's own wiring, so a pass means the
    reviewer's real surface worked, not a look-alike's.
    """
    from auto_research.agents.react import FILE_EDIT_TOOL, TURN_END_TOOL
    from auto_research.agents.roles import ReviewerAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.graph.runtime import readonly_roots_for
    from auto_research.models import load_registry
    from auto_research.report import (
        stage_figures,
        stage_papers,
        validate_latex_figures,
        validate_report_links,
    )
    from auto_research.schemas import AnalystReport, ResearchRequest, ReviewVerdict

    _, reviewer_seat, _ = _demo_seats()
    figures = _figures(limit=1)

    rdir = tmp_path / "report"
    rdir.mkdir()
    pdf = tmp_path / "cited.pdf"
    pdf.write_bytes(b"%PDF-1.5\nbody")
    staged = stage_figures(list(figures), rdir)
    staged_papers = stage_papers([pdf], rdir)
    tex = rdir / "report.tex"

    validations: list[list[str]] = []

    def problems() -> list[str]:
        """Byte-for-byte the check `compose_report_node` installs, minus the pdflatex call.

        `compile_latex` is dropped ON PURPOSE and the omission is stated: pdflatex is absent on
        some hosts, and a probe that skips there would measure the host rather than the seat. The
        two reference gates are the ones a model can actually get wrong.
        """
        if not tex.is_file():
            errs = [f"{tex.name} has not been written yet — draft it with file_edit."]
        else:
            latex = tex.read_text(encoding="utf-8")
            errs = validate_latex_figures(latex, staged)
            errs += validate_report_links(latex, rdir, staged_papers)
        validations.append(list(errs))
        return errs

    with seated(reviewer=from_seat(reviewer_seat)):
        handle = load_registry().resolve("reviewer").handles[0]
        assert handle.config.model == reviewer_seat.model
        recording = _Recording(handle)
        reviewer = ReviewerAgent(model=recording)
        toolkit_root = configured_toolkit_root()
        report = AnalystReport(
            summary="Cyg X-1 2-8 keV polarization measured from the May 2022 IXPE observation.",
            findings=["PD = 3.41 +/- 0.14 percent", "PA = -20.3 +/- 1.2 deg"],
            figures=list(figures),
            status="succeeded",
        )
        asyncio.run(reviewer.author_latex_report(
            ResearchRequest(question="Reproduce the Cyg X-1 May-2022 IXPE polarization."),
            report, staged, staged_papers,
            ReviewVerdict(accepted=True, rationale="Values are consistent with the archive."),
            tex_path=tex,
            validator=problems,
            roots=[rdir, *readonly_roots_for("seat-probe", toolkit_root)[1:]],
            cap=8,
        ))

    _record("reviewer_tex", {
        "member": reviewer_seat.model,
        "requests": recording.requests,
        "validations": validations,
        "tex_chars": len(tex.read_text(encoding="utf-8")) if tex.is_file() else 0,
        "tex_head": (tex.read_text(encoding="utf-8")[:400] if tex.is_file() else ""),
    })

    # The tools must have been offered: this seat cannot do the job without them.
    assert recording.requests, "the reviewer was never called"
    declared = recording.requests[0]["tools"]
    assert FILE_EDIT_TOOL in declared, f"file_edit was not declared; got {declared}"
    assert TURN_END_TOOL in declared, f"turn_end was not declared; got {declared}"

    # No output schema anywhere: F5e's point is that this path no longer composes a document
    # through structured output, which is the path this very seat is recorded as failing.
    schemas = [r["schema"] for r in recording.requests if r["schema"]]
    assert not schemas, f"the reviewer still composed through a schema: {schemas}"

    # Per-actor capability: it wrote the file, and the harness validated what it wrote.
    assert tex.is_file(), "the reviewer never wrote report.tex"
    assert validations, "turn_end was never reached, so nothing was validated"
    body = tex.read_text(encoding="utf-8")
    assert "\\documentclass" in body, "what was written is not a LaTeX document"
    # Model quality is recorded, not asserted: whether it cleared every reference gate is the
    # measurement. A residual error is a real result for a weaker seat, not a broken harness.
    if validations[-1]:
        pytest.skip(
            f"{reviewer_seat.model} left {len(validations[-1])} validation error(s) after "
            f"{len(validations)} attempt(s): {validations[-1][:2]} — recorded"
        )
