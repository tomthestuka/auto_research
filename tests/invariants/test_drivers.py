"""The driver itself (step 1 rewrites this file with ``drivers.py``): each role, built as production
builds it, files a minimal valid report through ``run_turn``; a prose-only script files nothing
(what each role then does today is recorded here; the clause tests judge it); the seats that
produce evidence cite it; handed work reaches the next seat; a wrong script is the test's error;
the scripted human answers a seat over the channel; a text resumes a finished seat; the lead's
coding chain commits through production's gate and co-lead review; a run's provider keys are its own."""

from __future__ import annotations

import asyncio

import pytest

from tests.invariants import drivers as d
from tests.invariants.drivers import (
    CI_FAILED, CLAIM, NEW_TOOL, REVIEW_REJECTED, ROLES, STOP, TRUSTED_BRANCH, Reply, ScriptError, ScriptedHuman,
    brief_step, coder_edit, handles_in, handles_shown, open_run, report_content, report_tools, run_chain, run_turn,
    submit_step,
)


@pytest.mark.parametrize("role", ROLES)
def test_a_minimal_valid_report_is_filed(role, tmp_path):
    result = asyncio.run(run_turn(role, lambda run: run.file_script(role), tmp_path))

    assert result.error is None and result.run_failure is None, result.error
    assert result.filed and CLAIM in result.report.model_dump_json()
    _, _, terminal = report_tools(role)
    assert [r.is_error for r in result.tool_results if r.name == terminal] == [False]
    assert any(e["event"].endswith(".filed") and e.get("actor") == result.unit for e in result.events)
    # The model was given the report tools by the role's own wiring.
    assert set(report_tools(role)) <= set(result.sent[0].tools)
    outcome = result.outcome
    assert outcome.filed and not outcome.failed and outcome.recorded == () and outcome.handed == result.report


#: What each role's entry point does today when its model answers only in prose and then stops
#: (recorded here; the one-exit clause tests judge it): the reviewer and the librarian raise, the
#: worker returns a failed report composed by the runtime, the lead returns no report.
_PROSE_OUTCOME = {
    "reviewer": "raises",
    "librarian": "raises",
    "worker": "returns failed",
    "lead": "returns none",
}


@pytest.mark.parametrize("role", ROLES)
def test_a_prose_only_script_files_nothing(role, tmp_path):
    prose = "The band ratios rise with the count total, which settles the question in my view."
    result = asyncio.run(run_turn(role, [prose, STOP], tmp_path))

    assert not result.filed
    # The prose turn was answered with a request to file (no escape path); the model then stopped.
    assert len(result.sent) == 2
    silent = [e["data"]["continued"] for e in result.events if e["event"].endswith(".silent_turn")]
    assert silent == [True, False] and sum(e["event"].endswith(".nudge") for e in result.events) == 1
    assert result.silent_turns() == silent and len(result.nudges()) == 1  # the event helpers read the same
    assert not [r for r in result.tool_results if r.name == report_tools(role)[2]]
    outcome = _PROSE_OUTCOME[role]
    if outcome == "raises":
        assert result.error is not None and result.returned is None
    elif outcome == "returns failed":
        assert result.error is None and result.returned.status == "failed"
    else:
        assert result.error is None and result.returned is not None and result.returned.report is None
    # The same facts, role-neutrally (`Outcome`): every role's unfiled turn reads as a failure.
    seen = result.outcome
    assert not seen.filed and seen.failed and seen.raised is result.error
    assert seen.failure_record == (outcome == "returns failed")
    assert (seen.handed is None) == (outcome != "returns failed")
    assert [e["event"].rsplit(".", 1)[-1] for e in seen.recorded] == (
        ["no_report"] if outcome == "returns failed" else [])


def _cite_first(source: str, letter: str, pointer: str = ""):
    """A claim citing the first ``<source>:<letter>n`` handle the turn's tool results named."""
    return lambda sent: (
        f"The work rests on {{{{{handles_shown(sent, source, letter)[0]}{pointer}}}}}."
    )


@pytest.mark.parametrize(("role", "letter", "pointer"), [("worker", "r", "#/ratio"), ("librarian", "p", "")])
def test_a_seat_that_produces_evidence_cites_it(role, letter, pointer, tmp_path):
    from auto_research.provenance.store import is_evidence

    async def go():
        async with open_run(tmp_path) as run:
            claim = _cite_first(run.source(role), letter, pointer)
            steps = run.evidence_steps(role) if role == "worker" else []
            return await run.turn(role, [*steps, *run.file_script(role, claim=claim)])

    result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    (handle,) = result.handles(letter)
    assert is_evidence(result.receipts.get(handle))
    assert handle in result.report.model_dump_json()


