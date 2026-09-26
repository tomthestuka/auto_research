"""Deterministic tests for the Phase-2 tool-authoring harness (merge_flow.propose_and_pr).

The bounded rework loop is exercised with injected fakes — a fake SDK client, a fake
analyst lead, and stub gate/review hooks — so no live LLM or git is involved.
"""

import asyncio

from auto_research.run_control import RunControl
import pathlib
import types

import pytest

from auto_research.sandbox.merge_flow import (
    HumanInputRequired,
    _build_structure_context,
    _make_ask_human_handler,
    _make_ask_lead_handler,
    build_authoring_session,
    propose_and_pr,
)
from auto_research.schemas import GateReport, ToolReviewVerdict, ToolSpec


def _coder_member(model: str):
    """A stand-in author member, so the flow needs no configured ``coder`` role to be exercised."""
    from auto_research.models.types import RoleModelConfig

    return RoleModelConfig(
        name="test_coder", provider="anthropic", harness="claude_sdk", model=model
    )


def _spec():
    return ToolSpec(
        science_context="need retBB",
        expected_behavior="add_retbb(indices)",
        proposed_tool_name="fit_retbb_hss",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
    )


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient — records the turns it is driven with."""

    def __init__(self):
        self.queries: list[str] = []
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, message, session_id="default"):
        self.queries.append(message)

    async def receive_response(self):
        for _ in ():  # empty async generator — the author "does its work" silently
            yield _


class _FakeLead:
    def __init__(self):
        self.messages: list[str] = []

    async def clarify_for_author(self, message, *, kind="clarify_code"):
        self.messages.append(message)
        return f"lead-answer:{message}"


async def _pass_gate(worktree, spec):
    # Default gate for the loop-orchestration tests: the real gate (gate.run_gate) needs
    # a git worktree + science stack — out of scope here, covered by tests/test_gate.py.
    # Tests exercising gate behaviour inject their own run_gate, overriding this default.
    return GateReport(passed=True)


async def _pass_review(worktree, spec, gate):
    # Default review for the loop-orchestration tests: the real co-lead review
    # (review.make_review_hook) needs a worktree + a live GPT call — out of scope here,
    # covered by tests/test_review.py. Tests exercising review behaviour inject their own.
    return ToolReviewVerdict(approved=True, rationale="ok")


class _RecordingApproveEdit:
    """Fake ``approve_edit`` seam — records the commit subject, returns a fixed sha. The
    real git commit is covered by ``test_approve_edit_*`` below (throwaway repo)."""

    def __init__(self, sha="fakesha123"):
        self.sha = sha
        self.messages: list[str] = []

    def __call__(self, worktree, *, message):
        self.messages.append(message)
        return self.sha


def _run(spec, **kwargs):
    client = _FakeClient()
    kwargs.setdefault("lead", _FakeLead())
    kwargs.setdefault("coder", _coder_member("fake-model"))
    kwargs.setdefault("run_gate", _pass_gate)
    kwargs.setdefault("run_review", _pass_review)
    kwargs.setdefault("worktree_factory", lambda root, base, branch: base / branch.replace("/", "_"))
    kwargs.setdefault("sdk_client_factory", lambda options: client)
    kwargs.setdefault("approve_edit", _RecordingApproveEdit())
    result = asyncio.run(propose_and_pr(spec, **kwargs))
    return result, client


class _RecordingMonitor:
    """Captures monitor.record() calls for assertion (async-compatible)."""

    def __init__(self):
        self.events: list[dict] = []

    async def record(self, team, event, *, level, summary, actor=None, data=None):
        self.events.append({"team": team, "event": event, "level": level, "data": data or {}})
        return None

    def of(self, name):
        return [e for e in self.events if e["event"] == name]


def test_structure_context_states_absolute_cwd_and_paths(tmp_path):
    """The author's structure context must name its cwd and print template paths absolute
    (todo #2) so it never guesses the toolkit root. ``workdir`` is the actual checkout it
    edits (toolkit_root in direct mode, the worktree otherwise)."""
    pkg = tmp_path / "xspec_utils"
    (pkg / "skills").mkdir(parents=True)
    (pkg / "lib").mkdir()
    (pkg / "__init__.py").write_text("# init\n")
    (pkg / "skills" / "__init__.py").write_text("TOOL_MANIFEST = {}\n")
    lib_file = pkg / "lib" / "xspec_help.py"
    lib_file.write_text("def load_data(): ...\n")

    ctx = _build_structure_context(tmp_path)

    # The cwd is stated up front, as an absolute path the author can use verbatim.
    assert f"Working directory (your cwd): {tmp_path}" in ctx
    # The template layout lists ABSOLUTE paths (not bare relative ones).
    assert str(lib_file) in ctx
    # Every listed template line sits under the absolute workdir.
    for line in ctx.splitlines():
        if line.endswith(".py") and "/" in line and not line.startswith("#"):
            assert line.startswith(str(tmp_path)), line


def test_drive_author_streams_sdk_blocks_to_monitor():
    """Fix #2 (a): the coder SDK stream is mirrored to the monitor — tool calls, results,
    narration, turn end — so the otherwise-opaque author session is legible in the JSONL."""
    from claude_agent_sdk import (
        AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
    )

    from auto_research.sandbox.merge_flow import _drive_author

    class _StreamClient(_FakeClient):
        async def receive_response(self):
            yield AssistantMessage(
                content=[TextBlock(text="editing red_ixpe.py"),
                         ToolUseBlock(id="1", name="Bash", input={"command": "pytest -q"})],
                model="m",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="1", content="2 passed", is_error=False)])
            yield ResultMessage(subtype="success", duration_ms=9, duration_api_ms=8,
                                is_error=False, num_turns=2, session_id="s")

    mon = _RecordingMonitor()
    asyncio.run(_drive_author(_StreamClient(), "go", monitor=mon))

    use = mon.of("coder.tool_use")
    assert use and use[0]["data"]["tool"] == "Bash" and "pytest" in use[0]["data"]["input"]
    res = mon.of("coder.tool_result")
    assert res and "2 passed" in res[0]["data"]["text"] and res[0]["data"]["is_error"] is False
    end = mon.of("coder.turn_end")
    assert end and end[0]["data"]["num_turns"] == 2
    assert mon.of("coder.text")
    assert all(e["team"] == "tooling" for e in mon.events)


def test_ask_lead_handler_records_the_question_to_monitor():
    """Escalations are logged when asked. The ANSWER is no longer part of this event.

    It cannot be: the handler returns before the lead has seen the question. The reply is a
    `drive_coder` steer later in the lead's own turn, and the ledger records it there.
    """
    from types import SimpleNamespace

    mon = _RecordingMonitor()
    control = RunControl()
    lead = SimpleNamespace(agent_key="analyst_lead", ensure_run_control=lambda: control)
    session = SimpleNamespace(_steer_event=object(), mark_question_open=lambda: None)
    handler = _make_ask_lead_handler(lambda: session, lead=lead, monitor=mon)
    asyncio.run(handler("which lib fn?"))
    ev = mon.of("coder.ask_lead")
    assert ev and ev[0]["data"]["message"] == "which lib fn?"
    assert "answer" not in ev[0]["data"], (
        "the event claims an answer the lead has not given yet"
    )


def test_propose_and_pr_emits_authoring_milestones(tmp_path):
    """Fix #2 (c): session start, gate result, review result, and the commit are recorded."""
    mon = _RecordingMonitor()
    result, _ = _run(_spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", monitor=mon)
    assert result.status == "committed"
    names = {e["event"] for e in mon.events}
    assert {"coder.session_start", "gate.result", "review.result", "commit"} <= names
    assert mon.of("gate.result")[0]["data"]["passed"] is True
    assert mon.of("commit")[0]["data"]["commit_sha"] == "fakesha123"
    assert all(e["team"] == "tooling" for e in mon.events)


def test_happy_path_returns_committed(tmp_path):
    result, client = _run(_spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt")
    assert result.status == "committed"
    assert result.commit_sha == "fakesha123"  # the seam's sha, via approve_edit
    assert result.branch and result.branch.startswith("tool/fit_retbb_hss-")
    assert result.gate_report.passed and result.review.approved
    assert client.connected is False  # disconnected in finally
    assert len(client.queries) == 1  # one initial author turn, no rework


def test_commit_subject_uses_colead_summary(tmp_path):
    approve = _RecordingApproveEdit()

    async def review_with_summary(worktree, spec, gate):
        return ToolReviewVerdict(approved=True, rationale="ok", commit_summary="Add retBB HSS fit")

    _run(_spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt",
         run_review=review_with_summary, approve_edit=approve)
    assert approve.messages == ["Add retBB HSS fit"]


def test_commit_subject_falls_back_to_tool_name(tmp_path):
    approve = _RecordingApproveEdit()
    # default _pass_review leaves commit_summary="" → fall back to proposed_tool_name.
    _run(_spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", approve_edit=approve)
    assert approve.messages == ["fit_retbb_hss"]


def test_gate_failures_drive_exactly_n_reworks_then_succeed(tmp_path):
    calls = {"n": 0}

    async def flaky_gate(worktree, spec):
        calls["n"] += 1
        if calls["n"] <= 2:  # fail twice, pass on the third evaluation
            return GateReport(
                passed=False,
                reason="bad",
                layer_b=[{"name": "inj_rec", "layer": "B", "passed": False, "detail": "off"}],
            )
        return GateReport(passed=True)

    result, client = _run(
        _spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", run_gate=flaky_gate
    )
    assert result.status == "committed"
    assert calls["n"] == 3  # initial gate + 2 re-evaluations
    assert len(client.queries) == 3  # initial author turn + 2 gate-feedback re-drives


def test_gate_cap_exhaustion_returns_failed_with_reason(tmp_path):
    async def always_fail(worktree, spec):
        return GateReport(passed=False, reason="never converges")

    result, _ = _run(
        _spec(),
        toolkit_root=tmp_path,
        worktree_base=tmp_path / "wt",
        run_gate=always_fail,
        gate_rework_cap=2,
    )
    assert result.status == "failed"
    assert "gate rework cap exhausted" in result.reason
    assert result.commit_sha is None
    assert result.branch is not None  # carried for the lead's human summary


def test_catch_gate_test_err(tmp_path):
    """An unexpected error while gating (a git hiccup, a decode error, a library crash, a
    pytest timeout surfaced as an exception) must degrade to an honest failed result, never
    propagate and abort the whole run."""
    async def boom_gate(worktree, spec):
        raise RuntimeError("subprocess timed out / git hiccup")

    result, client = _run(
        _spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", run_gate=boom_gate
    )
    assert result.status == "failed"
    assert "unexpected error" in result.reason and "RuntimeError" in result.reason
    assert client.connected is False  # finally still disconnected the SDK client


def test_human_input_required_propagates_through_backstop(tmp_path):
    """The §9.8 fail-safe must reach the caller — the backstop never swallows it into failed."""
    from auto_research.hitl import HumanInputRequired

    async def needs_human_gate(worktree, spec):
        raise HumanInputRequired("no handler in headless mode")

    with pytest.raises(HumanInputRequired):
        _run(_spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", run_gate=needs_human_gate)


def test_review_rejection_reworks_then_passes(tmp_path):
    review_calls = {"n": 0}

    async def flaky_review(worktree, spec, gate):
        review_calls["n"] += 1
        if review_calls["n"] == 1:
            return ToolReviewVerdict(approved=False, rationale="checks too loose", issues=["tighten tol"])
        return ToolReviewVerdict(approved=True, rationale="ok")

    result, client = _run(
        _spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", run_review=flaky_review
    )
    assert result.status == "committed"
    assert review_calls["n"] == 2
    # initial author turn + one review-feedback re-drive
    assert len(client.queries) == 2


def test_review_cap_exhaustion_returns_failed(tmp_path):
    async def reject(worktree, spec, gate):
        return ToolReviewVerdict(approved=False, rationale="subtle bug remains")

    result, _ = _run(
        _spec(),
        toolkit_root=tmp_path,
        worktree_base=tmp_path / "wt",
        run_review=reject,
        review_rework_cap=1,
    )
    assert result.status == "failed"
    assert "review rework cap exhausted" in result.reason
    assert result.review is not None and result.review.approved is False


def test_ask_lead_posts_to_the_leads_inbox_and_returns_at_once():
    """The coder's ``message(to="lead")`` is NON-BLOCKING: it posts and returns, it does not answer.

    It used to call `lead.clarify_for_author`, opening a second ReAct conversation on the lead
    concurrently with the lead's own turn. Measured live 2026-09-12: that loop held the
    coder-control tools and spent 85 calls polling the very coder blocked waiting for it, and its
    answer landed 326 s later, after the coder had timed out. Nobody waits on anybody now — the
    question lands in the lead's inbox (E12) and its own loop delivers it at a safe boundary.
    """
    from types import SimpleNamespace

    from auto_research.run_control import RunControl

    control = RunControl()
    lead = SimpleNamespace(agent_key="analyst_lead", ensure_run_control=lambda: control)
    session = SimpleNamespace(_steer_event=object(), opened=False)
    session.mark_question_open = lambda: setattr(session, "opened", True)
    handler = _make_ask_lead_handler(lambda: session, lead=lead)
    answer = asyncio.run(handler("which lib fn?"))
    assert answer.startswith("delivered.")
    assert session.opened
    assert control.drain_steer("analyst_lead") == ["which lib fn?"]


def test_ask_lead_without_a_session_says_so_rather_than_dropping_the_question():
    handler = _make_ask_lead_handler(lambda: None)
    answer = asyncio.run(handler("which lib fn?"))
    assert "not reachable" in answer


def test_ask_human_handler_returns_at_once_and_delivers_the_reply_into_the_coders_turn():
    """E12: the coder's ``message(to="human")`` never blocks — the human's answer is steered
    into the coder's turn later, through ``session.send``."""
    from types import SimpleNamespace

    seen = {}

    async def human(query):  # query is a HumanQuery (typed §9.8 contract)
        seen["kind"] = query.kind
        seen["message"] = query.message
        seen["asked_by"] = query.asked_by
        return "human-says-go"

    sent: list[tuple[str, bool]] = []

    async def send(text, *, mid_turn=False):
        sent.append((text, mid_turn))
        return "steered"

    session = SimpleNamespace(send=send)

    async def scenario():
        handler = _make_ask_human_handler(human, session_ref=lambda: session)
        answer = await handler("response convention?")
        assert answer.startswith("Sent to the human")
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert seen["kind"] == "blocked" and seen["message"] == "response convention?"
    assert seen["asked_by"] == "coder"
    assert sent == [("[Reply from the human] human-says-go", True)]


def test_ask_human_handler_without_handler_raises_fail_safe():
    handler = _make_ask_human_handler(None)
    with pytest.raises(HumanInputRequired):
        asyncio.run(handler("need an expert"))


# ── approve_edit — the real atomic commit (throwaway git repo, no LLM) ───────────


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _git_out(repo, *args):
    import subprocess
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


def _toolkit_repo(tmp_path):
    repo = tmp_path / "research_toolkit"
    (repo / "demo_utils" / "lib").mkdir(parents=True)
    (repo / "demo_utils" / "skills").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_approve_edit_stages_exactly_gated_files_and_returns_sha(tmp_path):
    from auto_research.sandbox.gate import collect_targets
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _toolkit_repo(tmp_path)
    # Tool files under a *_utils package — these are what the gate verified.
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text("def my_tool(o):\n    return o\n")
    (repo / "demo_utils" / "lib" / "reduce.sh").write_text("#!/bin/bash\nnicerl2 indir=$1\n")
    # Scratch OUTSIDE any *_utils package must never be staged (§9.9 explicit-path).
    (repo / "scratch.txt").write_text("worker scratch — must not leak\n")

    gated = {str(p.relative_to(repo)) for p in collect_targets(repo)}
    assert gated == {"demo_utils/lib/demo_help.py", "demo_utils/lib/reduce.sh"}

    sha = approve_edit(repo, message="Add my_tool to demo_utils")
    assert sha and sha == _git_out(repo, "rev-parse", "HEAD").strip()

    # Exactly the gated files landed in the commit; scratch.txt did not.
    committed = set(_git_out(repo, "show", "--name-only", "--pretty=format:", "HEAD").split())
    assert committed == gated
    assert _git_out(repo, "log", "-1", "--pretty=%s").strip() == "Add my_tool to demo_utils"
    # scratch.txt is still untracked — it was neither staged nor committed.
    assert "scratch.txt" in _git_out(repo, "status", "--porcelain")


# ── direct-branch authoring mode + the branch/clean preflight (§9.9, step 7) ──────


def test_check_toolkit_branch_clean_passes_on_correct_clean_branch(tmp_path):
    from auto_research.sandbox.merge_flow import check_toolkit_branch_clean

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    check_toolkit_branch_clean(repo, "ai")  # no raise


def test_check_toolkit_branch_clean_rejects_wrong_branch(tmp_path):
    from auto_research.sandbox.merge_flow import check_toolkit_branch_clean

    repo = _toolkit_repo(tmp_path)  # on the default branch, not "ai"
    with pytest.raises(RuntimeError, match="not the authoring branch"):
        check_toolkit_branch_clean(repo, "ai")


def test_check_toolkit_branch_clean_rejects_dirty_tree(tmp_path):
    from auto_research.sandbox.merge_flow import check_toolkit_branch_clean

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    (repo / "demo_utils" / "lib" / "leftover.py").write_text("x = 1\n")  # terminal-failure leftover
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        check_toolkit_branch_clean(repo, "ai")


def test_check_toolkit_branch_clean_tolerant_when_not_a_repo(tmp_path):
    from auto_research.sandbox.merge_flow import check_toolkit_branch_clean

    check_toolkit_branch_clean(tmp_path / "nope", "ai")  # not a git checkout → no raise


def test_direct_branch_mode_uses_toolkit_root_as_worktree(tmp_path):
    """In direct mode the author works in toolkit_root on `ai`; approve_edit sees toolkit_root."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    seen = {}

    def approve(worktree, *, message):
        seen["worktree"] = worktree
        seen["message"] = message
        return "directsha"

    result, _ = _run(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "unused",
        direct_branch="ai", approve_edit=approve,
    )

    assert result.status == "committed"
    assert result.branch == "ai"
    assert result.commit_sha == "directsha"
    assert seen["worktree"] == repo  # not an isolated worktree


def test_direct_branch_mode_rejects_wrong_branch(tmp_path):
    repo = _toolkit_repo(tmp_path)  # still on the default branch, not "ai"
    with pytest.raises(RuntimeError, match="not the authoring branch"):
        _run(_spec(), toolkit_root=repo, worktree_base=tmp_path / "unused", direct_branch="ai")


# ── auto-reset the toolkit tree after a non-committed authoring attempt (§9.9) ────


async def _dirtying_gate(worktree, spec):
    """A gate that fails after the 'coder' left edits behind: a tracked-file modification, a
    new untracked file under a *_utils package, and scratch outside any package."""
    (worktree / "README.md").write_text("MODIFIED by the coder\n")  # tracked modification
    (worktree / "demo_utils" / "lib" / "pcube_ts.py").write_text("x = 1  # phantom tool\n")
    (worktree / "scratch_out_of_pkg.txt").write_text("worker scratch\n")  # outside any *_utils
    return GateReport(passed=False, reason="non-deterministic test")


def test_direct_branch_failed_resets_dirty_tree(tmp_path):
    """A non-committed direct-branch attempt resets the live checkout to HEAD — tracked edits
    reverted AND new untracked files (incl. scratch outside *_utils) removed."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    result, _ = _run(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt",
        direct_branch="ai", run_gate=_dirtying_gate, gate_rework_cap=0,
    )
    assert result.status == "failed"
    assert _git_out(repo, "status", "--porcelain") == ""  # tree clean == HEAD
    assert _git_out(repo, "show", "HEAD:README.md") == "base\n"  # tracked edit reverted
    assert not (repo / "demo_utils" / "lib" / "pcube_ts.py").exists()
    assert not (repo / "scratch_out_of_pkg.txt").exists()


async def _committing_gate(worktree, spec):
    """A gate that fails AFTER the 'coder' committed its own work — the §9.9 breach we now
    contain (deny-rule + base-ref reset). A real commit lands on the branch, then the gate
    rejects, so the failure path must undo the commit, not keep it."""
    import subprocess
    (worktree / "demo_utils" / "lib" / "rogue.py").write_text("x = 1  # coder-committed\n")
    subprocess.run(["git", "add", "-A"], cwd=str(worktree), check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "coder self-commit"], cwd=str(worktree),
                   check=True, capture_output=True, text=True)
    return GateReport(passed=False, reason="rejected after a coder commit")


def test_direct_branch_failed_drops_coder_commit(tmp_path):
    """A failed attempt in which the coder committed its own work resets to the per-attempt base
    ref — dropping the illegitimate commit. A plain `reset --hard HEAD` would have KEPT it (the
    observed phantom-committed-tool hole); the patch artifact still captures the discarded commit."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    base = _git_out(repo, "rev-parse", "HEAD").strip()
    artifacts = tmp_path / "authoring"
    result, _ = _run(
        _spec(), toolkit_root=repo, worktree_base=artifacts,
        direct_branch="ai", run_gate=_committing_gate, gate_rework_cap=0,
    )
    assert result.status == "failed"
    assert _git_out(repo, "rev-parse", "HEAD").strip() == base  # coder commit dropped
    assert not (repo / "demo_utils" / "lib" / "rogue.py").exists()
    assert _git_out(repo, "status", "--porcelain") == ""  # clean tree at base
    patches = list(artifacts.glob("tool_author_failed_*.patch"))
    assert len(patches) == 1 and "coder-committed" in patches[0].read_text()


def test_direct_branch_coder_commit_auto_reverted_before_gate(tmp_path):
    """A coder that commits during its turn (slipping the deny-rule) has the commit auto-reverted
    by `_gated` (`reset --mixed base_ref`) BEFORE the scan, so the gate sees the work uncommitted and
    the only real commit is `approve_edit`'s. HEAD advances by exactly one from base (not two)."""
    from auto_research.sandbox.merge_flow import approve_edit as real_approve

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    base = _git_out(repo, "rev-parse", "HEAD").strip()

    class _CommittingClient(_FakeClient):
        async def query(self, message, session_id="default"):
            await super().query(message, session_id)
            if len(self.queries) == 1:  # first drive: the coder commits its own work
                (repo / "demo_utils" / "lib" / "tool.py").write_text("def t():\n    return 1\n")
                _git(repo, "add", "-A")
                _git(repo, "commit", "-qm", "coder self-commit")

    async def asserting_gate(worktree, spec):
        # By the time the gate runs, the auto-revert has undone the coder's commit …
        assert _git_out(worktree, "rev-parse", "HEAD").strip() == base
        # … and left the work in the working tree (visible to the scan).
        assert _git_out(worktree, "status", "--porcelain").strip()
        return GateReport(passed=True)

    mon = _RecordingMonitor()
    result, _ = _run(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
        sdk_client_factory=lambda options: _CommittingClient(),
        run_gate=asserting_gate, approve_edit=real_approve, monitor=mon,
    )
    assert result.status == "committed"
    assert mon.of("coder.committed"), "the auto-revert should be surfaced on the monitor"
    # Exactly ONE commit beyond base — approve_edit's, not the coder's + approve_edit's.
    assert _git_out(repo, "rev-list", "--count", f"{base}..HEAD").strip() == "1"
    assert (repo / "demo_utils" / "lib" / "tool.py").exists()


def test_direct_branch_committed_does_not_reset(tmp_path):
    """The success path must NOT reset — the committed tool survives (cleanup skipped)."""
    from auto_research.sandbox.merge_flow import approve_edit as real_approve

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")

    async def writing_gate(worktree, spec):
        (worktree / "demo_utils" / "lib" / "tool.py").write_text("def t():\n    return 1\n")
        return GateReport(passed=True)

    head_before = _git_out(repo, "rev-parse", "HEAD").strip()
    result, _ = _run(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt",
        direct_branch="ai", run_gate=writing_gate, approve_edit=real_approve,
    )
    assert result.status == "committed"
    assert _git_out(repo, "rev-parse", "HEAD").strip() != head_before  # HEAD advanced
    assert (repo / "demo_utils" / "lib" / "tool.py").exists()  # committed file survives
    assert _git_out(repo, "status", "--porcelain") == ""


def test_direct_branch_second_attempt_after_failure_does_not_crash(tmp_path):
    """Reproduces the observed crash: a 2nd authoring attempt after a failed one must NOT trip
    the clean-tree precondition — auto-clean restored the tree between attempts."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    r1, _ = _run(_spec(), toolkit_root=repo, worktree_base=tmp_path / "wt",
                 direct_branch="ai", run_gate=_dirtying_gate, gate_rework_cap=0)
    assert r1.status == "failed"
    # Before the fix this 2nd call raised RuntimeError("uncommitted changes") and crashed the run.
    r2, _ = _run(_spec(), toolkit_root=repo, worktree_base=tmp_path / "wt",
                 direct_branch="ai", run_gate=_dirtying_gate, gate_rework_cap=0)
    assert r2.status == "failed"


def test_direct_branch_failure_writes_patch_artifact(tmp_path):
    """The rejected attempt's diff is preserved as a .patch under the artifact dir before reset,
    so the failed code stays inspectable without a dirty live tree."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    artifacts = tmp_path / "authoring"
    result, _ = _run(_spec(), toolkit_root=repo, worktree_base=artifacts,
                     direct_branch="ai", run_gate=_dirtying_gate, gate_rework_cap=0)
    assert result.status == "failed"
    patches = list(artifacts.glob("tool_author_failed_*.patch"))
    assert len(patches) == 1
    body = patches[0].read_text()
    assert "phantom tool" in body and "MODIFIED by the coder" in body
    assert _git_out(repo, "status", "--porcelain") == ""  # tree clean after


def test_reset_direct_tree_swallows_errors(tmp_path):
    """Cleanup is best-effort — a git failure is logged, never raised (must not mask the result)."""
    from auto_research.sandbox.merge_flow import _reset_direct_tree

    bad = tmp_path / "not_a_repo"
    bad.mkdir()
    mon = _RecordingMonitor()
    asyncio.run(_reset_direct_tree(bad, artifact_dir=None, monitor=mon))  # no raise
    assert mon.of("coder.tree_reset.error")


# ── coder filesystem jail — the PreToolUse hook (todo #3 Stage B) ─────────────────


@pytest.mark.parametrize("jailed", [False, True])
def test_fs_guard_hook_allows_inside_denies_outside_and_governance(tmp_path, monkeypatch, jailed):
    """The hook denies when it is the only layer, and stands down when the kernel jail is on.

    Swept over both settings because the jail flag is gitignored machine-local config, and because
    the standing-down half is what stopped the hook from vetoing an author's own files.
    """
    from auto_research.sandbox import jail
    from auto_research.sandbox.fs_guard import PathGuard
    from auto_research.sandbox.merge_flow import _make_fs_guard_hook

    monkeypatch.setattr(jail, "jail_enabled", lambda: jailed, raising=True)

    tk = tmp_path / "research_toolkit"
    tk.mkdir()
    hook = _make_fs_guard_hook(PathGuard([tk]))

    def call(tool, ti):
        return asyncio.run(hook(
            {"tool_name": tool, "tool_input": ti, "hook_event_name": "PreToolUse"}, None, None,
        ))

    def denied(out):
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    assert call("Read", {"file_path": str(tk / "xspec_utils" / "lib" / "x.py")}) == {}  # inside → allow
    # Under the jail these paths are simply absent from the author's mount namespace, so the hook
    # allows and the kernel refuses; unjailed, the hook is the only layer and must deny.
    expect = (lambda out: out == {}) if jailed else denied
    assert expect(call("Read", {"file_path": str(tmp_path / "auto_research" / "gate.py")}))
    assert expect(call("Read", {"file_path": str(tk / "CLAUDE.md")}))  # governance, even inside
    assert call("Glob", {"pattern": "**/*.py"}) == {}  # no path arg → defaults to cwd → allow
    # Bash: named-path enforcement (best-effort)
    assert call("Bash", {"command": 'python -c "import xspec_utils"'}) == {}  # no abs path named
    assert expect(call("Bash", {"command": f"cat {tmp_path}/auto_research/gate.py"}))


def test_fs_guard_hook_inactive_when_allowlist_empty():
    from auto_research.sandbox.fs_guard import PathGuard
    from auto_research.sandbox.merge_flow import _make_fs_guard_hook

    hook = _make_fs_guard_hook(PathGuard([]))  # unconfigured → allow-all
    out = asyncio.run(hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"},
         "hook_event_name": "PreToolUse"}, None, None,
    ))
    assert out == {}


# ── gate feedback message (handoff #3): fix ALL; warn tests not run on structural fail ──


def test_gate_feedback_demands_all_and_warns_tests_not_run():
    from auto_research.sandbox.merge_flow import _gate_feedback_message
    from auto_research.schemas import CheckResult

    # Structural failure → no Layer-B results → the "tests have not run yet" note appears.
    report = GateReport(
        passed=False,
        layer_a=[
            CheckResult(name="structural.test_present", layer="A", passed=False,
                        detail="no test_*.py added"),
            CheckResult(name="no_fabrication.scan", layer="A", passed=True, detail="clean"),
        ],
        layer_b=[],
        reason="structural.test_present failed",
    )
    msg = _gate_feedback_message(report)
    assert "EVERY" in msg                                   # demand all checks fixed
    assert "structural.test_present: no test_*.py added" in msg
    assert "have not run yet" in msg                        # tests-not-run warning
    assert "Layer" not in msg and "determinism check" not in msg  # no internal jargon


def test_gate_feedback_no_tests_note_when_tests_ran():
    from auto_research.sandbox.merge_flow import _gate_feedback_message
    from auto_research.schemas import CheckResult

    # A pure test failure (Layer B ran) → no "tests have not run yet" note.
    report = GateReport(
        passed=False,
        layer_a=[CheckResult(name="determinism", layer="A", passed=True, detail="")],
        layer_b=[CheckResult(name="layer_b.pytest", layer="B", passed=False, detail="1 failed")],
        reason="layer_b.pytest failed",
    )
    msg = _gate_feedback_message(report)
    assert "layer_b.pytest: 1 failed" in msg
    assert "have not run yet" not in msg


def test_build_authoring_session_forwards_monitor_to_the_review_hook(monkeypatch, tmp_path):
    """The co-lead's actions must reach the run ledger, not just its final verdict.

    `build_authoring_session` used to call `make_review_hook(...)` without `monitor=`, so a
    co-lead built from config got `monitor=None` and the only provider-decorrelated check in
    `commit ⇔ gate ∧ review` was the one actor whose work was invisible in the record.
    """
    seen = {}

    def _fake_make_review_hook(*, colead=None, author_provider=None, monitor=None, **_kw):
        seen["monitor"] = monitor
        seen["author_provider"] = author_provider

        async def _hook(*_a, **_k):
            raise NotImplementedError

        return _hook

    monkeypatch.setattr(
        "auto_research.sandbox.review.make_review_hook", _fake_make_review_hook
    )
    mon = _RecordingMonitor()
    toolkit = tmp_path / "tk"
    (toolkit / ".git").mkdir(parents=True)

    def _fake_worktree(_root, base, slug):
        path = pathlib.Path(base) / slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    build_authoring_session(
        _spec(),
        lead=object(),
        toolkit_root=toolkit,
        worktree_base=tmp_path / "wt",
        coder=_coder_member("test-model"),
        sdk_client_factory=lambda _o: object(),
        worktree_factory=_fake_worktree,
        run_gate=lambda *_a, **_k: None,
        monitor=mon,
    )

    assert seen["monitor"] is mon, "the review hook was built without the run monitor"


def test_one_coder_member_carries_model_provider_and_harness_together(monkeypatch, tmp_path):
    """The three author facts come from one object, so they cannot disagree.

    They used to be resolved by three independent reads of the ``coder`` role: the model id for
    the record and the cost meter, the provider the review is routed away from, and the harness
    that spawns the author process. Passing one member is what lets a sweep author with a vendor
    the config does not name, and it makes a mismatch between the three unrepresentable.
    """
    from auto_research.sandbox.coder.codex_sdk import CodexBackend
    from auto_research.models.types import RoleModelConfig

    seen = {}

    def _fake_make_review_hook(*, colead=None, author_provider=None, monitor=None, **_kw):
        seen["author_provider"] = author_provider

        async def _hook(*_a, **_k):  # pragma: no cover — never invoked here
            raise AssertionError

        return _hook

    monkeypatch.setattr(
        "auto_research.sandbox.review.make_review_hook", _fake_make_review_hook, raising=True
    )

    toolkit = tmp_path / "research_toolkit"
    (toolkit / ".git").mkdir(parents=True)

    member = RoleModelConfig(
        name="sweep_leg", provider="openai", harness="codex", model="gpt-5.6-terra"
    )
    session = build_authoring_session(
        _spec(),
        lead=object(),
        toolkit_root=toolkit,
        worktree_base=tmp_path / "wt",
        coder=member,
        sdk_client_factory=lambda _o: object(),
        worktree_factory=lambda _r, base, slug: _mkdir(pathlib.Path(base) / slug.replace("/", "_")),
        run_gate=lambda *_a, **_k: None,
    )

    assert seen["author_provider"] == "openai", "review routing must read the passed member"
    assert session._coder_model == "gpt-5.6-terra", "the record must name the passed member"
    assert isinstance(session._backend, CodexBackend), "the harness must be the member's"


def _mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── submission snapshots: the checkpoint just before the gate scans (§9.9) ────


def _direct_session(tmp_path, repo, **kwargs):
    kwargs.setdefault("lead", _FakeLead())
    kwargs.setdefault("coder", _coder_member("fake-model"))
    kwargs.setdefault("run_gate", _pass_gate)
    kwargs.setdefault("run_review", _pass_review)
    kwargs.setdefault("sdk_client_factory", lambda options: _FakeClient())
    kwargs.setdefault("approve_edit", _RecordingApproveEdit())
    return build_authoring_session(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt",
        direct_branch="ai", **kwargs,
    )


def _coder_edits(repo):
    (repo / "demo_utils" / "lib" / "retbb.py").write_text("def add_retbb(i):\n    return i\n")
    (repo / "demo_utils" / "tests" / "test_retbb.py").write_text("def test_r():\n    pass\n")


def test_a_submission_is_checkpointed_before_the_gate_and_restorable_after_a_reset(tmp_path):
    """The working tree at submit time survives the failure reset as a restorable snapshot,
    and a fresh session for the same tool on the same base ref restores it and says so."""
    from auto_research.sandbox.merge_flow import _reset_direct_tree

    repo = _toolkit_repo(tmp_path)
    (repo / "demo_utils" / "tests").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tests dir", "--allow-empty")
    _git(repo, "checkout", "-q", "-b", "ai")

    session = _direct_session(tmp_path, repo)
    _coder_edits(repo)                                   # the "coder" worked
    gate = asyncio.run(session.run_gate_step())          # submit: snapshot, then scan
    assert gate.passed
    review = asyncio.run(session.run_review_step(gate))
    assert review.approved

    snap = tmp_path / "wt" / "submissions" / "fit_retbb_hss"
    assert (snap / "submit_01.patch").is_file()
    import json as _json
    state = _json.loads((snap / "state.json").read_text())
    assert state["submits"][-1]["gate_passed"] is True
    assert state["submits"][-1]["review_approved"] is True

    # crash before commit: the failure reset cleans the tree; the snapshot survives it
    asyncio.run(_reset_direct_tree(repo, base_ref=session._base_ref,
                                   artifact_dir=tmp_path / "wt"))
    assert not (repo / "demo_utils" / "lib" / "retbb.py").exists()

    resumed = _direct_session(tmp_path, repo)
    assert resumed.restored_submission is not None
    assert resumed.restored_submission["index"] == 1
    assert (repo / "demo_utils" / "lib" / "retbb.py").is_file()  # work is back in the tree
    assert "Restored prior work" in resumed.initial_message


def test_a_snapshot_never_restores_onto_a_moved_base_ref(tmp_path):
    """A chain-mate's commit moves HEAD; the old snapshot silently stays unrestored."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")

    session = _direct_session(tmp_path, repo)
    (repo / "demo_utils" / "lib" / "retbb.py").write_text("x = 1\n")
    asyncio.run(session.run_gate_step())
    _git(repo, "reset", "--hard", "HEAD"); _git(repo, "clean", "-fdq")
    _git(repo, "commit", "-qm", "chain-mate landed", "--allow-empty")  # HEAD moves

    resumed = _direct_session(tmp_path, repo)
    assert resumed.restored_submission is None
    assert not (repo / "demo_utils" / "lib" / "retbb.py").exists()


def test_commit_discards_the_tools_snapshots(tmp_path):
    """Committed work lives in git; the snapshot record has nothing left to restore."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")

    session = _direct_session(tmp_path, repo)
    (repo / "demo_utils" / "lib" / "retbb.py").write_text("x = 1\n")
    asyncio.run(session.run_gate_step())
    assert (tmp_path / "wt" / "submissions" / "fit_retbb_hss").exists()
    asyncio.run(session.commit("Add retbb"))
    assert not (tmp_path / "wt" / "submissions" / "fit_retbb_hss").exists()


