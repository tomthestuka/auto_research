"""Phase 3 freeze, CLAUDE.md §9.3.1 "One exit" (and §2b), for the reviewer, librarian, worker and lead.

A unit ends only through an accepted ``turn_end``:

- a call placed after the accepted ``turn_end`` in its batch is answered and never run, and the
  filed report stands;
- a filed report, the lead's briefs included, cannot change in a resumed turn (the same-batch
  guard and the resume guard hold this; the report lock behind them is defence in depth and is not
  separately observable through the drivers today, so no cell claims it alone; ``turn_end`` recording
  the brief no link closed as filed is frozen);
- the cap opens a grace window, filing inside it is accepted, and a window spent unfiled raises
  ``ReactBudgetExhausted``, recorded on the ledger; the absolute ceiling holds the same window,
  and a resumed turn that spends it ends the run (D4, D6);
- a turn that ends unfiled yields no artifact written on the model's behalf, and each unit records
  the unfiled end;
- no prose turn, short or long, reads as a finish: each is nudged and the loop goes on.

The co-lead (tool-code reviewer) and the coding author are outside this clause: their exits are a
gated commit or an honest failure (§2b), so no cell here drives them.
"""

from __future__ import annotations

import asyncio

import pytest

from auto_research.agents.react import TOOLSPEC_PRODUCER
from tests.invariants.drivers import (
    CLAIM, NOT_RUN, QUESTION, ROLES, STOP, ReactBudgetExhausted, brief_step, coder_edit, open_run,
    report_content, report_tools, review_script, submit_step,
)

#: A claim, free of digits, the model writes to replace the filed one.
_EDITED = "The band ratios are listed for each product row."
_LATER = "The band ratios are shown for a single row."
_DRAFT = "A draft the model wrote and never submitted."
_GRACE = 4  # grace rounds, enough for a librarian whose two reads fall inside the window
_WRITE, _EDIT, _END = report_tools("lead")  # the report tools' names, the same for every role
_CAPS = {"react_loop_cap": 2, "librarian_react_loop_cap": 2, "react_loop_absolute_cap": 2,
         "react_grace_rounds": _GRACE}


def _spend(run, role):
    """One call that costs the role's budget."""
    if role == "librarian":
        return ("search_literature", {"source": "arxiv", "query": QUESTION})
    return run.harness_call(role, "list_dir").step()


def _edit(old: str, new: str):
    return (_EDIT, {"old_string": old, "new_string": new, "replace_all": True})


# ── a call after an accepted turn_end ──────────────────────────────────────────────────────


def _noting(seen: dict, key: str, count, item):
    """A script item that records ``count()`` at the moment the model is called, then answers ``item``."""
    def answer(_sent):
        seen[key] = count()
        return item(_sent) if callable(item) else item
    return answer


@pytest.mark.parametrize("role", ROLES)
def test_a_call_after_an_accepted_turn_end_is_answered_and_never_run(role, tmp_path):
    # §9.3.1 a call after it in the same batch is answered and never run; the filed report stands
    write, edit, end = report_tools(role)
    seen: dict = {}

    async def go():
        async with open_run(tmp_path) as run:
            def minted():
                return len(run.receipts.receipts(source=run.source(role)))

            fs = run.file_script(role)
            kept = "The band ratios are listed before the turn ends."
            look = run.harness_calls(role)[0]  # a harness call whose return the runtime mints
            batch = [fs[-1], _edit(kept, _EDITED), (write, {"content": report_content(role, claim=_LATER)}),
                     _spend(run, role), look.step()]
            # Before turn_end an edit is accepted and the look mints a receipt: the same calls are
            # what is refused, and mint nothing, after it.
            result = await run.turn(role, [
                *fs[:-1], _edit(CLAIM, kept), _noting(seen, "before_look", minted, look.step()),
                _noting(seen, "before_batch", minted, batch),
            ])
            return result, minted()

    result, after = asyncio.run(go())

    assert result.error is None and result.filed, (result.error, [(r.name, r.text) for r in result.tool_results])
    filed = result.report.model_dump_json()
    assert "listed before the turn ends" in filed
    assert _EDITED not in filed and _LATER not in filed
    edits = result.results_named(edit)
    assert [r.is_error for r in edits] == [False, True]
    assert NOT_RUN in edits[1].text and end in edits[1].text
    (rewrite,) = result.results_named(write)[-1:]
    assert rewrite.is_error and NOT_RUN in rewrite.text
    assert [r.is_error for r in result.results_named(end)] == [False]
    # The batch's budget-costing call and its harness look were answered the same way.
    assert all(r.is_error and NOT_RUN in r.text for r in result.tool_results[-2:])
    # The look before the turn_end minted a receipt; the one after it minted nothing.
    assert seen["before_batch"] == seen["before_look"] + 1 and after == seen["before_batch"]


