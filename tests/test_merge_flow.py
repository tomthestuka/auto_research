"""Deterministic tests for the Phase-2 tool-authoring harness (merge_flow.propose_and_pr).

The bounded rework loop is exercised with injected fakes — a fake SDK client, a fake
analyst lead, and stub gate/review hooks — so no live LLM or git is involved.
"""

import asyncio

import pytest

from auto_research.sandbox.merge_flow import (
    HumanInputRequired,
    _make_ask_human_handler,
    _make_ask_lead_handler,
    propose_and_pr,
)
from auto_research.schemas import GateReport, ToolReviewVerdict, ToolSpec


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
        self.questions: list[str] = []

    async def clarify_for_author(self, question, *, kind="clarify_code", context=""):
        self.questions.append(question)
        return f"lead-answer:{question}"


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
    kwargs.setdefault("coder_model", "fake-model")
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


def test_ask_lead_handler_records_to_monitor():
    """Fix #2 (b): escalations are logged with question + answer."""
    mon = _RecordingMonitor()
    handler = _make_ask_lead_handler(_FakeLead(), monitor=mon)
    asyncio.run(handler("which lib fn?", context="ctx"))
    ev = mon.of("coder.ask_lead")
    assert ev and ev[0]["data"]["question"] == "which lib fn?"
    assert "lead-answer" in ev[0]["data"]["answer"]


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


def test_ask_lead_handler_routes_to_lead():
    lead = _FakeLead()
    handler = _make_ask_lead_handler(lead)
    answer = asyncio.run(handler("which lib fn?", context="ctx"))
    assert answer == "lead-answer:which lib fn?"
    assert lead.questions == ["which lib fn?"]


def test_ask_human_handler_routes_to_injected_handler():
    seen = {}

    def human(query):  # query is a HumanQuery (typed §9.8 contract)
        seen["kind"] = query.kind
        seen["question"] = query.question
        seen["asked_by"] = query.asked_by
        return "human-says-go"

    handler = _make_ask_human_handler(human)
    answer = asyncio.run(handler("response convention?", context="expert"))
    assert answer == "human-says-go"
    assert seen["kind"] == "blocked" and seen["question"] == "response convention?"
    assert seen["asked_by"] == "coder"


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


def test_fs_guard_hook_allows_inside_denies_outside_and_governance(tmp_path):
    from auto_research.sandbox.fs_guard import PathGuard
    from auto_research.sandbox.merge_flow import _make_fs_guard_hook

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
    assert denied(call("Read", {"file_path": str(tmp_path / "auto_research" / "gate.py")}))  # outside
    assert denied(call("Read", {"file_path": str(tk / "CLAUDE.md")}))  # governance, even inside
    assert call("Glob", {"pattern": "**/*.py"}) == {}  # no path arg → defaults to cwd → allow
    # Bash: named-path enforcement (best-effort)
    assert call("Bash", {"command": 'python -c "import xspec_utils"'}) == {}  # no abs path named
    assert denied(call("Bash", {"command": f"cat {tmp_path}/auto_research/gate.py"}))


def test_fs_guard_hook_inactive_when_allowlist_empty():
    from auto_research.sandbox.fs_guard import PathGuard
    from auto_research.sandbox.merge_flow import _make_fs_guard_hook

    hook = _make_fs_guard_hook(PathGuard([]))  # unconfigured → allow-all
    out = asyncio.run(hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"},
         "hook_event_name": "PreToolUse"}, None, None,
    ))
    assert out == {}
