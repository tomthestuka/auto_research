"""C1/C2: every commit closes its link and opens the next, and each link's outcome is told truly.

Run polevo-20261104: link 1 of a four-link chain was rejected, the lead rebutted with
``respond_to_review``, the co-lead was persuaded and the tool committed, and the chain stayed on
link 1. The lead then briefed link 2 into link 1's still-open session, link 2 committed under
link 1's name, and link 2's own session opened only once link 1's submissions were spent, with a
message saying the link "did not commit" (C2: it states the session's commits and saved patch).

Driven through the lead's real cycle: ``AnalystLeadAgent.run_cycle`` over a real
``ReadOnlyToolProvider`` and ``_ChainDriver``, each link a real ``AuthoringSession`` from
``build_authoring_session`` on a throwaway git repository, committing with the real
``approve_edit``, and minting commit receipts into a real ``ReceiptStore``. Faked: the lead's model
(a script), the coder harness (a client that writes the tool's file), the gate's verdict (pass)
and the co-lead's verdicts (a script), which are the seams ``tests/test_merge_flow.py`` injects.
The passing gate carries no changeset manifest, so the reviewed-equals-committed binding inside
``AuthoringSession.commit`` is left to ``tests/test_merge_flow.py``.
"""

from __future__ import annotations

import asyncio
import json

from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents.roles import AnalystLeadAgent, ModelCallFailed, _ChainDriver
from auto_research.models import ModelResponse, ToolCall, ToolResultMessage
from auto_research.provenance.store import ReceiptStore
from auto_research.sandbox.merge_flow import build_authoring_session
from auto_research.schemas import AnalysisTask, AnalystReport, ToolReviewVerdict
from auto_research.schemas.toolspec import AnalystToolRequest
from tests.chain_stub import stub_compactor
from tests.test_lead_forms import GOOD_LINK
from tests.test_merge_flow import _coder_member, _git, _git_out, _pass_gate, _toolkit_repo

REPORT = (
    "# summary\nthe chain is done\n# status\nsucceeded\n# findings\n- the tools landed\n"
    "# figures\n"
)


class _Coder:
    """The coder harness: each instruction rewrites this link's tool file, then narrates it."""

    def __init__(self, repo, tool, queries, broken_disconnect=False, disconnects=None):
        self.repo, self.tool, self.queries = repo, tool, queries
        self.broken_disconnect = broken_disconnect  # the first disconnect raises
        self.disconnects = disconnects if disconnects is not None else []

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnects.append(self.tool)
        if self.broken_disconnect:
            self.broken_disconnect = False
            raise RuntimeError("the author process is gone")

    async def query(self, message, session_id="default"):
        self.queries.append(message)
        path = self.repo / "demo_utils" / "lib" / f"{self.tool}.py"
        path.parent.mkdir(parents=True, exist_ok=True)  # a reset's `git clean` removes it empty
        path.write_text(f"def {self.tool}(x):\n    return x  # turn {len(self.queries)}\n")

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        await asyncio.sleep(0.2)  # long enough that a submit right after it waits for it
        yield AssistantMessage(
            content=[TextBlock(text=f"{self.tool} turn {len(self.queries)} written")], model="m"
        )


class _ScriptDone(Exception):
    """The lead's script ran out: the cycle went somewhere the test did not expect."""


class _Lead:
    """The lead's model: one scripted batch per call, recording what each call was answered."""

    def __init__(self, script, built):
        self.script, self.built = list(script), built
        self.seen: list[tuple[int, list[str]]] = []  # (sessions built, last tool results)

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto", images=(),
                   documents=(), cache_prefix=False):
        self.last_messages = list(messages)
        self.__dict__.setdefault("sent", []).append(list(messages))
        last = messages[-1]
        texts = [p.content for p in last.results] if isinstance(last, ToolResultMessage) else []
        self.seen.append((len(self.built), texts))
        if not self.script:
            raise _ScriptDone
        step = len(self.seen)
        batch = self.script.pop(0)
        if callable(batch):  # a step written against what the run holds by then (a handle)
            batch = batch(self)
        extra = {}
        if getattr(self, "prompt_tokens", None):  # a member near its window (segment tests)
            from auto_research.models.types import ModelUsage

            extra["usage"] = ModelUsage(input_tokens=self.prompt_tokens)
        return ModelResponse(tool_calls=[
            ToolCall(id=f"c{step}_{k}", name=name, arguments=args)
            for k, (name, args) in enumerate(batch)
        ], **extra)