def test_an_unappliable_snapshot_is_discarded_and_authoring_starts_clean(tmp_path):
    """The snapshot is an economy, never a dependency: a corrupt patch is dropped."""
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")

    session = _direct_session(tmp_path, repo)
    (repo / "demo_utils" / "lib" / "retbb.py").write_text("x = 1\n")
    asyncio.run(session.run_gate_step())
    _git(repo, "reset", "--hard", "HEAD"); _git(repo, "clean", "-fdq")
    snap = tmp_path / "wt" / "submissions" / "fit_retbb_hss"
    (snap / "submit_01.patch").write_text("this is not a patch\n")

    resumed = _direct_session(tmp_path, repo)
    assert resumed.restored_submission is None
    assert not snap.exists()
    assert "Restored prior work" not in resumed.initial_message


# ── an approved commit is the new base ref (§9.9) ────────────────────────────


def test_a_submit_after_a_commit_neither_reverts_nor_raises(tmp_path):
    """The real post-commit-submit trajectory, driven through ``execute_submit``.

    Attempt 5's ledger, link 2: ``review.result approved`` → ``commit … 1523349`` → **another**
    ``submit_for_review`` → ``coder.committed`` ("auto-reverted") → the whole gate + co-lead
    cycle again. ``submit_for_review`` stays available after a commit (budget-free in the lead's
    loop, and the loop continues past one), and the §9.9 backstop reverts on a moved HEAD, so
    with the base ref left behind it could not tell an approved commit from a coder slip.

    Two things have to hold, and the second is why this drives the tool rather than
    ``run_gate_step``: the approved commit survives, **and** the call does not raise. With the
    base ref advanced but nothing else changed, the gate would scan an empty changeset and
    ``approve_edit`` would run ``git commit`` with nothing staged — a non-zero exit that raises
    straight out of the tool call, and ``_react`` does not catch tool exceptions ([F6]).
    """
    from auto_research.agents.react import execute_submit
    from auto_research.sandbox.merge_flow import approve_edit as real_approve

    repo = _toolkit_repo(tmp_path)
    (repo / "demo_utils" / "tests").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tests dir", "--allow-empty")
    _git(repo, "checkout", "-q", "-b", "ai")

    mon = _RecordingMonitor()
    session = _direct_session(tmp_path, repo, approve_edit=real_approve, monitor=mon)
    _coder_edits(repo)

    assert "COMMITTED" in asyncio.run(execute_submit(session)).text
    sha = _git_out(repo, "rev-parse", "HEAD").strip()
    assert session._base_ref == sha, "the landed commit IS the attempt's base now"
    assert not session.pending_changes()

    outcome = asyncio.run(execute_submit(session))   # the lead submits again

    assert not outcome.is_error
    assert "already committed" in outcome.text
    assert _git_out(repo, "rev-parse", "HEAD").strip() == sha, "the approved commit survives"
    assert not mon.of("coder.committed"), "an approved commit is not a coder slip"
    assert session.submits_used == 1, "answering a question is not a submission"


