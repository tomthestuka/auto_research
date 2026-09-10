"""Deterministic tests for the analyst lead's cycle forms (§9.3, the forms refactor step 3).

The lead states each decision by *filing a form* instead of composing a structured answer, and
these tests pin the properties that make that safe:

- **The impossible state is inexpressible.** ``author_chain`` requires at least one link in its
  own schema, so "author, with nothing to author" — the shape that killed a run on 2026-09-01
  with ``should_author=true`` and an empty chain — cannot be filed at all.
- **Every question carries a description.** A field explained only by a Pydantic ``#`` comment is
  a field the model fills blind; that was the defect behind the same run.
- **The declared tool set never shrinks.** The stage is enforced by the *handler*, so an
  out-of-stage call comes back as a correction to act on and the loop continues — a declared set
  that shrinks mid-conversation is the "Tool reference not found" run-killer (2026-07-29).
- **Caps stay in Python.** ``request_another_round`` is refused with the cap named once the round
  budget is spent.
"""

import asyncio
from pathlib import Path

import pytest

from auto_research.agents.react import (
    ADJUDICATE_STAGE,
    ANOTHER_ROUND_TOOL,
    AUTHOR_CHAIN_TOOL,
    AUTHOR_STAGE,
    DECIDE_STAGE,
    DRIVE_CODER_TOOL,
    HOLD_AUTHORING_TOOL,
    LEAD_CYCLE_TERMINALS,
    LEAD_FORM_TOOLS,
    RESPOND_TO_REVIEW_TOOL,
    SUBMIT_ADJUDICATION_TOOL,
    SUBMIT_TOOL,
    TERMINAL_REPORT_TOOL,
    LeadCycle,
    ReadOnlyToolProvider,
    ReportWorkspace,
    ToolOutcome,
    author_chain_tool_def,
    hold_authoring_tool_def,
    request_another_round_tool_def,
    submit_adjudication_tool_def,
    submit_terminal_report_tool_def,
)
from auto_research.agents.roles import AnalystLeadAgent, _drop_coding_middle
from auto_research.models import (
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.schemas import AnalystReport


FORM_DEFS = (
    author_chain_tool_def,
    hold_authoring_tool_def,
    request_another_round_tool_def,
    submit_adjudication_tool_def,
    submit_terminal_report_tool_def,
)

GOOD_LINK = {
    "science_context": "Time-resolved polarization of Cyg X-1 in the hard state.",
    "expected_behavior": "pcube_timeseg(ixpe_path, obsid, n_seg) -> per-segment PD/PA table.",
    "proposed_tool_name": "ixpe_pcube_timeseg",
    "target_domain": "ixpe_utils",
    "disposition": "extend_existing",
    "validation_data": [{"mission": "IXPE", "obsid": "01002901"}],
}


def _cycle(stage=DECIDE_STAGE, *, authoring=True, rounds_left=2) -> LeadCycle:
    return LeadCycle(stage=stage, authoring_enabled=authoring, rounds_left=rounds_left)


def _run(coro):
    return asyncio.run(coro)


def _names(provider) -> list[str]:
    return [d["name"] for d in provider.tool_definitions()]


def _workspace(tmp_path: Path, report: AnalystReport | None) -> ReportWorkspace:
    ws = ReportWorkspace(
        report_path=tmp_path / "r.jsonl",
        report_model=AnalystReport,
        catalog={},
    )
    ws.written_report = report
    return ws


# ── the impossible state is inexpressible ────────────────────────────────────


def test_author_chain_expresses_at_least_one_link_as_a_REQUIRED_PROPERTY():
    """Cardinality is enforced on no path; ``required`` is. So the rule is a required property.

    The first version of this tool said ``tool_specs: {minItems: 1}``, and a live lead filed an
    empty array straight through it (2026-09-02) — Anthropic's strict mode is opt-in per tool and
    its compilable subset drops range/cardinality keywords anyway, so `minItems` was prose the
    model could read and ignore. Expressed as a required *property*, an empty chain is
    ungeneratable under strict mode and degrades correctly without it: a model filling the schema
    at all fills `first_link`.
    """
    definition = author_chain_tool_def()
    schema = definition["input_schema"]
    # Enforcement is opt-in per tool and we now ask for it: without `strict` the provider
    # documents that the model may omit required fields, which is what happened live.
    assert definition["strict"] is True
    assert schema["properties"]["first_link"]["type"] == "object"
    assert "first_link" in schema["required"]
    # Strict mode requires EVERY property in `required`, so optionality is nullability instead —
    # `additional_links` is listed but may arrive as an empty array.
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_author_chain_handler_refuses_an_empty_chain_and_names_the_other_branch():
    """The backstop for a vendor that enforces neither ``required`` nor anything else — which is
    every vendor until strict mode is on for every seat. It also points at the form that fits."""
    cycle = _cycle()
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(AUTHOR_CHAIN_TOOL, {})
    )
    assert out.is_error
    assert HOLD_AUTHORING_TOOL in out.text
    assert cycle.filed is None and cycle.decision is None


