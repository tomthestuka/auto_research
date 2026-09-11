"""F5a/F5b — normalised stop classification, and the classified continuation that replaced
`_react`'s silent `break`.

The loop used to end on `not response.tool_calls`, which collapsed five provider outcomes into
"the model is done". Slot 3's lead (2026-09-08) returned `output_tokens: 2` with no content after
nine consecutive tool-result turns and the run died with no report filed — Anthropic documents
exactly that shape as an accidental empty response, not a finished turn.
"""

from types import SimpleNamespace as NS

from auto_research.agents.roles import _SILENT_TURN_CAP, _continuation_for
from auto_research.models.types import (
    ModelResponse,
    ModelUsage,
    stop_from_anthropic,
    stop_from_chat_finish_reason,
    stop_from_google,
    stop_from_openai_responses,
)


# ── F5a: each vendor read from its own documented field ──────────────────────


def test_anthropic_stop_reasons():
    for reason, expected in [
        ("end_turn", "end_turn"), ("tool_use", "tool_use"), ("stop_sequence", "end_turn"),
        ("max_tokens", "truncated"), ("refusal", "refusal"), ("pause_turn", "paused"),
        ("model_context_window_exceeded", "truncated"),
    ]:
        assert stop_from_anthropic(NS(stop_reason=reason)) == expected, reason
    assert stop_from_anthropic(NS(stop_reason=None)) == "unknown"


def test_openai_responses_status_and_incomplete_details():
    assert stop_from_openai_responses(NS(status="completed")) == "end_turn"
    assert stop_from_openai_responses(
        NS(status="incomplete", incomplete_details=NS(reason="max_output_tokens"))
    ) == "truncated"
    assert stop_from_openai_responses(
        NS(status="incomplete", incomplete_details=NS(reason="content_filter"))
    ) == "refusal"
    # An `incomplete` with no reason is still incomplete, never a clean end.
    assert stop_from_openai_responses(NS(status="incomplete", incomplete_details=None)) == "truncated"


def test_the_two_vendors_that_report_infrastructure_failure_in_the_terminal_field():
    """DeepSeek's `insufficient_system_resource` and Z.AI's `network_error` are transients that
    arrive in `finish_reason`. The wider ecosystem maps `network_error` to `stop`, which would
    make a dropped inference read as a finished answer — so we deliberately do not."""
    assert stop_from_chat_finish_reason("insufficient_system_resource") == "transient"
    assert stop_from_chat_finish_reason("network_error") == "transient"
    assert stop_from_chat_finish_reason("stop") == "end_turn"
    assert stop_from_chat_finish_reason("tool_calls") == "tool_use"
    assert stop_from_chat_finish_reason("length") == "truncated"
    assert stop_from_chat_finish_reason("sensitive") == "refusal"


def test_google_finish_reasons_including_its_tool_specific_ones():
    def resp(name):
        return NS(candidates=[NS(finish_reason=NS(name=name))])

    assert stop_from_google(resp("STOP")) == "end_turn"
    assert stop_from_google(resp("MAX_TOKENS")) == "truncated"
    assert stop_from_google(resp("SAFETY")) == "refusal"
    # Values no other vendor has.
    assert stop_from_google(resp("MALFORMED_FUNCTION_CALL")) == "malformed"
    assert stop_from_google(resp("UNEXPECTED_TOOL_CALL")) == "malformed"
    assert stop_from_google(NS(candidates=[])) == "unknown"


def test_model_response_defaults_to_unknown_so_an_unwired_adapter_is_not_read_as_finished():
    assert ModelResponse().stop == "unknown"


# ── F5b: what the loop says to a silent turn ─────────────────────────────────


def _silent(stop, content="", output_tokens=2):
    return ModelResponse(content=content, stop=stop,
                         usage=ModelUsage(output_tokens=output_tokens))


FORMS = frozenset({"submit_adjudication", "author_chain"})