def test_handed_analysis_reaches_the_lead_then_the_reviewer_and_stays_citable(tmp_path):
    async def go():
        async with open_run(tmp_path) as run:
            claim = _cite_first(run.source("worker"), "r", "#/ratio")
            worker = await run.turn("worker", [*run.evidence_steps("worker"),
                                               *run.file_script("worker", claim=claim)])
            (handle,) = worker.handles("r")
            cited = f"The band ratio is {{{{{handle}#/ratio}}}}."
            lead = await run.turn("lead", run.file_script("lead", claim=cited), handed=[worker])
            reviewer = await run.turn("reviewer", run.file_script("reviewer", claim=cited), handed=[lead])
            return worker, lead, reviewer, handle

    worker, lead, reviewer, handle = asyncio.run(go())

    for turn in (worker, lead, reviewer):
        assert turn.error is None and turn.filed, (turn.role, [(r.name, r.text) for r in turn.tool_results])
    # Each seat's task carried the work handed to it (the run's payload, never the prompt's examples).
    assert handle in handles_in(lead.sent[0].user_text(), worker.source, "r")
    assert '"worker_sources": ["analyst_worker_0"]' in lead.sent[0].user_text()
    assert handle in handles_in(reviewer.sent[0].user_text(), worker.source, "r")
    assert '"arrived": "analysts"' in reviewer.sent[0].user_text()
    # The reviewer was given one analysis pass fewer, as the graph counts them.
    assert "2 analysis pass(es) remain" in reviewer.sent[0].user_text()


def test_a_literature_report_reaches_the_reviewer_with_the_documents_it_cites(tmp_path):
    async def go():
        async with open_run(tmp_path) as run:
            claim = _cite_first(run.source("librarian"), "p")
            librarian = await run.turn("librarian", run.file_script("librarian", claim=claim))
            (handle,) = librarian.handles("p")
            reviewer = await run.turn("reviewer", run.file_script(
                "reviewer", claim=f"The paper tabulates them {{{{{handle}}}}}."), handed=[librarian])
            return librarian, reviewer, handle

    librarian, reviewer, handle = asyncio.run(go())

    assert librarian.filed and reviewer.error is None and reviewer.filed, reviewer.error
    assert '"arrived": "librarian"' in reviewer.sent[0].user_text()
    assert handle in handles_in(reviewer.sent[0].user_text(), librarian.source, "p")


def test_the_reviewers_brief_is_the_workers_task(tmp_path):
    async def go():
        async with open_run(tmp_path) as run:
            reviewer = await run.turn("reviewer", run.file_script("reviewer"))
            worker = await run.turn("worker", run.file_script("worker"), handed=[reviewer])
            return reviewer, worker

    reviewer, worker = asyncio.run(go())

    assert reviewer.filed and worker.filed
    assert f"Reviewer dispatch: {reviewer.report.follow_up}" in worker.sent[0].user_text()


@pytest.mark.parametrize("script", [
    pytest.param(["one prose turn, then nothing scripted"], id="past-the-end"),
    pytest.param([STOP, ("turn_end", {"summary": "never asked for"})], id="left-unused"),
    pytest.param([["write_report", {"content": "x"}]], id="malformed-batch"),
    pytest.param([lambda sent: 1 / 0], id="callable-raises"),
])
def test_a_wrong_script_is_the_tests_error_never_the_units(script, tmp_path):
    with pytest.raises(ScriptError):
        asyncio.run(run_turn("lead", script, tmp_path))


# ── the human, a resume, the coding pipeline, the keys (D1) ──────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_a_seat_messages_the_human_and_reads_the_reply_as_a_nudge(role, tmp_path):
    from auto_research.provenance.store import is_evidence

    answer = "Tabulate the second product; my notes are attached."

    async def go():
        async with open_run(tmp_path, human=[Reply(answer, files={"notes.csv": "row,weight\n1,2\n"})]) as run:
            ask = ("message", {"to": "human", "text": "Which product should I tabulate?"})
            before = {r.handle for r in run.receipts.receipts()}
            result = await run.turn(role, [ask, ("wait_for_reply", {}), *run.file_script(role)])
            return run, result, before

    run, result, before = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    (query,) = run.human.queries
    assert query.asked_by == result.unit and query.message == "Which product should I tabulate?"
    # The reply reached the seat's model: its text, and the file as a receipt of the seat's own.
    (minted,) = [r for r in result.receipts.receipts() if r.handle not in before and r.produced_by == "human_reply"]
    assert answer in result.sent[2].user_text() and minted.handle in result.sent[2].user_text()
    assert minted.source == result.source and minted.kind == "data" and not is_evidence(minted)
    # The text minted nothing: the file is the only receipt the reply added.
    new = [r for r in result.receipts.receipts() if r.handle not in before]
    assert all(answer not in (result.receipts.text(r.handle) or "") for r in new)
    assert [r.handle for r in new if r.produced_by == "human_reply"] == [minted.handle]
    (reply,) = [e for e in result.events if e["event"] == "channel.reply"]
    assert reply["data"]["files"] == [minted.handle]


