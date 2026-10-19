"""A worker's next turn starts from its history rebuilt from the ledger (B2), with its earlier
tool outputs indexed to one line each and readable again by handle (B3). No LLM, no network.

The worker is driven through ``AnalystWorkerAgent.analyze`` exactly as the debate drives it: a
real ``SandboxToolProvider`` over a fake sandbox (the toolkit process is what is faked), a real
``ReceiptStore`` minting a receipt per call, a real ``RunMonitor`` writing the JSONL the history is
rebuilt from, and the real quick channel (``view_receipt`` lives there).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from auto_research.agents import history
from auto_research.agents.react import ToolOutcome, execute_view_receipt
from auto_research.agents.roles import AnalystWorkerAgent
from auto_research.hitl.router import SeatRouter
from auto_research.models.types import (
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.monitor import RunMonitor
from auto_research.provenance.store import ReceiptStore, ScopedLookup
from auto_research.run_control import RunControl
from auto_research.schemas import AnalysisTask
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

KEY = "analyst_worker_0"
BIG = "\n".join(f"row {i}: chi2 = {1000 + i}.5 for bin {i}" for i in range(2500))  # ~90k chars


def _report_args(summary):
    return {"content": f"# summary\n{summary}\n# status\nsucceeded\n# findings\n- the fit converged\n"}


class _Model:
    """Per turn: run the fit (a big output), write the report, end. Keeps what each call got."""

    def __init__(self, extra_first=()):
        self.config = SimpleNamespace(model="fake")
        self.sent: list[list] = []
        self.turn_calls = 0
        self.extra_first = list(extra_first)

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto", images=(),
                   documents=(), cache_prefix=False):
        self.sent.append(list(messages))
        if self.extra_first:
            return self.extra_first.pop(0)
        step = self.turn_calls % 3
        self.turn_calls += 1
        n = self.turn_calls
        if step == 0:
            return ModelResponse(tool_calls=[ToolCall(id=f"fit{n}", name="tk_fit",
                                                      arguments={"model": "tbabs*po"})])
        if step == 1:
            return ModelResponse(tool_calls=[ToolCall(id=f"er{n}", name="write_report",
                                                      arguments=_report_args("the fit is done"))])
        return ModelResponse(tool_calls=[ToolCall(id=f"te{n}", name="turn_end", arguments={})])


def _worker(tmp_path, model, store, monitor):
    sandbox = _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": BIG})
    worker = AnalystWorkerAgent(
        model=model, available_toolkits=[], sandbox=sandbox,
        registry=_FakeRegistry(known={"tk_fit"}, mcp=set()), monitor=monitor, prompt="worker",
    )
    worker.agent_key = KEY
    worker.report_dir = tmp_path / "runs" / "r1" / KEY
    worker.report_dir.mkdir(parents=True)
    worker.receipts = store
    control = RunControl()
    worker.run_control = control
    worker.channel = SeatRouter(control)
    worker.channel.register(KEY, worker)
    return worker


def _setup(tmp_path, model=None):
    store = ReceiptStore.for_run(tmp_path / "runs" / "r1")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = model or _Model()
    return _worker(tmp_path, model, store, monitor), model, store, monitor


def _chars(messages) -> int:
    return sum(len(json.dumps(history.encode_message(m))) for m in messages)


def test_the_second_turn_opens_with_one_transcript_of_the_first(tmp_path):
    """Ruling B6.2: at a turn boundary the earlier turns become ONE user message — no assistant
    turn, so no thinking, reasoning item or tool-use block is replayed — with each earlier tool
    output as its index line; then the new task."""
    worker, model, store, monitor = _setup(tmp_path)
    first = asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    assert first.status == "succeeded", "each scripted turn files its report"
    first_turn_end = len(model.sent)
    second = asyncio.run(worker.analyze(AnalysisTask(question="fit it again")))
    assert second.status == "succeeded" and len(model.sent) == 2 * first_turn_end

    seed = model.sent[first_turn_end]
    assert [type(m).__name__ for m in seed] == ["ModelMessage"] * 3
    system, transcript, task = seed
    assert system.role == "system" and transcript.role == "user" and task.role == "user"
    assert transcript.content.startswith(history.TRANSCRIPT_MARK)
    assert "fit it" in transcript.content and "fit it again" in task.content
    assert '"memory"' not in task.content, "no seed inlines memory.md"
    handle = next(r.handle for r in store.receipts(kind="result") if r.call_id)
    index_lines = [ln for ln in transcript.content.splitlines() if "tk_fit returned" in ln]
    assert len(index_lines) == 1 and handle in index_lines[0] and "tbabs*po" in index_lines[0]
    assert "row 2499" not in transcript.content, "the 90k output is one line, not the output"
    # An order of magnitude: the verbatim first turn against its transcript.
    turns = list(history.turns_for(monitor.jsonl_path, KEY).values())
    verbatim = sum(len(json.dumps(history.encode_message(m))) for m in turns[0].messages)
    assert len(transcript.content) * 10 < verbatim
    # And the rebuild of the second turn is exactly what was sent (object dumps, not encodings).
    from tests.test_history_ledger import _snapshot

    assert _snapshot(turns[1].calls[0].messages, [], [], None) == _snapshot(seed, [], [], None)


def test_a_boundary_seed_carries_no_earlier_thinking_or_tool_blocks(tmp_path):
    """Whatever the earlier turns hold (thinking with signatures, OpenAI reasoning items, tool
    search discovery), none of it reaches the next turn's request."""
    worker, model, store, monitor = _setup(tmp_path, _Model(extra_first=[ModelResponse(
        content="plotting", tool_calls=[ToolCall(id="fitx", name="tk_fit", arguments={})],
        provider_parts=[{"type": "thinking", "thinking": "secret plan", "signature": "SIG"},
                        {"type": "reasoning", "encrypted_content": "ENC"},
                        {"type": "server_tool_use", "id": "s1", "name": "tool_search_tool_bm25"}],
    )]))
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    start = len(model.sent)
    asyncio.run(worker.analyze(AnalysisTask(question="again")))
    dumped = json.dumps([history.encode_message(m) for m in model.sent[start]])
    for marker in ("secret plan", "SIG", "ENC", "server_tool_use", "tool_use"):
        assert marker not in dumped, marker
    assert not any(isinstance(m, ModelResponse) for m in model.sent[start])


