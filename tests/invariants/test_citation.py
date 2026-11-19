"""Freeze of CLAUDE.md §9.3.3 (every number cited, a fixed evidence predicate) and §7's one citing
syntax, through each role's own loop (Phase 3 step 0, item I3).

Every cell scripts a turn of the real role (``drivers.Run.turn``) and judges what the model was
shown (the ``turn_end`` error result) and what the unit filed. A refusal cell carries its accepted
counterpart in the same run: the number written as its own citation of something the unit can cite.

Evidence reaches a seat the way production routes it: the worker runs a toolkit tool and the
librarian opens a paper in its own turn; the lead and the reviewer cite what ``handed=`` brings
(worker -> lead -> reviewer). The web network is faked beneath ``WebSource`` (outside the code under
test), as the driver does for every seat.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest import mock

import pytest

from tests.invariants import drivers as d

NUMBER = "0.28"
"""A value the worker's toolkit tool returns (``ratio``) and the claims below state."""
REQUEST_NUMBER = "3.5"
NUMERIC_QUESTION = f"Estimate the {REQUEST_NUMBER} keV line ratio of the product."
PAGE_NUMBER = "7.5"


# ── script building ──────────────────────────────────────────────────────────────────────────


def _handle_of(sent: d.Sent, tool: str) -> str:
    """The receipt handle the result of the tool whose name ends with ``tool`` named."""
    for message in sent.messages:
        for result in getattr(message, "results", None) or ():
            if result.name.endswith(tool):
                return re.search(r"(?<![\w.-])([\w.-]+:[a-z]\d+)", result.content).group(1)
    raise LookupError(f"no result of {tool} on this call")


def _write(run: d.Run, role: str, claim):
    """The ``write_report`` call of ``role`` whose claim is ``claim`` (text, or a callable of what
    the call was sent); the librarian's finding names the paper it opened."""
    write, _, _ = d.report_tools(role)
    source = run.source(role)

    def item(sent: d.Sent):
        text = claim(sent) if callable(claim) else claim
        support = d.handles_shown(sent, source, "p")[-1:] if role == "librarian" else ()
        return write, {"content": d.report_content(role, claim=text, support=support)}

    return item


def _end(role: str):
    _, _, end = d.report_tools(role)
    return end, {"summary": "filed"}


async def _upstream(run: d.Run, role: str) -> tuple[list[d.TurnResult], str | None]:
    """The turns whose work reaches ``role`` and the handle of the worker's toolkit result in its
    scope: none for the seats that produce evidence themselves; the worker for the lead; the
    worker then the lead for the reviewer."""
    if role in ("worker", "librarian"):
        return [], None
    worker = await run.turn("worker", [*run.evidence_steps("worker"), *run.file_script("worker", claim=_ratio_of_worker)])
    assert worker.filed, [(r.name, r.text) for r in worker.tool_results]
    (handle,) = worker.handles("r")
    if role == "lead":
        return [worker], handle
    lead = await run.turn("lead", run.file_script("lead", claim=f"The band ratio is {{{{{handle}#/ratio}}}}."),
                          handed=[worker])
    assert lead.filed, [(r.name, r.text) for r in lead.tool_results]
    return [lead], handle


def _ratio_of_worker(sent: d.Sent) -> str:
    return f"The ratio is {{{{{_handle_of(sent, 'band_pa_summary')}#/ratio}}}}."


def _evidence(run: d.Run, role: str, upstream: str | None):
    """``(steps, cite)``: the script items through which ``role`` produces its own evidence and the
    claim stating :data:`NUMBER` as its own citation of what ``role`` can cite."""
    if role == "worker":
        return run.evidence_steps(role), _ratio_of_worker
    if role == "librarian":
        return run.evidence_steps(role), lambda sent: (
            f'The ratio is {{{{{_handle_of(sent, "read_paper")} "{NUMBER}" Table 2}}}}.')
    return [], f"The ratio is {{{{{upstream}#/ratio}}}}."


