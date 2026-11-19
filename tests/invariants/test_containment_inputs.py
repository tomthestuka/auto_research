"""Freezes CLAUDE.md §9.3.8 (contained failure) and §9.3.9 (inputs validated) for the four graph roles.

Every cell drives the role's own entry point through ``tests/invariants/drivers.py``; only the
provider network, the web network and the disk under the receipt store are faked.

§9.3.8: a provider exception ends that unit's turn as a recorded ``ModelCallFailed`` and the seat
serves its next turn; a cancel and ``ReactBudgetExhausted`` pass every containment; a tool's
fault is an ``is_error`` result the model reads and the turn goes on. The one gap built at
Phase 3 step 1 is a fault escaping ``tools.execute`` (``agents/roles.py`` ``_react_loop``).

§9.3.9: model inputs (each role's ``view_receipt`` attaches a peer's figure to the next model call;
a member that cannot take it gets a note; decoding and limits are ``tests/test_media_guards.py``'s)
and tool inputs (the worker's toolkit calls are judged by argument kind before they run;
the other roles' harness tools take no such check until step 1).
"""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import anthropic
import httpx
import pytest

from auto_research.provenance.refs import argument_value
from tests.invariants import drivers as d
from tests.invariants.drivers import ROLES, STOP, ModelCallFailed, ReactBudgetExhausted, open_run, tool_named

_REQUEST = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
_NET_DOWN = "the network is down"


def _bad_request() -> anthropic.BadRequestError:
    """A 400 no retry or reduction of the conversation can fix: Anthropic's refusal of a tool schema."""
    message = "tools.3.custom.input_schema: JSON schema is invalid"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return anthropic.BadRequestError(f"Error code: 400 - {message}",
                                     response=httpx.Response(400, request=_REQUEST, json=body), body=body)


def _overloaded() -> anthropic.APIStatusError:
    return anthropic.APIStatusError("Error code: 529 - overloaded",
                                    response=httpx.Response(529, request=_REQUEST), body=None)


def _recorded(result, suffix: str) -> list[dict]:
    """The unit's own failure records of the turn (``Outcome.recorded``) whose event ends with ``suffix``."""
    return [e for e in result.outcome.recorded if e["event"].endswith(suffix)]


# ── §9.3.8 contained failure ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_provider_exception_ends_the_turn_as_a_recorded_failure_and_the_seat_serves_on(role, tmp_path):
    # §9.3.8 a provider exception becomes that unit's recorded failure, never a dead process
    async def go():
        async with open_run(tmp_path) as run:
            failed = await run.turn(role, [_bad_request()])
            again = await run.turn(role, run.file_script(role))
            return failed, again

    failed, again = asyncio.run(go())

    assert isinstance(failed.error, ModelCallFailed) and failed.run_failure is None, failed.error
    assert failed.error.agent == failed.unit and isinstance(failed.error.cause, anthropic.BadRequestError)
    assert failed.error.status_code == 400
    (event,) = _recorded(failed, d.MODEL_CALL_FAILED)
    assert event["data"]["error_type"] == "BadRequestError" and event["data"]["status_code"] == 400
    assert event["data"]["filed"] is False and not failed.filed
    assert failed.outcome.failed and failed.outcome.raised is failed.error
    # The accepted counterpart: the same seat, in the same run, files its next turn.
    assert again.error is None and again.filed and not again.outcome.recorded


@pytest.mark.parametrize("role", ROLES)
def test_a_transient_provider_error_is_retried_and_the_turn_files(role, tmp_path):
    # §9.3.8 containment is selective: what retry can fix never ends the turn
    async def go():
        async with open_run(tmp_path) as run:
            return await run.turn(role, [_overloaded(), *run.file_script(role)])

    result = asyncio.run(go())

    assert result.error is None and result.filed
    assert not result.outcome.recorded and not result.outcome.failed


def _cancelled_while_calling_a_tool(sent):
    """The run task is cancelled (``/stop``) while the model's tool call runs: the next await raises."""
    asyncio.current_task().cancel()
    return ("web_fetch", {"url": "https://x.test/p"})


@pytest.mark.parametrize("role", ROLES)
def test_a_cancel_is_never_contained(role, tmp_path):
    # §9.3.8 cancellation is not an exception a unit contains
    async def go():
        async with open_run(tmp_path) as run:
            with pytest.raises(asyncio.CancelledError):
                await run.turn(role, [_cancelled_while_calling_a_tool])
            again = await run.turn(role, run.file_script(role))  # the seat serves on
            return [e["event"] for e in run.ledger_events()], again

    events, again = asyncio.run(go())

    assert not [e for e in events if e.endswith(d.MODEL_CALL_FAILED)]
    assert again.filed and again.error is None