def _read_back(worker, handle):
    """Page a receipt through the seat's own channel tool, as the model would."""
    from auto_research.agents.roles import ChannelTools

    channel = ChannelTools(worker._kept_turn["tools"], role=worker)
    pages, offset = [], 1
    for _ in range(200):
        out = asyncio.run(channel.execute("view_receipt", {"handle": handle, "offset": offset}))
        assert not out.is_error, out.text
        pages.append(out.text)
        nxt = history_next_offset(out.text)
        if nxt is None:
            return pages
        offset = nxt
    raise AssertionError("paging never ended")


def test_an_old_result_is_read_back_in_full_by_its_handle(tmp_path):
    worker, model, store, monitor = _setup(tmp_path)
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    handle = next(r.handle for r in store.receipts(kind="result") if r.call_id)
    body = "\n".join(_strip_numbers(p) for p in _read_back(worker, handle))
    assert body.count("chi2 =") == 2500 and "row 2499: chi2 = 3499.5" in body


def test_a_one_line_output_reads_back_whole(tmp_path):
    """Found by review: a 100k-character one-line JSON result read back its first 2,000 only."""
    one_line = "{" + ", ".join(f'"k{i}": {i}.25' for i in range(9000)) + "}"
    store = ReceiptStore.for_run(tmp_path / "runs" / "r1")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    worker = _worker(tmp_path, _Model(), store, monitor)
    worker.sandbox._tool_fn = lambda m, f, a: {"exit_code": 0, "stdout": one_line}
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    handle = next(r.handle for r in store.receipts(kind="result") if r.call_id)
    pages = _read_back(worker, handle)
    joined = "".join(_strip_numbers(p).replace("\n", "") for p in pages)
    assert one_line in joined and len(pages) > 1
    assert all(len(p) < 21_000 for p in pages), "a page is at most the view's cap"


def history_next_offset(text: str) -> int | None:
    import re

    found = re.search(r"offset=(\d+)", text.rsplit("\n", 3)[-1] if "\n" in text else text)
    return int(found.group(1)) if found else None