# ── an accepted turn_end locks the report ──────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_filed_report_cannot_change_in_a_resumed_turn(role, tmp_path):
    # §9.3.1 an accepted turn_end is final: a resumed turn's edit is refused and the filed report stands
    # (the same-batch guard, the resume guard or the lock may satisfy it)
    _, edit, end = report_tools(role)

    async def go():
        async with open_run(tmp_path) as run:
            first = await run.turn(role, run.file_script(role))
            assert first.filed, first.error
            # A text reaches the finished seat and resumes its turn.
            return first, await run.resume(role, [_edit(CLAIM, _EDITED), (end, {"summary": "still filed"})])

    first, resumed = asyncio.run(go())

    assert resumed.error is None
    by_tool = {r.name: r for r in resumed.tool_results}
    assert by_tool[edit].is_error
    assert not by_tool[end].is_error  # the filed report is final, so ending again is accepted
    # The report production holds after the resumed turn (read back by the driver) is the filed one.
    assert resumed.report == first.report and _EDITED not in resumed.report.model_dump_json()


_BRIEFED = "Total counts per band"  # a phrase of the brief `brief_step` files
_BRIEF_EDITED = "Summed counts per band"


def test_a_resumed_lead_turn_cannot_change_a_filed_report_or_a_brief(tmp_path):
    # §9.3.1 after the accepted turn_end the turn's reports stand: a resumed turn's edits change none
    # (the same-batch guard, the resume guard or the lock may satisfy it)
    async def go():
        async with open_run(tmp_path, pipeline=True, config={"tool_author_submit_cap": 1}) as run:
            # The link commits, so its brief closes with it; an open link would refuse turn_end.
            lead = await run.turn(
                "lead", [brief_step(), submit_step(), *run.file_script("lead")],
                coder=[coder_edit()], colead=review_script(True),
                compactor=["The coder added the tool and its test."],
            )
            assert lead.filed, [(r.name, r.text) for r in lead.tool_results]
            resumed = await run.resume("lead", [_edit_brief(_BRIEFED, _BRIEF_EDITED), _edit(CLAIM, _EDITED),
                                                (_END, {"summary": "still filed"})])
            return lead, resumed, _briefs(run)

    lead, resumed, briefs = asyncio.run(go())

    edits = resumed.results_named(_EDIT)
    assert len(edits) == 2 and all(r.is_error for r in edits), edits
    assert resumed.report == lead.report and _EDITED not in resumed.report.model_dump_json()
    (brief,) = briefs
    assert _BRIEFED in brief and _BRIEF_EDITED not in brief


def _briefs(run) -> list[str]:
    """The text of every tool brief the run minted, as the receipts hold it."""
    return [run.receipts.text(r.handle) for r in run.receipts.receipts() if r.produced_by == TOOLSPEC_PRODUCER]


def _two_links():
    """One ``author_chain`` call briefing two links, the second a different tool."""
    tool, arguments = brief_step()
    second = {**arguments["first_link"], "proposed_tool_name": "band_means"}
    return tool, {**arguments, "additional_links": [second]}