def test_author_chain_files_the_ordered_chain_as_an_authoring_decision():
    cycle = _cycle()
    second = {**GOOD_LINK, "proposed_tool_name": "ixpe_pcube_timeseg_plot"}
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            AUTHOR_CHAIN_TOOL,
            {"first_link": GOOD_LINK, "additional_links": [second], "rationale": "read lib/ first"},
        )
    )
    assert not out.is_error
    assert cycle.filed == AUTHOR_CHAIN_TOOL
    assert cycle.decision is not None
    assert cycle.decision.should_author is True
    assert [s.proposed_tool_name for s in cycle.decision.tool_specs] == [
        "ixpe_pcube_timeseg",
        "ixpe_pcube_timeseg_plot",
    ]
    assert cycle.decision.rationale == "read lib/ first"


def test_a_malformed_link_comes_back_as_a_correction_naming_the_field():
    """[F11]: a malformed argument is the model's slip to fix in-loop, never a crash."""
    cycle = _cycle()
    broken = {k: v for k, v in GOOD_LINK.items() if k != "target_domain"}
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            AUTHOR_CHAIN_TOOL, {"first_link": GOOD_LINK, "additional_links": [broken]}
        )
    )
    assert out.is_error
    assert "additional_links[0]" in out.text and "target_domain" in out.text
    assert cycle.filed is None and cycle.decision is None


def test_hold_authoring_files_the_no_code_branch():
    cycle = _cycle()
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            HOLD_AUTHORING_TOOL, {"reason": "load_data already exposes this"}
        )
    )
    assert not out.is_error
    assert cycle.filed == HOLD_AUTHORING_TOOL
    assert cycle.decision.should_author is False
    assert cycle.decision.tool_specs == []
    assert "load_data" in cycle.decision.rationale


def test_hold_authoring_needs_a_reason():
    cycle = _cycle()
    out = _run(ReadOnlyToolProvider(roots=[], cycle=cycle).execute(HOLD_AUTHORING_TOOL, {}))
    assert out.is_error and cycle.filed is None


# ── the stage lives in the handler, not in the declared set ──────────────────


@pytest.mark.parametrize(
    "stage,form",
    [
        (ADJUDICATE_STAGE, AUTHOR_CHAIN_TOOL),
        (ADJUDICATE_STAGE, HOLD_AUTHORING_TOOL),
        (DECIDE_STAGE, ANOTHER_ROUND_TOOL),
        (DECIDE_STAGE, SUBMIT_ADJUDICATION_TOOL),
        (AUTHOR_STAGE, AUTHOR_CHAIN_TOOL),
        (AUTHOR_STAGE, SUBMIT_ADJUDICATION_TOOL),
    ],
)
def test_an_out_of_stage_form_is_a_correction_naming_the_stage_and_what_is_open(stage, form):
    cycle = _cycle(stage)
    out = _run(ReadOnlyToolProvider(roots=[], cycle=cycle).execute(form, {}))
    assert out.is_error
    assert stage in out.text
    assert cycle.filed is None


