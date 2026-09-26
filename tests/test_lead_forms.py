"""Deterministic tests for the analyst lead's cycle (§9.3; the A-series topology refactor).

The lead's turn is **one ReAct loop with no stages**, ending on ``turn_end``, and **its report is
its decision** — the same contract an analyst already writes. These tests pin the properties that
make that safe:

- **The impossible state is inexpressible.** ``author_chain`` requires at least one link in its
  own schema, so "author, with nothing to author" — the shape that killed a run on 2026-09-01 —
  cannot be filed at all.
- **Every question carries a description.** A field explained only by a Pydantic ``#`` comment is
  a field the model fills blind; that was the defect behind the same run.
- **The declared tool set never shrinks.** Sessions are swapped per chain link and the declared
  set does not move — a set that shrinks mid-conversation is the "Tool reference not found"
  run-killer (2026-07-29).
- **A dead link does not end the turn.** The chain advances on a *tool result*, which is the whole
  of A7: slot 1 of the 2026-09-11 batch ended a whole analysis with 11 of 12 author credits
  unspent because every action open to it while authoring was a whole-cycle terminal.
- **Caps stay in Python**, and are enforced at ``turn_end`` with the cap named.

What is gone with the forms: ``hold_authoring``, ``request_another_round``, ``submit_adjudication``
and ``submit_terminal_report``, and the three-stage desk that decided which were callable when.
Their tests went with them; what they asserted is now asserted against the report and the
``turn_end`` validator below.
"""

import asyncio
import itertools
import types
from pathlib import Path

import pytest

from auto_research.agents.react import (
    AUTHOR_CHAIN_TOOL,
    EDIT_REPORT_TOOL,
    LEAD_FORM_TOOLS,
    LEAD_REPORT_FIELDS,
    LEAD_STATUS_VALUES,
    RESPOND_TO_REVIEW_TOOL,
    SUBMIT_TOOL,
    TURN_END_TOOL,
    WAIT_FOR_REPLY_TOOL,
    LeadCycle,
    ReadOnlyToolProvider,
    ReportWorkspace,
    author_chain_tool_def,
    edit_report_tool_def,
    lead_turn_problems,
)
from auto_research.agents.roles import (
    AnalystLeadAgent,
    _compact_coding_span,
    _unanswered_call_ids,
)
from auto_research.models import (
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.schemas import AnalystReport


FORM_DEFS = (author_chain_tool_def,)

GOOD_LINK = {
    "science_context": "Time-resolved polarization of Cyg X-1 in the hard state.",
    "expected_behavior": "pcube_timeseg(ixpe_path, obsid, n_seg) -> per-segment PD/PA table.",
    "proposed_tool_name": "ixpe_pcube_timeseg",
    "target_domain": "ixpe_utils",
    "disposition": "extend_existing",
    "validation_data": [{"mission": "IXPE", "obsid": "01002901"}],
}


def _cycle(*, authoring=True, rounds_left=2) -> LeadCycle:
    return LeadCycle(authoring_enabled=authoring, rounds_left=rounds_left)


def _run(coro):
    return asyncio.run(coro)


def _names(provider) -> list[str]:
    return [d["name"] for d in provider.tool_definitions()]


def _workspace(tmp_path: Path, report: AnalystReport | None) -> ReportWorkspace:
    ws = ReportWorkspace(
        report_path=tmp_path / "r.jsonl",
        report_model=AnalystReport,
        catalog={},
        prose_fields=LEAD_REPORT_FIELDS,
        status_values=LEAD_STATUS_VALUES,
    )
    ws.written_report = report
    return ws


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
    assert cycle.brief is not None
    assert cycle.brief.should_author is True
    assert [s.proposed_tool_name for s in cycle.brief.tool_specs] == [
        "ixpe_pcube_timeseg",
        "ixpe_pcube_timeseg_plot",
    ]
    assert cycle.brief.rationale == "read lib/ first"


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
    assert cycle.brief is None


# The lead reaches its coder with `message(to="coder")` (E12); these read that address off the
# lead's channel layer, built as `_react` builds it.
_CODING_CALL = "message"


def _lead_channel(provider):
    from types import SimpleNamespace

    from auto_research.agents.roles import ChannelTools
    from auto_research.hitl.router import SeatRouter
    from auto_research.run_control import RunControl

    seat = SimpleNamespace(
        channel=SeatRouter(RunControl()), agent_key="analyst_lead", role="analyst_lead",
        run_control=None, model=None,
    )
    return ChannelTools(provider, role=seat)


def _message_addresses(provider) -> list[str]:
    defs = {d["name"]: d for d in _lead_channel(provider).tool_definitions()}
    return list(defs["message"]["input_schema"]["properties"]["to"]["enum"])


def test_the_drive_tools_are_declared_before_any_session_exists():
    """Declared from the cycle's start, so the set does not grow when a link opens."""
    provider = ReadOnlyToolProvider(roots=[], cycle=_cycle())
    assert {SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL} <= set(_names(provider))
    assert "coder" in _message_addresses(provider), "the coder address is declared from the start"


def test_swapping_the_authoring_session_never_changes_the_declared_set():
    """Each chain link gets its own session; a set that gained then lost the coder address between
    links would replay a trajectory naming an undeclared tool (run-killer 2026-07-29)."""
    cycle = _cycle()
    provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    before = (_names(provider), _message_addresses(provider))
    provider.authoring_session = object()
    during = (_names(provider), _message_addresses(provider))
    provider.authoring_session = None
    after = (_names(provider), _message_addresses(provider))
    assert before == during == after


def test_a_drive_tool_with_no_open_session_points_at_author_chain():
    cycle = _cycle()
    out = _run(_lead_channel(ReadOnlyToolProvider(roots=[], cycle=cycle)).execute(
        "message", {"to": "coder", "text": "x"}
    ))
    assert out.is_error and AUTHOR_CHAIN_TOOL in out.text


def test_no_cycle_means_no_forms(tmp_path):
    """Every other role, and the lead's own pre-forms single-action calls, are unchanged."""
    declared = set(_names(ReadOnlyToolProvider(roots=[tmp_path])))
    assert not (LEAD_FORM_TOOLS & declared)


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


class _FormsProvider:
    """A cycle-backed provider over a scripted call sequence (no model, no network)."""

    def __init__(self, cycle, turn_end_validator=None):
        self.cycle = cycle
        self.inner = ReadOnlyToolProvider(roots=[], cycle=cycle)
        if turn_end_validator is not None:
            # `turn_end`'s refusal is what this exercises, and the validator IS that decision.
            # Building a real report workspace needs a memory path too, and a half-built one
            # returns None — which silently makes the validator vacuous and the test hollow.
            self.inner.turn_end_validator = turn_end_validator

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
            terminal_tools=frozenset({TURN_END_TOOL}),
            **kwargs,
        )
    )
    return lead, model, messages


_ids = itertools.count()