def _the_commit(run: d.Run):
    (commit,) = run.receipts.receipts(kind="commit")
    return commit


def _turn_ends(result: d.TurnResult, role: str):
    _, _, end = d.report_tools(role)
    return result.results_named(end)


def _filed_json(result: d.TurnResult) -> str:
    return result.report.model_dump_json()


# ── a number is its own citation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", d.ROLES)
def test_a_typed_number_is_refused_and_its_citation_accepted(role, tmp_path):  # §9.3.3 every number cited
    async def go():
        async with d.open_run(tmp_path) as run:
            handed, upstream = await _upstream(run, role)
            steps, cite = _evidence(run, role, upstream)
            typed = f"The ratio is {NUMBER}."
            return await run.turn(
                role, [*steps, _write(run, role, typed), _end(role), _write(run, role, cite), _end(role)],
                handed=handed,
            ), typed

    result, typed = asyncio.run(go())

    refused, accepted = _turn_ends(result, role)
    assert refused.is_error and d.TYPED_NUMBER in refused.text and NUMBER in refused.text
    assert not accepted.is_error
    assert result.error is None and result.filed
    assert typed not in _filed_json(result) and "{{" in _filed_json(result)


# ── a harness return is navigation ───────────────────────────────────────────────────────────

async def _fetch(self, url, *, depth="advanced"):
    return f"# Page\nThe measured ratio is {PAGE_NUMBER} in this page.\n"


async def _search(self, query, *, limit=10):
    from auto_research.schemas import SearchResult

    return [SearchResult(source="web", title="A hit", url="https://example.org/a",
                         abstract=f"ratio {PAGE_NUMBER}")]


#: A text each harness return holds, quoted by the citation that rests on it; a kind absent here
#: (a listing the run does not control) is cited as a whole receipt.
_QUOTED = {"read_file": "400.0", "list_dir": "table_12345.csv", "web_fetch": PAGE_NUMBER,
           "web_search": PAGE_NUMBER}


@pytest.mark.parametrize("role", d.ROLES)
def test_a_harness_return_grounds_no_number(role, tmp_path):  # §9.3.3 every harness return is navigation
    from auto_research.sources import WebSource

    def resting_on_what_was_read(calls):
        def claim(sent: d.Sent) -> str:
            cites = []
            for call in calls:
                handle = _handle_of(sent, call.tool)
                cites.append(f'{call.kind} showed {{{{{handle} "{_QUOTED[call.kind]}"}}}}.'
                             if call.kind in _QUOTED else f"{call.kind} showed {{{{{handle}}}}}.")
            return " ".join(cites)
        return claim

    async def go():
        async with d.open_run(tmp_path) as run:
            calls = run.harness_calls(role)
            listing = next((c for c in calls if c.kind == "list_dir"), None)
            if listing is not None:  # a file the listing names, beside the product
                (Path(listing.arguments["path"]) / "table_12345.csv").write_text("x\n", encoding="utf-8")
            handed, upstream = await _upstream(run, role)
            steps, cite = _evidence(run, role, upstream)
            with mock.patch.object(WebSource, "fetch", _fetch), mock.patch.object(WebSource, "search", _search):
                result = await run.turn(
                    role, [*(call.step() for call in calls), *steps,
                           _write(run, role, resting_on_what_was_read(calls)), _end(role),
                           _write(run, role, cite), _end(role)],
                    handed=handed,
                )
            return result, calls, {call.kind: _handle_of(result.sent_after(call.tool), call.tool) for call in calls}

    result, calls, handles = asyncio.run(go())

    refused, accepted = _turn_ends(result, role)
    assert refused.is_error and not accepted.is_error and result.filed
    for call in calls:
        handle = handles[call.kind]
        assert f"cites {handle}, {call.named}: {d.NOT_EVIDENCE}" in refused.text, call.kind
    assert _QUOTED["web_fetch"] not in _filed_json(result)