def _review(verdicts, reviews):
    """The co-lead: answers from ``verdicts`` in order, recording (tool, rebuttal) per call."""
    verdicts = list(verdicts)

    async def review(worktree, spec, gate, *, prior_review=None, lead_rebuttal=None):
        reviews.append((spec.proposed_tool_name, lead_rebuttal))
        review.specs.append(spec.model_copy())
        approved = verdicts.pop(0)
        return ToolReviewVerdict(
            approved=approved, rationale="ok" if approved else "the test misses an edge",
            issues=[] if approved else ["edge case untested"],
            commit_summary=f"Add {spec.proposed_tool_name}" if approved else "",
        )

    review.specs = []  # the brief each review read
    return review


def _session(spec, *, lead, repo, worktree_base, review, queries, submit_cap=None, monitor=None,
             broken_disconnect=False, disconnects=None, gate_specs=None):
    """A real authoring session over the seams above, as `_ChainDriver._build_session` builds one."""

    async def gate(worktree, spec_read):
        if gate_specs is not None:
            gate_specs.append(spec_read.model_copy())   # the brief each gate run read
        return await _pass_gate(worktree, spec_read)

    return build_authoring_session(
        spec, lead=lead, toolkit_root=repo, worktree_base=worktree_base, direct_branch="ai",
        coder=_coder_member("fake-model"), monitor=monitor, run_gate=gate,
        run_review=review, submit_cap=submit_cap,
        sdk_client_factory=lambda options: _Coder(
            repo, spec.proposed_tool_name, queries.setdefault(spec.proposed_tool_name, []),
            broken_disconnect=broken_disconnect, disconnects=disconnects,
        ),
    )


def _wire(tmp_path, monkeypatch, script, verdicts, submit_cap=None, broken_disconnect=(),
          monitor=None, run_control=None, seat=False, review_hook=None, human=None):
    """The lead, its sessions and the co-lead, wired as for one cycle (``review_hook`` replaces
    the scripted co-lead verdicts; ``human`` answers the lead's texts to the human, on the run's
    channel). Returns (lead, model, built, queries, reviews, repo, store)."""
    stub_compactor(monkeypatch)
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    built: list = []      # (spec, session) per link opened
    queries: dict = {}    # tool -> instructions its coder received
    reviews: list = []    # (tool, rebuttal) per co-lead call
    review = _review(verdicts, reviews)
    disconnects: list = []
    gate_specs: list = []

    async def build(self, spec):
        session = _session(
            spec, lead=self.lead, repo=self.toolkit_root, worktree_base=self.worktree_base,
            review=review_hook or review, queries=queries, submit_cap=submit_cap,
            monitor=self.lead.monitor, broken_disconnect=spec.proposed_tool_name in broken_disconnect,
            disconnects=disconnects, gate_specs=gate_specs,
        )
        built.append((spec, session))
        return session

    monkeypatch.setattr(_ChainDriver, "_build_session", build)
    model = _Lead(script, built)
    model.disconnects, model.gate_specs, model.review_specs = disconnects, gate_specs, review.specs
    # A spy on the real builder: the test reads the turn's reports after the cycle.
    make = AnalystLeadAgent._make_report_workspace
    model.workspaces = []

    def spy(self, source_reports):
        workspace = make(self, source_reports)
        model.workspaces.append(workspace)
        return workspace

    monkeypatch.setattr(AnalystLeadAgent, "_make_report_workspace", spy)
    lead = AnalystLeadAgent(model=model, prompt="lead", monitor=monitor)
    model.lead = lead
    lead.run_control = run_control
    lead.report_dir = tmp_path / "reports"
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    # As `runtime.readonly_tools_for` wires it: each commit becomes a receipt of the lead's.
    lead.tools.receipts = store = ReceiptStore(tmp_path / "receipts")
    lead.tools.receipt_source = "analyst_lead"
    model.store = store
    if seat:  # the lead as the runtime seats it: its key and the run's store (`attach_lead_runtime`)
        lead.agent_key, lead.receipts = "analyst_lead", store
    if human is not None:  # the run's channel, as `run_research` seats the team on it
        from auto_research.hitl.router import SeatRouter

        lead.channel = SeatRouter(run_control, human_handler=human, monitor=monitor)
        lead.channel.register(lead.agent_key, lead)
    return lead, model, built, queries, reviews, repo, store