@pytest.mark.parametrize("sender", ["human", "reviewer"])
def test_a_text_resumes_a_finished_seat_through_the_channel(sender, tmp_path):
    text = "Please confirm the table covers every row."

    async def go():
        async with open_run(tmp_path) as run:
            first = await run.turn("worker", run.file_script("worker"))
            rewrite = ("write_report", {"content": report_content("worker", claim="A second account.")})
            resumed = await run.resume("worker", [rewrite, ("turn_end", {"summary": "confirmed"})],
                                       text=text, sender=sender)
            return first, resumed

    first, resumed = asyncio.run(go())

    assert first.filed and resumed.error is None and resumed.run_failure is None
    assert text in resumed.sent[0].user_text()
    # The report as production holds it after the resume is the one the first turn filed.
    assert resumed.report == first.report and resumed.outcome.filed and not resumed.outcome.recorded
    # The resumed turn ran the seat's own loop: its report stayed final, and it ended by turn_end.
    assert [(r.name, r.is_error) for r in resumed.tool_results] == [("write_report", True), ("turn_end", False)]
    names = resumed.event_names()
    assert "channel.resume" in names and "channel.resume.end" in names


@pytest.mark.parametrize(("passing", "approved"), [
    pytest.param(True, True, id="gate-passes-colead-approves"),
    pytest.param(True, False, id="colead-rejects"),
    pytest.param(False, None, id="gate-fails"),
])
def test_a_lead_chain_commits_only_when_the_gate_passes_and_the_colead_approves(passing, approved, tmp_path,
                                                                               request):
    from auto_research.provenance.store import is_evidence
    from tests.fixtures.toolkit import BRANCH

    commits = passing and bool(approved)
    # The approved link is the session's shared one (``approved_chain``); the others run their own.
    chain = (request.getfixturevalue("approved_chain") if commits
             else asyncio.run(run_chain(tmp_path, passing=passing, approved=approved)))
    run, worker, lead = chain.run, chain.worker, chain.lead

    assert worker.report.tool_request.proposed_tool_name == NEW_TOOL
    assert lead.error is None and lead.filed, [(r.name, r.text) for r in lead.tool_results]
    # The coder was briefed with the lead's link, and the co-lead reviewed only what the gate passed.
    assert NEW_TOOL in run.coder_briefs()[0]
    assert len(run.colead_calls()) == (2 if passing else 0)
    receipts = lead.receipts.receipts(kind="commit")
    submitted = [r for r in lead.tool_results if r.name == "submit_for_review"][0]
    assert (REVIEW_REJECTED in submitted.text) == (passing and not approved)
    assert (CI_FAILED in submitted.text) == (not passing)
    assert chain.status == ""  # a refused change is cleared
    assert chain.branch == BRANCH and chain.providers == ("anthropic", "openai" if passing else None)
    # The trusted branch stands where the run opened it; only the authoring branch moves.
    assert set(chain.tips) == {BRANCH, TRUSTED_BRANCH}
    assert chain.tips[TRUSTED_BRANCH] == run.trusted_tip == chain.head
    if commits:
        (commit,) = receipts
        assert commit.source == lead.source and is_evidence(commit)
        assert lead.handles("c") == [commit.handle] and commit.handle in submitted.text
        assert [line.split(" ", 1)[1] for line in chain.log] == [f"Add {NEW_TOOL}"]
        assert chain.tips[BRANCH] == chain.log[0].split()[0] != run.trusted_tip
        (event,) = lead.commits()
        assert event["commit_sha"] == chain.log[0].split()[0] and event["tool"] == NEW_TOOL
        assert lead.tree_resets() == []
    else:
        assert receipts == [] and lead.handles("c") == [] and chain.log == []
        assert chain.tips[BRANCH] == chain.head
        assert lead.commits() == [] and lead.tree_resets()