def test_a_real_follow_up_after_a_commit_still_lands(tmp_path):
    """The no-op guard keys on *nothing changed*, not on *already committed*.

    A lead that drives the coder again after a commit — a genuine follow-up — must still be able
    to gate, review and land the increment, or the guard would trade one failure for another.
    """
    from auto_research.agents.react import execute_submit
    from auto_research.sandbox.merge_flow import approve_edit as real_approve

    repo = _toolkit_repo(tmp_path)
    (repo / "demo_utils" / "tests").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tests dir", "--allow-empty")
    _git(repo, "checkout", "-q", "-b", "ai")

    session = _direct_session(tmp_path, repo, approve_edit=real_approve)
    _coder_edits(repo)
    asyncio.run(execute_submit(session))
    first = _git_out(repo, "rev-parse", "HEAD").strip()

    (repo / "demo_utils" / "lib" / "retbb.py").write_text("def add_retbb(i):\n    return i + 1\n")
    assert session.pending_changes()
    assert "COMMITTED" in asyncio.run(execute_submit(session)).text

    second = _git_out(repo, "rev-parse", "HEAD").strip()
    assert second != first and session._base_ref == second
    assert session.submits_used == 2


def test_a_commit_advances_the_persisted_base_ref(tmp_path):
    """The resume-time heal resets ``--hard`` to the persisted base ref, so a commit has to
    advance the file too — otherwise a hard kill after an approved commit heals *behind* it and
    the tool is silently gone from the branch."""
    from auto_research.sandbox.merge_flow import _read_persisted_base_ref, approve_edit as real_approve

    repo = _toolkit_repo(tmp_path)
    (repo / "demo_utils" / "tests").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tests dir", "--allow-empty")
    _git(repo, "checkout", "-q", "-b", "ai")
    run_dir = tmp_path / "run"
    (run_dir / "authoring").mkdir(parents=True)

    session = build_authoring_session(
        _spec(), toolkit_root=repo, worktree_base=run_dir / "authoring",
        direct_branch="ai", lead=_FakeLead(), coder=_coder_member("fake-model"),
        run_gate=_pass_gate, run_review=_pass_review,
        sdk_client_factory=lambda options: _FakeClient(), approve_edit=real_approve,
    )
    base = session._base_ref
    assert _read_persisted_base_ref(run_dir) == base

    _coder_edits(repo)
    sha = asyncio.run(session.commit("land it"))
    assert _read_persisted_base_ref(run_dir) == sha != base


