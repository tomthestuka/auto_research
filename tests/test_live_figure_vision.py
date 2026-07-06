"""Opt-in LIVE check that models actually SEE figures — not the path text.

Confirms the [FIG-VIEW] + [V2] fix against real provider APIs. Before the fix every
adapter serialized a figure as the line ``"Figure reference: <path>"``, so every
"vision" review of a plot was blind. Here we hand real run figures to the roles that
review plots and require each to report it actually saw an image:

- **analyst worker / analyst lead / analyst co-lead (the code reviewer)** through our
  provider adapters, exercising BOTH figure paths:
    1. the call-time ``images=`` channel (cross-agent figures — a peer's/worker's plot);
    2. the inline-in-history ``ToolResultPart.figures`` path (a tool's own figure), driven
       as a simulated author→analyze→debate trajectory.
- **the coder** through the Claude Agent SDK's ``query()`` image transport (the coder's
  own path — distinct from our adapters). Note: the current authoring flow does not yet
  route figures to the coder; this validates the SDK *can* carry one if we wire it (a
  tracked follow-up), using the real ``coder`` model.

Figures are the real plots from the first successful end-to-end run (46ed6671…), the
same PNGs ``report.tex`` references. Override the directory with
``AUTO_RESEARCH_FIGURE_TEST_DIR``.

Run with:
    AUTO_RESEARCH_LIVE_FIGURE_TESTS=1 pytest tests/test_live_figure_vision.py -s
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from auto_research.models import load_registry
from auto_research.models.types import (
    AssistantToolUse,
    ModelMessage,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.schemas import FigureRef
from tests.dev_logs import dev_log_path


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_FIGURE_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_FIGURE_TESTS=1 to spend provider tokens",
)

# The real PA/PD-vs-time diagnostic from run 46ed6671… (the PNG report.tex includes).
_FIG_DIR = Path(
    os.getenv(
        "AUTO_RESEARCH_FIGURE_TEST_DIR",
        "/data/fzhou/ar_runtime/test_runtime/runs/"
        "46ed6671484349a99cc58c8c0d12ccda/reviewer/report/figures",
    )
)
_FIG_NAME = "stability_plot_1782983803257_pa_pd_timeseries.png"

# Roles that review plots and are configured for vision.
_VISION_ROLES = ("analyst_worker", "analyst_lead", "analyst_colead")

# A deliberately NON-revealing caption: it never says the chart is a time series, so a
# model that only received the path text cannot fake `chart_type` — it has to see pixels.
_CAPTION = "IXPE polarization diagnostic panel"

_ASK = (
    "You are shown exactly one figure. Set sees_figure=true ONLY if an actual image is "
    "present; if you received only a file path or a text reference and no image, set "
    "sees_figure=false. Then state chart_type (e.g. 'time series', 'histogram', "
    "'scatter'), describe the axes and what the data shows in described_content, and give "
    "a one-line critique. Ground every field in what is actually visible."
)


class FigureVision(BaseModel):
    sees_figure: bool
    chart_type: str
    described_content: str
    critique: str


def _figure() -> FigureRef:
    path = _FIG_DIR / _FIG_NAME
    if not path.exists():
        pytest.skip(f"figure fixture not found: {path} (set AUTO_RESEARCH_FIGURE_TEST_DIR)")
    data = path.read_bytes()
    return FigureRef(
        path=path,
        caption=_CAPTION,
        mime_type="image/png",
        produced_by="ixpe_pa_pd_stability_plot",
        created_at=datetime(2026, 7, 2),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _vision_members():
    registry = load_registry()
    for role in _VISION_ROLES:
        for handle in registry.resolve(role).handles:
            if handle.config.vision:
                yield role, handle


def _log(rows: list, log_path: Path, *, role: str, member: str, path: str, vision: FigureVision):
    rows.append(
        {
            "role": role,
            "member": member,
            "path": path,
            "sees_figure": vision.sees_figure,
            "chart_type": vision.chart_type,
            "described_content": vision.described_content,
            "critique": vision.critique,
        }
    )
    log_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_cross_agent_images_channel_seen():
    """Cross-agent figures (images= channel) reach every vision role as real pixels."""
    load_dotenv()
    log_path = dev_log_path("figure-vision", "cross_agent_images", ".jsonl")
    figure = _figure()
    rows: list = []
    for role, handle in _vision_members():
        messages = [ModelMessage(role="user", content=_ASK)]
        response = asyncio.run(handle.call(messages, schema=FigureVision, images=[figure]))
        vision = FigureVision.model_validate(response.parsed)
        _log(rows, log_path, role=role, member=handle.config.name, path="images=", vision=vision)
        assert vision.sees_figure, (
            f"{role}/{handle.config.name} reported NOT seeing the figure via images= "
            f"(chart_type={vision.chart_type!r})"
        )
        assert vision.described_content.strip()
    assert rows, "no vision-capable members resolved"


def test_inline_tool_result_seen():
    """A tool's own figure (ToolResultPart.figures) is seen inline every round.

    Simulates the author→analyze→debate trajectory: the agent 'called' the plotter, the
    tool result carries the figure, and a follow-up turn asks for a critique — so the
    figure must survive inline in history to be seen.
    """
    load_dotenv()
    log_path = dev_log_path("figure-vision", "inline_tool_result", ".jsonl")
    figure = _figure()
    rows: list = []
    for role, handle in _vision_members():
        messages = [
            ModelMessage(role="user", content="Analyze the IXPE observation and plot PA/PD vs time."),
            AssistantToolUse(
                content="Producing the stability plot.",
                tool_calls=[ToolCall(id="c1", name="ixpe_pa_pd_stability_plot", arguments={})],
            ),
            ToolResultMessage(
                results=[
                    ToolResultPart(
                        tool_call_id="c1",
                        name="ixpe_pa_pd_stability_plot",
                        content="wrote 1 figure",
                        figures=[figure],
                    )
                ]
            ),
            ModelMessage(
                role="user",
                content="Critique the figure the plot tool just produced. " + _ASK,
            ),
        ]
        response = asyncio.run(handle.call(messages, schema=FigureVision))
        vision = FigureVision.model_validate(response.parsed)
        _log(rows, log_path, role=role, member=handle.config.name, path="tool_result.figures", vision=vision)
        assert vision.sees_figure, (
            f"{role}/{handle.config.name} reported NOT seeing the inline tool figure "
            f"(chart_type={vision.chart_type!r})"
        )
        assert vision.described_content.strip()
    assert rows, "no vision-capable members resolved"


def test_coder_sdk_sees_figure():
    """The Claude Agent SDK coder path can carry an image via query() (real coder model).

    Distinct from the adapters above: the coder runs through the SDK. The current
    authoring flow does not route figures to it (tracked follow-up); this confirms the
    transport works if we do.
    """
    load_dotenv()
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )

    figure = _figure()
    coder = load_registry().resolve("coder").handles[0]
    b64 = __import__("base64").b64encode(figure.path.read_bytes()).decode("ascii")

    prompt = (
        _ASK
        + ' Reply with ONLY a JSON object: {"sees_figure": bool, "chart_type": str, '
        + '"described_content": str, "critique": str}.'
    )

    async def _run() -> str:
        options = ClaudeAgentOptions(
            model=coder.config.model,
            system_prompt="Answer only with the requested JSON object. Use no tools.",
            max_turns=1,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()

        async def _stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": figure.mime_type, "data": b64},
                        },
                    ],
                },
                "parent_tool_use_id": None,
            }

        chunks: list[str] = []
        try:
            await client.query(_stream())
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
        finally:
            await client.disconnect()
        return "".join(chunks)

    text = asyncio.run(_run())
    match = re.search(r"\{.*\}", text, re.DOTALL)
    assert match, f"coder returned no JSON object: {text[:400]!r}"
    vision = FigureVision.model_validate_json(match.group(0))

    log_path = dev_log_path("figure-vision", "coder_sdk", ".jsonl")
    log_path.write_text(
        json.dumps(
            {
                "role": "coder",
                "member": coder.config.model,
                "path": "sdk.query.image",
                "sees_figure": vision.sees_figure,
                "chart_type": vision.chart_type,
                "described_content": vision.described_content,
                "critique": vision.critique,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert vision.sees_figure, f"coder SDK reported NOT seeing the figure (chart_type={vision.chart_type!r})"
    assert vision.described_content.strip()