class _SpentBalance(Exception):
    """The provider network's answer for a key whose credit balance is spent."""

    status_code = 400
    type = "billing_error"


def test_a_key_scoped_error_rotates_only_the_runs_own_keys(tmp_path, monkeypatch):
    """W4: a scripted spent balance retires the run's stand-in key and the retry runs on its fallback;
    the process's keys and key pool are as they were."""
    from auto_research.models import keys

    monkeypatch.setattr(keys, "_POOLS", {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "process-primary")
    monkeypatch.setenv("ANTHROPIC_API_KEY_FALLBACK", "process-fallback")
    pool = keys.key_pool("anthropic")

    async def go():
        async with open_run(tmp_path) as run:
            result = await run.turn("worker", [_SpentBalance("Your credit balance is too low"),
                                               *run.file_script("worker")])
            return result, keys.key_pool("anthropic").active()

    result, live_in_run = asyncio.run(go())

    assert result.filed and result.error is None
    assert live_in_run == "freeze-anthropic_api_key_fallback"
    assert keys.key_pool("anthropic") is pool and pool.active_label() == "primary"
    # Nothing of the process's pool was rotated or retired: it rotates away and back.
    assert pool.active() == "process-primary" and pool.rotate() == "fallback1" and pool.rotate() == "primary"



def test_a_failed_human_script_reaches_the_seat_as_a_channel_fault_and_fails_the_turn(tmp_path):
    """The human is called from the channel's task: a query past its script is a channel fault the
    seat hears at once, and the turn raises the script's error."""

    async def go():
        async with open_run(tmp_path, human=[]) as run:
            ask = ("message", {"to": "human", "text": "Which product should I tabulate?"})
            await run.turn("worker", [ask, ("wait_for_reply", {}), *run.file_script("worker")])

    with pytest.raises(ScriptError, match="past the end of the human's script"):
        asyncio.run(asyncio.wait_for(go(), timeout=120))


def test_a_failed_coder_script_stops_the_coder_at_once_and_fails_the_turn(tmp_path):
    """The author process is driven from a background task: an edit past its script stops the coder
    at once (production's harness-error path), and the turn raises the script's error."""

    async def go():
        async with open_run(tmp_path, pipeline=True) as run:
            worker = await run.turn("worker", run.file_script("worker", tool_request=NEW_TOOL))
            await run.turn("lead", [brief_step(), submit_step(), STOP, STOP], handed=[worker], coder=[],
                           compactor=["The coder stopped."])

    with pytest.raises(ScriptError, match="past the end of the coder's script"):
        asyncio.run(asyncio.wait_for(go(), timeout=180))


# ── the role-neutral surface (FX-D) ─────────────────────────────────────────────────────────


def test_a_resumed_turn_that_rewrites_the_filed_report_shows_the_rewrite(tmp_path):
    """``Run.resume`` reads the report back from where production keeps it. Production's two guards
    on a filed report (the lock ``turn_end`` sets and the tools a resumed turn answers without
    running) are switched off, as a regression would; the resumed rewrite then shows in ``report``."""
    from unittest import mock

    from auto_research.agents import react, roles

    rewritten = "A second account, written after the report was filed."

    async def go():
        async with open_run(tmp_path) as run:
            with mock.patch.object(react, "lock_turn_reports", lambda *args, **kwargs: None):
                first = await run.turn("worker", run.file_script("worker"))
            stored = run.report_file("worker")
            with mock.patch.object(roles, "_RESUME_CLOSED", frozenset()):
                resumed = await run.resume("worker", [
                    ("write_report", {"content": report_content("worker", claim=rewritten)}),
                    ("turn_end", {"summary": "rewritten"}),
                ])
            return first, resumed, stored

    first, resumed, stored = asyncio.run(go())

    assert first.filed and CLAIM in first.report.summary and stored is not None
    assert [(r.name, r.is_error) for r in resumed.tool_results] == [("write_report", False), ("turn_end", False)]
    assert resumed.report != first.report and resumed.report.summary == rewritten
    assert rewritten in stored.read_text(encoding="utf-8")


#: Per role, the declared tools that are no harness navigation tool: the report and channel
#: tools, and what produces evidence or mints nothing (the librarian's search tools).
_NOT_HARNESS = {"write_report", "edit_report", "turn_end", "message", "view_receipt", "wait_for_reply",
                "list_peers", "read_paper", "search_literature"}
_NOT_HARNESS_BY_ROLE = {"librarian": {"text_search"}}


@pytest.mark.parametrize("role", ROLES)
def test_each_harness_call_mints_the_navigation_receipt_it_names(role, tmp_path):
    from auto_research.provenance.store import NOT_EVIDENCE_NAMES, is_evidence
    from tests.fixtures.toolkit import TOOLKIT_NAME

    async def go():
        async with open_run(tmp_path) as run:
            calls = run.harness_calls(role)
            return calls, await run.turn(role, [*(c.step() for c in calls), *run.file_script(role)])

    calls, result = asyncio.run(go())

    assert result.filed, [(r.name, r.text) for r in result.tool_results]
    # The table holds every declared tool of the role that is neither report, channel nor toolkit.
    declared = {t for t in result.sent[0].tools if not t.startswith(f"{TOOLKIT_NAME}_")}
    assert declared - _NOT_HARNESS - _NOT_HARNESS_BY_ROLE.get(role, set()) == {c.tool for c in calls}
    for call in calls:
        (seen,) = [r for r in result.tool_results if r.name == call.tool]
        (handle,) = handles_in(seen.text, result.source, call.letter)
        receipt = result.receipts.get(handle)
        assert not seen.is_error and receipt.produced_by == call.producer and not is_evidence(receipt), call
        assert call.named == NOT_EVIDENCE_NAMES.get(call.producer, f"a {call.producer} return")


def test_a_peer_seat_files_a_real_toolkit_result_of_its_own(tmp_path):
    from auto_research.provenance.store import is_evidence

    async def go():
        async with open_run(tmp_path, workers=2) as run:
            peer = await run.peer_result()
            own = await run.turn("worker", run.file_script("worker"))
            return peer, own

    peer, own = asyncio.run(go())

    assert peer.filed and own.filed
    assert (peer.unit, own.unit) == ("analyst_worker_1", "analyst_worker_0")
    (handle,) = peer.handles("r")
    receipt = peer.receipts.get(handle)
    assert handle.startswith("analyst_worker_1:r") and receipt.source == "analyst_worker_1" and is_evidence(receipt)
    assert f"{handle}#/ratio" in peer.report.model_dump_json()
    # Each seat's model answered its own turn: the peer's three calls, seat 0's two.
    assert len(peer.sent) == 3 and len(own.sent) == 2


def test_the_reviewer_reads_the_humans_reply_as_the_graph_hands_it(tmp_path):
    from auto_research.provenance.store import INPUT_SOURCE

    nudge = "Tabulate the second product as well."
    to_human = report_content("reviewer", assign_to="human")

    async def go():
        async with open_run(tmp_path, human=[]) as run:
            first = await run.turn("reviewer", [("write_report", {"content": to_human}),
                                                ("turn_end", {"summary": "filed"})])
            with pytest.raises(ValueError, match="assigned to the human"):
                await run.turn("reviewer", [], handed=[], nudges=[nudge])
            files = run.human_files({"notes.csv": "row,weight\n1,2\n"})
            again = await run.turn("reviewer", run.file_script("reviewer"), handed=[first],
                                   nudges=[nudge], human_files=files)
            return run, first, again, files

    run, first, again, files = asyncio.run(go())

    assert first.filed and first.report.assign_to == "human", [(r.name, r.text) for r in first.tool_results]
    assert again.filed, again.error
    shown = again.sent[0].user_text()
    assert '"arrived": "human"' in shown and nudge in shown and files[0] in shown
    (minted,) = [run.receipts.get(h) for h in files]
    assert minted.source == INPUT_SOURCE and minted.produced_by == "human_reply"


# ── the driver round after the second review (FX2-D) ─────────────────────────────────────────


#: Each phrase and event constant, and the production module whose own text holds it.
_WORDS = {
    "NOT_RUN": "agents/roles.py", "TYPED_NUMBER": "provenance/bindings.py",
    "NOT_CITABLE": "provenance/bindings.py", "NOT_EVIDENCE": "provenance/bindings.py",
    "LOCATOR_NOT_PAPER": "provenance/bindings.py", "PAPER_UNPLACED": "provenance/bindings.py",
    "CI_FAILED": "agents/react.py", "REVIEW_REJECTED": "agents/react.py", "NOT_COMMITTED": "agents/react.py",
    "FLAGGED_FAILED": "provenance/refs.py", "UNDECLARED_ARGUMENT": "provenance/inputs.py",
    "WITHHELD": "models/sanitise.py", "FIGURE_HASH_OVERRIDDEN": "provenance/capture.py",
    "FILE_ABSENT": "provenance/capture.py",
    "APPROVE_EDIT": "agents/react.py", "TOOLSPEC": "agents/react.py", "TOOL_BRIEF": "provenance/store.py",
    "TASK_BRIEF": "provenance/store.py", "CHANGED_AFTER": "sandbox/merge_flow.py", "GRACE_OPENED": "agents/roles.py",
    "GRACE_EXHAUSTED": "agents/roles.py", "SILENT_TURN": "agents/roles.py", "NUDGE": "agents/roles.py",
    "MODEL_CALL_FAILED": "agents/roles.py", "NO_REPORT": "agents/roles.py", "FILED": "agents/roles.py",
    "COMMIT_EVENT": "sandbox/merge_flow.py", "TREE_RESET_EVENT": "sandbox/merge_flow.py",
}


@pytest.mark.parametrize("name", sorted(_WORDS))
def test_each_production_word_is_written_where_its_comment_says(name):
    """A phrase or event name a clause matches is one production writes: a string literal of the
    named module (adjacent literals joined, f-string parts read) holds it whole."""
    import ast
    from pathlib import Path

    import auto_research

    source = (Path(auto_research.__file__).parent / _WORDS[name]).read_text(encoding="utf-8")
    literals = [node.value for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    word = getattr(d, name)
    if name in ("COMMIT_EVENT", "TREE_RESET_EVENT"):
        # The event's whole name, as the name a ledger record is written under.
        recorded = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("_emit", "_record")
                    and any(isinstance(a, ast.Constant) and a.value == word for a in node.args)]
        assert recorded, (name, word)
    else:
        assert any(word in text for text in literals), (name, word)


@pytest.mark.parametrize("optional", [True, False])
def test_an_optional_human_may_leave_replies_unused_and_the_human_reads_the_channels_notices(optional, tmp_path):
    text = "The second product holds the brighter rows."

    async def go():
        human = ScriptedHuman(["A reply no one asks for."], optional=optional)
        async with open_run(tmp_path, human=human) as run:
            result = await run.turn("worker", [("message", {"to": run.unit("lead"), "text": text}),
                                               *run.file_script("worker")])
            async with asyncio.timeout(30):  # the channel posts its mirror in a task of its own
                while not run.inbox_of("human"):
                    await asyncio.sleep(0.01)
            return result, run.inbox_of("lead"), run.inbox_of("human")

    if not optional:
        with pytest.raises(ScriptError, match="left unused"):
            asyncio.run(go())
        return
    result, lead_inbox, human_inbox = asyncio.run(go())

    assert result.filed
    (queued,) = lead_inbox
    assert (queued.sender, queued.text, queued.kind) == (result.unit, text, "message")
    (notice,) = human_inbox
    assert notice.kind == "notice" and text in notice.text and result.unit in notice.text


def test_a_turns_calls_and_results_are_found_by_what_they_hold(tmp_path):
    async def go():
        async with open_run(tmp_path) as run:
            claim = _cite_first(run.source("worker"), "r", "#/ratio")
            return await run.turn("worker", [*run.evidence_steps("worker"),
                                              *run.file_script("worker", claim=claim)])

    result = asyncio.run(go())

    assert result.filed and len(result.sent) == 3
    tool = result.declared("band_pa_summary")
    assert tool.endswith("band_pa_summary") and tool != "band_pa_summary"
    (handle,) = result.handles("r")
    # The call that first read the toolkit result, and the one that read the report's write.
    assert result.sent_after(tool) is result.sent[1] and result.sent_with(handle) is result.sent[1]
    assert result.sent_after("write_report") is result.sent[2]
    assert [r.is_error for r in result.results_named("turn_end")] == [False]
    assert result.results_named(tool)[0].text.count(handle) >= 1
    for lookup in (lambda: result.sent_after(tool, nth=1), lambda: result.sent_with("no call was sent this"),
                   lambda: result.declared("no_such_tool")):
        with pytest.raises(LookupError):
            lookup()
    assert result.silent_turns() == [] and result.nudges() == [] and result.grace_opened() == []


def _bad_request():
    """A provider's 400 for a request no retry can mend (an invalid tool schema)."""
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error",
                                       "message": "tools.0.custom.input_schema: JSON schema is invalid"}}
    return anthropic.BadRequestError("Error code: 400 - invalid tool schema",
                                     response=httpx.Response(400, request=request, json=body), body=body)