def test_aclose_after_a_commit_leaves_the_commit_alone(tmp_path):
    """A commit followed by nothing: ``aclose`` has no unbanked work to reset, so the commit and
    its files survive untouched. (F1: the guard is ``pending_changes()``, and a commit advances
    the base ref, so right after one there is provably nothing pending.)"""
    from auto_research.sandbox.merge_flow import approve_edit as real_approve

    repo = _toolkit_repo(tmp_path)
    (repo / "demo_utils" / "tests").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tests dir", "--allow-empty")
    _git(repo, "checkout", "-q", "-b", "ai")

    session = _direct_session(tmp_path, repo, approve_edit=real_approve)
    _coder_edits(repo)
    sha = asyncio.run(session.commit("land it"))
    asyncio.run(session.aclose())

    assert _git_out(repo, "rev-parse", "HEAD").strip() == sha
    assert (repo / "demo_utils" / "lib" / "retbb.py").is_file()


# ── F1: "did this session commit?" is not "is the tree's work banked?" ────────


def _f1_session(tmp_path, repo):
    """A direct-branch session on a repo with a real ``approve_edit`` (so the base ref advances
    to a real sha and ``pending_changes`` can tell banked from unbanked)."""
    from auto_research.sandbox.merge_flow import approve_edit as real_approve

    (repo / "demo_utils" / "tests").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tests dir", "--allow-empty")
    _git(repo, "checkout", "-q", "-b", "ai")
    return _direct_session(tmp_path, repo, approve_edit=real_approve)