def _assistant(*names):
    """One assistant turn with uniquely-identified calls.

    Ids are unique across the whole module because the pairing invariant these tests exist to
    check is stated over ids: a fixture that reuses ``t0`` on every turn makes an orphaned call
    look answered by an unrelated turn, which is precisely the defect being guarded.
    """
    calls = [ToolCall(id=f"t{next(_ids)}", name=n, arguments={}) for n in names]
    return ModelResponse(tool_calls=calls)


def _results_for(response, content="ok"):
    """The results turn that answers `response`, correlated by the ids it actually carries."""
    return ToolResultMessage(
        results=[
            ToolResultPart(tool_call_id=c.id, name=c.name, content=content)
            for c in response.tool_calls
        ]
    )


def _results(*names):
    """Legacy helper: a results turn whose ids nothing answers. Prefer `_results_for`."""
    return ToolResultMessage(
        results=[
            ToolResultPart(tool_call_id=f"t{next(_ids)}", name=n, content="ok") for n in names
        ]
    )


# ── the compaction, over conversations the real driver built ──────────────────
#
# These no longer hand-build a message list. The fixture that did encoded a `mark` production
# never produces — it placed the opening call's ANSWER before the mark, where production places
# it AT the mark — so the pairing assertion below passed against a shape the run path had not
# had since the driver was rewritten, and a committed link killed a live run at $13.26.


def _conversation_through_one_link(monkeypatch, status="committed"):
    """Drive one link through the REAL driver and return (driver, messages).

    Every append here is one the ReAct loop makes, in the loop's order: the assistant turn, then
    the tools run (which is when `advance` is called, and when the mark is recorded), then the
    one results turn. Getting that order right is the entire point — the defect lived in the gap
    between the call and its answer.
    """
    from tests.chain_stub import stub_compactor

    driver, messages, aio = _driver_with(monkeypatch, [status])
    stub_compactor(monkeypatch)

    opening = _assistant(AUTHOR_CHAIN_TOOL)
    messages.append(opening)
    aio.run(driver.advance())                      # the handler: opens link 1, records the mark
    messages.append(_results_for(opening))         # the loop answers the opening call

    coding = _assistant(_CODING_CALL)
    messages.extend([coding, _results_for(coding)])
    reading = _assistant("read_file")
    messages.extend([reading, _results_for(reading)])

    closing = _assistant(SUBMIT_TOOL)
    messages.append(closing)
    aio.run(driver.advance())                      # the handler: closes link 1, compacts
    messages.append(_results_for(closing))         # the loop answers the closing call
    return driver, messages


def test_every_call_keeps_an_answer_across_the_compaction(monkeypatch):
    """THE invariant. A call replayed without its result is a malformed request at every vendor.

    Written against the production ordering rather than a fixture, which is the only reason it
    can fail: the shipped code passed the old fixture while orphaning `author_chain` in the run
    path, and the live 400 named that call by id.
    """
    _, messages = _conversation_through_one_link(monkeypatch)
    assert _unanswered_call_ids(messages) == [], (
        "a tool call survived the compaction with no result — this is the 400 that killed "
        "run 328347c4 after it had already committed a tool"
    )


def test_the_coding_middle_goes_and_the_opening_call_carries_the_outcome(monkeypatch):
    _, messages = _conversation_through_one_link(monkeypatch)
    names = [c.name for m in messages for c in (getattr(m, "tool_calls", None) or ())]
    assert AUTHOR_CHAIN_TOOL in names and SUBMIT_TOOL in names
    assert _CODING_CALL not in names and "read_file" not in names
    answer = next(
        part
        for m in messages
        if isinstance(m, ToolResultMessage)
        for part in m.results
        if part.name == AUTHOR_CHAIN_TOOL
    )
    assert "COMMITTED" in answer.content, (
        "the opening call's answer must carry the link's outcome — it is the only account of "
        "the link the lead keeps"
    )


def test_a_failed_link_hands_the_choice_back(monkeypatch):
    """B6's requirement, now met by the summary rather than by keeping the transcript: the lead
    must be able to choose between proceeding, re-planning and retrying, from this text alone."""
    _, messages = _conversation_through_one_link(monkeypatch, status="failed")
    answer = next(
        part
        for m in messages
        if isinstance(m, ToolResultMessage)
        for part in m.results
        if part.name == AUTHOR_CHAIN_TOOL
    )
    assert "NOT MERGEABLE" in answer.content
    assert "proceed without this tool" in answer.content
    assert "re-investigate" in answer.content


def test_a_batched_turn_keeps_its_other_results(monkeypatch):
    """A lead may file the brief in the same turn as an ordinary read. Only the chain-driving
    part is rewritten; rewriting the whole turn would answer the read with the wrong thing."""
    from tests.chain_stub import stub_compactor

    driver, messages, aio = _driver_with(monkeypatch, ["committed"])
    stub_compactor(monkeypatch)
    opening = _assistant("read_file", AUTHOR_CHAIN_TOOL)
    messages.append(opening)
    aio.run(driver.advance())
    messages.append(_results_for(opening, content="file contents"))
    closing = _assistant(SUBMIT_TOOL)
    messages.append(closing)
    aio.run(driver.advance())
    messages.append(_results_for(closing))

    parts = {p.name: p.content for m in messages if isinstance(m, ToolResultMessage) for p in m.results}
    assert parts["read_file"] == "file contents", "the batched read lost its own answer"
    assert "COMMITTED" in parts[AUTHOR_CHAIN_TOOL]
    assert _unanswered_call_ids(messages) == []


def test_nothing_is_compacted_before_the_answer_lands():
    """Called with no answer yet at the mark, the span is left exactly as it is — there is
    nothing to rewrite, and inventing a turn to rewrite would synthesize one."""
    opening = _assistant(AUTHOR_CHAIN_TOOL)
    messages = [ModelMessage(role="user", content="seed"), opening]
    assert _compact_coding_span(messages, 2, "outcome") == messages


def test_a_span_whose_answer_is_not_chain_driving_is_left_whole():
    """Defensive: if the message at the mark answers something else, the driver has lost track
    of its own boundary and the safe move is to change nothing."""
    opening = _assistant("read_file")
    messages = [ModelMessage(role="user", content="seed"), opening, _results_for(opening)]
    assert _compact_coding_span(messages, 2, "outcome") == messages


# ── author_chain is a BRIEF, not a decision: it does not end anything ─────────


def test_author_chain_handler_refuses_an_empty_chain_and_says_what_to_do_instead():
    """The backstop for a vendor that enforces neither ``required`` nor anything else. It no
    longer points at a sibling form, because there is none — it points at the report."""
    cycle = _cycle()
    out = _run(ReadOnlyToolProvider(roots=[], cycle=cycle).execute(AUTHOR_CHAIN_TOOL, {}))
    assert out.is_error
    assert "report" in out.text and "end your turn" in out.text
    assert cycle.brief is None