def test_turn_end_records_a_brief_no_link_closed_as_filed(tmp_path):
    # §9.3.1 an accepted turn_end covers every report of the turn: the brief no link opened is minted
    # by it (the commit covers the first link's brief; nothing else reaches the second).
    seen: dict = {}

    async def go():
        # One author credit: the first link commits and no link can open the second.
        config = {"tool_author_submit_cap": 1, "tool_author_cap": 1}
        async with open_run(tmp_path, pipeline=True, config=config) as run:
            def briefs():
                return _briefs(run)

            lead = await run.turn(
                "lead",
                [_two_links(), submit_step(),
                 # Before turn_end the second brief is still the lead's to edit, and has no receipt.
                 _noting(seen, "before_edit", briefs, (_edit_brief_of("spec2", _BRIEFED, _BRIEF_EDITED))),
                 _noting(seen, "before_end", briefs, run.file_script("lead")[0]),
                 *run.file_script("lead")[1:]],
                coder=[coder_edit()], colead=review_script(True),
                compactor=["The coder added the tool and its test."],
            )
            return lead, briefs()

    lead, after = asyncio.run(go())

    assert lead.filed, [(r.name, r.text) for r in lead.tool_results]
    assert not [r for r in lead.tool_results if r.is_error], [(r.name, r.text) for r in lead.tool_results]
    assert len(seen["before_edit"]) == 1 and len(seen["before_end"]) == 1  # the commit minted spec1's
    # turn_end minted the second brief's receipt, as last edited.
    assert len(after) == 2
    assert [_BRIEF_EDITED in text for text in after] == [False, True]


def _edit_brief_of(report: str, old: str, new: str):
    return (_EDIT, {"report": report, "old_string": old, "new_string": new})


def _edit_brief(old: str, new: str):
    return _edit_brief_of("spec1", old, new)


# ── the cap, then grace, then ReactBudgetExhausted ─────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_spent_budget_opens_grace_and_filing_inside_it_is_accepted(role, tmp_path):
    # §9.3.1 exhausting the cap opens grace; a report filed inside it is accepted
    async def go():
        async with open_run(tmp_path, config=_CAPS) as run:
            fs = run.file_script(role)
            # The librarian's two reads already spend its budget; the others spend it first.
            prelude = [] if role == "librarian" else [_spend(run, role), _spend(run, role)]
            return await run.turn(role, [*prelude, *fs])

    result = asyncio.run(go())

    assert result.error is None and result.filed, (result.error, [(r.name, r.text) for r in result.tool_results])
    assert len(result.grace_opened()) == 1
    assert not result.grace_exhausted() and not result.outcome.recorded


@pytest.mark.parametrize("role", ROLES)
def test_a_grace_window_spent_unfiled_raises_and_is_recorded(role, tmp_path):
    # §9.3.1 cap -> grace -> ReactBudgetExhausted, recorded; §9.3.8 it propagates, never caught
    async def go():
        async with open_run(tmp_path, config=_CAPS) as run:
            return await run.turn(role, [_spend(run, role)] * (2 + _GRACE))

    result = asyncio.run(go())

    assert isinstance(result.error, ReactBudgetExhausted), repr(result.error)
    assert not result.filed
    names = result.event_names()
    (opened,) = result.grace_opened()
    (exhausted,) = result.grace_exhausted()
    assert names.index(opened["event"]) < names.index(exhausted["event"])
    assert exhausted["data"]["grace"] == _GRACE
    assert result.outcome.recorded and not result.outcome.filed and result.outcome.handed is None
    assert not result.results_named(_END)


@pytest.mark.parametrize("role", ROLES)
def test_the_absolute_ceiling_holds_the_same_grace_window(role, tmp_path):
    # §9.3.1 the ceiling is a cap like the budget (D4): free calls spinning to it open grace, then raise
    ceiling = {"react_loop_cap": 2, "librarian_react_loop_cap": 2, "react_loop_absolute_cap": 6,
               "react_grace_rounds": 2}

    async def go():
        async with open_run(tmp_path, config=ceiling) as run:
            # A turn_end with nothing written is refused and costs no budget: only the ceiling advances.
            return await run.turn(role, [(_END, {"summary": "nothing to file"})] * 8)

    result = asyncio.run(go())

    assert isinstance(result.error, ReactBudgetExhausted), repr(result.error)
    assert not result.filed and all(r.is_error for r in result.results_named(_END))
    (opened,) = result.grace_opened()
    (exhausted,) = result.grace_exhausted()
    assert opened["data"]["at"] == exhausted["data"]["at"] == "ceiling"
    assert result.event_names().index(opened["event"]) < result.event_names().index(exhausted["event"])
    assert result.outcome.recorded and result.outcome.handed is None