def test_the_honest_terminal_is_reachable_from_every_stage(tmp_path):
    """§9.6: an honest stop is a correct outcome, so no stage puts it out of reach."""
    for stage in (DECIDE_STAGE, AUTHOR_STAGE, ADJUDICATE_STAGE):
        cycle = _cycle(stage)
        ws = _workspace(tmp_path, AnalystReport(summary="what is unavailable and why"))
        out = _run(
            ReadOnlyToolProvider(roots=[], cycle=cycle, report_workspace=ws).execute(
                TERMINAL_REPORT_TOOL, {"status": "failed"}
            )
        )
        assert not out.is_error, stage
        assert cycle.filed == TERMINAL_REPORT_TOOL


# ── caps stay in Python ──────────────────────────────────────────────────────


def test_request_another_round_files_a_review_verdict_that_dispatches():
    cycle = _cycle(ADJUDICATE_STAGE)
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            ANOTHER_ROUND_TOOL,
            {"rationale": "they disagree on the PD", "what_to_probe": "refit segment 3"},
        )
    )
    assert not out.is_error
    assert cycle.filed == ANOTHER_ROUND_TOOL
    assert cycle.verdict.accepted is False
    assert cycle.verdict.needs_analysis_rework is True
    assert cycle.verdict.follow_up == "refit segment 3"


def test_request_another_round_is_refused_once_the_round_cap_is_spent():
    """The cap is Python's; the handler names it rather than letting the loop try."""
    cycle = _cycle(ADJUDICATE_STAGE, rounds_left=0)
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            ANOTHER_ROUND_TOOL, {"rationale": "r", "what_to_probe": "p"}
        )
    )
    assert out.is_error
    assert SUBMIT_ADJUDICATION_TOOL in out.text
    assert cycle.filed is None


def test_request_another_round_needs_both_fields():
    cycle = _cycle(ADJUDICATE_STAGE)
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            ANOTHER_ROUND_TOOL, {"rationale": "r"}
        )
    )
    assert out.is_error and "what_to_probe" in out.text and cycle.filed is None


# ── the report terminals file what edit_report wrote ─────────────────────────


def test_submit_adjudication_files_the_written_report(tmp_path):
    cycle = _cycle(ADJUDICATE_STAGE)
    report = AnalystReport(summary="adjudicated", findings=["a"])
    ws = _workspace(tmp_path, report)
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle, report_workspace=ws).execute(
            SUBMIT_ADJUDICATION_TOOL, {}
        )
    )
    assert not out.is_error
    assert cycle.filed == SUBMIT_ADJUDICATION_TOOL
    assert cycle.report is report


def test_a_report_terminal_with_nothing_written_points_at_edit_report(tmp_path):
    """No compose safety net any more: the disciplined write path is the only path."""
    for form, args in ((SUBMIT_ADJUDICATION_TOOL, {}), (TERMINAL_REPORT_TOOL, {"status": "failed"})):
        cycle = _cycle(ADJUDICATE_STAGE)
        ws = _workspace(tmp_path, None)
        out = _run(
            ReadOnlyToolProvider(roots=[], cycle=cycle, report_workspace=ws).execute(form, args)
        )
        assert out.is_error and "edit_report" in out.text
        assert cycle.filed is None


def test_submit_terminal_report_stamps_the_status_the_lead_filed(tmp_path):
    cycle = _cycle(ADJUDICATE_STAGE)
    ws = _workspace(tmp_path, AnalystReport(summary="blocked on a calibration question"))
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle, report_workspace=ws).execute(
            TERMINAL_REPORT_TOOL, {"status": "needs_human"}
        )
    )
    assert not out.is_error
    assert cycle.report.status == "needs_human"


def test_submit_terminal_report_requires_a_real_status(tmp_path):
    cycle = _cycle(ADJUDICATE_STAGE)
    ws = _workspace(tmp_path, AnalystReport(summary="s"))
    for args in ({}, {"status": "succeeded"}, {"status": "nonsense"}):
        out = _run(
            ReadOnlyToolProvider(roots=[], cycle=cycle, report_workspace=ws).execute(
                TERMINAL_REPORT_TOOL, args
            )
        )
        assert out.is_error and cycle.filed is None