def test_author_chain_is_the_only_form_and_it_is_not_a_terminal():
    """A7's core claim, as a property of the declared set: the lead has exactly one form left,
    and the loop's terminal is `turn_end` — so filing a brief cannot end the turn."""
    assert LEAD_FORM_TOOLS == frozenset({AUTHOR_CHAIN_TOOL})
    assert TURN_END_TOOL not in LEAD_FORM_TOOLS
    declared = set(_names(ReadOnlyToolProvider(roots=[], cycle=_cycle())))
    assert AUTHOR_CHAIN_TOOL in declared


def test_a_run_without_authoring_declares_no_form_at_all():
    declared = set(_names(ReadOnlyToolProvider(roots=[], cycle=_cycle(authoring=False))))
    assert AUTHOR_CHAIN_TOOL not in declared
    assert "coder" not in _message_addresses(
        ReadOnlyToolProvider(roots=[], cycle=_cycle(authoring=False))
    )


def test_briefing_a_chain_does_not_end_the_loop():
    """THE regression for slot 1. Before A7 a filed form was a terminal, so briefing a chain —
    or anything else the lead could say while authoring — ended the round. It must not."""
    cycle = _cycle()
    lead, model, _ = _drive(
        cycle,
        [
            (AUTHOR_CHAIN_TOOL, {"first_link": GOOD_LINK}),
            (AUTHOR_CHAIN_TOOL, {"first_link": GOOD_LINK}),  # a revised brief, same turn
            (TURN_END_TOOL, {"summary": "done"}),
        ],
    )
    assert model.calls == 3, "the loop ended on a brief instead of continuing"
    assert lead.last_filed_form[0] == TURN_END_TOOL


def test_the_chain_advances_on_a_tool_result_rather_than_by_ending_the_round():
    """The mechanism A7 replaced the nested per-link loop with: a finished link reports itself
    and the next one opens, inside the same conversation."""
    cycle = _cycle()
    opened: list[str] = []

    async def _advance():
        opened.append(f"link {len(opened) + 1}")
        return f"Coding session {len(opened)} is open."

    cycle.advance_chain = _advance
    out = _run(
        ReadOnlyToolProvider(roots=[], cycle=cycle).execute(
            AUTHOR_CHAIN_TOOL, {"first_link": GOOD_LINK}
        )
    )
    assert not out.is_error
    assert opened == ["link 1"], "briefing did not open the first session"
    assert "Coding session 1 is open." in out.text


# ── turn_end: the report is the decision, and completeness is checked here ────


def test_turn_end_refuses_a_turn_with_no_report(tmp_path):
    problems = lead_turn_problems(_cycle(), _workspace(tmp_path, None))
    assert problems and "edit_report" in problems[0]
    assert "nothing is composed on your behalf" in problems[0]


def test_turn_end_refuses_rework_without_a_probe(tmp_path):
    report = AnalystReport(summary="not settled", rework=True, rationale="disagreement stands")
    problems = lead_turn_problems(_cycle(), _workspace(tmp_path, report))
    assert any("what_to_probe" in p for p in problems)
    assert any("verbatim" in p for p in problems), "say why the field matters"


def test_turn_end_refuses_rework_without_a_rationale(tmp_path):
    report = AnalystReport(summary="not settled", rework=True, what_to_probe="refit DU3")
    problems = lead_turn_problems(_cycle(), _workspace(tmp_path, report))
    assert any("rationale" in p for p in problems)


def test_turn_end_refuses_rework_once_the_round_cap_is_spent(tmp_path):
    """Caps stay in Python and are named when they bind — the loop chooses what, the harness
    decides how many times."""
    report = AnalystReport(
        summary="not settled", rework=True, rationale="r", what_to_probe="refit DU3"
    )
    problems = lead_turn_problems(_cycle(rounds_left=0), _workspace(tmp_path, report))
    assert any("No debate rounds remain" in p for p in problems)
    assert any("rework=false" in p for p in problems), "name the way out, not just the refusal"
    # ...and with a round left, the same report is accepted.
    assert lead_turn_problems(_cycle(rounds_left=1), _workspace(tmp_path, report)) == []


def test_turn_end_refuses_a_conclusion_with_no_findings(tmp_path):
    """rework=False means this report IS the analysis result."""
    report = AnalystReport(summary="all done", status="succeeded")
    problems = lead_turn_problems(_cycle(), _workspace(tmp_path, report))
    assert any("no findings" in p for p in problems)


def test_turn_end_accepts_an_honest_failure_with_no_penalty(tmp_path):
    """§9.6: a report that says the work could not be done is a CORRECT outcome, and the lead's
    surface — unlike a worker's — may say a person is needed."""
    for status in ("failed", "needs_human"):
        report = AnalystReport(
            summary="the capability could not be built", findings=["what blocked it"],
            status=status,
        )
        assert lead_turn_problems(_cycle(), _workspace(tmp_path, report)) == []


def test_turn_end_stats_every_cited_figures_path(tmp_path):
    """Citation resolution checks the catalog handle; this checks the FILE. A report whose
    figure cannot be opened is not a deliverable, and the reviewer would be the one to find out."""
    from datetime import datetime

    from auto_research.schemas import FigureRef

    real = tmp_path / "fig.png"
    real.write_bytes(b"\x89PNG")
    gone = tmp_path / "missing.png"

    def _ref(path):
        return FigureRef(
            path=path, caption=f"{path.name} caption", mime_type="image/png",
            produced_by="research_toolkit.t.tool", created_at=datetime.now().astimezone(), sha256="0" * 64,
        )

    ok = AnalystReport(summary="s", findings=["f"], figures=[_ref(real)])
    assert lead_turn_problems(_cycle(), _workspace(tmp_path, ok)) == []

    broken = AnalystReport(summary="s", findings=["f"], figures=[_ref(real), _ref(gone)])
    problems = lead_turn_problems(_cycle(), _workspace(tmp_path, broken))
    assert len(problems) == 1
    assert "Figure 2" in problems[0] and str(gone) in problems[0]


def test_turn_end_refuses_to_walk_away_from_a_running_coder(tmp_path):
    cycle = _cycle()
    cycle.session_open = True
    report = AnalystReport(summary="s", findings=["f"])
    problems = lead_turn_problems(cycle, _workspace(tmp_path, report))
    assert any("coding session is still open" in p for p in problems)


def test_a_turn_end_refusal_keeps_the_lead_in_its_loop(tmp_path):
    """A rejection is a correction, not a finish: the model gets its tools and its context back.
    Without this the validator would be a way to kill a turn rather than to fix one."""
    from auto_research.agents.react import execute_turn_end

    problems = ["The report's summary is empty."]
    out = _run(execute_turn_end(lambda: problems, {"summary": "done"}))
    assert out.is_error
    assert "summary is empty" in out.text and "turn_end again" in out.text


# ── the report surface is the lead's, and differs from a worker's ────────────