def test_consume_hands_each_outcome_to_the_unit_the_graph_or_the_debate_hands_it_to(tmp_path):
    prose = "The ratios settle the question, in my view."
    probe = "Recount the brighter row of the product."

    async def go():
        async with open_run(tmp_path) as run:
            seen = {}
            # An unfiled worker: the lead reads the runtime's failure record, never the prose.
            worker = await run.turn("worker", [prose, STOP])
            seen["worker"] = worker, await run.consume(worker)
            # A librarian pass whose model call failed: the reviewer reads the graph's notice.
            librarian = await run.turn("librarian", [_bad_request()])
            seen["librarian"] = librarian, await run.consume(librarian)
            # A lead that filed rework: a worker's next round carries its probe.
            lead = await run.turn("lead", [("write_report", {"content": report_content(
                "lead", claim="One row disagrees with the other.", what_to_probe=probe)}),
                ("turn_end", {"summary": "another round"})])
            seen["lead"] = lead, await run.consume(lead)
            # The lead judges the round in the same analysis pass, one round fewer left; the pass
            # then reaches the reviewer counted once.
            judged = await run.turn("lead", run.file_script("lead"), handed=[seen["lead"][1].turn])
            seen["judged"] = judged, await run.consume(judged)
            # A lead whose cycle returned no report, and a reviewer that raised: the run ends.
            silent = await run.turn("lead", [prose, STOP])
            seen["silent"] = silent, await run.consume(silent)
            with pytest.raises(ValueError, match="ends the run"):
                await run.turn("reviewer", [], handed=[silent])
            with pytest.raises(ValueError, match="rework round"):
                await run.turn("worker", [], handed=[worker])
            reviewer = await run.turn("reviewer", [prose, STOP])
            seen["reviewer"] = reviewer, await run.consume(reviewer)
            to_human = await run.turn("reviewer", [("write_report", {"content": report_content(
                "reviewer", assign_to="human")}), ("turn_end", {"summary": "filed"})])
            seen["human"] = to_human, await run.consume(to_human)
            return run, seen

    run, seen = asyncio.run(go())

    worker, to_lead = seen["worker"]
    assert worker.outcome.failure_record and to_lead.consumer == "lead" and to_lead.turn.filed
    assert '"status": "failed"' in to_lead.shown() and prose not in to_lead.shown()
    librarian, to_reviewer = seen["librarian"]
    assert isinstance(librarian.error, d.ModelCallFailed) and to_reviewer.consumer == "reviewer"
    assert '"arrived": "librarian_failed"' in to_reviewer.shown() and "filed no report" in to_reviewer.shown()
    assert to_reviewer.turn.filed and [e["event"] for e in to_reviewer.turn.events].count("librarian.failed") == 1
    lead, to_worker = seen["lead"]
    assert lead.filed and lead.report.rework and to_worker.consumer == "worker" and to_worker.turn.filed
    assert probe in to_worker.shown()
    judged, to_reviewer_again = seen["judged"]
    assert judged.filed and "1 round(s) still" in judged.sent[0].text()
    assert "2 round(s) still" in lead.sent[0].text()
    assert "2 analysis pass(es) remain" in to_reviewer_again.shown()
    for key in ("silent", "reviewer"):
        turn, ended = seen[key]
        assert not turn.filed and ended.consumer is None and ended.turn is None and ended.shown() == ""
        assert ended.ended is turn.error
    assert seen["silent"][0].error is None and seen["reviewer"][0].error is not None
    to_human, published = seen["human"]
    assert to_human.filed and (published.consumer, published.turn, published.ended) == ("human", None, None)