def test_a_number_printed_by_the_shell_grounds_nothing(tmp_path):  # §9.3.3 the shell grounds nothing (§7 laundering)
    """``echo`` and ``python -c`` print the number the claim wants; the receipt of the shell's output
    is where the worker looked, and the same turn's accepted claim rests on its toolkit result."""

    async def go():
        async with d.open_run(tmp_path) as run:
            shell = run.harness_call("worker", "shell")
            result = await run.turn("worker", [
                shell.step(command=f"echo {NUMBER}"),
                shell.step(command=f"python -c 'print({NUMBER})'"),
                _write(run, "worker", lambda sent: " ".join(
                    f'{{{{{h} "{NUMBER}"}}}}' for h in d.handles_in(sent.results_text(), run.source("worker"), "r")[:2])),
                _end("worker"),
                *run.evidence_steps("worker"),
                _write(run, "worker", _ratio_of_worker), _end("worker"),
            ])
            return result, shell

    result, shell = asyncio.run(go())

    refused, accepted = _turn_ends(result, "worker")
    assert refused.is_error and not accepted.is_error and result.filed
    first, second = d.handles_in(result.sent_after(shell.tool, 1).results_text(), result.source, "r")[:2]
    assert f"cites {first}, {shell.named}: {d.NOT_EVIDENCE}" in refused.text
    assert f"cites {second}, {shell.named}" in refused.text
    assert first not in _filed_json(result) and second not in _filed_json(result)


@pytest.mark.parametrize("role", ["worker", "lead", "reviewer"])
def test_a_locator_on_a_toolkit_result_is_refused(role, tmp_path):  # §9.3.3 only a paper takes a locator
    async def go():
        async with d.open_run(tmp_path) as run:
            handed, upstream = await _upstream(run, role)
            steps, cite = _evidence(run, role, upstream)
            handle = (lambda sent: _handle_of(sent, "band_pa_summary")) if role == "worker" else (lambda sent: upstream)
            located = lambda sent: f'The ratio is {{{{{handle(sent)} "{NUMBER}" Table 2}}}}.'  # noqa: E731
            return await run.turn(role, [*steps, _write(run, role, located), _end(role),
                                         _write(run, role, cite), _end(role)], handed=handed)

    result = asyncio.run(go())

    refused, accepted = _turn_ends(result, role)
    assert refused.is_error and d.LOCATOR_NOT_PAPER in refused.text
    assert not accepted.is_error and result.filed and "Table 2" not in _filed_json(result)


def test_a_catalog_listing_grounds_nothing():  # §9.3.3 a catalog listing is navigation
    pytest.skip("needs driver: the lead's catalog_query exists only on a run with a declared store "
                "(open_run drops the fixture's store tools, drivers._drop_store_tools), and no "
                "open_run option keeps one")


@pytest.mark.xfail(strict=True, reason="built in Phase 3 step 2: the evidence predicate asks whether a toolkit "
                   "registered for the unit, at a recorded commit, produced the receipt; today it reads the "
                   "producer's name alone")
def test_a_toolkit_receipt_without_a_recorded_commit_grounds_nothing(tmp_path):  # §9.3.3 a registered toolkit at a recorded commit
    """The worker's own result is evidence for the worker (and its citation is accepted); the same
    producer stamped with no toolkit commit is not. A reviewer's citation of a handed worker result
    (``test_a_typed_number_is_refused_and_its_citation_accepted``) rests on the first kind."""

    async def go():
        async with d.open_run(tmp_path) as run:
            worker = await run.turn("worker", [*run.evidence_steps("worker"),
                                               *run.file_script("worker", claim=_ratio_of_worker)])
            assert worker.filed
            (handle,) = worker.handles("r")
            real = run.receipts.get(handle)
            unrecorded = run.receipts.mint(source=worker.source, kind="result", produced_by=real.produced_by,
                                           data="ratio: 0.28\n", toolkit_commit=None)
            return real, unrecorded, worker.unit

    real, unrecorded, unit = asyncio.run(go())

    assert real.toolkit_commit and d.is_evidence(real, unit)
    assert not d.is_evidence(unrecorded, unit)