@pytest.mark.parametrize("role", ROLES)
def test_react_budget_exhausted_is_raised_never_caught(role, tmp_path):
    # §9.3.8 ReactBudgetExhausted is never caught; §9.3.1 exhausting a cap is not an exit
    config = {"react_loop_cap": 3, "librarian_react_loop_cap": 3, "react_grace_rounds": 0}
    fetch = ("web_fetch", {"url": "https://x.test/p"})

    async def go():
        async with open_run(tmp_path, config=config) as run:
            spent = await run.turn(role, [fetch, fetch, fetch])
            again = await run.turn(role, run.file_script(role))
            return spent, again

    spent, again = asyncio.run(go())

    assert isinstance(spent.error, ReactBudgetExhausted), spent.error
    assert not isinstance(spent.error, ModelCallFailed) and not spent.filed
    assert spent.outcome.failed and spent.outcome.recorded
    assert [e["data"]["at"] for e in _recorded(spent, d.GRACE_EXHAUSTED)] == ["budget"]
    assert not _recorded(spent, d.MODEL_CALL_FAILED)
    # The accepted counterpart: within the same cap a turn that files is not refused.
    assert again.error is None and again.filed


def _flaky_fetch():
    """``WebSource.fetch`` whose network is down for its first call only."""
    from tests.invariants.drivers import WEB_PAGE

    calls = []

    async def fetch(self, url, *, depth="advanced"):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError(_NET_DOWN)
        return WEB_PAGE

    return fetch


@pytest.mark.parametrize("role", ROLES)
def test_a_tool_fault_becomes_an_error_result_and_the_turn_files(role, tmp_path):
    # §9.3.8 a tool exception becomes an is_error result the model reads
    from auto_research.sources import WebSource

    page = ("web_fetch", {"url": "https://x.test/p"})

    async def go():
        async with open_run(tmp_path) as run:
            with mock.patch.object(WebSource, "fetch", _flaky_fetch()):
                return await run.turn(role, [page, page, *run.file_script(role)])

    result = asyncio.run(go())

    first, second = result.results_named("web_fetch")
    assert first.is_error and _NET_DOWN in first.text
    assert not second.is_error  # the accepted counterpart: the network is back
    assert result.error is None and result.filed


def test_a_toolkit_tool_that_raises_in_the_workers_repl_is_an_error_result(tmp_path):
    # §9.3.8 a toolkit tool's exception becomes an is_error result
    async def go():
        async with open_run(tmp_path) as run:
            tool = lambda sent: tool_named(sent, "band_pa_summary")  # noqa: E731
            return await run.turn("worker", [
                lambda sent: (tool(sent), {"csv_path": str(run.product.parent / "absent.csv")}),
                lambda sent: (tool(sent), {"csv_path": str(run.product)}),
                *run.file_script("worker"),
            ])

    result = asyncio.run(go())

    broken, fine = result.results_named(result.declared("band_pa_summary"))
    assert broken.is_error and "FileNotFoundError" in broken.text
    assert not fine.is_error and result.error is None and result.filed


#: The unit a role reports to (§9.3.8: a harness failure kills the agent, with a message to its parent).
_PARENT = {"worker": "lead", "lead": "reviewer", "librarian": "reviewer", "reviewer": "human"}


def _lock_blobs(store, mode: int) -> None:
    """Make the receipt store's blob folders read-only (``0o500``) or writable again: a disk fault
    from outside our code, which fails every write of a new blob."""
    store.blobs.mkdir(parents=True, exist_ok=True)
    for folder in (store.blobs, *[p for p in store.blobs.iterdir() if p.is_dir()]):
        folder.chmod(mode)


@pytest.mark.xfail(strict=True, raises=AssertionError,  # today: the OSError ends the turn raised, with no ledger record
                   reason="built in Phase 3 step 1: a harness failure ends the unit's turn as its recorded failure "
                   "and tells its parent (today it propagates raw: no failure record, no message to the parent)")