def test_f1_work_after_a_commit_is_reset_on_close(tmp_path):
    """THE F1 REGRESSION. One session, two tools: the first commits, the second is rejected and
    its edits are still on disk at close. Those edits must be reset.

    Observed live 2026-09-08 (slot 2): the old guard was a commit latch set by
    the first commit and never cleared, so this exact sequence exited **without** resetting. The
    rejected tool's ``lib/`` code and its ``TOOL_MANIFEST`` entry stayed in the tree; because
    ``ToolkitRegistry.reload`` discovers tools from the tree rather than from git, it became a
    callable tool and its figures reached a reviewer-accepted report.
    """
    repo = _toolkit_repo(tmp_path)
    session = _f1_session(tmp_path, repo)

    _coder_edits(repo)                                   # tool 1
    sha = asyncio.run(session.commit("land tool 1"))
    assert not session.pending_changes()                 # banked

    # tool 2, in the SAME session: rejected, never committed — the shape that survived.
    rejected_lib = repo / "demo_utils" / "lib" / "plotter.py"
    rejected_lib.write_text("def plot():\n    return 2\n")
    manifest = repo / "demo_utils" / "skills" / "__init__.py"
    manifest.write_text('TOOL_MANIFEST = {"demo_plot": {"fn": "plot"}}\n')
    assert session.pending_changes()                     # unbanked

    asyncio.run(session.aclose())

    assert not rejected_lib.exists(), "rejected lib/ code must not survive the session"
    assert not manifest.exists(), "a rejected TOOL_MANIFEST entry must not survive: the registry "\
                                  "reloads from the tree, so leaving it makes the tool callable"
    assert _git_out(repo, "status", "--porcelain") == ""
    assert _git_out(repo, "rev-parse", "HEAD").strip() == sha, "the approved commit survives"
    assert (repo / "demo_utils" / "lib" / "retbb.py").is_file(), "tool 1's committed file survives"
    assert list((tmp_path / "wt").glob("tool_author_failed_*.patch")), "discarded work preserved"