def test_the_leads_report_surface_carries_its_decision_fields():
    declared = edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES)["input_schema"]
    assert {"rework", "rationale", "what_to_probe"} <= set(declared["properties"])
    assert "tool_request" not in declared["properties"], "the lead authors, it does not ask"
    assert declared["properties"]["status"]["enum"] == list(LEAD_STATUS_VALUES)


def test_the_lead_provider_declares_its_own_surface(tmp_path):
    """The workspace carries the surface and the declaration reads it, so what the model is
    offered and what the handler copies cannot drift."""
    provider = ReadOnlyToolProvider(roots=[tmp_path], cycle=_cycle())
    provider.report_workspace = _workspace(tmp_path, None)
    declared = next(
        d for d in provider.tool_definitions() if d["name"] == EDIT_REPORT_TOOL
    )
    assert set(declared["input_schema"]["properties"]) == set(LEAD_REPORT_FIELDS) | {
        "cited_figures"
    }


# ── B6: the coding middle is dropped on a COMMITTED link only ────────────────


def _driver_with(monkeypatch, statuses):
    """A `_ChainDriver` whose links finish with the given statuses, over a live message list."""
    import asyncio

    from auto_research.agents.react import LeadCycle, LeadCycleOutcome
    from auto_research.agents.roles import _ChainDriver
    from auto_research.schemas import AuthoringDecision

    specs = [
        {**GOOD_LINK, "proposed_tool_name": f"tool_{i}"} for i in range(len(statuses))
    ]
    from auto_research.schemas import ToolSpec

    parsed = [ToolSpec.model_validate(spec) for spec in specs]
    cycle = LeadCycle(authoring_enabled=True, rounds_left=1)
    cycle.brief = AuthoringDecision(should_author=True, tool_specs=parsed, rationale="r")
    outcome = LeadCycleOutcome(filed=None, author_budget=len(statuses) + 2)
    messages: list = [ModelMessage(role="user", content="seed")]

    class _Lead:
        monitor = None
        run_control = None

        async def _record(self, *a, **k):
            return None

    driver = _ChainDriver(
        lead=_Lead(), provider=type("P", (), {"authoring_session": None})(), cycle=cycle,
        outcome=outcome, messages=messages, toolkit_root=None, worktree_base=None,
        direct_branch=None, colead=None, human_handler=None, coder=None,
        allow_correlated_review=False,
    )
    order = iter(statuses)

    # The double sits at `_build_session` — the one method whose real body costs money. `_open`,
    # `_close_current`, `advance`, the mark and the compaction all run as production runs them.
    # Replacing `_open` itself (as this helper used to) removed the bookkeeping under test: the
    # fake never assigned `driver.mark`, so it stayed 0 and every cut assertion was vacuous.
    from tests.chain_stub import StubSession

    async def _build(self, spec):
        return StubSession(spec, status=next(order), sha="0123456789ab")

    monkeypatch.setattr(_ChainDriver, "_build_session", _build)
    return driver, messages, asyncio


def test_both_arms_compact_and_the_chain_still_advances(monkeypatch):
    """A committed link and a failed one are both replaced by their outcome.

    B6 used to keep a failed link's transcript whole, on the grounds that a rejection is what
    the next brief has to be written against. That requirement did not go away — it moved into
    the summary, which is why the failure wording ends by handing the choice back to the lead
    (`test_a_failed_link_hands_the_choice_back`). Keeping the raw span was the expensive way of
    meeting it, and it is the run ledger that keeps it now.
    """
    from tests.chain_stub import stub_compactor

    driver, messages, aio = _driver_with(monkeypatch, ["committed", "failed"])
    stub_compactor(monkeypatch)

    for expected in ("COMMITTED", "NOT MERGEABLE"):
        opening = _assistant(AUTHOR_CHAIN_TOOL if expected == "COMMITTED" else SUBMIT_TOOL)
        messages.append(opening)
        aio.run(driver.advance())
        messages.append(_results_for(opening))
        coding = _assistant(_CODING_CALL)
        messages.extend([coding, _results_for(coding)])

    closing = _assistant(SUBMIT_TOOL)
    messages.append(closing)
    aio.run(driver.advance())
    messages.append(_results_for(closing))

    assert [r.status for r in driver.outcome.authored] == ["committed", "failed"], (
        "a failed link must not end the chain (F9)"
    )
    names = [c.name for m in messages for c in (getattr(m, "tool_calls", None) or ())]
    assert _CODING_CALL not in names, "both arms compact, so no coding turn survives"
    assert _unanswered_call_ids(messages) == []


def test_the_lead_is_actually_OFFERED_its_decision_fields(tmp_path):
    """The defect a live lead found, pinned so a model never has to find it again.

    `_make_report_workspace` is shared by the worker and the lead and defaulted to the worker's
    surface for both, so `edit_report` never declared `rework`/`rationale`/`what_to_probe` to the
    lead. The live cell caught it because the lead SAID so in its own report — *"the schema I am
    given has no rework field, so the decision is carried in prose"* — and a decision carried in
    prose is one the harness cannot act on, which is the exact failure A3 exists to remove.

    Asserted through the real construction path rather than on the constants: the constants were
    right the whole time, and nothing read them. And the lead OVERRIDES the base builder, which is
    why the first fix — applied only to the shared one — did not reach it; both now read the
    surface off the role, so they cannot disagree.
    """
    from auto_research.agents.roles import AnalystLeadAgent, AnalystWorkerAgent

    lead = AnalystLeadAgent(model=object(), prompt="p")
    lead.report_dir = tmp_path
    workspace = lead._make_report_workspace({})
    assert workspace is not None
    declared = edit_report_tool_def(workspace.prose_fields, workspace.status_values)
    properties = set(declared["input_schema"]["properties"])
    assert {"rework", "rationale", "what_to_probe"} <= properties, (
        "the lead was not offered the fields its decision rides on"
    )
    assert "tool_request" not in properties, "the lead authors; it does not ask for tools"
    assert declared["input_schema"]["properties"]["status"]["enum"] == list(LEAD_STATUS_VALUES)

    # ...and the worker's surface is unchanged by the same helper.
    worker = AnalystWorkerAgent(model=object(), prompt="p")
    worker.report_dir = tmp_path
    worker_ws = worker._make_report_workspace()
    worker_props = set(
        edit_report_tool_def(worker_ws.prose_fields, worker_ws.status_values)
        ["input_schema"]["properties"]
    )
    assert {"rework", "rationale", "what_to_probe"}.isdisjoint(worker_props)
    assert "tool_request" in worker_props