def _cycle(tmp_path, monkeypatch, script, verdicts, submit_cap=None, broken_disconnect=(),
           monitor=None, run_control=None, seat=False, review_hook=None, human=None,
           setup=None):
    """Run one lead cycle over a two-link chain; return what the test inspects (the coders'
    disconnects are on ``model.disconnects``). ``setup(lead, model)`` runs before the cycle."""
    lead, model, built, queries, reviews, repo, store = _wire(
        tmp_path, monkeypatch, script, verdicts, submit_cap, broken_disconnect, monitor,
        run_control, seat, review_hook, human,
    )
    if setup is not None:
        setup(lead, model)
    worker = AnalystReport(summary="w0", tool_request=AnalystToolRequest(
        science_context="c", expected_behavior="b", proposed_tool_name="t",
        validation_data=[{"mission": "IXPE", "obsid": "01002901"}],
    ))
    try:
        outcome = asyncio.run(lead.run_cycle(
            AnalysisTask(question="q"), [worker], sources=["analyst_worker_0"], rounds_left=1,
            author_budget=3, toolkit_root=repo, worktree_base=tmp_path / "wt",
            direct_branch="ai", coder=_coder_member("fake-model"),
        ))
    except (ModelCallFailed, _ScriptDone) as exc:
        # The script ran out: the cycle went somewhere unexpected, which the assertions name.
        if isinstance(exc, ModelCallFailed) and not isinstance(exc.__cause__, _ScriptDone):
            raise
        outcome = None
    return outcome, model, built, queries, reviews, repo, store


def _link(i):
    return {**GOOD_LINK, "proposed_tool_name": f"tool_{i}"}


CHAIN = ("author_chain", {"first_link": _link(0), "additional_links": [_link(1)], "rationale": "r"})
SUBMIT = ("submit_for_review", {})
REBUT = ("respond_to_review", {"message": "the edge case is covered by the second test"})
FINISH = [[("write_report", {"content": REPORT})], [("turn_end", {"summary": "done"})]]


def _assert_two_links_committed(outcome, built, queries, repo, store):
    assert [spec.proposed_tool_name for spec, _ in built] == ["tool_0", "tool_1"]
    assert outcome is not None and outcome.report is not None, "turn_end was accepted"
    assert [r.status for r in outcome.authored] == ["committed", "committed"]
    # Each link's coder had its own brief, and only its own.
    assert "tool_1" in queries["tool_1"][0]
    assert not any("tool_1" in q for q in queries["tool_0"])
    # One commit per link, each under its own name, on the branch.
    subjects = _git_out(repo, "log", "--format=%s", "-2").split("\n")[:2]
    assert subjects == ["Add tool_1", "Add tool_0"]
    # Each commit's receipt names its own link's tool and that link's commit.
    shas = _git_out(repo, "log", "--format=%H", "-2").split()[::-1]
    records = [json.loads(store.text(r.handle)) for r in store.receipts(kind="commit")]
    assert [(r["tool"], r["commit_sha"]) for r in records] == list(zip(["tool_0", "tool_1"], shas))


def test_a_persuaded_commit_opens_the_next_link_with_its_own_brief(tmp_path, monkeypatch):
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [SUBMIT], [REBUT], [SUBMIT], *FINISH],
        verdicts=[False, True, True],
    )
    # The call after the rebuttal: link 2 is already open, and the lead is told so.
    sessions, answered = model.seen[3]
    assert sessions == 2, "the persuaded commit did not open the next link"
    assert any("PERSUADED" in t and "Coding session 2 of 2 is open on 'tool_1'" in t
               for t in answered)
    assert reviews == [("tool_0", None), ("tool_0", REBUT[1]["message"]), ("tool_1", None)]
    _assert_two_links_committed(outcome, built, queries, repo, store)
    # The submission count is the new link's own.
    assert [session.submits_used for _, session in built] == [1, 1]


