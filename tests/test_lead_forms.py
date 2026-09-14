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
    CHECK_CODER_TOOL,
    DRIVE_CODER_TOOL,
    EDIT_REPORT_TOOL,
    LEAD_FORM_TOOLS,
    LEAD_REPORT_FIELDS,
    LEAD_STATUS_VALUES,
    RESPOND_TO_REVIEW_TOOL,
    SUBMIT_TOOL,
    TURN_END_TOOL,
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

    coding = _assistant(DRIVE_CODER_TOOL)
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
    assert DRIVE_CODER_TOOL not in names and "read_file" not in names
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
    assert DRIVE_CODER_TOOL not in declared


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
            produced_by="tool", created_at=datetime.now().astimezone(), sha256="0" * 64,
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
        coding = _assistant(DRIVE_CODER_TOOL)
        messages.extend([coding, _results_for(coding)])

    closing = _assistant(SUBMIT_TOOL)
    messages.append(closing)
    aio.run(driver.advance())
    messages.append(_results_for(closing))

    assert [r.status for r in driver.outcome.authored] == ["committed", "failed"], (
        "a failed link must not end the chain (F9)"
    )
    names = [c.name for m in messages for c in (getattr(m, "tool_calls", None) or ())]
    assert DRIVE_CODER_TOOL not in names, "both arms compact, so no coding turn survives"
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
    authoring = {DRIVE_CODER_TOOL, CHECK_CODER_TOOL, SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL}
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
        "check_coder would report 'you have not sent the coder anything yet' to a lead whose "
        "coder is working"
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


def test_submitting_while_the_coder_is_still_writing_is_refused():
    """The gate reads the working tree, so a mid-write submit judges a half-written tool.

    Unreachable before the seed drive was backgrounded — a coder could not be working while the
    lead acted — so the guard lands with the change that makes the race possible.
    """
    from auto_research.agents.react import execute_submit

    session = types.SimpleNamespace(drive_status="working")
    out = _run(execute_submit(session, None))
    assert out.is_error
    assert "still working" in out.text and CHECK_CODER_TOOL in out.text


# ---------------------------- the coder's question reaches the lead's OWN loop (2026-09-12) ----


class _AskingSession:
    """A session double with the question park and the one send path."""

    initial_message = "brief"

    def __init__(self, status: str = "working") -> None:
        self.drive_status = status
        self.questions: list[str] = []
        self.sent: list[str] = []
        self.steered: list[str] = []
        self.started: list[str] = []

    def park_question(self, message: str) -> None:
        self.questions.append(message)

    def take_questions(self) -> list[str]:
        pending, self.questions = self.questions, []
        return pending

    async def send(self, message: str) -> str:
        self.sent.append(message)
        if self.drive_status == "working":
            self.steered.append(message)
            return "steered"
        self.started.append(message)
        return "started"


def test_a_parked_question_is_collected_once_for_injection():
    """The loop PULLS the question at a boundary it knows is safe; the session never pushes.

    Same seam and same hold rule as the background-coder notice, for the reason A2 exists: an
    injected user turn appended after an assistant turn that left a server-tool call unresolved
    is the 400 that killed a run at $42.11 with four tools committed.
    """
    from auto_research.agents.roles import _coder_questions

    cycle = _cycle()
    provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    session = _AskingSession()
    provider.authoring_session = session
    session.park_question("ENERGY or PI?")

    first = _coder_questions(provider)
    assert len(first) == 1 and "ENERGY or PI?" in first[0]
    assert CHECK_CODER_TOOL not in first[0], "the lead is pointed at the reply path, not at polling"
    assert DRIVE_CODER_TOOL in first[0], "the lead must be told how to answer"
    assert _coder_questions(provider) == [], "a collected question must not be re-injected"


def test_a_session_with_no_question_park_is_simply_quiet():
    """A benchmark's session or an older double predates the channel and must not crash the loop."""
    from auto_research.agents.roles import _coder_questions

    cycle = _cycle()
    provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    provider.authoring_session = object()
    assert _coder_questions(provider) == []


def test_drive_coder_steers_a_working_coder_instead_of_being_refused():
    """ONE lead->coder path. A working coder is steered; an idle one is given a new turn.

    The old path could only start a turn, so a lead with something to say to a working coder was
    told "the coder is already working on an instruction" — the refusal it hit twice in the live
    run of 2026-09-12 while holding the answer to that coder's own question.
    """
    from auto_research.agents.react import execute_drive_coder

    working = _AskingSession(status="working")
    out = _run(execute_drive_coder(working, {"instructions": "use PI"}))
    assert not out.is_error
    assert working.steered == ["use PI"]
    assert "inside the turn it is running" in out.text

    idle = _AskingSession(status="idle")
    out = _run(execute_drive_coder(idle, {"instructions": "next link"}))
    assert not out.is_error and idle.started == ["next link"]