def test_every_declared_authoring_tool_is_actually_ROUTED():
    """A tool the lead is offered must reach a handler — `check_coder` did not.

    It was declared, defined, given an executor and given an inner dispatch branch, and every one
    of those passed review; what was missing was its name in the outer membership test that guards
    the authoring block, so the inner branch was unreachable and the call fell through to
    `unknown tool: check_coder`. Measured live 2026-09-12: the lead filed `author_chain`, called
    `check_coder` to poll its coder, and was told the tool does not exist.

    Asserted over the DECLARED set rather than a written-out list, so a tool added to the surface
    without being wired fails here instead of in front of a model.
    """
    cycle = _cycle()
    provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    authoring = {WAIT_FOR_REPLY_TOOL, SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL}
    declared = set(_names(provider)) & authoring
    assert declared == authoring, f"an authoring tool is not declared at all: {authoring - declared}"

    # With no session open every one of them must answer with the author_chain pointer. That text
    # is only reachable from inside the block, so "not unknown tool" is exactly the property.
    for name in sorted(declared):
        out = _run(provider.execute(name, {"instructions": "x"}))
        assert out.is_error, f"{name} should refuse without a session"
        assert "unknown tool" not in out.text, (
            f"{name} is declared to the model but reaches no handler: {out.text!r}"
        )
        assert AUTHOR_CHAIN_TOOL in out.text, f"{name} gave the wrong no-session guidance"


# ------------------------------------------------ the seed drive runs BESIDE the lead (2026-09-12)


class _SeedSession:
    """An AuthoringSession double with the background drive seam the real one has."""

    initial_message = "here is your brief"

    def __init__(self) -> None:
        self.started: list[str] = []
        self.blocking: list[str] = []
        self.drive_status = "idle"

    def start_drive(self, message: str) -> None:
        self.started.append(message)
        self.drive_status = "working"

    async def drive(self, message: str) -> str:
        self.blocking.append(message)
        return "narration"

    async def connect(self) -> None:
        return None


def _seed_driver(session, monkeypatch):
    from auto_research.agents import roles

    monkeypatch.setattr(roles, "build_authoring_session", lambda *a, **k: session, raising=False)
    monkeypatch.setattr(
        "auto_research.sandbox.merge_flow.build_authoring_session",
        lambda *a, **k: session,
    )
    cycle = _cycle()
    driver = roles._ChainDriver.__new__(roles._ChainDriver)
    driver.cycle = cycle
    driver.messages = []
    driver.provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    driver.lead = types.SimpleNamespace(monitor=None, run_control=None)
    driver.toolkit_root = driver.worktree_base = Path("/tmp/tk")
    driver.colead = driver.human_handler = driver.coder = None
    driver.allow_correlated_review = False
    driver.direct_branch = "ai"
    driver.session = driver.spec = None
    return driver


def test_opening_a_link_sends_the_brief_in_the_background(monkeypatch):
    """Filing `author_chain` must hand over the brief and RETURN, not wait out the coder's turn.

    Measured live 2026-09-12 on the blocking version: the lead froze for 263 s inside
    `author_chain`, its coder's whole first turn. Two consequences, both observed in that run —
    `check_coder` reported "you have not sent the coder anything yet" (no background task existed
    to report on), advice that would have had the lead re-send the entire brief; and the first
    turn's narration was discarded, so the lead re-read five files by hand to learn what its own
    coder had already told it.
    """
    session = _SeedSession()
    driver = _seed_driver(session, monkeypatch)
    _run(driver._open(types.SimpleNamespace(proposed_tool_name="t")))

    assert session.started == ["here is your brief"], "the brief was not sent in the background"
    assert session.blocking == [], "the lead blocked on the coder's first turn"
    assert session.drive_status == "working", (
        "the lead would be told 'you have not sent the coder anything yet' by a coder that is "
        "working"
    )


def test_a_session_with_no_background_seam_still_gets_its_brief(monkeypatch):
    """A benchmark's session or a test double predates the seam and keeps the blocking call."""
    class _Old:
        initial_message = "here is your brief"

        def __init__(self) -> None:
            self.blocking: list[str] = []

        async def drive(self, message: str) -> str:
            self.blocking.append(message)
            return "narration"

        async def connect(self) -> None:
            return None

    session = _Old()
    assert not hasattr(session, "start_drive"), "the double must lack the seam it is testing"
    driver = _seed_driver(session, monkeypatch)
    _run(driver._open(types.SimpleNamespace(proposed_tool_name="t")))
    assert session.blocking == ["here is your brief"]


def test_submitting_while_a_legacy_coder_is_still_writing_is_refused():
    """The gate reads the working tree, so a mid-write submit judges a half-written tool.

    A session with a blocking wait holds the submit until the coder stops (pinned in
    `test_merge_flow`); one without it (a benchmark's, a double's) still refuses.
    """
    from auto_research.agents.react import execute_submit

    session = types.SimpleNamespace(drive_status="working")
    out = _run(execute_submit(session, None))
    assert out.is_error
    assert "still working" in out.text


# ---------------------------- the coder's question reaches the lead's OWN loop (2026-09-12) ----


class _AskingSession:
    """A session double with the one send path."""

    initial_message = "brief"

    def __init__(self, status: str = "working") -> None:
        self.drive_status = status
        self.questions: list[str] = []
        self.sent: list[str] = []
        self.steered: list[str] = []
        self.started: list[str] = []

    async def send(self, message: str, *, mid_turn: bool = False) -> str:
        self.sent.append(message)
        self.mid_turn = mid_turn
        if self.drive_status == "working":
            if mid_turn:
                self.steered.append(message)
                return "steered"
            return "queued"
        self.started.append(message)
        return "started"


def test_the_coders_question_reaches_the_leads_inbox_and_opens_the_question():
    """E12: ``message(to="lead")`` posts to the lead's inbox — the one queue every text a seat
    receives travels, drained at a boundary the loop knows is safe (A2) — and marks the question
    open, so the lead's answer (``message`` to ``coder``) lands inside the coder's running turn."""
    from types import SimpleNamespace

    from auto_research.run_control import RunControl
    from auto_research.sandbox.merge_flow import _make_ask_lead_handler

    control = RunControl()
    lead = SimpleNamespace(agent_key="analyst_lead", run_control=control)
    lead.ensure_run_control = lambda: control
    session = SimpleNamespace(
        _steer_event=None, opened=0,
        watch_steers=lambda event: setattr(session, "_steer_event", event),
    )
    session.mark_question_open = lambda: setattr(session, "opened", session.opened + 1)
    handler = _make_ask_lead_handler(lambda: session, lead=lead)

    out = _run(handler("ENERGY or PI?"))
    assert out.startswith("delivered.")
    assert session.opened == 1 and session._steer_event is control.steer_event("analyst_lead")
    [entry] = control.drain_inbox("analyst_lead")
    rendered = entry.render()
    assert "ENERGY or PI?" in rendered and 'message(to="coder")' in rendered
    assert WAIT_FOR_REPLY_TOOL not in rendered, "the lead is pointed at the reply path"
    assert control.drain_inbox("analyst_lead") == [], "a delivered question is not re-injected"