def test_a_direct_approval_still_opens_the_next_link(tmp_path, monkeypatch):
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[CHAIN], [SUBMIT], [SUBMIT], *FINISH], verdicts=[True, True],
    )
    sessions, answered = model.seen[2]
    assert sessions == 2
    assert any("COMMITTED tool_0" in t and "Coding session 2 of 2 is open on 'tool_1'" in t
               for t in answered)
    _assert_two_links_committed(outcome, built, queries, repo, store)


def test_a_held_rebuttal_keeps_the_link_open(tmp_path, monkeypatch):
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [SUBMIT], [REBUT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False, False, True, True],
    )
    sessions, answered = model.seen[3]   # the call after the rebuttal
    assert sessions == 1, "a rebuttal the co-lead rejected must not advance the chain"
    assert any("HELD its position" in t for t in answered)
    assert not any("Coding session 2" in t for t in answered)
    assert model.seen[4][0] == 2, "the link's own approval then advances it"
    _assert_two_links_committed(outcome, built, queries, repo, store)


# ── C2: what a link's end tells the lead ─────────────────────────────────────


def test_a_spent_link_names_its_saved_patch_and_the_next_link_has_its_own_cap(tmp_path, monkeypatch):
    """Link 1 spends its submissions uncommitted: the lead is told so, told where the reset saved
    the work, and link 2 starts its own count (the cap is per link, as each link is a session)."""
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[CHAIN], [SUBMIT], [SUBMIT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False, False, True], submit_cap=2,
    )
    sessions, answered = model.seen[4]   # the call after the third submit
    assert sessions == 2
    (told,) = [t for t in answered if "CI BUDGET SPENT" in t]
    assert "Coding session 2 of 2 is open on 'tool_1'" in told
    patch = built[0][1].reset_patch
    assert patch is not None and (
        "all 2 review submissions for tool_0 are used and it did not commit. This session's "
        f"uncommitted changes were cleared from the toolkit and saved as a patch at {patch}."
    ) in told
    assert "def tool_0" in patch.read_text(), "the saved patch holds the uncommitted work"
    assert not (repo / "demo_utils" / "lib" / "tool_0.py").exists(), "the tree was reset"
    # Link 2 ran on its own count: its one submission reached the co-lead and committed.
    assert [session.submits_used for _, session in built] == [2, 1]
    assert reviews[-1] == ("tool_1", None)
    assert [r.status for r in outcome.authored] == ["failed", "committed"]
    assert _git_out(repo, "log", "--format=%s", "-1").strip() == "Add tool_1"


def test_a_spent_session_that_committed_says_which_commits_it_made(tmp_path):
    """Outside a chain (no cycle: the session stays open after its commit) a session can commit
    and then spend its cap on a follow-up; the message names the commit instead of "it did not
    commit", the wording run polevo-20261104 showed a session that had committed twice."""
    from auto_research.agents.react import execute_submit
    from tests.test_merge_flow import _FakeLead

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    reviews: list = []
    from auto_research.schemas import ToolSpec

    spec = ToolSpec.model_validate(_link(0))
    session = _session(spec, lead=_FakeLead(), repo=repo, worktree_base=tmp_path / "wt",
                       review=_review([True, False], reviews), queries={}, submit_cap=2)

    async def scenario():
        await session.connect()
        await session.drive("write it")
        committed = await execute_submit(session)
        landed = _git_out(repo, "rev-parse", "HEAD").strip()
        await session.drive("a follow-up")
        await execute_submit(session)          # rejected: the cap is now spent
        spent = await execute_submit(session)
        await session.aclose()
        return committed, landed, spent

    committed, sha, spent = asyncio.run(scenario())
    assert "COMMITTED tool_0" in committed.text
    assert session.commits == [sha], "the session's record is the commit git holds"
    assert (f"all 2 review submissions for tool_0 are used. This session committed tool_0 "
            f"({sha[:10]}); that commit stays on the authoring branch.") in spent.text
    assert "did not commit" not in spent.text