# ── a receipt outside the unit's scope ───────────────────────────────────────────────────────


@pytest.mark.parametrize("role", d.ROLES)
def test_a_receipt_the_unit_was_never_handed_grounds_nothing(role, tmp_path):  # §9.3.3 scope of citation
    """The peer's receipt exists in the run (a ``view_receipt`` opens it); citing it is scoped out."""

    async def go():
        async with d.open_run(tmp_path) as run:
            if role == "worker":  # a literature seat's paper
                peer = await run.turn("librarian", run.file_script("librarian"))
                foreign = f'{{{{{peer.handles("p")[0]} "4.0" Table 2}}}}'
            else:  # the worker's toolkit result
                peer = await run.turn("worker", [*run.evidence_steps("worker"),
                                                 *run.file_script("worker", claim=_ratio_of_worker)])
                foreign = f"{{{{{peer.handles('r')[0]}#/ratio}}}}"
            assert peer.filed
            steps, cite = _evidence(run, role, None)
            plain = d.CLAIM
            result = await run.turn(role, [
                *steps, _write(run, role, f"The ratio is {foreign}."), _end(role),
                _write(run, role, cite if steps else plain), _end(role),
            ])
            return result, foreign

    result, foreign = asyncio.run(go())

    refused, accepted = _turn_ends(result, role)
    handle = re.search(r"\{\{([^\s#}]+)", foreign).group(1)
    assert refused.is_error and f"cites {handle}, {d.NOT_CITABLE}" in refused.text
    assert not accepted.is_error and result.filed and handle not in _filed_json(result)


# ── a paper value is attributed with a locator ───────────────────────────────────────────────


@pytest.mark.parametrize("role", ["librarian", "reviewer"])
def test_a_paper_value_needs_attribution_with_a_locator(role, tmp_path):  # §9.3.3 a paper, by attribution with a locator
    async def go():
        async with d.open_run(tmp_path) as run:
            opened = run.evidence_steps("librarian")
            attributed = lambda sent: (  # noqa: E731
                f'The paper reports {{{{{_handle_of(sent, "read_paper")} "4.0" Table 2}}}}.')
            if role == "librarian":
                handed, steps, handle = [], opened, lambda sent: _handle_of(sent, "read_paper")
            else:
                paper = await run.turn("librarian", [*opened, *run.file_script("librarian", claim=attributed)[len(opened):]])
                assert paper.filed, [(r.name, r.text) for r in paper.tool_results]
                handed, steps, handle = [paper], [], lambda sent: paper.handles("p")[0]
            bare = lambda sent: f"The paper reports 4.0 ({{{{{handle(sent)}}}}})."  # noqa: E731
            no_place = lambda sent: f'The paper reports {{{{{handle(sent)} "4.0"}}}}.'  # noqa: E731
            good = lambda sent: f'The paper reports {{{{{handle(sent)} "4.0" Table 2}}}}.'  # noqa: E731
            return await run.turn(role, [
                *steps, _write(run, role, bare), _end(role), _write(run, role, no_place), _end(role),
                _write(run, role, good), _end(role),
            ], handed=handed)

    result = asyncio.run(go())

    typed, unplaced, accepted = _turn_ends(result, role)
    assert typed.is_error and d.TYPED_NUMBER in typed.text and "4.0" in typed.text
    assert unplaced.is_error and d.PAPER_UNPLACED in unplaced.text
    assert not accepted.is_error and result.filed and "Table 2" in _filed_json(result)