def test_a_message_to_the_coder_reports_what_the_session_did_with_it():
    """ONE lead->coder path; the session picks the primitive and the tool says which it was.

    Queued is the default for a working coder (slot 6, 2026-09-15: 47 mid-turn steers), so the
    lead is told the message waits for the next turn; ``mid_turn`` reaches the running turn.
    """
    from auto_research.agents.react import execute_coder_message

    working = _AskingSession(status="working")
    out = _run(execute_coder_message(working, "use PI"))
    assert not out.is_error and working.mid_turn is False
    assert "Queued" in out.text and WAIT_FOR_REPLY_TOOL in out.text

    out = _run(execute_coder_message(working, "stop, use PI", mid_turn=True))
    assert working.steered == ["stop, use PI"] and working.mid_turn is True
    assert "inside the turn the coder is running" in out.text

    idle = _AskingSession(status="idle")
    out = _run(execute_coder_message(idle, "next link"))
    assert not out.is_error and idle.started == ["next link"]
    assert WAIT_FOR_REPLY_TOOL in out.text


# ── wait_coder replaces polling ───────────────────────────────────────────────────────────
#
# Slot 6 (2026-09-15, gpt-5.6-terra lead, $20.43): 134 check_coder calls (114 refused by the
# 900 s rate limit), 47 mid-turn steers, 31 refused submits and 9 refused turn_ends at a 3-10 s
# cadence. A refusal returns at once and costs a model turn, so rate-limiting the poll moved the
# spam onto the other tools. The session-level behaviour is pinned in test_merge_flow; these pin
# the lead-facing surface.


def test_no_model_visible_string_tells_the_lead_to_poll():
    """The declared surface: wait_coder is free, advertises long waits, and nothing says 'check'."""
    from auto_research.agents.react import message_tool_def, wait_for_reply_tool_def
    from auto_research.agents.roles import _CYCLE_FREE_TOOLS

    desc = wait_for_reply_tool_def()["description"].lower()
    assert "prefer long waits" in desc
    assert "check again" not in desc
    assert WAIT_FOR_REPLY_TOOL in _CYCLE_FREE_TOOLS, "the wait costs nothing, and says so"
    coder = message_tool_def(["coder"])
    assert "check_coder" not in coder["description"]
    assert "mid_turn" in coder["input_schema"]["properties"]
    assert "mid_turn" not in message_tool_def(["reviewer"])["input_schema"]["properties"]


def test_the_wait_limits_are_read_from_config_and_kept_ordered(monkeypatch):
    import auto_research.env as env

    monkeypatch.setattr(env, "_orch_config", lambda: {})
    assert env.coder_wait_limits_s() == (60, 300, 1800)
    monkeypatch.setattr(
        env, "_orch_config",
        lambda: {"coder_wait_floor_s": 120, "coder_wait_default_s": 30, "coder_wait_max_s": 90},
    )
    floor, default, maximum = env.coder_wait_limits_s()
    assert floor <= default <= maximum and floor == 120, (floor, default, maximum)
    monkeypatch.setattr(env, "_orch_config", lambda: {"coder_wait_floor_s": "x"})
    assert env.coder_wait_limits_s()[0] == 60, "a malformed value falls back to its default"


def test_a_short_timeout_is_raised_to_the_floor_not_refused(monkeypatch):
    """The whole point: no calling pattern gets an instant answer from a coder with no news."""
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (60, 300, 1800))
    assert react._wait_timeout({"timeout_s": 5}) == (60, " (asked 5s, raised to the 60s minimum)")
    assert react._wait_timeout({"timeout_s": 9999})[0] == 1800
    assert react._wait_timeout({}) == (300, "")
    assert react._wait_timeout({"timeout_s": "soon"}) == (300, "")


# ── no escape path: for the lead, turn_end is the only way out ───────────────
#
# Human ruling 2026-09-15: "THERE WILL BE NO ESCAPE PATH. the normal exit is the only allowed
# exit." The lead writes its report INSIDE its loop (`edit_report` into the workspace) and
# `turn_end` is what submits it, so a loop that ends any other way discards work already done —
# and for the lead specifically `strategy.py` then RAISES, killing the run.
#
# The hole was `_continuation_for`'s `len(content) > 40` test. Measured over 192 recorded silent
# turns it never once discriminated a value between 1 and 40 (the short side is exactly the 8
# turns with zero content), and the three destructive cases sat at 58/62/66 characters against
# 799/35/62 output tokens — mostly reasoning the text channel cannot see.


def _silent(content: str, stop: str = "end_turn"):
    from auto_research.models.types import ModelResponse, ModelUsage

    return ModelResponse(
        content=content, tool_calls=[], stop=stop,
        usage=ModelUsage(input_tokens=1, output_tokens=799),
    )


class _SilentThenScripted:
    """Emits `n_silent` prose-only turns, then follows a script. The shape that lost the work."""

    def __init__(self, n_silent, content, script=()):
        self.n_silent, self.content = n_silent, list(script)
        self.text = content
        self.script = list(script)
        self.calls = 0

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        from auto_research.models.types import ModelUsage

        self.calls += 1
        if self.n_silent > 0:
            self.n_silent -= 1
            return ModelResponse(
                content=self.text, tool_calls=[], stop="end_turn",
                usage=ModelUsage(input_tokens=1, output_tokens=799),
            )
        if not self.script:
            return ModelResponse(content="", tool_calls=[], stop="end_turn")
        name, arguments = self.script.pop(0)
        return ModelResponse(
            tool_calls=[ToolCall(id=f"c{self.calls}", name=name, arguments=arguments)]
        )


def _drive_with_model(model, **kwargs):
    from auto_research.agents.react import LeadCycle

    cycle = LeadCycle(authoring_enabled=False, rounds_left=1)
    lead = AnalystLeadAgent(model=model, prompt="p")
    asyncio.run(
        lead._react(
            seed_messages=[],
            tools=_FormsProvider(cycle), action="run_cycle",
            cap=8, absolute_cap=12, terminal_tools=frozenset({TURN_END_TOOL}), **kwargs,
        )
    )
    return model


def test_a_prose_turn_does_not_end_the_loop_when_the_terminal_is_required():
    """THE behavioural test. 58 characters of prose, no tool call — the exact shape that lost a
    completed 14-quantity reproduction. With `require_terminal` the loop must keep going."""
    prose = "Analysis complete. All quantities measured and written."
    assert len(prose) > 40

    without = _drive_with_model(_SilentThenScripted(3, prose))
    with_flag = _drive_with_model(_SilentThenScripted(3, prose), require_terminal=True)

    assert without.calls == 1, "today's behaviour: the first prose turn ends the loop"
    assert with_flag.calls > 3, (
        f"require_terminal must re-inject past every silent turn, got {with_flag.calls} calls"
    )


def test_the_re_injected_message_reaches_the_model():
    """Not just 'the loop continued' — the lead must actually be told what to do."""
    from auto_research.agents.roles import _TERMINAL_REQUIRED_NUDGE

    seen = []

    class _Recorder(_SilentThenScripted):
        async def call(self, messages, **kw):
            seen.append(" ".join(getattr(m, "content", "") or "" for m in messages))
            return await super().call(messages, **kw)

    _drive_with_model(_Recorder(2, "Work is finished, nothing further to do here."),
                      require_terminal=True)
    assert any(_TERMINAL_REQUIRED_NUDGE in s for s in seen), (
        "the required-terminal message was never sent to the model"
    )