def test_every_rebuttal_outcome_carries_the_coders_narration(tmp_path, monkeypatch):
    """The rebuttal waits for a working coder, as a submit does, and the coder's turn it waited
    for reaches the lead with the verdict, here a HELD one, as `execute_submit` does."""
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch,
        [[CHAIN], [SUBMIT], [("message", {"to": "coder", "text": "cover the edge case"})],
         [REBUT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False, False, True, True],
    )
    assert any("cover the edge case" in q for q in queries["tool_0"][1:]), "the coder was driven again"
    _, answered = model.seen[4]          # the call after the rebuttal
    (held,) = [t for t in answered if "HELD its position" in t]
    assert held.startswith("[waited ") and "tool_0 turn 2 written" in held
    _assert_two_links_committed(outcome, built, queries, repo, store)


# ── C2b: a close that goes wrong is told, never lost ─────────────────────────


ONE_LINK = ("author_chain", {"first_link": _link(0), "additional_links": [], "rationale": "r"})


def _failing_git(monkeypatch, *failing):
    """Make git itself fail on the given leading arguments; the code around it stays real."""
    from auto_research.sandbox import merge_flow

    real_git = merge_flow._git_in

    def git(root, *args, **kwargs):
        if args[:len(failing)] == failing:
            raise RuntimeError(f"git {' '.join(failing)}: index.lock exists")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(merge_flow, "_git_in", git)


def test_a_failed_reset_names_the_patch_and_says_the_tree_may_hold_the_work(tmp_path, monkeypatch):
    """The reset after a spent link fails once its patch is saved: the lead is told where the work
    is and that the toolkit may still hold it, never that it was cleared."""
    _failing_git(monkeypatch, "reset", "--hard")
    one_link = ONE_LINK
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[one_link], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False], submit_cap=1,
    )
    session = built[0][1]
    assert session.reset_failed and session.reset_patch is not None
    _, answered = model.seen[3]          # the call after the spent submit
    (told,) = [t for t in answered if "CI BUDGET SPENT" in t]
    assert (
        "it did not commit. Clearing this session's uncommitted changes from the toolkit failed, "
        "so the toolkit tree may still hold them; they are saved as a patch at "
        f"{session.reset_patch}."
    ) in told
    assert "were cleared" not in told
    assert "def tool_0" in session.reset_patch.read_text()
    assert (repo / "demo_utils" / "lib" / "tool_0.py").exists(), "the reset really did fail"
    assert outcome is not None and outcome.report is not None


def test_a_session_that_will_not_close_after_its_commit_keeps_its_receipt(tmp_path, monkeypatch):
    """The coder's disconnect raises while the committed link closes: the tool call still
    returns, the commit keeps its receipt, the failure is recorded and told, and the chain goes
    on to its next link."""
    from auto_research.monitor import RunMonitor

    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[CHAIN], [SUBMIT], [SUBMIT], *FINISH], verdicts=[True, True],
        broken_disconnect=("tool_0",), monitor=monitor,
    )
    _, answered = model.seen[2]          # the call after link 1's commit
    assert any(
        "COMMITTED tool_0" in t
        and "Closing the coding session for tool_0 failed (RuntimeError: the author process is "
            "gone); the toolkit tree is clean." in t
        and "Coding session 2 of 2 is open on 'tool_1'" in t
        for t in answered
    )
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    assert [r["data"]["error"] for r in rows if r["event"].endswith("tool.close_failed")] == [
        "RuntimeError('the author process is gone')"
    ]
    _assert_two_links_committed(outcome, built, queries, repo, store)


def test_a_rebuttal_to_a_coder_with_no_blocking_wait_is_answered():
    """A session predating the background drive (a benchmark's, a double's) has no wait; a
    rebuttal while it works is answered, as a submit is, instead of raising."""
    import types

    from auto_research.agents.react import LeadCycle

    legacy = types.SimpleNamespace(drive_status="working", last_gate=object(),
                                   last_review=object(), debates_used=0, debate_cap=5)
    provider = ReadOnlyToolProvider(roots=[], cycle=LeadCycle(authoring_enabled=True),
                                    authoring_session=legacy)
    out = asyncio.run(provider.execute("respond_to_review", {"message": "it is handled"}))
    assert out.is_error and "still working" in out.text
    assert legacy.debates_used == 0, "answering is not a debate turn"