def test_the_slot3_empty_turn_is_continued_not_treated_as_finished():
    """`output_tokens: 2`, no content, `end_turn` — the documented empty response, and the exact
    shape that killed slot 3."""
    nudge = _continuation_for(_silent("end_turn"), FORMS)
    assert nudge is not None
    assert "empty" in nudge.lower()
    # The continuation must name the terminal tool, so the model knows how to actually finish.
    assert "author_chain" in nudge


def test_a_real_end_turn_carrying_content_ends_the_loop():
    """A model that answered and asked for nothing is genuinely done; nudging it would loop."""
    substantive = "I have completed the analysis and the report is written to the workspace."
    assert _continuation_for(_silent("end_turn", content=substantive, output_tokens=400), FORMS) is None


def test_truncation_is_continued_rather_than_read_as_done():
    nudge = _continuation_for(_silent("truncated"), FORMS)
    assert nudge and "cut off" in nudge


def test_a_malformed_tool_call_is_asked_for_again():
    nudge = _continuation_for(_silent("malformed"), FORMS)
    assert nudge and "could not parse" in nudge


def test_a_paused_server_tool_loop_is_continued():
    assert _continuation_for(_silent("paused"), FORMS) is not None


def test_a_refusal_and_a_transient_are_never_nudged():
    """Repeating the request achieves nothing for either: a refusal needs a different member and
    a transient needs the retry layer."""
    assert _continuation_for(_silent("refusal"), FORMS) is None
    assert _continuation_for(_silent("transient"), FORMS) is None


def test_a_role_with_no_terminal_tool_keeps_the_old_no_tool_calls_means_done_rule():
    """Without a terminal tool there is no other way for that loop to finish, so the absence of
    tool calls IS its completion signal — nudging would change every pre-F5 loop for nothing."""
    for stop in ("end_turn", "unknown", "truncated", "malformed", "paused"):
        assert _continuation_for(_silent(stop), frozenset()) is None, stop


def test_unknown_is_judged_on_the_turn_not_the_label():
    """`unknown` means the vendor reported nothing usable, so the turn itself is the evidence: an
    empty one is continued, one carrying a real answer is finished."""
    assert _continuation_for(_silent("unknown"), FORMS) is not None
    answered = "I measured PD = 3.4 +/- 0.14 percent and wrote the report to the workspace."
    assert _continuation_for(_silent("unknown", content=answered, output_tokens=300), FORMS) is None


def test_the_nudge_is_bounded():
    """A model that keeps returning nothing is not going to start, so the loop gives up."""
    assert _SILENT_TURN_CAP <= 3


# ── F5c/F5d: turn_end validates; nothing is composed on a model's behalf ─────


import asyncio  # noqa: E402

from auto_research.agents.react import (  # noqa: E402
    TURN_END_TOOL,
    execute_turn_end,
    turn_end_tool_def,
    worker_report_problems,
)


def test_turn_end_accepts_when_the_validator_is_satisfied():
    out = asyncio.run(execute_turn_end(lambda: [], {"summary": "PD measured at 3.4%."}))
    assert not out.is_error
    assert "PD measured" in out.text


def test_turn_end_rejects_with_the_specific_reason_and_stays_in_the_loop():
    """A rejection is an `is_error` outcome, which `_react` treats as a correction rather than a
    finish — so the model keeps its tools and its context and can fix what was named."""
    out = asyncio.run(execute_turn_end(
        lambda: ["No report has been written.", "The report's summary is empty."],
        {"summary": "done"},
    ))
    assert out.is_error
    assert "No report has been written." in out.text
    assert "The report's summary is empty." in out.text
    assert "call turn_end again" in out.text


def test_turn_end_is_declared_as_a_strict_tool_with_a_summary():
    d = turn_end_tool_def()
    assert d["name"] == TURN_END_TOOL
    assert d["input_schema"]["required"] == ["summary"]
    # The description must tell the model the rejection is recoverable, or it reads a rejection
    # as a dead end (a constraint the model reads belongs in the description it reads).
    assert "call this again" in d["description"]