def _strip_numbers(page: str) -> str:
    """The ``cat -n`` window's body lines, numbers removed."""
    import re

    lines = []
    for line in page.splitlines():
        hit = re.match(r"^\s*\d+\t(.*)$", line)
        if hit:
            lines.append(hit.group(1))
    return "\n".join(lines)


def test_indexing_keeps_short_outputs_names_receipts_and_is_idempotent():
    call = ToolCall(id="c1", name="plot", arguments={"what": "residuals"})
    short = ToolCall(id="c2", name="note", arguments={})
    bare = ToolCall(id="c3", name="web_fetch", arguments={"url": "u"})
    messages = [
        ModelMessage(role="system", content="s"),
        ModelResponse(tool_calls=[call, short, bare]),
        ToolResultMessage(results=[
            ToolResultPart(tool_call_id="c1", name="plot", content="x" * 5000, receipt="w:r1"),
            ToolResultPart(tool_call_id="c2", name="note", content="ok"),
            ToolResultPart(tool_call_id="c3", name="web_fetch", content="HEAD" + "y" * 9000),
        ]),
    ]
    once = history.apply_view(messages, "indexed", receipts={})
    twice = history.apply_view(once, "indexed", receipts={})
    parts = once[2].results
    assert 'view_receipt("w:r1")' in parts[0].content and len(parts[0].content) < 400
    assert parts[1].content == "ok", "an output already one line long is kept as it was"
    assert parts[2].content.count("HEAD") == 1 and len(parts[2].content) < 1800, (
        "an output with no receipt keeps its head: nothing else holds it for the agent"
    )
    assert [history.encode_message(m) for m in once] == [history.encode_message(m) for m in twice]


def test_repeated_call_ids_index_each_output_with_its_own_call():
    """Found by review: Google's id-less calls share the tool's name as id; each index line must
    name its own arguments and receipt, not the last call's."""
    turns = []
    for q, handle in (("polarization", "lib:r1"), ("faraday", "lib:r2")):
        turns += [
            ModelResponse(tool_calls=[ToolCall(id="web_search", name="web_search",
                                               arguments={"q": q})]),
            ToolResultMessage(results=[ToolResultPart(tool_call_id="web_search", name="web_search",
                                                      content=q * 200, receipt=handle)]),
        ]
    indexed = history.apply_view([ModelMessage(role="system", content="s"), *turns], "indexed",
                                 receipts={})
    lines = [m.results[0].content for m in indexed if isinstance(m, ToolResultMessage)]
    assert '"polarization"' in lines[0] and "lib:r1" in lines[0]
    assert '"faraday"' in lines[1] and "lib:r2" in lines[1]


def test_a_trailing_unanswered_call_is_dropped_from_the_indexed_base():
    """A turn that ended mid-batch (a provider error) leaves a call with no result; the next turn
    must start from a conversation every provider accepts."""
    messages = [
        ModelMessage(role="system", content="s"),
        ModelResponse(tool_calls=[ToolCall(id="c1", name="plot", arguments={})]),
    ]
    assert history.apply_view(messages, "indexed", receipts={}) == messages[:1]


def test_a_worker_without_a_ledger_starts_fresh_each_turn(tmp_path):
    store = ReceiptStore.for_run(tmp_path / "runs" / "r1")
    model = _Model()
    worker = _worker(tmp_path, model, store, None)
    asyncio.run(worker.analyze(AnalysisTask(question="one")))
    first = len(model.sent)
    asyncio.run(worker.analyze(AnalysisTask(question="two")))
    assert len(model.sent[first]) == 2, "system + task: nothing to continue without a ledger"