def test_a_patch_that_could_not_be_saved_is_no_failed_reset(tmp_path, monkeypatch):
    """Saving the patch fails, and the reset after it still clears the tree: the lead is not told
    the tree may still hold the work."""
    from auto_research.monitor import RunMonitor

    _failing_git(monkeypatch, "diff", "--cached")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[ONE_LINK], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False], submit_cap=1, monitor=monitor,
    )
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    assert not [r for r in rows if r["event"] == "coder.tree_reset.error"], "no failed reset"
    assert [r["data"]["save_error"] for r in rows if r["event"] == "coder.tree_reset"] == [
        "PatchSaveFailed('RuntimeError: git diff --cached: index.lock exists')"
    ]
    session = built[0][1]
    assert not session.reset_failed and session.reset_patch is None
    assert not (repo / "demo_utils" / "lib" / "tool_0.py").exists(), "the reset did run"
    (told,) = [t for t in model.seen[3][1] if "CI BUDGET SPENT" in t]
    assert "Clearing this session's uncommitted changes from the toolkit failed" not in told
    assert ("it did not commit. This session's uncommitted changes were cleared from the toolkit; "
            "saving them as a patch failed.") in told


def test_a_spent_session_whose_disconnect_raises_still_has_its_tree_reset(tmp_path, monkeypatch):
    """The disconnect raises while a spent link closes: the reset runs first, so the tree is
    clean, the lead is told both facts, and the next link opens on the clean tree."""
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[CHAIN], [SUBMIT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False, True], submit_cap=1, broken_disconnect=("tool_0",),
    )
    session = built[0][1]
    assert session.reset_patch is not None and not session.reset_failed
    assert not (repo / "demo_utils" / "lib" / "tool_0.py").exists(), "the reset ran"
    (told,) = [t for t in model.seen[3][1] if "CI BUDGET SPENT" in t]
    assert (
        "Closing the coding session for tool_0 failed (RuntimeError: the author process is gone); "
        "the toolkit tree is clean.\n\nCoding session 2 of 2 is open on 'tool_1'"
    ) in told
    assert f"saved as a patch at {session.reset_patch}." in told
    assert [r.status for r in outcome.authored] == ["failed", "committed"]


# ── C2c: what a close leaves behind is told and cleaned up ───────────────────


THREE_LINKS = ("author_chain", {"first_link": _link(0), "additional_links": [_link(1), _link(2)],
                               "rationale": "r"})


def test_a_dirty_tree_stops_the_chain_at_its_first_refusal(tmp_path, monkeypatch):
    """Link 1's reset fails, so the tree is dirty and link 2 cannot open: the result that closed
    link 1 says so in one sentence, and the chain stops there (link 3 is never tried, no further
    credit spent; C2c, C2d)."""
    _failing_git(monkeypatch, "reset", "--hard")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[THREE_LINKS], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[False], submit_cap=1,
    )
    assert len(built) == 1, "no later link got a session"
    (told,) = [t for t in model.seen[3][1] if "CI BUDGET SPENT" in t]
    assert ("Coding session 2 of 3 on 'tool_1' could not be opened: the toolkit tree still holds "
            "uncommitted changes. It is recorded as a failed link and used one author credit.\n\n"
            "The chain stops here") in told
    assert "git " not in told.split("CI BUDGET SPENT", 1)[1], "the refusal's human text stays out"
    assert [r.status for r in outcome.authored] == ["failed", "failed"]
    assert outcome.author_budget == 1, "the third link spent no credit"


def test_a_stopped_chain_opens_nothing_for_a_new_brief(tmp_path, monkeypatch):
    """After the dirty-tree refusal the lead briefs again: no link opens and no credit is spent,
    because every link would be refused the same way (C2d)."""
    _failing_git(monkeypatch, "reset", "--hard")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[THREE_LINKS], [SUBMIT], [SUBMIT], [THREE_LINKS], *FINISH],
        verdicts=[False], submit_cap=1,
    )
    assert len(built) == 1 and outcome.author_budget == 1
    (rebrief,) = model.seen[4][1]
    assert rebrief.endswith("The chain stops here: the toolkit tree still holds uncommitted "
                            "changes, so no further link can be opened. Write your report with "
                            "edit_report — say which capabilities are still missing — and call "
                            "turn_end.")



