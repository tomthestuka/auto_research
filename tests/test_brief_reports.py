"""C3: a tool brief (``ToolSpec``) is a report of the lead's turn (ruling Q3, 2026-11-04).

``author_chain`` files one brief report per link (``spec1``, ``spec2`` …); the lead revises one
with ``edit_report``/``write_report`` and ``report`` set to its id. A coding session is bound to
its brief by id: the coder (at its next turn, as a diff), the gate and the co-lead read the
current version and each read is recorded. The link's commit locks its brief and the commit
receipt names the reviewed version; ``turn_end`` locks every report of the turn, and a locked brief
is minted once as a receipt the run can view and never cite.

Driven through the lead's real cycle, over the seams of ``tests/test_chain_advance.py``: real
``run_cycle``, ``_ChainDriver``, ``AuthoringSession`` on a throwaway git repository and
``ReceiptStore``; faked are the lead's model, the coder harness, the gate's verdict and the
co-lead's verdicts.
"""

from __future__ import annotations

import asyncio
import json

from auto_research.agents.react import execute_edit_report
from auto_research.provenance.store import ReceiptStore, is_evidence
from tests.test_chain_advance import (
    CHAIN, FINISH, SUBMIT, _cycle, _git_out, _link,
)

ENDING = "-> per-segment PD/PA table."
REVISED = "-> per-segment PD/PA table with 1-sigma errors."
# Four links on three author credits: link 4 stays queued, so its brief is open at turn_end.
FOUR_LINKS = ("author_chain", {"first_link": _link(0), "additional_links": [_link(1), _link(2), _link(3)],
                              "rationale": "r"})


def _edit(report, old, new):
    return ("edit_report", {"report": report, "old_string": old, "new_string": new})


def _results(model, call):
    """The tool results the model was answered with before its ``call``-th call (0-based)."""
    return model.seen[call][1]


def test_a_revision_reaches_the_coder_the_gate_and_the_colead(tmp_path, monkeypatch):
    """Mid-session the lead revises spec1: the coder's next turn opens with the diff, the gate and
    the co-lead read the revised brief, the commit receipt names that version, and the committed
    brief refuses a further edit."""
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [_edit("spec1", ENDING, REVISED)],
         [("message", {"to": "coder", "text": "add the error columns"})],
         [SUBMIT], [_edit("spec1", REVISED, ENDING)], [SUBMIT], *FINISH],
        verdicts=[True, True],
    )
    revised = _results(model, 2)[0]
    assert "Brief spec1 (tool_0) is version" in revised and "its running coding session" in revised
    second_turn = queries["tool_0"][1]
    assert second_turn.startswith("The lead revised your brief spec1; it is now version ")
    assert any(line.startswith("+") and line.endswith(REVISED) for line in second_turn.splitlines())
    assert any(line.startswith("-") and line.endswith(ENDING) for line in second_turn.splitlines())
    assert second_turn.rstrip().endswith("add the error columns")
    assert model.gate_specs[0].expected_behavior.endswith(REVISED)
    assert model.review_specs[0].expected_behavior.endswith(REVISED)

    brief = model.workspaces[0].briefs["spec1"]
    session = built[0][1]
    reads = [(r["by"], r["sha256"]) for r in session.spec_reads]
    assert [by for by, _ in reads] == ["coder", "coder", "gate", "co-lead"]
    assert reads[0][1] != brief.sha256 and {sha for _, sha in reads[1:]} == {brief.sha256}

    (commit_receipt, _) = store.receipts(kind="commit")
    commit = json.loads(store.text(commit_receipt.handle))
    assert commit["tool"] == "tool_0" and commit["toolspec_sha256"] == brief.sha256
    receipt = store.get(commit["toolspec"])
    assert receipt.produced_by == "toolspec" and not is_evidence(receipt)
    assert receipt.sha256 == brief.sha256, "the reviewed version is the brief receipt's bytes"
    assert receipt.handle in commit_receipt.parents
    assert brief.bound is False, "its session closed"
    assert store.text(receipt.handle) == brief.document() and REVISED in brief.document()

    frozen = _results(model, 5)[0]
    assert "brief spec1 is locked (its link committed)" in frozen
    assert f"view it as receipt {receipt.handle}" in frozen
    assert brief.value.expected_behavior.endswith(REVISED), "the committed brief stands"


