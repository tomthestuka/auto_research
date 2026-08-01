"""Deterministic tests for `MonitorToolProvider` — the `/status` reporter's view of the ledger.

The reporter reads the run's own monitor JSONL through three tools instead of being handed a
fixed digest. What matters here, in order:

- **the payload is reachable.** The old digest dropped every event's ``data``, so "what did the
  reviewer decide?" could see that a verdict happened and never what it said. That is the whole
  reason this provider exists.
- **every result is bounded by construction**, whatever the model asks for. An agent reading its
  own run log is the one place a harness can feed itself without limit (openai/codex#27131), so
  the caps are enforced in code, not requested in a prompt.
- **read-only is structural**: no tool takes a path, so there is nothing to confine.

No LLM: the provider is pure functions over a JSONL file.
"""

from __future__ import annotations

import asyncio
import json

from auto_research.agents.react import MonitorToolProvider


def _write_ledger(path, events) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def _event(seq, **over):
    base = {
        "run_id": "r",
        "sequence": seq,
        "created_at": f"2026-07-31T10:{seq:02d}:00+00:00",
        "team": "analysts",
        "event": "analyst_worker.analyze.tool_call",
        "level": 7,
        "summary": f"event {seq}",
        "actor": "analyst_worker_0",
        "data": {},
        "cost_usd_total": float(seq),
    }
    base.update(over)
    return base


def _ledger(tmp_path, events=None):
    path = tmp_path / "monitor.jsonl"
    _write_ledger(path, events if events is not None else [_event(i) for i in range(1, 6)])
    return MonitorToolProvider(path)


def _call(provider, name, **args):
    return asyncio.run(provider.execute(name, args)).text


# ── brief_history: the index ───────────────────────────────────────────────────────────────────


def test_history_rows_carry_seq_cost_elapsed_actor_and_summary(tmp_path):
    """Cost and elapsed ride in the index because two of the three things `/status` is asked are
    answerable from it alone — without them the model opens an event to learn what a row knew."""
    out = _call(_ledger(tmp_path), "brief_history")

    first = out.splitlines()[0]
    assert first.startswith("#1 · L7 · $1.00 · +0m · analysts · analyst_worker_0 · ")
    assert "analyst_worker.analyze.tool_call — event 1" in first
    assert "5 of 5 matching events" in out


def test_history_filters_combine(tmp_path):
    provider = _ledger(tmp_path, [
        _event(1, level=2, event="reviewer.analysis_review.verdict", team="reviewer",
               actor="reviewer", summary="verdict returned"),
        _event(2, level=7, event="analyst_lead.drive_authoring.tool_call", actor="analyst_lead"),
        _event(3, level=10, event="cost.call", actor="analyst_lead"),
        _event(4, level=3, event="analyst_lead.drive_authoring.gate", actor="analyst_lead"),
    ])

    assert "#1" in _call(provider, "brief_history", max_lvl=4) and "#3" not in _call(
        provider, "brief_history", max_lvl=4
    )
    lead_only = _call(provider, "brief_history", actor="analyst_lead", max_lvl=4)
    assert "#4" in lead_only and "#1" not in lead_only and "#3" not in lead_only
    prefixed = _call(provider, "brief_history", event_prefix=["reviewer."])
    assert "#1" in prefixed and "#2" not in prefixed
    assert "#1" in _call(provider, "brief_history", contains="VERDICT")  # case-insensitive
    assert "#3" in _call(provider, "brief_history", min_seq=3, max_seq=3)


def test_history_accepts_a_bare_string_for_the_array_filter(tmp_path):
    """Models pass either a string or a list for an array-typed argument; both must work."""
    provider = _ledger(tmp_path, [_event(1, event="reviewer.x"), _event(2, event="analyst.y")])
    assert "#1" in _call(provider, "brief_history", event_prefix="reviewer.")


def test_history_order_head_reads_the_start_of_the_run(tmp_path):
    provider = _ledger(tmp_path, [_event(i) for i in range(1, 11)])

    tail = _call(provider, "brief_history", limit=2)
    head = _call(provider, "brief_history", limit=2, order="head")

    assert "#9" in tail and "#10" in tail
    assert "#1" in head and "#2" in head
    assert head.splitlines()[0].startswith("#1")  # rows always read oldest→newest