@pytest.mark.skipif(os.geteuid() == 0, reason="chmod does not stop root writing: the disk fault cannot be made")
@pytest.mark.parametrize("role", ROLES)
def test_a_harness_fault_kills_the_agent_with_a_message_to_its_parent(role, tmp_path):
    # §9.3.8 a fault of our own runtime code (here: the receipt store's disk) ends that unit's turn as its
    # recorded failure, never an is_error result the model reads; its parent is told; the run is not dead
    human = d.ScriptedHuman([lambda query: "noted"], optional=True)  # the reviewer's parent; the reply may go unread

    def fetch(sent):
        _lock_blobs(run.receipts, 0o500)
        return ("web_fetch", {"url": "https://x.test/p"})

    async def go():
        nonlocal run
        async with open_run(tmp_path, human=human) as run:
            parent = _PARENT[role]
            before = len(run.inbox_of(parent))
            try:
                failed = await run.turn(role, [fetch])
            finally:
                _lock_blobs(run.receipts, 0o700)
            # "The parent is told": a message to a unit, or a query or a notice to the human.
            told = [t for t in run.inbox_of(parent)[before:] if parent == "human" or t.sender == failed.unit]
            again = await run.turn(role, run.file_script(role))  # the run is not dead: the seat serves on
            return failed, told, again

    run = None
    failed, told, again = asyncio.run(go())

    assert not failed.results_named("web_fetch"), "the model was shown the fault"
    assert not failed.filed and failed.outcome.failed
    assert failed.outcome.recorded  # the failure is on the ledger for that unit
    assert told  # the parent received a message saying so
    assert again.error is None and again.filed


# ── §9.3.9 model inputs ───────────────────────────────────────────────────────────────────


async def _peer_figure(run):
    """The receipt of a figure a peer seat's real toolkit call drew (``workers=2``)."""
    source = run.source("worker", 1)
    plot = lambda sent: (tool_named(sent, "plot_band_ratio"),  # noqa: E731
                         {"ratios": {"$ref": f"{d.handles_shown(sent, source, 'r')[0]}#/ratios"}})
    table = lambda sent: (tool_named(sent, "write_band_table"), {"csv_path": str(run.product)})  # noqa: E731
    await run.turn("worker", [table, plot, STOP], seat=1)
    (figure,) = [r for r in run.receipts.receipts(source=source) if r.kind == "figure"]
    return figure


@pytest.mark.parametrize("role", ROLES)
def test_each_roles_view_receipt_hands_the_figure_to_the_model_call(role, tmp_path):
    # §9.3.9 model inputs: any unit may view any receipt of the run; the figure reaches the next
    # model call as an attachment (decoding and limits: tests/test_media_guards.py)
    async def go():
        async with open_run(tmp_path, workers=2) as run:
            figure = await _peer_figure(run)
            result = await run.turn(role, [("view_receipt", {"handle": figure.handle}), STOP])
            return figure, result

    figure, result = asyncio.run(go())

    (viewed,) = result.results_named("view_receipt")
    assert not viewed.is_error, viewed.text
    # The call that carried the view's result holds the figure, named by its receipt.
    parts = [p for m in result.sent_after("view_receipt").messages for p in getattr(m, "results", None) or ()]
    assert [[f.receipt for f in p.figures] for p in parts] == [[figure.handle]]
    # The accepted counterpart: the call before the view held none.
    before = [s for s in result.sent if "view_receipt" not in s.result_names()]
    assert before and not [p for s in before for m in s.messages
                           for p in getattr(m, "results", None) or () if p.figures]


def test_an_image_for_a_text_only_member_is_withheld_with_a_note_the_model_reads(tmp_path):
    # §9.3.9 model inputs: a medium the member cannot take becomes a note (the sanitiser in ModelHandle.call)
    async def go():
        async with open_run(tmp_path, provider="deepseek", workers=2) as run:
            figure = await _peer_figure(run)
            return await run.turn("lead", [("view_receipt", {"handle": figure.handle}), STOP]), figure

    result, figure = asyncio.run(go())

    sent = result.sent_after("view_receipt")
    parts = [p for m in sent.messages for p in getattr(m, "results", None) or ()]
    assert [p.figures for p in parts] == [[]] and not sent.images
    assert d.WITHHELD in parts[0].content and figure.handle in parts[0].content


# ── §9.3.9 tool inputs ────────────────────────────────────────────────────────────────────


