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
    ModelResponse,
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
            ModelResponse(
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


def test_live_many_image_request_that_used_to_400(tmp_path):
    """21 oversized figures in one request — the shape that killed run `8c75990e`.

    Anthropic tightens its per-image dimension cap to 2000 px once a request carries more than
    20 image blocks. On 2026-08-06 a worker's plotter had produced a 2677x1554 `dcf_panels.png`,
    each plotter call added 3 PNGs that replay inline on every later request, the count crossed
    20, and the run died at $116.10 — one step after its fifth tool committed:

        400 image.source.base64.data: At least one of the image dimensions exceed
            max allowed size for many-image requests: 2000 pixels

    The adapter now resizes on the way out. This sends that exact shape and requires it through.
    Deterministic coverage is `tests/test_media_guards.py`; only a live round trip can prove the
    request the guard produces is one Anthropic actually accepts.
    """
    from PIL import Image

    from auto_research.models.providers.anthropic import AnthropicProvider
    from auto_research.models.types import (
        RoleModelConfig,
        ToolCall,
        ToolResultMessage,
        ToolResultPart,
    )

    load_dotenv()
    figures = []
    for index in range(21):
        path = tmp_path / f"panel_{index}.png"
        # The real offender's dimensions, and enough structure that the resize has to do work.
        Image.new("RGB", (2677, 1554), (20 * (index % 12), 90, 140)).save(path, format="PNG")
        figures.append(
            FigureRef(
                path=path,
                caption=f"DCF panel {index}",
                mime_type="image/png",
                produced_by="test",
                created_at=datetime.now(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )

    # A real trajectory: the tool_result must answer a tool_use in the preceding assistant turn,
    # which is also how these figures reach a request in production.
    convo = [
        ModelMessage(role="system", content="You review X-ray polarization figures."),
        ModelMessage(role="user", content="How many panels did you receive? Answer with a number."),
        ModelResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="qu_lag_dcf_plot", arguments={})],
        ),
        ToolResultMessage(
            results=[
                ToolResultPart(
                    tool_call_id="c1", name="qu_lag_dcf_plot", content="21 panels", figures=figures
                )
            ]
        ),
    ]
    cfg = RoleModelConfig(
        name="worker",
        provider="anthropic",
        model="claude-sonnet-5",
        vision=True,
        parameters={"max_tokens": 512},
    )

    response = asyncio.run(AnthropicProvider().call(convo, cfg))

    print(f"[live] 21-image request accepted; reply: {(response.content or '')[:120]}")
    assert response.usage.input_tokens > 0, "the request reached the model"