def test_turn_end_locks_every_report_of_the_turn(tmp_path, monkeypatch):
    """Each brief locks at its link's end — committed (spec1, spec3) or not (spec2) — and spec4,
    never opened once the credits ran out, locks at turn_end with the result report; each refuses
    an edit afterwards."""
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[FOUR_LINKS], [SUBMIT], [SUBMIT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, False, True], submit_cap=1,
    )
    assert [r.status for r in outcome.authored] == ["committed", "failed", "committed"]
    workspace = model.workspaces[0]
    briefs = workspace.briefs
    assert workspace.locked
    assert [briefs[b].locked_by for b in ("spec1", "spec2", "spec3", "spec4")] == [
        "commit", "link_end", "commit", "turn_end"]
    minted = {r.handle for r in store.receipts(kind="result") if r.produced_by == "toolspec"}
    assert minted == {briefs[b].receipt for b in ("spec1", "spec2", "spec3", "spec4")}
    assert store.text(briefs["spec4"].receipt) == briefs["spec4"].document()

    result = asyncio.run(execute_edit_report(
        workspace, {"old_string": "the chain is done", "new_string": "changed"}))
    assert result.is_error and "locked by turn_end" in result.text
    for brief_id, why in (("spec1", "its link committed"),
                          ("spec2", "its link has ended, so no session reads it"),
                          ("spec4", "turn_end filed the turn")):
        out = asyncio.run(execute_edit_report(
            workspace, {"report": brief_id, "old_string": ENDING, "new_string": REVISED}))
        assert out.is_error and f"brief {brief_id} is locked ({why})" in out.text
    assert workspace.written_report.summary == "the chain is done"


def test_a_second_chain_appends_and_opens_its_own_links(tmp_path, monkeypatch):
    """C5: a chain filed while link 1 runs is queued after the waiting link, and every link opens
    in turn; the decision lists each brief beside its own authored result."""
    later = ("author_chain", {"first_link": _link(2), "additional_links": [], "rationale": "r2"})
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[CHAIN], [later], [SUBMIT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, True, True],
    )
    filed = _results(model, 2)[0]
    assert "filed as briefs spec3 (tool_2), in order." in filed
    assert "They open after the links still waiting (spec2)." in filed
    assert "They open once the running link ends." in filed
    assert [spec.proposed_tool_name for spec, _ in built] == ["tool_0", "tool_1", "tool_2"]
    assert any("Coding session 3 of 3 is open on 'tool_2' (brief spec3)" in t
               for t in _results(model, 4))
    assert [s.proposed_tool_name for s in outcome.decision.tool_specs] == ["tool_0", "tool_1", "tool_2"]
    assert [r.status for r in outcome.authored] == ["committed"] * 3
    assert _git_out(repo, "log", "--format=%s", "-3").split("\n")[:3] == [
        "Add tool_2", "Add tool_1", "Add tool_0"]


def test_a_running_links_name_stays_and_a_waiting_link_is_revised(tmp_path, monkeypatch):
    """Ruling (b): spec1's name is fixed while its session runs; spec2, still waiting, takes a new
    name, and its link opens as the revised tool."""
    rename = ("# proposed_tool_name\ntool_{}\n", "# proposed_tool_name\ntool_9\n")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [_edit("spec1", rename[0].format(0), rename[1])],
         [_edit("spec2", rename[0].format(1), rename[1])], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, True],
    )
    refused = _results(model, 2)[0]
    assert "brief spec1 is unchanged" in refused
    assert ("# proposed_tool_name stays 'tool_0' while this link's coding session runs; brief a "
            "different tool as a new link with author_chain.") in refused
    assert "Brief spec2 (tool_9) is version" in _results(model, 3)[0]
    assert [spec.proposed_tool_name for spec, _ in built] == ["tool_0", "tool_9"]
    assert "tool_9" in queries["tool_9"][0], "the waiting link opened on its revised brief"