def test_history_limit_is_capped_however_large_the_model_asks(tmp_path):
    provider = _ledger(tmp_path, [_event(i) for i in range(1, 501)])
    out = _call(provider, "brief_history", limit=100_000)
    assert len([ln for ln in out.splitlines() if ln.startswith("#")]) == 400


def test_history_reports_no_match_rather_than_an_empty_reply(tmp_path):
    assert "No events match" in _call(_ledger(tmp_path), "brief_history", actor="nobody")


# ── ledger_event: the payload, windowed ────────────────────────────────────────────────────────


def test_ledger_event_returns_the_payload_the_digest_used_to_drop(tmp_path):
    provider = _ledger(tmp_path, [
        _event(1, level=2, event="reviewer.analysis_review.verdict",
               summary="Reviewer returned final/rework verdict",
               data={"verdict": {"accepted": True, "rationale": "the PD is 2.1σ"}}),
    ])
    out = _call(provider, "ledger_event", sequence=1)

    assert "the PD is 2.1σ" in out          # the answer, unreachable before
    assert "event: reviewer.analysis_review.verdict" in out
    assert "end of payload" in out


def test_ledger_event_windows_a_large_payload_and_names_the_next_offset(tmp_path):
    provider = _ledger(tmp_path, [_event(1, data={"blob": "x" * 50_000})])

    first = _call(provider, "ledger_event", sequence=1, max_chars=1_000)
    assert "data chars 0–1000 of" in first and "continue with bias=1000" in first

    second = _call(provider, "ledger_event", sequence=1, max_chars=1_000, bias=1_000)
    assert "data chars 1000–2000 of" in second


def test_ledger_event_negative_bias_reads_the_tail(tmp_path):
    """A long tool dump keeps its errors and results at the end."""
    provider = _ledger(tmp_path, [_event(1, data={"out": "y" * 5_000 + "FIT FAILED"})])
    out = _call(provider, "ledger_event", sequence=1, max_chars=200, bias=-200)
    assert "FIT FAILED" in out and "end of payload" in out


def test_ledger_event_window_is_capped(tmp_path):
    provider = _ledger(tmp_path, [_event(1, data={"blob": "x" * 200_000})])
    out = _call(provider, "ledger_event", sequence=1, max_chars=1_000_000)
    assert "data chars 0–32000 of" in out


def test_ledger_event_missing_sequence_says_so(tmp_path):
    assert "No event #999" in _call(_ledger(tmp_path), "ledger_event", sequence=999)


# ── ledger_grep: find by content ───────────────────────────────────────────────────────────────


def test_grep_finds_an_event_by_payload_content_and_returns_index_rows(tmp_path):
    """The only way to find an event by what is inside it — and it returns rows, never the raw
    payload, so one match inside a 130k-char event cannot flood the context."""
    provider = _ledger(tmp_path, [
        _event(1, data={"note": "nothing here"}),
        _event(2, summary="acquire failed", data={"stderr": "ObsID 04004001 is encrypted"}),
    ])
    out = _call(provider, "ledger_grep", pattern="04004001")

    assert "#2" in out and "#1" not in out
    assert "encrypted" not in out            # payload text never echoed
    assert "open one with ledger_event" in out


def test_grep_respects_a_level_ceiling_and_a_limit(tmp_path):
    provider = _ledger(tmp_path, [
        _event(i, level=(2 if i < 3 else 9), data={"k": "needle"}) for i in range(1, 6)
    ])
    assert len([ln for ln in _call(provider, "ledger_grep", pattern="needle",
                                   max_lvl=4).splitlines() if ln.startswith("#")]) == 2
    assert len([ln for ln in _call(provider, "ledger_grep", pattern="needle",
                                   limit=1).splitlines() if ln.startswith("#")]) == 1


def test_grep_with_no_hit_says_so(tmp_path):
    assert "No event payload contains" in _call(_ledger(tmp_path), "ledger_grep", pattern="zzz")


# ── the file itself ────────────────────────────────────────────────────────────────────────────


def test_a_torn_final_line_is_skipped_not_fatal(tmp_path):
    """A hard kill can stop mid-write; the ledger is append-only, so only that line is lost."""
    path = tmp_path / "monitor.jsonl"
    path.write_text(json.dumps(_event(1)) + "\n" + '{"sequence": 2, "tea', encoding="utf-8")
    out = _call(MonitorToolProvider(path), "brief_history")
    assert "#1" in out and "1 of 1 matching" in out