def test_f1_the_commit_record_survives_the_reset(tmp_path):
    """``last_commit_sha`` is the durable record ``_authoring_result`` reads to report
    ``status="committed"``. The F1 change must not clear it, or every successful commit would
    report as a failed attempt."""
    from auto_research.agents.roles import _authoring_result

    repo = _toolkit_repo(tmp_path)
    session = _f1_session(tmp_path, repo)

    _coder_edits(repo)
    sha = asyncio.run(session.commit("land it"))
    (repo / "demo_utils" / "lib" / "leftover.py").write_text("x = 1\n")   # unbanked
    asyncio.run(session.aclose())                                        # ... and reset away

    assert session.last_commit_sha == sha
    assert _authoring_result(session, session.tool_spec).status == "committed"


def test_f1_a_clean_close_writes_no_patch_artifact(tmp_path):
    """A session that closes with nothing unbanked writes no patch: ``_save_diff_and_reset``
    returns ``None`` for an empty diff, so the now-routine close does not litter the run dir."""
    repo = _toolkit_repo(tmp_path)
    session = _f1_session(tmp_path, repo)

    _coder_edits(repo)
    asyncio.run(session.commit("land it"))
    asyncio.run(session.aclose())

    assert not list((tmp_path / "wt").glob("tool_author_failed_*.patch"))


# ── the author's environment carries no host-tooling variables ────────────────