def test_a_resume_seeds_from_the_ledger_rebuild_of_the_kept_turn(tmp_path, monkeypatch):
    """B8: the kept turn is rebuilt from the ledger, so a resume re-sends what the ledger holds,
    and the loop continues on the rebuilt list itself (not the list kept in memory)."""
    worker, model, store, monitor = _setup(tmp_path)
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    original = worker._kept_turn["messages"]
    kept = list(original)
    rebuilds: list[list] = []
    real = history.conversation

    def spy(*args, **kwargs):
        rebuilds.append(real(*args, **kwargs))
        return rebuilds[-1]

    monkeypatch.setattr(history, "conversation", spy)
    worker.run_control.add_steer(KEY, "one more question", sender="analyst_lead", kind="message")
    model.extra_first = [ModelResponse(tool_calls=[ToolCall(id="z", name="turn_end", arguments={})])]
    before = len(model.sent)
    asyncio.run(worker.resume())
    sent = model.sent[before]
    assert [history.encode_message(m) for m in sent[: len(kept)]] == [
        history.encode_message(m) for m in kept
    ]
    assert sent[-1].content.endswith("one more question") or "one more question" in sent[-1].content
    assert len(rebuilds) == 1 and rebuilds[0] is not None
    assert worker._kept_turn["messages"] is rebuilds[0], "the loop ran on the ledger's rebuild"
    assert worker._kept_turn["messages"] is not original



def test_the_leads_next_cycle_continues_its_session(tmp_path):
    """Human ruling (board B1, 2026-10-18): every graph role is one session across its turns."""
    from auto_research.agents.react import ReadOnlyToolProvider
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.schemas import AnalystReport

    class _LeadModel:
        def __init__(self):
            self.config = SimpleNamespace(model="fake-lead")
            self.sent: list[list] = []
            self.n = 0

        async def call(self, messages, **kwargs):
            self.sent.append(list(messages))
            self.n += 1
            if self.n % 2 == 1:
                return ModelResponse(tool_calls=[ToolCall(
                    id=f"e{self.n}", name="write_report",
                    arguments={"content": "# summary\nthe workers agree\n# status\nsucceeded\n"
                                          "# rework\nfalse\n# rationale\nconsistent\n"
                                          "# findings\n- they agree\n"})])
            return ModelResponse(tool_calls=[ToolCall(id=f"t{self.n}", name="turn_end",
                                                      arguments={})])

    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _LeadModel()
    lead = AnalystLeadAgent(model=model, prompt="lead", monitor=monitor)
    lead.agent_key = "analyst_lead"
    lead.report_dir = tmp_path / "runs" / "r1" / "analyst_lead"
    lead.report_dir.mkdir(parents=True)
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    report = AnalystReport(summary="w", findings=["f"], status="succeeded")
    first = asyncio.run(lead.run_cycle(AnalysisTask(question="first question"), [report]))
    assert first.report is not None and first.report.status == "succeeded"
    second_start = len(model.sent)
    asyncio.run(lead.run_cycle(AnalysisTask(question="second question"), [report]))
    seed = model.sent[second_start]
    users = [m.content for m in seed if isinstance(m, ModelMessage) and m.role == "user"]
    assert "first question" in users[0] and "second question" in users[-1]
    turns = list(history.turns_for(monitor.jsonl_path, "analyst_lead").values())
    assert len(turns) == 2 and turns[1].calls[0].messages == seed


def test_a_file_planted_in_the_agent_folder_never_reaches_a_seed(tmp_path):
    """F22's concern, restated now that memory.md is gone: the history comes from the ledger,
    which no role can write, so nothing an agent's shell writes into its folder is read back."""
    worker, model, store, monitor = _setup(tmp_path)
    (worker.report_dir / "memory.md").write_text("PLANTED prior results\n")
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    asyncio.run(worker.analyze(AnalysisTask(question="again")))
    assert not any("PLANTED" in json.dumps(history.encode_message(m))
                   for call in model.sent for m in call)