def test_live_oversized_pdf_request_that_used_to_413(tmp_path):
    """The two real PDFs that killed run `fbb93f26`, in one request, through `ModelHandle`.

    That run died at its very last node — `review_analysis`, with no report composed — on
    Anthropic `413 request_too_large`, and **no media-guard event fired**: the guard had judged
    the payload legal. Measured from its ledger, the reviewer held `arxiv_2206.06713v2.pdf`
    (24.6 MB) and `arxiv_2307.14604v2.pdf` (0.47 MB), which base64 to 33,512,296 bytes against a
    33,554,432 ceiling — 42 KB of headroom for a conversation orders of magnitude larger. The
    ceiling was being applied to a subset of what counts toward it; Anthropic states the rule for
    both of its document limits: "Both limits are on the entire request payload, including any
    other content sent alongside PDFs".

    The adapter now reserves the non-media weight, so the oldest document is withheld with a
    model-visible note and the request goes through. Deterministic coverage is
    `tests/test_media_guards.py`; only a live round trip proves the request the guard now
    produces is one Anthropic actually accepts. Driven through `ModelHandle` rather than the
    provider, so key rotation and the retry layer are in the path (the 2026-08-25 altitude fix).

    Set `AUTO_RESEARCH_LIVE_413_PAPERS` to a directory holding those two PDFs if the run's world
    has been swept.
    """
    from auto_research.models.providers._media import nonmedia_weight, plan_for_call
    from auto_research.models.providers.anthropic import _LIMITS
    from auto_research.schemas import DocumentRef

    load_dotenv()
    default = next(
        (
            p
            for p in sorted(Path("/data/fzhou/ar_runtime/ws").glob("*attempt5*/papers"))
            if p.is_dir()
        ),
        Path("/data/fzhou/ar_runtime/papers"),
    )
    papers = Path(os.getenv("AUTO_RESEARCH_LIVE_413_PAPERS", str(default)))
    big = papers / "arxiv_2206.06713v2.pdf"
    small = papers / "arxiv_2307.14604v2.pdf"
    if not (big.is_file() and small.is_file()):
        pytest.skip(f"the two PDFs from run fbb93f26 are not under {papers}")

    def _doc(path: Path) -> DocumentRef:
        return DocumentRef(
            path=path,
            title=path.stem,
            mime_type="application/pdf",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            created_at=datetime.now(),
        )

    # Emission order is the order they were opened, so "oldest first" evicts the small one.
    documents = [_doc(small), _doc(big)]
    raw = small.stat().st_size + big.stat().st_size
    print(f"[live] documents: {raw:,} raw -> {raw * 4 // 3:,} encoded, ceiling 33,554,432")

    # A trajectory the SIZE THE REVIEWER ACTUALLY CARRIED, and this is load-bearing: a first
    # version of this probe used a two-message conversation, whose reserve was 21,583 bytes, so
    # the request came to 33,533,879 and simply FIT. It passed while evicting nothing — proving
    # only that Anthropic accepts ~33.5 MB, not that the guard works. The real call was 12
    # messages over five ReAct rounds with six tools declared (72,124 bytes on the wire, reserve
    # 81,787), which is what makes the reserve bite.
    convo = [
        ModelMessage(role="system", content="You review X-ray polarization analyses. " + "S" * 9_000),
        ModelMessage(role="user", content="Name one paper you were given. " + "U" * 12_000),
    ]
    for index in range(5):
        convo.append(
            ModelResponse(
                # No synthesized `thinking` part: Anthropic requires a `signature` on one it
                # emitted, and inventing a block a provider never sent is the replay mistake
                # the carrier design exists to prevent (a 400 caught it here).
                content=f"Reading the analysis, step {index}. " + "a" * 2_000,
                tool_calls=[
                    ToolCall(id=f"c{index}", name="read_file",
                             arguments={"path": f"/run/report/section_{index}.md", "limit": 200})
                ],
            )
        )
        convo.append(
            ToolResultMessage(
                results=[ToolResultPart(tool_call_id=f"c{index}", name="read_file",
                                        content="r" * 9_000)]
            )
        )
    tools = [
        {"name": name, "description": "d" * 600,
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"], "additionalProperties": False}}
        for name in ("read_file", "list_dir", "text_search", "open_paper", "web_search", "web_fetch")
    ]

    reserve = nonmedia_weight(convo, tools=tools)
    plan = plan_for_call(convo, (), documents, _LIMITS, reserved_bytes=reserve)
    withheld = [d for d in documents if plan.resolve(d).dropped]
    print(f"[live] reserve {reserve:,}; withheld {[Path(d.path).name for d in withheld]}")
    assert withheld, (
        f"the guard evicted nothing (reserve {reserve:,}), so this request would fit anyway and "
        "the probe would pass without exercising the fix"
    )
    note = plan.resolve(withheld[0]).note or ""
    assert Path(withheld[0].path).name in note and "withheld" in note, note

    handle = load_registry().resolve("reviewer").member("claude_fable_5")
    response = asyncio.run(handle.call(convo, documents=documents, tools=tools))

    log = Path("logs/dev/media-reserve")
    log.mkdir(parents=True, exist_ok=True)
    (log / "live_413_probe.json").write_text(
        json.dumps(
            {
                "raw_bytes": raw,
                "encoded_bytes": raw * 4 // 3,
                "reserve": reserve,
                "withheld": [Path(d.path).name for d in withheld],
                "reply": (response.content or "")[:400],
                "tool_calls": [c.name for c in response.tool_calls],
                "input_tokens": response.usage.input_tokens,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    answered = (response.content or "").strip() or [c.name for c in response.tool_calls]
    print(f"[live] accepted; {response.usage.input_tokens:,} input tokens; answered: {answered}")
    # The surviving 24.6 MB PDF has to have been *processed*, not merely accepted: a request
    # whose documents were all withheld would return 200 just the same, so acceptance alone
    # proves nothing. The input-token count is the evidence of delivery.
    assert response.usage.input_tokens > 100_000, (
        f"only {response.usage.input_tokens:,} input tokens — the surviving PDF did not reach "
        "the model, so this proves acceptance without proving delivery"
    )
    # What the model *says* is deliberately not asserted (§10: never assert subjective LLM
    # behaviour). The stimulus is filler sized to reproduce the real reserve, not a question
    # worth answering, and an empty reply to it is recorded rather than failed.