def test_a_resumed_lead_sees_its_briefs_as_they_were(tmp_path, monkeypatch):
    """After turn_end a message resumes the lead (E12), its kept turn rebuilt from the ledger: the
    brief it filed and revised is there as it locked, through view_receipt, and the store as
    reloaded from disk holds that same version."""
    from auto_research.monitor import RunMonitor
    from auto_research.run_control import RunControl

    control = RunControl()
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")

    def view_spec2(model):
        return [("view_receipt", {"handle": model.workspaces[0].briefs["spec2"].receipt})]

    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [_edit("spec2", ENDING, REVISED)], [SUBMIT], [SUBMIT], *FINISH,
         view_spec2, [("turn_end", {"summary": "answered"})]],
        verdicts=[True, True], monitor=monitor, run_control=control, seat=True,
    )
    assert outcome.report is not None and model.lead.has_kept_turn()
    brief = model.workspaces[0].briefs["spec2"]
    control.add_steer("analyst_lead", "which brief did link 2 run on?", sender="human",
                      kind="message")
    asyncio.run(model.lead.resume())

    (shown,) = _results(model, len(model.seen) - 1)
    assert brief.document().strip() in shown and REVISED in shown
    assert brief.locked_by == "commit"
    reloaded = ReceiptStore(tmp_path / "receipts")
    assert reloaded.text(brief.receipt) == brief.document()


def test_a_stranded_brief_is_locked_unblocking_at_turn_end(tmp_path, monkeypatch):
    """Ruling C3c: spec4, queued when the credits ran out, is never attempted; a handle it names
    that is no receipt of the run is noted at the edit, blocks nothing, and turn_end locks the
    brief as what was never attempted."""
    ghost = "{{analyst_worker_0:r99}}"
    with_ghost = f"{ENDING} Bins as in {ghost}."
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[FOUR_LINKS], [_edit("spec4", ENDING, with_ghost)], [SUBMIT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, True, True],
    )
    noted = _results(model, 2)[0]
    assert "turn_end will refuse until these are fixed:" in noted and "analyst_worker_0:r99" in noted
    assert outcome is not None and outcome.report is not None, "turn_end was accepted at once"
    spec4 = model.workspaces[0].briefs["spec4"]
    assert spec4.stranded and spec4.locked_by == "turn_end"
    assert store.text(spec4.receipt) == spec4.document() and ghost in spec4.document()


def test_a_brief_changed_after_its_review_cannot_commit(tmp_path):
    """The commit names the version the co-lead reviewed, so a brief that moved after the review
    is refused at commit (C3), as a changed changeset is."""
    from auto_research.agents.react import BriefReport
    from auto_research.schemas import ToolSpec
    from tests.test_chain_advance import _review, _session
    from tests.test_merge_flow import _FakeLead, _git, _toolkit_repo

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    brief = BriefReport(id="spec1", value=ToolSpec.model_validate(_link(0)))
    session = _session(brief.value, lead=_FakeLead(), repo=repo, worktree_base=tmp_path / "wt",
                       review=_review([True], []), queries={})
    session.brief = brief

    async def scenario():
        await session.connect()
        await session.drive("write it")
        gate = await session.run_gate_step()
        await session.run_review_step(gate)
        brief.value = brief.value.model_copy(update={"notes": "one more thing"})
        try:
            await session.commit("Add tool_0")
        except RuntimeError as exc:
            return str(exc)
        finally:
            await session.aclose()

    assert asyncio.run(scenario()) == "the brief changed after the co-lead reviewed it; submit again"
    assert brief.locked_by is None and session.last_commit_sha is None