def test_a_shaped_toolkit_holds_a_failed_product_and_misdeclared_figures_the_runtime_corrects(tmp_path):
    from auto_research.provenance.store import sha256_file

    async def go():
        async with open_run(tmp_path, shapes=True) as run:
            unit = run.unit("worker")
            copied = lambda sent: run.receipts.get(handles_shown(sent, unit, "d")[0]).path  # noqa: E731
            result = await run.turn("worker", [
                lambda sent: (d.tool_named(sent, d.FAILING_PRODUCT_TOOL), {"csv_path": str(run.product)}),
                lambda sent: (d.tool_named(sent, "count_rows"), {
                    "csv_path": str(copied(sent)), "min_counts": {"value": 0, "reason": "every row"}}),
                lambda sent: (d.tool_named(sent, "write_band_table"), {"csv_path": str(run.product)}),
                lambda sent: (d.tool_named(sent, d.MISDECLARED_FIGURE_TOOL),
                              {"ratios": {"$ref": f"{handles_shown(sent, unit, 'r')[-1]}#/ratios"}}),
                ("write_report", {"content": report_content("worker") + "# figures\n- [self:1]\n"}),
                ("turn_end", {"summary": "filed"}),
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.filed, [(r.name, r.text) for r in result.tool_results]
    mine = run.receipts.receipts(source=result.source)
    by = lambda suffix: [r for r in mine if r.produced_by.endswith(suffix)]  # noqa: E731
    failed_call, product = sorted(by(d.FAILING_PRODUCT_TOOL), key=lambda r: r.kind != "result")
    assert failed_call.flags == ["failed"] and product.kind == "data" and "failed" in product.flags
    (count,) = by("count_rows")
    assert product.handle in count.parents and "failed" in count.flags  # the descendant carries the flag
    assert run.product.read_bytes() != product.path.read_bytes()  # its own bytes: no false ancestry
    (figure,) = [r for r in by(d.MISDECLARED_FIGURE_TOOL) if r.kind == "figure"]
    assert figure.caption == d.MISDECLARED_CAPTION and figure.sha256 == sha256_file(figure.path)
    (shown,) = result.results_named(result.declared(d.MISDECLARED_FIGURE_TOOL))
    assert d.FIGURE_HASH_OVERRIDDEN in shown.text and d.FILE_ABSENT in shown.text and d.ABSENT_CAPTION in shown.text
    assert not [r for r in mine if r.caption == d.ABSENT_CAPTION]


def test_a_colead_group_whose_first_member_shares_the_coders_vendor_reviews_through_the_other(tmp_path):
    with pytest.raises(ValueError, match="openai"):
        d.Run(tmp_path, pipeline=True, pipeline_colead=("anthropic",))
    chain = asyncio.run(run_chain(tmp_path, colead_members=("anthropic", "openai")))

    assert chain.colead_members == ("anthropic", "openai") and chain.providers == ("anthropic", "openai")
    # The openai member answered the review (a call to the anthropic one is the script's error).
    assert len(chain.run.colead_calls()) == 2 and len(chain.log) == 1 and chain.lead.commits()


def test_the_worker_repl_and_shell_answer_writability_and_write_nothing(tmp_path):
    import sys
    from pathlib import Path

    async def go():
        before = d.tree_snapshot()
        async with open_run(tmp_path) as run:
            shell = run.harness_call("worker", "shell")
            shell_probe = lambda path: shell.step(command=d.writability_probe(path))  # noqa: E731
            own, inputs, toolkit = run.workdir("worker"), run.product.parent, run.toolkit_root
            prefix = Path(sys.prefix)
            repl = {"own": await run.repl_writable(own), "inputs": await run.repl_writable(inputs),
                    "toolkit": await run.repl_writable(toolkit), "repo": await run.repl_writable(d.REPO),
                    "prefix": await run.repl_writable(prefix)}
            result = await run.turn("worker", [shell_probe(own), shell_probe(toolkit), shell_probe(prefix),
                                               *run.file_script("worker")])
        return repl, result, shell.tool, d.tree_changes(before, d.tree_snapshot())

    repl, result, tool, changed = asyncio.run(go())

    assert repl == {"own": True, "inputs": False, "toolkit": False, "repo": False, "prefix": False}
    writable, *read_only = result.results_named(tool)
    assert d.probed_writable(writable.text) and not any(d.probed_writable(r.text) for r in read_only)
    assert changed == []
    with pytest.raises(AssertionError):
        d.probed_writable("bash: [: missing ]")


def test_a_tree_snapshot_names_each_change_and_passes_over_bytecode(tmp_path):
    (tmp_path / "kept.txt").write_text("a", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("b", encoding="utf-8")
    before = d.tree_snapshot(tmp_path)
    (tmp_path / "kept.txt").write_text("a longer text", encoding="utf-8")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "new.txt").write_text("c", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.cpython.pyc").write_bytes(b"x")
    (tmp_path / "loose.pyc").write_bytes(b"x")
    (tmp_path / "__pycache__" / "hidden.py").write_text("x", encoding="utf-8")  # no bytecode: seen
    (tmp_path / "empty").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "empty")

    assert d.tree_changes(before, d.tree_snapshot(tmp_path)) == sorted([
        f"+ {tmp_path / 'new.txt'}", f"- {tmp_path / 'gone.txt'}", f"~ {tmp_path / 'kept.txt'}",
        f"+ {tmp_path / '__pycache__' / 'hidden.py'}", f"+ {tmp_path / 'empty'}", f"+ {tmp_path / 'link'}"])