def test_a_workers_toolkit_call_is_judged_by_argument_kind_before_it_runs(tmp_path):
    # §9.3.9 tool inputs: undeclared / peer $ref / typed parameter without a reason refused before the
    # call runs; a typed parameter with its reason is recorded behind #/$args/<name>
    async def go():
        async with open_run(tmp_path, workers=2) as run:
            (peer_handle,) = (await run.peer_result(seat=1)).handles("r")
            csv = str(run.product)
            tool = lambda sent: tool_named(sent, "band_pa_summary")  # noqa: E731
            call = lambda **arguments: (lambda sent: (tool(sent), {"csv_path": csv, **arguments}))  # noqa: E731
            own = lambda sent: (tool(sent), {"csv_path": csv, "min_total_counts": {
                "$ref": f"{d.handles_shown(sent, run.source('worker'), 'r')[0]}#/n_rows_used"}})  # noqa: E731
            result = await run.turn("worker", [
                call(bogus=1),
                call(min_total_counts=100),
                call(min_total_counts={"$ref": f"{peer_handle}#/n_rows_used"}),
                call(min_total_counts={"value": 100, "reason": "drops rows too faint to constrain a ratio"}),
                call(),
                own,
                STOP,
            ])
            return run, result, peer_handle

    run, result, peer_handle = asyncio.run(go())

    undeclared, bare, foreign, typed, default, own = result.results_named(result.declared("band_pa_summary"))
    refused = (undeclared, bare, foreign)
    assert all(r.is_error and "[receipt" not in r.text for r in refused)
    assert f"argument bogus {d.UNDECLARED_ARGUMENT}" in undeclared.text
    assert 'pass {"$ref"' in bare.text and '"reason"' in bare.text
    peer_unit = run.unit("worker", 1)
    assert f"{peer_handle} is {peer_unit}'s result" in foreign.text
    assert not any(r.is_error for r in (typed, default, own))
    # Only the three calls that ran minted a result, each handle named by its own result.
    first, second, third = result.handles("r")
    # The reason is recorded behind the call's handle; the call that passed a reference names its parent.
    assert argument_value(result.receipts, first, "/$args/min_total_counts") == 100
    assert first in result.receipts.get(third).parents


@pytest.mark.xfail(strict=True, raises=AssertionError,  # today: the call runs, no is_error result
                   reason="built in Phase 3 step 1: one input validator for every unit's tools "
                   "(today only the worker's toolkit calls are judged by argument kind)")
@pytest.mark.parametrize("role", ROLES)
def test_every_declared_tool_refuses_an_undeclared_argument_before_it_runs(role, tmp_path):
    # §9.3.9 tool inputs for every unit, harness tools included: an argument a tool's schema does not
    # declare is refused with an is_error result naming it, and the call does not run (no receipt is
    # minted); today a harness tool drops it silently and runs
    async def go():
        async with open_run(tmp_path) as run:
            calls = run.harness_calls(role)
            return calls, await run.turn(role, [[c.step(bogus=1) for c in calls], STOP])

    calls, result = asyncio.run(go())

    shown = [r for r in result.tool_results if r.name in {c.tool for c in calls}]
    assert len(shown) == len(calls)
    for call, seen in zip(calls, shown):
        assert seen.is_error and "bogus" in seen.text, (call.tool, seen.text)
        assert "[receipt" not in seen.text, call.tool
    assert not any(result.handles(c.letter) for c in calls)


# ── §9.3.9 a tool name the unit does not declare ───────────────────────────────────────────

#: The words of the rejection (ruling 2026-11-18).
NO_SUCH_TOOL = "no such tool found"
_ABSENT = "undeclared_tool_for_the_freeze"
_STEP_1 = "built in Phase 3 step 1: a call to a tool name the unit does not declare is rejected with " \
          "'no such tool found' and mints no receipt (today the reviewer, librarian and lead answer 'unknown tool: <name>'; " \
          "the worker mints a 'Tool not registered' receipt)"


async def _call_undeclared(run, role):
    """One turn calling a name the role does not declare, then a declared harness tool as the
    accepted counterpart; returns the turn and the receipts the two calls minted."""
    before = {r.handle for r in run.receipts.receipts(source=run.source(role))}
    fetch = run.harness_call(role, "web_fetch")
    result = await run.turn(role, [(_ABSENT, {}), fetch.step(), STOP])
    minted = [r for r in run.receipts.receipts(source=result.source) if r.handle not in before]
    return result, minted, fetch


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_STEP_1)
@pytest.mark.parametrize("role", ROLES)
def test_a_call_to_a_tool_the_unit_does_not_declare_is_rejected_and_mints_no_receipt(role, tmp_path):
    # §9.3.9 tool inputs: an undeclared tool name is "no such tool found"; nothing runs, no receipt
    async def go():
        async with open_run(tmp_path) as run:
            return await _call_undeclared(run, role)

    result, minted, fetch = asyncio.run(go())

    assert _ABSENT not in {n for sent in result.sent for n in sent.tools}  # the unit never declared it
    (absent,) = result.results_named(_ABSENT)
    assert absent.is_error and NO_SUCH_TOOL in absent.text, absent.text
    assert "[receipt" not in absent.text
    # No receipt for the call; the accepted counterpart (a declared tool) minted exactly its own.
    (seen,) = result.results_named(fetch.tool)
    assert not seen.is_error
    assert [r.produced_by for r in minted] == [fetch.producer], [(r.handle, r.produced_by) for r in minted]