def test_the_silent_cap_no_longer_ends_a_required_terminal_loop():
    """`_SILENT_TURN_CAP` is 2. Without the override it bounds the nudging; with it, it must not
    become a second escape two turns later."""
    from auto_research.agents.roles import _SILENT_TURN_CAP

    model = _drive_with_model(
        _SilentThenScripted(_SILENT_TURN_CAP + 3, "Finished the analysis, all done here."),
        require_terminal=True,
    )
    assert model.calls > _SILENT_TURN_CAP + 1, (
        f"the loop stopped at the silent cap ({model.calls} calls) — still an escape"
    )


def test_a_prose_turn_no_longer_ends_the_leads_loop():
    """The exact shape that lost a completed analysis: 58 characters, no tool call."""
    from auto_research.agents.roles import _TERMINAL_REQUIRED_NUDGE, _continuation_for

    response = _silent("Analysis complete. All quantities measured and written to CSV.")
    assert len(response.content) > 40
    # Unchanged for a role that legitimately ends in prose …
    assert _continuation_for(response, frozenset({"turn_end"})) is None
    # … and the lead's loop overrides it, which is what `require_terminal` selects.
    assert "turn_end" in _TERMINAL_REQUIRED_NUDGE


def test_the_nudge_states_the_channel_rule_then_the_routes_out():
    """The lead must be told what 'done' actually requires, not merely that it is not done."""
    from auto_research.agents.roles import _TERMINAL_REQUIRED_NUDGE

    text = _TERMINAL_REQUIRED_NUDGE
    # The channel rule comes first — that is the actual mistake — then the three routes out.
    assert text.index("tool calls") < text.index("edit_report") < text.index("turn_end")
    for route in ("investigate", "edit_report", "review_report", "turn_end"):
        assert route in text, route
    assert "only way" in text and "nothing is written on your behalf" in text


@pytest.mark.parametrize(
    "method_path",
    [("AnalystLeadAgent", "run_cycle"), ("AnalystWorkerAgent", "analyze")],
    ids=["lead", "worker"],
)
def test_every_loop_that_writes_its_artifact_inside_itself_has_no_escape(method_path):
    """Both roles whose report is written IN the loop opt in — the rule is the shape's, not the
    lead's. All three measured losses were the WORKER's; extending it was the whole point.

    Asserted at the call site because the parameter defaults to False: a loop that needs it and
    does not pass it looks identical to one that does not need it.
    """
    import inspect

    import auto_research.agents.roles as roles_mod

    cls, method = method_path
    source = inspect.getsource(getattr(getattr(roles_mod, cls), method))
    assert "require_terminal=True" in source, (
        f"{cls}.{method} writes its report inside its loop and turn_end submits it, so it must "
        "opt in — otherwise a prose turn discards the work (CLAUDE.md §2b)"
    )


def test_the_lead_loop_is_wired_with_no_escape_path():
    """Asserted at the call site, because the parameter defaults to False: a role that needs it
    and does not pass it looks identical to one that does not need it."""
    import inspect

    from auto_research.agents.roles import AnalystLeadAgent

    source = inspect.getsource(AnalystLeadAgent.run_cycle)
    assert "require_terminal=True" in source, (
        "run_cycle must opt in — its report is written in-loop and turn_end submits it"
    )


def test_a_refusal_still_ends_the_loop():
    """The one deliberate exception. A refusal needs a different member and a transient needs the
    retry layer; re-injecting into either spins to the absolute ceiling achieving nothing."""
    from auto_research.agents.roles import _continuation_for

    for stop in ("refusal", "transient"):
        assert _continuation_for(_silent("", stop=stop), frozenset({"turn_end"})) is None


def test_the_colead_composes_after_its_loop_and_is_untouched():
    """The co-lead produces its verdict in a SEPARATE call after the loop (it sits in the coding
    pipeline, outside §2b's role edges), so a silent exit costs it nothing. Nudging it would be
    wrong, and `require_terminal` defaults False so it keeps the old behaviour."""
    import inspect

    from auto_research.agents.roles import AnalystColeadAgent

    assert "require_terminal" not in inspect.getsource(AnalystColeadAgent.review_tool)


def test_the_reviewer_has_no_escape_path():
    """The reviewer writes its report inside its loop (2026-09-24), so `turn_end` is its only exit,
    exactly as for the lead, the workers and the librarian."""
    import inspect

    from auto_research.agents.roles import ReviewerAgent

    assert "require_terminal=True" in inspect.getsource(ReviewerAgent.review)


def test_require_terminal_defaults_off():
    import inspect

    from auto_research.agents.roles import AgentRole

    sig = inspect.signature(AgentRole._react_loop)  # `_react` is its channel wrapper (E12)
    assert sig.parameters["require_terminal"].default is False


# ── grace: budget exhaustion is not an exit (CLAUDE.md §2b) ──────────────────
#
# A loop that hits its cap is not finished, it is out of time. It is told so, given a bounded
# window to compose and file, and counted down on every tool result. Failing to file inside that
# window is fatal to the RUN — the alternative is the silent unfiled return that discarded a
# completed 14-quantity reproduction on 2026-09-15 and that the ledger could not tell apart from
# a role never asked to run.


class _ToolSpammer:
    """Calls one budget-consuming tool per turn, forever. Never files."""

    def __init__(self, tool="list_dir", arguments=None):
        self.tool, self.arguments = tool, arguments or {"path": "."}
        self.calls = 0

    async def call(self, messages, **kwargs):
        self.calls += 1
        return ModelResponse(
            tool_calls=[ToolCall(id=f"c{self.calls}", name=self.tool, arguments=self.arguments)]
        )