def test_the_reviewer_continues_and_is_handed_only_this_turns_report(tmp_path):
    """Ruling B7: with the session carrying earlier turns, the payload carries only what arrived."""
    from auto_research.agents.react import ReadOnlyToolProvider
    from auto_research.agents.roles import ReviewerAgent
    from auto_research.schemas import ResearchRequest
    from auto_research.schemas.literature import LiteratureReport

    class _ReviewerModel:
        def __init__(self):
            self.sent: list[list] = []
            self.n = 0

        async def call(self, messages, **kwargs):
            self.sent.append(list(messages))
            self.n += 1
            if self.n % 2 == 1:
                return ModelResponse(tool_calls=[ToolCall(
                    id=f"e{self.n}", name="write_report",
                    arguments={"content": "# assign_to\nlibrarian\n# rationale\nneed more\n"
                                          "# follow_up\nread further\n"})])
            return ModelResponse(tool_calls=[ToolCall(id=f"t{self.n}", name="turn_end",
                                                      arguments={})])

    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _ReviewerModel()
    reviewer = ReviewerAgent(model=model, prompt="reviewer", monitor=monitor)
    reviewer.agent_key = "reviewer"
    reviewer.report_dir = tmp_path / "reviewer"
    reviewer.receipts = ReceiptStore.for_run(tmp_path / "runs" / "r1")
    reviewer.tools = ReadOnlyToolProvider(roots=[tmp_path], include_open_paper=False)
    request = ResearchRequest(question="what is the spin")
    budgets = {"librarian": 3, "analysts": 1}
    first = asyncio.run(reviewer.review(request, budgets=budgets))
    lit = [LiteratureReport(summary=f"pass {i}", findings=[], status="failed") for i in (1, 2)]
    start = len(model.sent)
    asyncio.run(reviewer.review(request, arrived="librarian", literature=lit, previous=[first],
                                budgets=budgets))
    seed = model.sent[start]
    payload = json.loads(seed[-1].content)
    assert [r["summary"] for r in payload["literature_reports"]] == ["pass 2"]
    assert payload["your_earlier_reports"] == []
    assert any("what is the spin" in m.content for m in seed[:-1]
               if isinstance(m, ModelMessage)), "the first turn is in the history"


def test_an_unreadable_history_starts_the_seat_fresh_and_says_so(tmp_path):
    """Found by review: a history that does not rebuild must cost the context, not the run."""
    worker, model, store, monitor = _setup(tmp_path)
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    with monitor.jsonl_path.open("a", encoding="utf-8") as fh:  # a part naming no started turn
        fh.write(json.dumps({"run_id": "r1", "actor": KEY, "event": "x",
                             "data": {"history": {"part": "user", "turn": "ghost"}}}) + "\n")
    start = len(model.sent)
    report = asyncio.run(worker.analyze(AnalysisTask(question="again")))
    assert report.status == "succeeded"
    assert len(model.sent[start]) == 2, "system + task"
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    assert any(r["event"].endswith("history.unreadable") and r["level"] == 1 for r in rows)


def test_another_runs_rows_in_a_reused_ledger_are_not_this_seats_history(tmp_path):
    worker, model, store, monitor = _setup(tmp_path)
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    monitor.run_id = "r2"                       # the same file, a new run
    start = len(model.sent)
    asyncio.run(worker.analyze(AnalysisTask(question="again")))
    assert len(model.sent[start]) == 2




def test_a_resume_reads_its_turn_from_the_ledger_and_flags_a_divergence(tmp_path):
    """B8: the kept turn is resumed from its ledger rebuild; a kept list that no longer matches
    the record is used as is and the divergence is recorded at level 1."""
    worker, model, store, monitor = _setup(tmp_path)
    asyncio.run(worker.analyze(AnalysisTask(question="fit it")))
    kept = worker._kept_turn
    original = kept["messages"]
    worker.run_control.add_steer(KEY, "one more", sender="analyst_lead", kind="message")
    model.extra_first = [ModelResponse(tool_calls=[ToolCall(id="z1", name="turn_end", arguments={})])]
    asyncio.run(worker.resume())
    assert worker._kept_turn["messages"] is not original, "the rebuilt list became the kept one"
    # Now make memory disagree with the record.
    worker._kept_turn["messages"].append(ModelMessage(role="user", content="not in the ledger"))
    worker.run_control.add_steer(KEY, "again", sender="analyst_lead", kind="message")
    model.extra_first = [ModelResponse(tool_calls=[ToolCall(id="z2", name="turn_end", arguments={})])]
    asyncio.run(worker.resume())
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    assert any(r["event"].endswith("history.diverged") and "differs" in str(r["data"])
               for r in rows)
    # And the divergent resume was recorded whole, so the seat's history still rebuilds.
    turns = list(history.turns_for(monitor.jsonl_path, KEY).values())
    assert len(turns) == 3 and "not in the ledger" in json.dumps(
        [history.encode_message(m) for m in turns[-1].messages])