def test_a_missing_ledger_is_reported_not_raised(tmp_path):
    provider = MonitorToolProvider(tmp_path / "nope.jsonl")
    assert "empty or unreadable" in _call(provider, "brief_history")
    assert "empty or unreadable" in _call(MonitorToolProvider(None), "brief_history")


def test_tools_take_no_path_argument(tmp_path):
    """Read-only is structural: the provider is built with the run's ledger and the model cannot
    point any tool at another file."""
    schemas = {t["name"]: t["input_schema"]["properties"] for t in _ledger(tmp_path).tool_definitions()}
    assert set(schemas) == {"brief_history", "ledger_event", "ledger_grep"}
    assert not any("path" in props for props in schemas.values())


def test_level_direction_is_spelled_out_where_the_model_reads_it(tmp_path):
    """1 = most important, 10 = chattiest is exactly the inversion a model guesses wrong."""
    history = next(
        t for t in _ledger(tmp_path).tool_definitions() if t["name"] == "brief_history"
    )
    assert "1 (most important)" in history["description"]
    assert "MORE important" in history["input_schema"]["properties"]["max_lvl"]["description"]


def test_unknown_tool_is_an_error_outcome(tmp_path):
    outcome = asyncio.run(_ledger(tmp_path).execute("rm_rf", {}))
    assert outcome.is_error and "unknown tool" in outcome.text


# ── StatusMonitorAgent: the reporter driving those tools ───────────────────────────────────────


class _ScriptedModel:
    """A model that emits a scripted sequence of turns and records what it was asked."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def call(self, messages, **kwargs):
        self.calls.append({"messages": messages, "tools": kwargs.get("tools")})
        return self._turns.pop(0) if self._turns else _text("")


def _text(content):
    from auto_research.models.types import ModelResponse

    return ModelResponse(content=content)


def _tool_turn(name, arguments):
    from auto_research.models.types import ModelResponse, ToolCall

    return ModelResponse(
        content="", tool_calls=[ToolCall(id="c1", name=name, arguments=arguments)]
    )


def _agent(model):
    from auto_research.agents.roles import StatusMonitorAgent

    return StatusMonitorAgent(model=model)


def test_reporter_searches_then_answers(tmp_path):
    """The intended shape: look at the index, open what matters, answer."""
    path = tmp_path / "monitor.jsonl"
    _write_ledger(path, [
        _event(1, level=2, event="reviewer.analysis_review.verdict",
               data={"verdict": {"accepted": True, "rationale": "PD 2.1σ, accepted"}}),
    ])
    model = _ScriptedModel([
        _tool_turn("brief_history", {"max_lvl": 4}),
        _tool_turn("ledger_event", {"sequence": 1}),
        _text("The reviewer accepted it (#1): PD 2.1σ."),
    ])

    answer = asyncio.run(_agent(model).report("what did the reviewer decide?", jsonl_path=path))

    assert answer == "The reviewer accepted it (#1): PD 2.1σ."
    assert len(model.calls) == 3
    assert [t["name"] for t in model.calls[0]["tools"]] == [
        "brief_history", "ledger_event", "ledger_grep",
    ]


def test_reporter_is_given_only_the_monitor_tools(tmp_path):
    """No file reads, no web, no sandbox: the reporter cannot reach past its own run's ledger."""
    path = tmp_path / "monitor.jsonl"
    _write_ledger(path, [_event(1)])
    model = _ScriptedModel([_text("all quiet")])

    asyncio.run(_agent(model).report("", jsonl_path=path))

    names = {t["name"] for t in model.calls[0]["tools"]}
    assert names == {"brief_history", "ledger_event", "ledger_grep"}


def test_reporter_answers_even_when_the_loop_ends_on_its_cap(tmp_path, monkeypatch):
    """A human waiting on a phone must not get silence because the investigation ran long."""
    from auto_research.agents import roles

    monkeypatch.setattr(roles, "_status_react_loop_cap", lambda: 1)
    path = tmp_path / "monitor.jsonl"
    _write_ledger(path, [_event(1)])
    model = _ScriptedModel([
        _tool_turn("brief_history", {}),      # burns the only iteration, no prose
        _text("Ran out of turns, but: one event so far."),
    ])

    answer = asyncio.run(_agent(model).report("status?", jsonl_path=path))

    assert answer == "Ran out of turns, but: one event so far."
    assert model.calls[-1]["tools"] is None   # the final ask carries no tools