def test_an_unwritten_worker_report_is_a_named_rejection_not_a_compose():
    """F5d: the fallback compose was the ONLY path that sent a worker the runtime-owned
    `figures`/`FigureRef` tree, which `_sanitize_strict` promoted to required — that is how four
    `placeholder_*` digests killed slot 5, one line before they would have been discarded."""
    from auto_research.agents.react import ReportWorkspace

    ws = ReportWorkspace.__new__(ReportWorkspace)
    object.__setattr__(ws, "written_report", None)
    object.__setattr__(ws, "catalog", {})

    problems = worker_report_problems(ws)
    assert problems
    assert "edit_report" in problems[0]
    assert "nothing is composed on your behalf" in problems[0]


def test_uncited_catalog_figures_are_surfaced_as_a_problem():
    from auto_research.agents.react import ReportWorkspace

    report = NS(summary="measured", status="succeeded", figures=[])
    ws = ReportWorkspace.__new__(ReportWorkspace)
    object.__setattr__(ws, "written_report", report)
    object.__setattr__(ws, "catalog", {"self": [object(), object()]})

    problems = worker_report_problems(ws)
    assert any("2 figure(s)" in p for p in problems)


def test_a_complete_worker_report_has_no_problems():
    from auto_research.agents.react import ReportWorkspace

    report = NS(summary="PD = 3.4 +/- 0.14 percent", status="succeeded", figures=[object()])
    ws = ReportWorkspace.__new__(ReportWorkspace)
    object.__setattr__(ws, "written_report", report)
    object.__setattr__(ws, "catalog", {"self": [object()]})

    assert worker_report_problems(ws) == []


# ── F5f: figures never follow tool_result blocks in the same message ─────────


def test_figures_nest_inside_the_tool_result_not_after_it(tmp_path):
    """A caption must not sit *beside* the tool results, in this message or a following one.

    Anthropic's prevention for the accidental empty turn is *"Adding text blocks immediately
    after `tool_result`"*, and `_figure_blocks` renders a caption (a text block) then the image —
    so a figure-bearing tool result was producing exactly the shape that made slot 3's lead
    return 2 tokens and kill the run (F5f's motivation, still pinned here).

    F5f answered it with a following user message, which satisfies this rule and breaks another:
    when the same assistant turn has an unresolved server-tool call, *"that message must contain
    nothing except the `tool_result` blocks"*, and a second message is still "anything after".
    That cost a run $42.11. Nesting the caption inside the result satisfies both, so this test now
    pins the nested shape — the hazard it was written for is unchanged, and no sibling text block
    is produced in either message position."""
    from auto_research.models.providers.anthropic import _to_anthropic_messages
    from auto_research.models.types import ToolResultMessage, ToolResultPart
    from auto_research.schemas import FigureRef

    import base64
    from datetime import UTC, datetime

    png = tmp_path / "f.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    ))
    fig = FigureRef(
        path=str(png), caption="PD(E)", mime_type="image/png",
        produced_by="t.plot", sha256="a" * 64, created_at=datetime.now(UTC),
    )
    turn = ToolResultMessage(results=[
        ToolResultPart(tool_call_id="1", name="plot", content="wrote 1 figure", figures=[fig]),
    ])

    _system, msgs = _to_anthropic_messages([turn], plan=None)

    # The tool-result message carries ONLY tool_result blocks, and nothing follows it.
    assert len(msgs) == 1
    assert {b["type"] for b in msgs[0]["content"]} == {"tool_result"}
    # The caption and the image are the result's own content, not siblings of it.
    nested = msgs[0]["content"][0]["content"]
    assert [b["type"] for b in nested] == ["text", "text", "image"]
    assert nested[0]["text"] == "wrote 1 figure"


def test_a_tool_result_with_no_figures_adds_no_extra_message():
    from auto_research.models.providers.anthropic import _to_anthropic_messages
    from auto_research.models.types import ToolResultMessage, ToolResultPart

    turn = ToolResultMessage(results=[ToolResultPart(tool_call_id="1", name="t", content="ok")])
    _system, msgs = _to_anthropic_messages([turn], plan=None)
    assert len(msgs) == 1