# ── the request is cite-only ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", d.ROLES)
def test_the_requests_number_grounds_only_where_the_request_is_cited(role, tmp_path):  # §9.3.3 the task, cite-only
    """Production order: the reviewer's turn mints the request's receipt first and its brief reaches
    the unit. The number is typed in a citation of the request, or refused; a citation of the task
    brief (not evidence) is refused as well."""

    async def go():
        async with d.open_run(tmp_path) as run:
            handed = []
            if role != "reviewer":
                first = await run.turn("reviewer", run.file_script("reviewer"), question=NUMERIC_QUESTION)
                assert first.filed
                handed = [first]
            from auto_research.provenance.store import INPUT_SOURCE, REQUEST_PRODUCER

            def request_handle() -> str:
                (handle,) = [r.handle for r in run.receipts.receipts(source=INPUT_SOURCE)
                             if r.produced_by == REQUEST_PRODUCER]
                return handle

            def brief_handle() -> str | None:
                """The task brief's receipt (the librarian's turn mints none)."""
                return next((r.handle for r in run.receipts.receipts(source=INPUT_SOURCE)
                             if r.produced_by != REQUEST_PRODUCER), None)

            typed = f"The line is at {REQUEST_NUMBER} keV."
            quote = lambda h: f'The line is at {{{{{h} "{REQUEST_NUMBER} keV"}}}}.'  # noqa: E731
            steps, _ = _evidence(run, role, None)
            if role == "reviewer":
                script = [_write(run, role, typed), _end(role),
                          _write(run, role, lambda sent: quote(request_handle())), _end(role)]
                return await run.turn(role, script, question=NUMERIC_QUESTION), None
            # The brief the unit is handed is minted when its turn starts: a first, failing pass names it.
            on_brief = [] if role == "librarian" else [
                _write(run, role, lambda sent: quote(brief_handle())), _end(role)]
            result = await run.turn(role, [
                *steps, _write(run, role, typed), _end(role), *on_brief,
                _write(run, role, lambda sent: quote(request_handle())), _end(role),
            ], handed=handed, question=NUMERIC_QUESTION)
            return result, (request_handle(), brief_handle())

    result, handles = asyncio.run(go())

    ends = _turn_ends(result, role)
    assert ends[0].is_error and d.TYPED_NUMBER in ends[0].text and REQUEST_NUMBER in ends[0].text
    assert result.filed and not ends[-1].is_error
    if role == "reviewer":
        assert len(ends) == 2 and "input:h" in _filed_json(result)
    else:
        request, brief = handles
        assert request != brief
        if brief is not None and role != "librarian":
            assert ends[1].is_error and f"cites {brief}" in ends[1].text and d.NOT_EVIDENCE in ends[1].text
            assert brief not in _filed_json(result)
        assert request in _filed_json(result)


# ── the coding pipeline's commit ─────────────────────────────────────────────────────────────


def test_a_commit_receipt_grounds_a_number(tmp_path):  # §9.3.3 a coding-pipeline commit
    """The lead's chain commits; the ``commit`` receipt's record holds the submissions used, which
    stands as its own citation and is refused typed bare."""
    typed = "The tool was committed after 1 submission."

    async def go():
        async with d.open_run(tmp_path, pipeline=True, config={"tool_author_submit_cap": 1}) as run:
            worker = await run.turn("worker", run.file_script("worker", tool_request=d.NEW_TOOL))
            cited = lambda sent: (  # noqa: E731
                f"The tool was committed after {{{{{_the_commit(run).handle}#/submissions_used}}}} submission.")
            return await run.turn(
                "lead", [d.brief_step(), d.submit_step(), _write(run, "lead", typed), _end("lead"),
                         _write(run, "lead", cited), _end("lead")],
                handed=[worker], coder=[d.coder_edit(passing=True)], colead=d.review_script(approved=True),
                compactor=["The coder added the tool and its test."],
            )

    result = asyncio.run(go())

    (commit,) = result.receipts.receipts(kind="commit")
    assert d.is_evidence(commit, result.unit) and commit.source == result.source
    refused, accepted = _turn_ends(result, "lead")
    assert refused.is_error and d.TYPED_NUMBER in refused.text
    assert not accepted.is_error and result.filed
    assert f"{commit.handle}#/submissions_used" in _filed_json(result)