class _SpamsThenFiles(_ToolSpammer):
    """Spams until told it is out of rounds, then files — the behaviour grace exists to buy."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saw_grace_notice = False
        self.countdowns: list[str] = []

    async def call(self, messages, **kwargs):
        import re

        blob = " ".join(
            (getattr(m, "content", "") or "") if not isinstance(m, ToolResultMessage)
            else " ".join(p.content for p in m.results)
            for m in messages
        )
        if "run out of rounds" in blob:
            self.saw_grace_notice = True
        self.countdowns += re.findall(r"\[(\d+) grace round\(s\) left", blob)
        # Spend one grace round working before filing. A model that files the instant it is
        # told would never SEE a countdown — the countdown rides tool results, and the
        # turn_end result ends the loop.
        if self.saw_grace_notice and self.countdowns:
            self.calls += 1
            return ModelResponse(
                tool_calls=[ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={})]
            )
        return await super().call(messages, **kwargs)


def _drive_budget(model, *, cap, grace, monkeypatch, terminal=True, absolute_cap=None,
                  turn_end_validator=None):
    """Drive a real `_react` with a tiny budget.

    `turn_end_validator` makes `turn_end` actually refuse. Without one the provider's default is
    vacuous for a loop with no report workspace (`lead_turn_problems` returns `[]`, the path
    recorded against [A26]), which would turn a test of refusal into a test of nothing.
    """
    from auto_research.agents.react import LeadCycle
    import auto_research.agents.roles as roles_mod

    monkeypatch.setattr(roles_mod, "react_grace_rounds", lambda: grace)
    cycle = LeadCycle(authoring_enabled=False, rounds_left=1)
    lead = AnalystLeadAgent(model=model, prompt="p")
    lead.tools = None
    return asyncio.run(
        lead._react(
            seed_messages=[],
            tools=_FormsProvider(cycle, turn_end_validator=turn_end_validator),
            action="run_cycle",
            cap=cap, absolute_cap=absolute_cap if absolute_cap is not None else cap + grace + 5,
            terminal_tools=frozenset({TURN_END_TOOL}) if terminal else frozenset(),
        )
    )


def test_exhausting_the_budget_opens_a_grace_window_rather_than_ending(monkeypatch):
    model = _SpamsThenFiles()
    _drive_budget(model, cap=3, grace=20, monkeypatch=monkeypatch)
    assert model.saw_grace_notice, "the role was never told it had run out of rounds"
    assert model.calls > 3, "the loop ended at the cap instead of opening grace"


def test_every_tool_result_in_grace_carries_the_countdown(monkeypatch):
    model = _SpamsThenFiles()
    _drive_budget(model, cap=2, grace=5, monkeypatch=monkeypatch)
    assert model.countdowns, "no countdown reached the model"
    # It counts DOWN, and never below zero.
    values = [int(v) for v in model.countdowns]
    assert values == sorted(values, reverse=True), values
    assert min(values) >= 0


def test_failing_to_file_within_grace_is_fatal(monkeypatch):
    """The whole point: an unfiled loop must die loudly rather than return nothing."""
    from auto_research.agents.roles import ReactBudgetExhausted

    with pytest.raises(ReactBudgetExhausted) as caught:
        _drive_budget(_ToolSpammer(), cap=2, grace=3, monkeypatch=monkeypatch)
    assert "grace" in str(caught.value) and "not an exit" in str(caught.value)


def test_grace_never_opens_for_a_loop_with_no_terminal_tool(monkeypatch):
    """The librarian, the co-lead's review and `drive_authoring` have no exit to force, so their
    exhaustion is a legitimate end and must keep the old behaviour."""
    model = _ToolSpammer()
    _drive_budget(model, cap=2, grace=20, monkeypatch=monkeypatch, terminal=False)
    assert model.calls <= 3, "grace opened for a loop that has no terminal tool to reach"


def test_grace_rounds_are_spent_separately_from_the_budget(monkeypatch):
    """Grace is a window, not extra budget: a filing inside it must not look like a role that
    stayed within its cap."""
    model = _SpamsThenFiles()
    _drive_budget(model, cap=2, grace=4, monkeypatch=monkeypatch)
    # It filed during grace, so it consumed more rounds than its cap allowed.
    assert model.calls > 2


def test_the_absolute_ceiling_cannot_cut_the_grace_window_short(monkeypatch):
    """Found live 2026-09-15 by the adversarial idling cell, not by this suite.

    The cell drove with `absolute_cap = cap + 2`. Grace opened at the budget cap and the ceiling
    then ended the loop two rounds later — unfiled, and with nothing recorded. A window a bound
    can retract is not a window, so the ceiling is raised to accommodate it.
    """
    from auto_research.agents.roles import ReactBudgetExhausted

    # A ceiling far below cap + grace: the loop must still run the full window and then die
    # loudly, rather than returning silently when the ceiling bites first.
    with pytest.raises(ReactBudgetExhausted):
        # `absolute_cap` deliberately BELOW cap + grace — the live cell's exact shape.
        _drive_budget(_ToolSpammer(), cap=2, grace=4, monkeypatch=monkeypatch, absolute_cap=4)


class _FilesBadlyThenWell(_ToolSpammer):
    """Calls turn_end and is refused `refusals` times, then files a valid one — inside grace.

    Human clarification 2026-09-15: *"turn_end() within grace can fail; it is acceptable if one
    turn_end() succeeded before the grace period exhausted."* A refused turn_end returns
    `is_error=True` and must NOT count as filed, and must not be fatal on its own.
    """

    def __init__(self, refusals: int):
        super().__init__()
        self.refusals = refusals
        self.turn_end_attempts = 0
        self.saw_grace = False

    async def call(self, messages, **kwargs):
        blob = " ".join(
            (getattr(m, "content", "") or "") if not isinstance(m, ToolResultMessage)
            else " ".join(p.content for p in m.results)
            for m in messages
        )
        if "run out of rounds" in blob:
            self.saw_grace = True
        if not self.saw_grace:
            return await super().call(messages, **kwargs)
        self.calls += 1
        self.turn_end_attempts += 1
        # A report is never written, so `turn_end` is refused by the validator — until the last
        # attempt, where one is written first so it validates.
        if self.turn_end_attempts > self.refusals:
            return ModelResponse(tool_calls=[
                ToolCall(id=f"e{self.calls}", name=EDIT_REPORT_TOOL,
                         arguments={"summary": "s", "findings": ["f"], "status": "succeeded",
                                    "rework": False}),
                ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={}),
            ])
        return ModelResponse(
            tool_calls=[ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={})]
        )


def test_a_refused_turn_end_inside_grace_is_not_fatal_and_one_success_is_enough(monkeypatch):
    """The clarified contract: failing turn_end within grace is fine; succeeding once ends it."""
    refused = {"n": 0}

    def _validator():
        refused["n"] += 1
        return ["the report is not complete"] if refused["n"] <= 2 else []

    model = _FilesBadlyThenWell(refusals=2)
    _drive_budget(model, cap=2, grace=10, monkeypatch=monkeypatch,
                  turn_end_validator=_validator)
    assert refused["n"] >= 3, "the validator was never consulted three times"
    assert model.turn_end_attempts >= 3, "the refused attempts did not happen"


def test_the_absolute_ceiling_cannot_cut_the_grace_window_short(monkeypatch):
    """Found LIVE 2026-09-15 by the adversarial idling cell, not by this suite.

    That cell drove with `absolute_cap = cap + 2`. Grace opened at the budget cap and the ceiling
    ended the loop two rounds later — unfiled, and with nothing recorded. A window a bound can
    retract is not a window, so the ceiling is raised to accommodate it and the loop reaches its
    fatal grace exhaustion instead of returning quietly.
    """
    from auto_research.agents.roles import ReactBudgetExhausted

    with pytest.raises(ReactBudgetExhausted):
        # absolute_cap deliberately BELOW cap + grace — the live cell's exact shape.
        _drive_budget(_ToolSpammer(), cap=2, grace=4, monkeypatch=monkeypatch, absolute_cap=4)