def test_other_roles_report_tools_are_unchanged():
    """Only a lead with authoring on names a target report; every other surface keeps its schema."""
    from auto_research.agents.react import (
        LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES, edit_report_tool_def, write_report_tool_def,
    )

    assert set(edit_report_tool_def()["input_schema"]["properties"]) == {
        "old_string", "new_string", "replace_all"}
    assert set(write_report_tool_def()["input_schema"]["properties"]) == {"content"}
    lead = edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES, briefs=True)
    assert "report" in lead["input_schema"]["properties"]
    assert "# proposed_tool_name —" in lead["description"]
    assert edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES)["description"] == (
        edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES, briefs=False)["description"])


def test_every_units_turn_end_locks_its_report(tmp_path):
    """Ruling Q3 binds every unit: a worker's and a librarian's accepted turn_end locks the
    report, and the next edit is answered with the report as filed."""
    from auto_research.agents.react import ReportWorkspace, SandboxToolProvider
    from auto_research.schemas import AnalystReport
    from tests.test_librarian import _read_one, _report
    from tests.test_report_authoring import _StubRegistry

    workspace = ReportWorkspace(report_path=tmp_path / "w.jsonl", report_model=AnalystReport,
                                catalog={"self": []})
    worker = SandboxToolProvider(_StubRegistry(), object(), report_workspace=workspace)
    draft = {"content": "# summary\nnothing ran\n# status\nfailed\n# findings\n# figures\n"
                        "# uncertainty\n# tool_request\n"}
    assert not asyncio.run(worker.execute("write_report", draft)).is_error
    assert not asyncio.run(worker.execute("turn_end", {"summary": "s"})).is_error
    after = asyncio.run(worker.execute("edit_report", {"old_string": "nothing ran", "new_string": "x"}))
    assert after.is_error and "locked by turn_end" in after.text and workspace.locked

    librarian = _read_one(tmp_path / "lib")
    assert not asyncio.run(librarian.execute("write_report", _report())).is_error
    assert not asyncio.run(librarian.execute("turn_end", {"summary": "s"})).is_error
    late = asyncio.run(librarian.execute("write_report", _report(summary="Another summary.")))
    assert late.is_error and "locked by turn_end" in late.text


def test_a_brief_is_filed_as_its_document_reads_it_back(tmp_path):
    """Review of C3: a value the brief document would change is refused at filing, by field; a
    mission named with spaces survives, and an unrelated edit changes nothing else."""
    from auto_research.agents.react import (
        AUTHOR_CHAIN_TOOL, LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES, LeadCycle, ReadOnlyToolProvider,
        ReportWorkspace,
    )
    from auto_research.schemas import AnalystReport

    cycle = LeadCycle(authoring_enabled=True)
    provider = ReadOnlyToolProvider(roots=[], cycle=cycle)
    provider.report_workspace = ReportWorkspace(
        report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog={},
        prose_fields=LEAD_REPORT_FIELDS, status_values=LEAD_STATUS_VALUES, briefs=cycle.briefs,
    )
    bad = {**_link(0), "proposed_checks": [{"kind": "diff: vs xspec", "description": "d"}],
           "expected_behavior": "f(x)\n# load the spectrum\nthen fit"}
    refused = asyncio.run(provider.execute(AUTHOR_CHAIN_TOOL, {"first_link": bad, "additional_links": []}))
    assert refused.is_error and "# load the spectrum" in refused.text
    assert not cycle.briefs, "nothing was filed"

    good = {**_link(0), "notes": "  spaced  ",
            "validation_data": [{"mission": "Cyg X-1", "obsid": "lit-2024"}]}
    filed = asyncio.run(provider.execute(AUTHOR_CHAIN_TOOL, {"first_link": good, "additional_links": []}))
    assert not filed.is_error and cycle.briefs["spec1"].value.notes == "spaced"
    before = cycle.briefs["spec1"].value.model_dump(exclude={"notes"})
    edited = asyncio.run(provider.execute("edit_report", {
        "report": "spec1", "old_string": "# notes\nspaced", "new_string": "# notes\nquiet"}))
    assert not edited.is_error, edited.text
    after = cycle.briefs["spec1"].value
    assert after.notes == "quiet" and after.model_dump(exclude={"notes"}) == before
    assert after.validation_data[0].mission == "Cyg X-1"