# ── declarations: constant for the cycle, and monotonic across session swaps ──


def test_the_forms_are_declared_for_the_whole_cycle():
    declared = _names(ReadOnlyToolProvider(roots=[], cycle=_cycle()))
    assert LEAD_FORM_TOOLS <= set(declared)


def test_a_run_without_authoring_declares_only_the_judging_forms():
    declared = set(_names(ReadOnlyToolProvider(roots=[], cycle=_cycle(authoring=False))))
    assert AUTHOR_CHAIN_TOOL not in declared and HOLD_AUTHORING_TOOL not in declared
    assert {ANOTHER_ROUND_TOOL, SUBMIT_ADJUDICATION_TOOL, TERMINAL_REPORT_TOOL} <= declared
    assert DRIVE_CODER_TOOL not in declared


def test_the_drive_tools_are_declared_before_any_session_exists():
    """Declared from the cycle's start, so the set does not grow when a link opens."""
    declared = set(_names(ReadOnlyToolProvider(roots=[], cycle=_cycle())))
    assert {DRIVE_CODER_TOOL, SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL} <= declared


def test_swapping_the_authoring_session_never_changes_the_declared_set():
    """Each chain link gets its own session; a set that gained then lost drive_coder between
    links would replay a trajectory naming an undeclared tool (run-killer 2026-07-29)."""
    cycle = _cycle()
    provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    before = _names(provider)
    provider.authoring_session = object()
    during = _names(provider)
    provider.authoring_session = None
    after = _names(provider)
    assert before == during == after


def test_a_drive_tool_with_no_open_session_points_at_author_chain():
    cycle = _cycle()
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(DRIVE_CODER_TOOL, {"instructions": "x"})
    )
    assert out.is_error and AUTHOR_CHAIN_TOOL in out.text


def test_no_cycle_means_no_forms(tmp_path):
    """Every other role, and the lead's own pre-forms single-action calls, are unchanged."""
    declared = set(_names(ReadOnlyToolProvider(roots=[tmp_path])))
    assert not (LEAD_FORM_TOOLS & declared)


def test_form_names_and_terminals_match_what_is_declared():
    assert {f()["name"] for f in FORM_DEFS} == set(LEAD_FORM_TOOLS)
    assert LEAD_CYCLE_TERMINALS <= LEAD_FORM_TOOLS
    assert AUTHOR_CHAIN_TOOL not in LEAD_CYCLE_TERMINALS  # the chain opens a stage, not the end


# ── every question carries a description (the defect that started this) ──────


def _described(schema: dict, trail: str) -> list[str]:
    """Field paths under ``schema`` whose description is missing or empty."""
    missing: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        where = f"{trail}.{name}"
        if not str(prop.get("description") or "").strip():
            missing.append(where)
        if prop.get("type") == "object":
            missing.extend(_described(prop, where))
        items = prop.get("items")
        if isinstance(items, dict):
            missing.extend(_described(items, f"{where}[]"))
    return missing


@pytest.mark.parametrize("factory", FORM_DEFS, ids=lambda f: f()["name"])
def test_every_form_field_carries_a_description(factory):
    """A field explained only in a Pydantic ``#`` comment is a field filled blind — the defect
    that killed a run on 2026-09-01. The prose comes from the schema's own ``Field(description=)``,
    so the worker's view and the lead's view of one object cannot drift."""
    definition = factory()
    assert definition["description"].strip()
    assert _described(definition["input_schema"], definition["name"]) == []