def test_author_env_drops_host_tooling_variables(monkeypatch):
    """Measured 2026-09-03 (run fbb93f26, seq 922): the coder ran `env | grep -i path` and read
    `CLAUDE_CODE_EXECPATH=…/anthropic.claude-code-…/claude` and a Copilot variable, inherited from
    the VS Code terminal that launched the backend. The science stack's own variables and the
    vendor key must survive the filter untouched."""
    from auto_research.sandbox import merge_flow, runner

    sourced = {
        "PATH": "/opt/heasoft/bin", "HEADAS": "/opt/heasoft", "HOME": "/home/x",
        "ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CODE_EXECPATH": "/ext/anthropic.claude-code/claude",
        "CLAUDECODE": "1", "COPILOT_OTEL_FILE_EXPORTER_PATH": "/dev/null", "VSCODE_PID": "42",
    }
    monkeypatch.setattr(runner, "_source_ini_env", lambda ini: dict(sourced))
    monkeypatch.setattr(
        merge_flow, "_orchestration_config",
        lambda: {"analyze_env": "/envs/astropy", "analyze_ini_sh": "/x/ini.sh"},
    )
    env = merge_flow._author_env(pathlib.Path("/tk"))
    assert env is not None
    for gone in ("CLAUDE_CODE_EXECPATH", "CLAUDECODE", "COPILOT_OTEL_FILE_EXPORTER_PATH", "VSCODE_PID"):
        assert gone not in env, gone
    assert env["HEADAS"] == "/opt/heasoft" and env["HOME"] == "/home/x"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["PATH"].startswith("/envs/astropy/bin:") and env["PYTHONPATH"].startswith("/tk")


# ══════════════════════════════════════════════ B1/B2: the coder runs beside the lead


class _SlowBackend:
    """A backend whose drive takes as long as we tell it to, and records its lifecycle order."""

    def __init__(self, events: list, *, delay: float = 0.05, fail: bool = False):
        self.events = events
        self.delay = delay
        self.fail = fail
        self.driven: list[str] = []
        self.steered: list[str] = []

    async def connect(self):
        self.events.append("connect")

    async def drive(self, message: str) -> str:
        self.driven.append(message)
        self.events.append("drive:start")
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.events.append("drive:cancelled")
            raise
        if self.fail:
            raise RuntimeError("Credit balance is too low")
        self.events.append("drive:end")
        return f"coder says: {message}"

    async def steer(self, message: str) -> bool:
        """Mid-turn delivery, available only while a drive is running (as on both harnesses)."""
        if self.events.count("drive:start") <= self.events.count("drive:end") + self.events.count(
            "drive:cancelled"
        ):
            return False
        self.steered.append(message)
        return True

    async def disconnect(self):
        self.events.append("disconnect")


def _bg_session(events, **kw):
    """An AuthoringSession with everything but the backend stubbed out."""
    from auto_research.sandbox.merge_flow import AuthoringSession

    session = object.__new__(AuthoringSession)
    session._backend = _SlowBackend(events, **kw)
    session._closed = False
    session._drive_task = None
    session._drive_status = "idle"
    session._drive_result = None
    session._drive_error = None
    session._direct_branch = None
    session._monitor = None
    session._pending_questions = []
    session._init_channel()
    session.tool_spec = types.SimpleNamespace(proposed_tool_name="ixpe_pcube_bands")
    return session


def test_start_drive_returns_before_the_coder_finishes():
    """B1's whole point: the lead keeps its turn while the coder works."""
    events: list = []

    async def scenario():
        session = _bg_session(events, delay=0.2)
        session.start_drive("write the wrapper")
        # We are back immediately, and the coder has not finished.
        assert session.drive_status == "working"
        assert "drive:end" not in events
        await asyncio.sleep(0.35)
        assert session.drive_status == "stopped"
        text, error = session.take_drive_result()
        assert error is None and "write the wrapper" in text
        # Consumed once, so it is reported once.
        assert session.take_drive_result() == (None, None)

    asyncio.run(scenario())


def test_a_second_instruction_while_the_coder_works_is_refused_not_queued():
    """One conversation per warm session: interleaving two is undefined on every harness."""

    async def scenario():
        session = _bg_session([], delay=0.2)
        session.start_drive("first")
        with pytest.raises(RuntimeError, match="already working"):
            session.start_drive("second")
        await asyncio.sleep(0.3)

    asyncio.run(scenario())


def test_a_coder_that_dies_is_reported_as_an_error_not_as_narration():
    """The 2026-09-09 defect: a Claude-harness session whose credential ran out returned the
    CLI's own billing string with `is_error` discarded, and the lead spent three drives steering
    a corpse at $0 — then recorded the failure with a confidently wrong cause."""

    async def scenario():
        session = _bg_session([], delay=0.01, fail=True)
        session.start_drive("go")
        await asyncio.sleep(0.1)
        assert session.drive_status == "stopped"
        text, error = session.take_drive_result()
        assert error and "Credit balance" in error
        assert "harness error" in text

    asyncio.run(scenario())


def test_aclose_cancels_the_drive_before_it_disconnects():
    """B2 — the ordering IS the property.

    `aclose` disconnects the backend and may reset the worktree; a drive still in flight would be
    writing into that tree while it is repaired. `RunControl.interrupt` cancels only the
    registered run task, and a task created inside the session is not in that set — so it is
    cancelled and awaited here, explicitly, first.
    """
    events: list = []

    async def scenario():
        session = _bg_session(events, delay=5.0)
        session.start_drive("a long one")
        await asyncio.sleep(0.05)          # let the drive actually start
        await session.aclose()

    asyncio.run(scenario())
    assert "drive:start" in events
    assert events.index("drive:cancelled") < events.index("disconnect"), (
        f"the backend was disconnected while a drive was still in flight: {events}"
    )
    assert "drive:end" not in events, "the drive ran to completion despite being cancelled"


def test_aclose_is_still_idempotent_with_a_background_drive():
    """The interrupt path closes the session from shielded cleanup and the `finally` may still
    run afterwards; a second close must be a no-op, not a second cancel."""
    events: list = []

    async def scenario():
        session = _bg_session(events, delay=5.0)
        session.start_drive("x")
        await asyncio.sleep(0.05)
        await session.aclose()
        await session.aclose()

    asyncio.run(scenario())
    assert events.count("disconnect") == 1