@pytest.mark.parametrize("role", ROLES)
def test_a_resumed_turn_that_spends_its_grace_ends_the_run(role, tmp_path):
    # §9.3.1 / §9.3.8 (D6) ReactBudgetExhausted in a resumed turn is never caught: it ends the run
    async def go():
        async with open_run(tmp_path, config=_CAPS) as run:
            first = await run.turn(role, run.file_script(role))
            assert first.filed, first.error
            return await run.resume(role, [_spend(run, role)] * (2 + _GRACE))

    resumed = asyncio.run(go())

    assert isinstance(resumed.run_failure, ReactBudgetExhausted), repr(resumed.run_failure)
    assert len(resumed.grace_exhausted()) == 1


# ── a turn that ends unfiled composes nothing ──────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_turn_that_ends_unfiled_yields_no_artifact_the_model_did_not_file(role, tmp_path):
    # §2b no path composes an artifact on a model's behalf; an unfiled draft is no report
    async def go():
        async with open_run(tmp_path) as run:
            fs = run.file_script(role, claim=_DRAFT)
            first = await run.turn(role, [*fs[:-1], STOP])  # the draft is written, turn_end never called
            assert first.outcome.failed, "an unfiled turn ends as a failure"
            # What the runtime hands on goes to its consumer as it is.
            return first, await run.consume(first)

    first, handoff = asyncio.run(go())
    outcome = first.outcome

    assert not first.filed and not first.results_named(_END)
    assert _DRAFT in outcome.drafted  # the model did write it
    assert not outcome.filed
    if outcome.handed is not None:
        assert _DRAFT not in outcome.handed.model_dump_json()
    if handoff.turn is not None:
        assert outcome.failure_record, "what reaches the consumer is the runtime's own failure record"
        shown = handoff.shown()
        assert _DRAFT not in shown, "the model's unfiled draft reached its consumer as a report"
        assert '"status": "failed"' in shown


#: Roles whose unfiled end leaves no record on the ledger today (found by running the cell).
_UNRECORDED_TODAY = {"reviewer", "librarian", "lead"}


@pytest.mark.parametrize("role", [
    pytest.param(r, marks=pytest.mark.xfail(
        r in _UNRECORDED_TODAY, strict=True,
        reason="built in Phase 3 step 1: every unit records its unfiled end (§2b)"))
    for r in ROLES])
def test_every_units_unfiled_end_is_recorded(role, tmp_path):
    # §2b a loop that ends unfiled records an honest failure, for every role
    async def go():
        async with open_run(tmp_path) as run:
            fs = run.file_script(role, claim=_DRAFT)
            return await run.turn(role, [*fs[:-1], STOP])

    result = asyncio.run(go())

    assert not result.filed and result.outcome.failed
    assert result.outcome.recorded, "the unfiled end left no failure on the ledger"


# ── no heuristic reads a turn as finished ──────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_prose_turn_short_or_long_is_nudged_and_never_files(role, tmp_path):
    # §2b no heuristic decides a turn was a finish (the 58-character turn of 2026-09-15)
    short = "Done."
    sixty = "The analysis is complete and the result is as stated above"  # 58 characters
    long = "The band ratios rise with the count total, which settles the question. " * 8
    assert len(sixty) == 58

    async def go():
        async with open_run(tmp_path) as run:
            return await run.turn(role, [short, sixty, long, *run.file_script(role)])

    result = asyncio.run(go())

    assert result.error is None and result.filed, (result.error, [(r.name, r.text) for r in result.tool_results])
    assert result.silent_turns() == [True, True, True]
    # Each prose turn was answered with a request to call a tool, and the loop went on.
    assert len(result.nudges()) == 3
    # The filed report is the one the model wrote with the report tools; no prose went into it.
    filed = result.report.model_dump_json()
    assert CLAIM in filed and short not in filed and "settles the question" not in filed
    assert [r.is_error for r in result.results_named(_END)] == [False]