def test_the_chain_link_schema_asks_for_every_lead_owned_field():
    """``target_domain`` and ``disposition`` are the lead's architectural call and were required
    while appearing in no channel the model could read."""
    link = author_chain_tool_def()["input_schema"]["properties"]["first_link"]
    # Under strict mode every property is required, so the lead-owned fields are present by
    # construction; what this pins is that they EXIST and carry their enum/cardinality.
    assert {
        "science_context",
        "expected_behavior",
        "proposed_tool_name",
        "target_domain",
        "disposition",
        "validation_data",
    } <= set(link["required"])
    assert set(link["required"]) == set(link["properties"])
    assert link["properties"]["disposition"]["enum"] == ["extend_existing", "new_package"]
    assert link["properties"]["validation_data"]["minItems"] == 1
    # The fields Pydantic treats as optional are NULLABLE rather than absent from `required`,
    # which is the only way strict mode can express an optional field.
    for optional in ("observed_issue", "replication_tolerance", "notes"):
        branches = link["properties"][optional]["anyOf"]
        assert {"type": "null"} in branches, optional


# ── the loop contract: a rejected form is a correction, a filed one is the end ──


class _FormsProvider:
    """A cycle-backed provider over a scripted call sequence (no model, no network)."""

    def __init__(self, cycle):
        self.cycle = cycle
        self.inner = ReadOnlyToolProvider(roots=[], cycle=cycle)

    def tool_definitions(self):
        return self.inner.tool_definitions()

    async def execute(self, name, arguments):
        return await self.inner.execute(name, arguments)