def test_a_short_new_brief_after_the_stop_is_told_the_chain_stopped(tmp_path, monkeypatch):
    """A new brief shorter than the links already opened still gets the stop sentence, not
    "the chain is finished" (C2d review)."""
    _failing_git(monkeypatch, "reset", "--hard")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[THREE_LINKS], [SUBMIT], [SUBMIT], [ONE_LINK], *FINISH],
        verdicts=[False], submit_cap=1,
    )
    (rebrief,) = model.seen[4][1]
    assert "The chain stops here" in rebrief and "The chain is finished" not in rebrief
    assert outcome.author_budget == 1


def test_an_unconnected_session_that_will_not_close_is_handed_to_the_teardown(tmp_path, monkeypatch):
    """Link 2's coder fails to connect and its close fails too (the disconnect raises): the
    session goes to the run's unclosed sessions, so the run's teardown retries it (C2d)."""
    from auto_research.run_control import RunControl

    real = _Coder.connect

    async def connect(self):
        if self.tool == "tool_1":
            raise RuntimeError("the author process did not start")
        return await real(self)

    monkeypatch.setattr(_Coder, "connect", connect)
    control = RunControl()
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[THREE_LINKS], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, True], broken_disconnect=("tool_1",), run_control=control,
    )
    assert control.unclosed_sessions == [built[1][1]]
    assert [r.status for r in outcome.authored] == ["committed", "failed", "committed"]
    assert model.workspaces[0].briefs["spec2"].bound is False, "the unopened link's brief is free"
    spec2 = model.workspaces[0].briefs["spec2"]
    assert spec2.locked_by == "link_end" and store.text(spec2.receipt) == spec2.document(), (
        "an attempt that never opened locks its brief as given (C3 ruling)")


def test_a_session_whose_connect_raises_is_closed_and_named(tmp_path, monkeypatch):
    """Link 2's coder fails to connect: that session is closed (its coder disconnected), and the
    result that closed link 1 names the link and the error; the chain goes on (C2c, C2d)."""
    real = _Coder.connect

    async def connect(self):
        if self.tool == "tool_1":
            raise RuntimeError("the author process did not start")
        return await real(self)

    monkeypatch.setattr(_Coder, "connect", connect)
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[THREE_LINKS], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[True, True],
    )
    assert [spec.proposed_tool_name for spec, _ in built] == ["tool_0", "tool_1", "tool_2"]
    assert "tool_1" in model.disconnects, "the unconnected session was closed"
    _, answered = model.seen[2]          # the call after link 1's commit
    assert any(
        "Coding session 2 of 3 on 'tool_1' could not be opened (RuntimeError: the author process "
        "did not start); it is recorded as a failed link and used one author credit.\n\n"
        "Coding session 3 of 3 is open on 'tool_2'" in t for t in answered
    )
    assert [r.status for r in outcome.authored] == ["committed", "failed", "committed"]


def test_a_session_whose_close_raised_waits_for_the_runs_teardown(tmp_path, monkeypatch):
    """The committed link's disconnect raises: its session moves to the run's unclosed sessions,
    out of the slot the next link takes, and closing it again retries the disconnect."""
    from auto_research.run_control import RunControl

    control = RunControl()
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[CHAIN], [SUBMIT], [SUBMIT], *FINISH], verdicts=[True, True],
        broken_disconnect=("tool_0",), run_control=control,
    )
    first = built[0][1]
    assert control.unclosed_sessions == [first]
    assert control.authoring_session is None, "link 2 closed normally and released its slot"
    asyncio.run(first.aclose())          # what the run's teardown does
    assert model.disconnects == ["tool_0", "tool_1", "tool_0"], "the disconnect was retried"
    # The retry disconnects only: link 2's commit, landed after link 1's base, is still the head.
    assert _git_out(repo, "log", "--format=%s", "-2").split("\n")[:2] == ["Add tool_1", "Add tool_0"]
    assert (repo / "demo_utils" / "lib" / "tool_1.py").exists()