def test_a_brief_is_validated_before_its_submission(tmp_path, monkeypatch):
    """C3 ruling: a commit locks the brief, so a handle it names that is no receipt of the run
    stops the submission before the gate, spending nothing; once fixed, the next one commits."""
    ghost = "{{analyst_worker_0:r99}}"
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [_edit("spec1", ENDING, f"{ENDING} See {ghost}.")], [SUBMIT],
         [_edit("spec1", f" See {ghost}.", "")], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, True, True],
    )
    stopped = _results(model, 3)[0]
    assert "Not submitted: brief spec1 needs a fix first, since a commit locks it" in stopped
    assert "analyst_worker_0:r99" in stopped and "Nothing was spent." in stopped
    assert [r.status for r in outcome.authored] == ["committed", "committed"]
    assert built[0][1].submits_used == 1, "the refused submission cost nothing"
    assert reviews[0] == ("tool_0", None) and len(model.gate_specs) == 2, "no gate ran for it"


def test_a_failed_links_brief_says_its_link_ended_and_the_result_keeps_what_ran(tmp_path, monkeypatch):
    """C3 ruling: link 2 ends uncommitted, so its brief locks as the failed attempt was given; an
    edit is refused saying the link ended, and the decision pairs the failure with that version."""
    rename = ("# proposed_tool_name\ntool_1\n", "# proposed_tool_name\ntool_7\n")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [SUBMIT], [SUBMIT], [SUBMIT], [_edit("spec2", *rename)], *FINISH],
        verdicts=[True, False], submit_cap=1,
    )
    reply = _results(model, 5)[0]
    assert ("brief spec2 is locked (its link has ended, so no session reads it) and stands as it "
            "is") in reply and "Brief a further change as a new link with author_chain." in reply
    assert [s.proposed_tool_name for s in outcome.decision.tool_specs] == ["tool_0", "tool_1"]
    assert [r.status for r in outcome.authored] == ["committed", "failed"]


def test_a_rebuttal_with_an_unready_brief_spends_no_debate_turn(tmp_path, monkeypatch):
    """The brief check runs before a rebuttal's review as before a submission's gate (C3 ruling)."""
    from tests.test_chain_advance import REBUT

    ghost = "{{analyst_worker_0:r99}}"
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [SUBMIT], [_edit("spec1", ENDING, f"{ENDING} See {ghost}.")], [REBUT],
         [_edit("spec1", f" See {ghost}.", "")], [REBUT], [SUBMIT], *FINISH],
        verdicts=[False, True, True],
    )
    (stopped,) = _results(model, 4)
    assert "Not submitted: brief spec1 needs a fix first" in stopped and "Nothing was spent." in stopped
    assert built[0][1].debates_used == 1, "only the second rebuttal reached the co-lead"
    assert [r.status for r in outcome.authored] == ["committed", "committed"]


def test_a_dirty_tree_strands_the_rest_of_the_queue(tmp_path, monkeypatch):
    """Review of C3c: link 1's reset fails, so link 2 is refused and the chain stops; spec3, still
    queued and naming a handle no receipt carries, is stranded at once: turn_end accepts the turn
    and locks it as never attempted, and an edit says so."""
    from tests.test_chain_advance import THREE_LINKS, _failing_git

    ghost = "{{analyst_worker_0:r99}}"
    _failing_git(monkeypatch, "reset", "--hard")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[THREE_LINKS], [SUBMIT], [SUBMIT], [_edit("spec3", ENDING, f"{ENDING} See {ghost}.")],
         *FINISH],
        verdicts=[False], submit_cap=1,
    )
    spec3 = model.workspaces[0].briefs["spec3"]
    assert "No link can open brief spec3 any more, so turn_end locks it as written" in _results(model, 4)[0]
    assert outcome is not None and outcome.report is not None, "turn_end was accepted at once"
    assert spec3.stranded and spec3.locked_by == "turn_end"