class _ScriptedModel:
    """Emits one scripted tool call per turn; records how many turns it was asked for."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        self.calls += 1
        if not self.script:
            return ModelResponse(content="done")
        name, arguments = self.script.pop(0)
        return ModelResponse(
            tool_calls=[ToolCall(id=f"c{self.calls}", name=name, arguments=arguments)]
        )


def _drive(cycle, script, **kwargs):
    lead = AnalystLeadAgent(model=_ScriptedModel(script), prompt="p")
    model = lead.model
    messages, _, _ = asyncio.run(
        lead._react(
            seed_messages=[],
            tools=_FormsProvider(cycle),
            action="run_cycle",
            cap=8,
            terminal_tools=LEAD_FORM_TOOLS,
            **kwargs,
        )
    )
    return lead, model, messages


def test_an_out_of_stage_form_does_not_end_the_loop():
    """The stage check can only live in the handler because an ``is_error`` terminal continues."""
    cycle = _cycle(DECIDE_STAGE)
    lead, model, _ = _drive(
        cycle,
        [
            (SUBMIT_ADJUDICATION_TOOL, {}),  # out of stage → correction
            (AUTHOR_CHAIN_TOOL, {"first_link": GOOD_LINK}),  # in stage → files, ends
            (HOLD_AUTHORING_TOOL, {"reason": "never reached"}),
        ],
    )
    assert model.calls == 2
    assert lead.last_filed_form[0] == AUTHOR_CHAIN_TOOL
    assert cycle.filed == AUTHOR_CHAIN_TOOL


def test_filing_a_form_ends_the_loop_with_its_result_appended():
    """The loop stops AFTER the result turn, so every ``tool_use`` keeps its ``tool_result``."""
    cycle = _cycle(DECIDE_STAGE)
    _, model, messages = _drive(cycle, [(HOLD_AUTHORING_TOOL, {"reason": "already exposed"})])
    assert model.calls == 1
    tool_results = [m for m in messages if type(m).__name__ == "ToolResultMessage"]
    assert len(tool_results) == 1


def test_a_loop_that_never_files_leaves_no_form():
    """Budget spent with nothing filed is the failure the caller has to treat as one."""
    cycle = _cycle(DECIDE_STAGE)
    lead, _, _ = _drive(cycle, [])
    assert lead.last_filed_form is None
    assert cycle.filed is None


# ── step 6: the coding middle is dropped at its own boundary ─────────────────


def _assistant(*names):
    return ModelResponse(
        tool_calls=[ToolCall(id=f"t{i}", name=n, arguments={}) for i, n in enumerate(names)]
    )


def _results(*names):
    return ToolResultMessage(
        results=[
            ToolResultPart(tool_call_id=f"t{i}", name=n, content="ok") for i, n in enumerate(names)
        ]
    )


def _coding_conversation():
    """[seed, author_chain pair] then a coding span ending in submit_for_review."""
    before = [
        ModelMessage(role="user", content="seed"),
        _assistant(AUTHOR_CHAIN_TOOL),
        _results(AUTHOR_CHAIN_TOOL),
        ModelMessage(role="user", content="session 1 of 1 is open"),
    ]
    span = [
        _assistant(DRIVE_CODER_TOOL),
        _results(DRIVE_CODER_TOOL),
        _assistant("read_file"),
        _results("read_file"),
        _assistant(SUBMIT_TOOL),
        _results(SUBMIT_TOOL),
    ]
    return before + span, len(before)


def test_the_coding_middle_goes_and_both_anchors_survive():
    messages, mark = _coding_conversation()
    kept = _drop_coding_middle(messages, mark)
    assert kept[:mark] == messages[:mark]  # everything before the session is untouched
    names = [c.name for m in kept for c in (getattr(m, "tool_calls", None) or ())]
    assert AUTHOR_CHAIN_TOOL in names  # the lead's own filed brief
    assert SUBMIT_TOOL in names  # its own submit, whose result carries the real SHA
    assert DRIVE_CODER_TOOL not in names and "read_file" not in names  # the transcript is gone


def test_the_drop_leaves_no_tool_use_without_its_result():
    """A half-dropped pair is a 400 — whole turns only, contiguous span."""
    messages, mark = _coding_conversation()
    kept = _drop_coding_middle(messages, mark)
    for index, message in enumerate(kept):
        if getattr(message, "tool_calls", None):
            following = kept[index + 1] if index + 1 < len(kept) else None
            assert isinstance(following, ToolResultMessage), f"turn {index} lost its results"


def test_the_drop_synthesizes_no_assistant_turn():
    """Every surviving turn is one the lead produced or received; nothing is written for it."""
    messages, mark = _coding_conversation()
    kept = _drop_coding_middle(messages, mark)
    assert all(m in messages for m in kept)


def test_a_span_with_no_submit_is_left_whole():
    """The lead gave up without submitting: there is no anchor, and dropping the span would
    leave the failure unexplained."""
    before = [ModelMessage(role="user", content="seed")]
    messages = before + [_assistant(DRIVE_CODER_TOOL), _results(DRIVE_CODER_TOOL)]
    assert _drop_coding_middle(messages, len(before)) == messages


def test_respond_to_review_also_anchors_the_drop():
    """A debate that persuaded the co-lead commits through respond_to_review, so that call and
    its result are the outcome anchor."""
    before = [ModelMessage(role="user", content="seed")]
    messages = before + [
        _assistant(SUBMIT_TOOL),
        _results(SUBMIT_TOOL),
        _assistant(DRIVE_CODER_TOOL),
        _results(DRIVE_CODER_TOOL),
        _assistant(RESPOND_TO_REVIEW_TOOL),
        _results(RESPOND_TO_REVIEW_TOOL),
    ]
    kept = _drop_coding_middle(messages, len(before))
    names = [c.name for m in kept for c in (getattr(m, "tool_calls", None) or ())]
    assert names == [RESPOND_TO_REVIEW_TOOL]  # the LAST submit-class call is the anchor


def test_a20_request_another_round_is_open_at_the_author_stage():
    """[A20]: after a commit the only open form was a terminal, so a lead that wanted a worker
    rerun — the correct request, since a committed tool is evidence the current reports predate —
    filed `submit_terminal_report(status='needs_human')` instead. Attempt 6 did exactly that and
    its ledger holds no `ask_human`/`HumanQuery` event at all; [A11] delivered the rerun anyway,
    so the outcome was right and only the record was wrong. The record is what an offline scoring
    pass reads as an honest-stop row."""
    from auto_research.agents.react import (
        ANOTHER_ROUND_TOOL,
        AUTHOR_STAGE,
        TERMINAL_REPORT_TOOL,
        _STAGE_FORMS,
    )

    assert ANOTHER_ROUND_TOOL in _STAGE_FORMS[AUTHOR_STAGE]
    assert TERMINAL_REPORT_TOOL in _STAGE_FORMS[AUTHOR_STAGE], "the honest terminal stays open"