def test_the_background_drive_inherits_the_cost_meter_context():
    """B7: the meter is a ContextVar, and a task created by `start_drive` inherits the context
    that created it — so the coder's spend lands in the run total exactly as when it blocked."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    seen: list[str] = []

    async def scenario():
        marker.set("run-42")
        session = _bg_session([], delay=0.01)

        original = session._backend.drive

        async def observing(message):
            seen.append(marker.get())
            return await original(message)

        session._backend.drive = observing
        session.start_drive("go")
        await asyncio.sleep(0.1)

    asyncio.run(scenario())
    assert seen == ["run-42"], "the background drive ran outside the caller's context"


# ══════════════════════════════════════════ wait_coder + queued drive_coder (2026-09-24)
#
# Slot 6 (2026-09-15): 134 checks (114 refused), 47 mid-turn steers, 31 refused submits at a
# 3-10 s cadence. The replacement is a blocking wait with early wake, and a message to a working
# coder is queued for its next turn unless the lead asks for mid-turn delivery.


def test_a_message_to_a_working_coder_is_queued_and_becomes_its_next_turn():
    events: list = []

    async def scenario():
        session = _bg_session(events, delay=0.1)
        session.start_drive("first")
        await asyncio.sleep(0.02)                   # the turn is genuinely running now
        assert await session.send("second") == "queued"
        assert session._backend.steered == [], "a plain send must not steer the running turn"
        assert session.drive_status == "working"
        await asyncio.sleep(0.35)
        assert session.drive_status == "stopped"
        assert session._backend.driven == ["first", "second"]
        text, error = session.take_drive_result()
        assert error is None and "coder says: first" in text and "coder says: second" in text

    asyncio.run(scenario())


def test_mid_turn_steers_the_running_turn():
    async def scenario():
        session = _bg_session([], delay=0.2)
        session.start_drive("first")
        await asyncio.sleep(0.02)
        assert await session.send("change course", mid_turn=True) == "steered"
        assert session._backend.steered == ["change course"]
        await asyncio.sleep(0.3)
        assert session._backend.driven == ["first"], "a steer is not a new turn"

    asyncio.run(scenario())


def test_an_answer_to_an_open_question_goes_mid_turn_without_the_flag():
    """The coder may be waiting on the answer, so it must not sit in the queue behind the turn."""

    async def scenario():
        session = _bg_session([], delay=0.2)
        session.start_drive("first")
        await asyncio.sleep(0.02)
        session.mark_question_open()
        assert await session.send("PI") == "answered"
        assert session._backend.steered == ["PI"]
        assert await session.send("and add a test") == "queued", "only the answer skips the queue"
        await asyncio.sleep(0.5)

    asyncio.run(scenario())


def test_a_send_that_races_the_turn_end_still_delivers():
    """A mid-turn steer refused because the turn just ended falls through to a new turn."""

    async def scenario():
        session = _bg_session([], delay=0.05)
        session.start_drive("first")
        await asyncio.sleep(0.15)
        assert session.drive_status == "stopped"
        assert await session.send("late", mid_turn=True) == "started"
        await asyncio.sleep(0.15)
        assert session._backend.driven == ["first", "late"]

    asyncio.run(scenario())


def _timed_wait(session, timeout):
    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        reason = await session.wait_for_event(timeout)
        return reason, loop.time() - t0
    return go()


def test_the_wait_returns_when_the_turn_ends_not_at_its_timeout():
    async def scenario():
        session = _bg_session([], delay=0.1)
        session.start_drive("go")
        reason, waited = await _timed_wait(session, 30)
        assert reason == "turn_ended" and waited < 1.0, (reason, waited)

    asyncio.run(scenario())


def test_the_wait_returns_when_a_human_steer_arrives_for_the_lead():
    from auto_research.run_control import RunControl

    async def scenario():
        rc = RunControl()
        session = _bg_session([], delay=5.0)
        session.watch_steers(rc.steer_event("analyst_lead"))
        session.start_drive("go")
        asyncio.get_running_loop().call_later(0.1, rc.add_steer, "analyst_lead", "wrap up")
        reason, waited = await _timed_wait(session, 30)
        assert reason == "steer" and waited < 1.0, (reason, waited)
        # The wait consumed nothing: the steer is still delivered by the loop's own drain.
        assert rc.drain_steer("analyst_lead") == ["wrap up"]
        assert not rc.steer_event("analyst_lead").is_set()
        await session.aclose()

    asyncio.run(scenario())


def test_the_wait_times_out_while_the_coder_keeps_working():
    async def scenario():
        session = _bg_session([], delay=5.0)
        session.start_drive("go")
        reason, waited = await _timed_wait(session, 0.2)
        assert reason == "timeout" and 0.15 < waited < 1.0, (reason, waited)
        assert session.drive_status == "working"
        await session.aclose()

    asyncio.run(scenario())


def test_waiting_on_an_idle_coder_returns_at_once():
    async def scenario():
        session = _bg_session([], delay=5.0)
        reason, waited = await _timed_wait(session, 30)
        assert reason == "idle" and waited < 0.1

    asyncio.run(scenario())


def test_a_stop_cancels_a_pending_wait_and_leaves_no_waiter_behind():
    async def scenario():
        session = _bg_session([], delay=5.0)
        session.start_drive("go")
        before = len(asyncio.all_tasks())
        wait = asyncio.create_task(session.wait_for_event(30))
        await asyncio.sleep(0.05)
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
        await asyncio.sleep(0)
        assert len(asyncio.all_tasks()) <= before, "a waiter task outlived the cancelled wait"
        await session.aclose()

    asyncio.run(scenario())


def test_wait_coder_reports_the_narration_and_the_time(monkeypatch):
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 3))

    async def scenario():
        session = _bg_session([], delay=0.1)
        session.start_drive("write it")
        out = await react.execute_wait_coder(session, {"timeout_s": 99})
        assert not out.is_error
        assert out.text.startswith("[waited ") and "lowered to the 3s maximum" in out.text
        assert "coder says: write it" in out.text and "It has stopped" in out.text
        again = await react.execute_wait_coder(session, {})
        assert "nothing to wait for" in again.text, "a narration is reported once"

    asyncio.run(scenario())


def test_wait_coder_raises_a_short_timeout_to_the_floor(monkeypatch):
    """wait_coder(0) on a busy coder must hold for the floor, not answer instantly."""
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 3))

    async def scenario():
        session = _bg_session([], delay=5.0)
        session.start_drive("go")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        out = await react.execute_wait_coder(session, {"timeout_s": 0})
        assert loop.time() - t0 >= 0.95, "the floor was not enforced"
        assert "raised to the 1s minimum" in out.text and "still working" in out.text
        await session.aclose()

    asyncio.run(scenario())


def test_wait_coder_reports_a_dead_coder_as_an_error(monkeypatch):
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 3))

    async def scenario():
        session = _bg_session([], delay=0.01, fail=True)
        session.start_drive("go")
        out = await react.execute_wait_coder(session, {})
        assert out.is_error and "FAILED" in out.text and "Credit balance" in out.text

    asyncio.run(scenario())


def test_submit_waits_for_the_coder_to_stop_before_the_gate(monkeypatch):
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 30))
    seen: list = []

    async def fake_submit_now(session, cycle):
        seen.append(session.drive_status)
        return react.ToolOutcome(text="GATE FAILED — x")

    monkeypatch.setattr(react, "_submit_now", fake_submit_now)

    async def scenario():
        session = _bg_session([], delay=0.2)
        session.start_drive("finish it")
        await session.send("and the test")          # queued: the wait must span both turns
        out = await react.execute_submit(session, None)
        assert seen == ["stopped"], "the gate ran while the coder was still writing"
        assert "coder says: finish it" in out.text and "coder says: and the test" in out.text
        assert out.text.endswith("GATE FAILED — x")

    asyncio.run(scenario())


def _coder_asks(session, rc, text):
    """What the coder's ``message(to="lead")`` does: the text to the lead's inbox, the question
    marked open (``sandbox.merge_flow._make_ask_lead_handler``)."""
    rc.add_steer("analyst_lead", text, sender="coder")
    session.mark_question_open()


def test_submit_returns_a_question_instead_of_gating(monkeypatch):
    """A coder waiting on the lead would never stop; the submit hands the question back."""
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 30))

    async def never(session, cycle):  # pragma: no cover — reaching it is the failure
        raise AssertionError("gated while the coder had an open question")

    monkeypatch.setattr(react, "_submit_now", never)

    async def scenario():
        rc = RunControl()
        session = _bg_session([], delay=5.0)
        session.watch_steers(rc.steer_event("analyst_lead"))
        session.start_drive("go")
        asyncio.get_running_loop().call_later(0.1, _coder_asks, session, rc, "PI or ENERGY?")
        out = await react.execute_submit(session, None)
        assert not out.is_error and "Not submitted" in out.text and "message" in out.text
        assert rc.drain_steer("analyst_lead") == ["PI or ENERGY?"], "the question waits in the inbox"
        await session.aclose()

    asyncio.run(scenario())


def test_a_question_wake_does_not_claim_the_coder_started_a_next_turn(monkeypatch):
    """Found live 2026-09-24 (question cell): woken by a question while the coder was still in
    its FIRST turn, the wait also said "It has already started its next turn on the message(s)
    you queued" — nothing was queued and no turn had ended."""
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 3))

    async def scenario():
        rc = RunControl()
        session = _bg_session([], delay=5.0)
        session.watch_steers(rc.steer_event("analyst_lead"))
        session.start_drive("go")
        asyncio.get_running_loop().call_later(0.1, _coder_asks, session, rc, "PI or ENERGY?")
        out = await react.execute_wait_coder(session, {})
        assert "A message for you has arrived" in out.text
        assert "next turn" not in out.text and "has stopped" not in out.text, out.text
        await session.aclose()

    asyncio.run(scenario())


def test_a_stop_cancels_a_submit_that_is_waiting_on_the_coder(monkeypatch):
    """`/stop` cancels the run task; a submit parked in its wait must die with it, never run the
    gate afterwards, and leave no waiter behind."""
    import auto_research.agents.react as react

    monkeypatch.setattr(react, "coder_wait_limits_s", lambda: (1, 2, 30))

    async def never(session, cycle):  # pragma: no cover — reaching it is the failure
        raise AssertionError("gated after the stop")

    monkeypatch.setattr(react, "_submit_now", never)

    async def scenario():
        session = _bg_session([], delay=5.0)
        session.start_drive("go")
        before = len(asyncio.all_tasks())
        submit = asyncio.create_task(react.execute_submit(session, None))
        await asyncio.sleep(0.05)
        submit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submit
        await asyncio.sleep(0)
        assert len(asyncio.all_tasks()) <= before, "a waiter outlived the cancelled submit"
        await session.aclose()

    asyncio.run(scenario())
