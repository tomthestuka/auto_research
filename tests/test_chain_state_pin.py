"""C4: every cut keeps what code knows — the seat's exchange with the human, word for word, and the
lead's chain — and the summariser never writes it.

Run polevo-20261104 lost two human rulings, a committed link and a saved patch to the lead's
coding-span cut (``_ChainDriver.apply_cut``), whose only record of them was a summariser's
paraphrase. Driven through the lead's real cycle (``tests/test_chain_advance.py``'s wiring, the
run's real channel with a scripted human) and a worker's real loop at a segment boundary
(``tests/test_compaction.py``'s near-the-window member and scripted summariser).
"""

from __future__ import annotations

import asyncio

from auto_research.agents import history
from auto_research.agents.roles import AnalystLeadAgent, AnalystWorkerAgent
from auto_research.models import ModelMessage
from auto_research.monitor import RunMonitor
from auto_research.run_control import RunControl
from auto_research.schemas import HumanResponse
from tests.test_chain_advance import CHAIN, FINISH, SUBMIT, _cycle, _link
from tests.test_compaction import KEY, _Long, _on, _Tools, _wire_summariser

QUESTION = "A pointing has no _rej event file. Should it be dropped from the study?"
RULING = "When no _rej is found, run the reduction first."
NUDGE = "The human's texts are direction for your work: follow each as the ruling it is"


async def _human(query):
    return HumanResponse(query_id=query.id, answer=RULING)


def _texts(messages):
    out = []
    for m in messages:
        out.append(str(getattr(m, "content", "") or ""))
        out.extend(str(p.content) for p in getattr(m, "results", None) or ())
    return out


def _lead_cycle(tmp_path, monkeypatch, script, **kw):
    control = RunControl()
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    result = _cycle(tmp_path, monkeypatch, script, verdicts=[True, True], monitor=monitor,
                    run_control=control, seat=True, human=_human, **kw)
    return (*result, monitor)


ASK = [("message", {"to": "human", "text": QUESTION})]
WAIT = [("wait_for_reply", {"seconds": 5})]


def test_the_coding_span_cut_restates_the_exchange_and_the_chain(tmp_path, monkeypatch):
    """The human's ruling arrives while link 1 runs; when link 1 commits, its span is cut, and
    the answer that replaces it carries the question and the ruling word for word, labelled as
    direction, and each brief's state — as the ledger's rebuild of the turn does."""
    outcome, model, built, queries, reviews, repo, store, monitor = _lead_cycle(
        tmp_path, monkeypatch, [[CHAIN], ASK, WAIT, [SUBMIT], [SUBMIT], *FINISH],
    )
    assert model.lead.human_exchange == [("to", QUESTION), ("from", RULING)]
    after_cut = _texts(model.seen and model.last_messages)
    (restated,) = [t for t in after_cut if "Your coding chain this turn" in t and RULING in t]
    sha = outcome.authored[0].commit_sha[:10]
    assert f"- you, to the human: {QUESTION}" in restated
    assert f"- the human, to you: {RULING}" in restated and NUDGE in restated
    assert f"- brief spec1 (tool_0): committed {sha}; version" in restated
    assert "locked (its link committed), receipt analyst_lead:r" in restated
    # The ledger rebuild replays the rewrite with the same restated text.
    (turn,) = history.turns_for(monitor.jsonl_path, "analyst_lead").values()
    assert any(restated == t for t in _texts(turn.messages))


def test_the_state_reaches_the_next_cycle_and_a_seat_in_a_new_process(tmp_path, monkeypatch):
    """After the cycle, the lead's pin names its briefs as they ended; a lead seat built anew on
    the same ledger (a resumed process) starts from the same exchange."""
    outcome, model, built, queries, reviews, repo, store, monitor = _lead_cycle(
        tmp_path, monkeypatch, [[CHAIN], ASK, WAIT, [SUBMIT], [SUBMIT], *FINISH],
    )
    pin = model.lead._pin_state()
    assert "Your briefs of earlier cycles, as they ended:" in pin
    assert f"brief spec2 (tool_1): committed {outcome.authored[1].commit_sha[:10]}" in pin
    fresh = AnalystLeadAgent(model=model, prompt="lead", monitor=monitor)
    fresh.agent_key = "analyst_lead"
    assert fresh.human_exchange == [("to", QUESTION), ("from", RULING)]
    assert f"- the human, to you: {RULING}" in fresh._pin_state()


def test_a_segment_boundary_after_the_chain_restates_it_in_the_pin(tmp_path, monkeypatch):
    """The lead's turn nears its window after the chain finished: the pin of the new segment
    carries the exchange and the chain, and the summariser was handed neither block."""
    from auto_research.models.types import RoleModelConfig

    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    one = ("author_chain", {"first_link": _link(0), "additional_links": [], "rationale": "r"})
    look = [("list_dir", {"path": str(tmp_path)})]

    def near_the_window(lead, model):
        model.config = RoleModelConfig(name="m", provider="deepseek", model="m",
                                       context_tokens=100_000)
        model.prompt_tokens = 95_000

    outcome, model, built, queries, reviews, repo, store, monitor = _lead_cycle(
        tmp_path, monkeypatch, [[one], ASK, WAIT, [SUBMIT], look, look, look, look, look, *FINISH],
        setup=near_the_window,
    )
    pins = [t for sent in [model.last_messages] for t in _texts(sent)
            if t.startswith("The earlier part of this turn was condensed")]
    assert pins, "a new segment started"
    assert RULING in pins[-1] and NUDGE in pins[-1]
    assert f"- brief spec1 (tool_0): committed {outcome.authored[0].commit_sha[:10]}" in pins[-1]
    assert not any("Your coding chain this turn" in r or NUDGE in r for r in summariser.read)


def test_a_workers_exchange_survives_its_segment_boundary(tmp_path, monkeypatch):
    """Every unit keeps its exchange (ruling A): a worker told by the human mid-turn reads the
    human's words, word for word and as direction, in the pin of its next segment."""
    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _Long("deepseek", calls=9)
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = KEY
    worker.run_control = control = RunControl()
    control.add_steer(KEY, "Use the 2-8 keV band for every fit.", sender="human", kind="steer")
    seed = [ModelMessage(role="system", content="w"), ModelMessage(role="user", content="TASK")]
    asyncio.run(worker._react_loop(
        seed_messages=seed, tools=_Tools(), action="analyze", cap=20,
        terminal_tools=frozenset({"turn_end"}), task_receipt="input:h1",
    ))
    assert worker.human_exchange == [("from", "Use the 2-8 keV band for every fit.")]
    pins = [m.content for sent in model.sent for m in sent
            if isinstance(m, ModelMessage) and m.content.startswith("The earlier part of this turn")]
    assert pins and "- the human, to you: Use the 2-8 keV band for every fit." in pins[-1]
    assert NUDGE in pins[-1]
    assert not any(NUDGE in r for r in summariser.read), "the summariser never writes the state"


def test_a_condensed_seed_restates_the_earlier_turns_exchange(tmp_path, monkeypatch):
    """At a turn boundary the earlier turns are condensed (``_history_seed``): the new turn's
    opening carries the exchange with the human word for word, after the summary, and the
    summariser was handed none of it as restated state."""
    from auto_research.models.types import RoleModelConfig
    from auto_research.schemas import AnalysisTask
    from tests.test_session_history import _setup

    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    worker, model, store, monitor = _setup(tmp_path)
    model.config = RoleModelConfig(name="m", provider="anthropic", model="fake", context_tokens=200)
    worker.run_control = control = RunControl()
    control.add_steer(worker.agent_key, "Report the 90% upper limits only.", sender="human",
                      kind="steer")
    asyncio.run(worker.analyze(AnalysisTask(question="one")))
    asyncio.run(worker.analyze(AnalysisTask(question="two")))
    (turn2,) = list(history.turns_for(monitor.jsonl_path, worker.agent_key).values())[-1:]
    opening = next(m.content for m in turn2.messages
                   if isinstance(m, ModelMessage) and m.content.startswith(history.TRANSCRIPT_MARK))
    assert "- the human, to you: Report the 90% upper limits only." in opening and NUDGE in opening
    assert not any(NUDGE in r for r in summariser.read)
    # C4b: the turn-boundary summariser read the placeholder where the human spoke.
    assert any(HUMAN_PLACEHOLDER in r for r in summariser.read)
    assert not any("90% upper limits" in r for r in summariser.read)


def test_the_next_cycle_opens_with_the_exchange_and_the_earlier_chain(tmp_path, monkeypatch):
    """Review of C4: the lead's next cycle opens on its earlier turn's transcript (not condensed:
    a long answer is shortened to its index line), followed whole by what code restates — the
    human's ruling and the briefs as they ended."""
    from auto_research.schemas import AnalysisTask, AnalystReport
    from auto_research.schemas.toolspec import AnalystToolRequest
    from tests.test_chain_advance import _coder_member, _wire

    control = RunControl()
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    lead, model, built, queries, reviews, repo, store = _wire(
        tmp_path, monkeypatch, [[CHAIN], ASK, WAIT, [SUBMIT], [SUBMIT], *FINISH, *FINISH],
        verdicts=[True, True], monitor=monitor, run_control=control, seat=True, human=_human,
    )
    worker = AnalystReport(summary="w0", tool_request=AnalystToolRequest(
        science_context="c", expected_behavior="b", proposed_tool_name="t",
        validation_data=[{"mission": "IXPE", "obsid": "01002901"}],
    ))
    for _ in range(2):
        asyncio.run(lead.run_cycle(
            AnalysisTask(question="q"), [worker], sources=["analyst_worker_0"], rounds_left=1,
            author_budget=3, toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
            coder=_coder_member("fake-model"),
        ))
    first_of_cycle_two = model.sent[-2]           # write_report, then turn_end
    opening = _texts(first_of_cycle_two)[1]
    assert opening.startswith(history.TRANSCRIPT_MARK)
    assert f"- the human, to you: {RULING}" in opening
    assert "Your briefs of earlier cycles, as they ended:" in opening
    assert opening.rstrip().endswith("[End of what the runtime restates.]")


def test_a_summariser_reads_around_each_restated_block_never_inside_it():
    """Review of C4: what follows a restated block in a longer text (a transcript holding an
    earlier pin) reaches the summariser; the block itself does not."""
    from auto_research.agents import compaction

    text = f"before\n{compaction.restated('the human, to you: ' + RULING)}\nafter"
    rendered = compaction.render_middle([ModelMessage(role="user", content=text)], [0])
    assert "before" in rendered and "after" in rendered and RULING not in rendered


def test_a_text_put_back_unshown_is_kept_once_when_it_is_shown(tmp_path):
    """Review of C4: a human text drained in the batch that files turn_end goes back to the inbox
    unshown (D5); the exchange keeps it when the next turn shows it, once."""
    from auto_research.agents.react import ToolOutcome
    from auto_research.models import ModelResponse, ToolCall

    control = RunControl()

    class Tools:
        def tool_definitions(self):
            return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                    for n in ("note", "turn_end")]

        async def execute(self, name, args):
            if name == "note":
                control.add_steer(KEY, "Keep the 2-8 keV band.", sender="human", kind="steer")
            return ToolOutcome(text="ok")

    class Model:
        def __init__(self):
            self.script = [[("note", {}), ("turn_end", {})], [("turn_end", {})]]

        async def call(self, messages, **kwargs):
            batch = self.script.pop(0)
            return ModelResponse(tool_calls=[ToolCall(id=f"{n}{len(self.script)}", name=n,
                                                      arguments=a) for n, a in batch])

    worker = AnalystWorkerAgent(model=Model(), prompt="w")
    worker.agent_key, worker.run_control = KEY, control
    for _ in range(2):
        asyncio.run(worker._react_loop(
            seed_messages=[ModelMessage(role="user", content="TASK")], tools=Tools(),
            action="analyze", cap=5, terminal_tools=frozenset({"turn_end"}),
        ))
    assert worker.human_exchange == [("from", "Keep the 2-8 keV band.")]


# -- C4b: summarisers read a placeholder where the human spoke; a new process restores the rest --

from auto_research.agents.compaction import HUMAN_PLACEHOLDER  # noqa: E402


def test_every_summariser_reads_a_placeholder_where_the_human_spoke(tmp_path, monkeypatch):
    """Ruling C4b: the segment summariser and the coding-span compactor are handed a placeholder
    for each human text, which code restates after the summary."""
    from auto_research.agents.roles import _render_span

    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    worker = AnalystWorkerAgent(model=_Long("deepseek", calls=9), prompt="w", monitor=monitor)
    worker.agent_key = KEY
    worker.run_control = control = RunControl()
    control.add_steer(KEY, "Use the 2-8 keV band for every fit.", sender="human", kind="steer")
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="system", content="w"), ModelMessage(role="user", content="TASK")],
        tools=_Tools(), action="analyze", cap=20, terminal_tools=frozenset({"turn_end"}),
    ))
    assert summariser.read and all("2-8 keV band" not in r for r in summariser.read)
    assert any(HUMAN_PLACEHOLDER in r for r in summariser.read)
    span = [ModelMessage(role="user", content=f"[Reply from the human] {RULING}\n\n(Answer the "
                                              "sender with message(to=\"human\") when an answer helps.)")]
    rendered = _render_span(span)
    assert HUMAN_PLACEHOLDER in rendered and RULING not in rendered


def test_a_new_process_restores_the_steers_and_the_earlier_chain(tmp_path, monkeypatch):
    """Review of C4: a seat built anew on the run's ledger starts with the human's /steer texts
    too, and the lead with its earlier cycles' chain lines (recorded when each cycle ended)."""
    outcome, model, built, queries, reviews, repo, store, monitor = _lead_cycle(
        tmp_path, monkeypatch, [[CHAIN], ASK, WAIT, [SUBMIT], [SUBMIT], *FINISH],
        setup=lambda lead, m: lead.run_control.add_steer(
            "analyst_lead", "Keep every link's figures as PNG.", sender="human", kind="steer"),
    )
    original = model.lead.human_exchange
    assert ("from", "Keep every link's figures as PNG.") in original
    fresh = AnalystLeadAgent(model=model, prompt="lead", monitor=monitor)
    fresh.agent_key = "analyst_lead"
    assert fresh.human_exchange == original, "the ledger holds the same exchange, steers included"
    pin = fresh._pin_state()
    assert "Your briefs of earlier cycles, as they ended:" in pin
    assert f"brief spec1 (tool_0): committed {outcome.authored[0].commit_sha[:10]}" in pin


def test_a_close_that_raises_still_releases_the_chain_driver(tmp_path, monkeypatch):
    """Review of C4: the pin never restates a dead driver's chain."""
    import pytest

    from auto_research.agents.roles import _ChainDriver

    real = _ChainDriver.close

    async def close(self):
        await real(self)
        raise RuntimeError("the close failed")

    monkeypatch.setattr(_ChainDriver, "close", close)
    holder = {}
    with pytest.raises(RuntimeError, match="the close failed"):
        _lead_cycle(tmp_path, monkeypatch, [[CHAIN], [SUBMIT], [SUBMIT], *FINISH],
                    setup=lambda lead, m: holder.setdefault("lead", lead))
    lead = holder["lead"]
    assert lead._chain_driver is None and "Your coding chain this turn" not in (lead._pin_state() or "")
    assert lead.tools.cycle is None and lead.tools.report_workspace is None, "the provider is restored"


def test_a_nested_human_text_is_replaced_whole_even_when_it_looks_like_a_transcript():
    """Review of C4b: inside an earlier turns' transcript, a human text holding a line that looks
    like a transcript entry is replaced whole, and the entries around it are kept."""
    from auto_research.agents.compaction import for_summariser
    from auto_research.run_control import InboxEntry

    steer = InboxEntry("human", "rule A\n--- note\nrule B", kind="steer").render()
    text = (f"{history.TRANSCRIPT_MARK} earlier]\n--- to you ---\n{steer}\n--- you ---\nI fit it."
            "\n← result line")
    read = for_summariser(text)
    assert HUMAN_PLACEHOLDER in read and "rule A" not in read and "rule B" not in read
    assert "--- you ---\nI fit it." in read and "← result line" in read


def test_a_link_the_run_ended_reads_as_ended_with_the_run(monkeypatch):
    """Review of C4b: a link still open when a stop ends the run is told as such in the chain
    lines recorded for later cycles, never as merely filed."""
    from tests.test_lead_forms import _driver_with

    driver, messages, aio = _driver_with(monkeypatch, ["committed"])
    aio.run(driver.advance())                      # link 1 opens
    control = RunControl()
    control.request_stop("human /stop")
    driver.lead.run_control = control
    aio.run(driver.close())                        # the run-ending close leaves it to the stop
    (line,) = driver.state_lines()
    assert "its coding session ended with the run; the stop's cleanup resets its work" in line


def test_a_cleanup_failure_never_replaces_the_cycles_own_failure(tmp_path, monkeypatch):
    """Review of C4b: the lead's model call fails, then the driver's close raises too: the
    model failure leaves run_cycle (the debate charges its credits), and the provider is restored."""
    import pytest

    from auto_research.agents.roles import ModelCallFailed, _ChainDriver
    from tests.test_chain_advance import _wire

    real = _ChainDriver.close

    async def close(self):
        await real(self)
        raise RuntimeError("the close failed")

    def fails(model):
        raise RuntimeError("upstream overloaded")

    monkeypatch.setattr(_ChainDriver, "close", close)
    lead, model, built, queries, reviews, repo, store = _wire(
        tmp_path, monkeypatch, [[CHAIN], fails], verdicts=[], monitor=RunMonitor(
            run_id="r1", jsonl_path=tmp_path / "monitor.jsonl"))
    from auto_research.schemas import AnalysisTask, AnalystReport
    from auto_research.schemas.toolspec import AnalystToolRequest
    from tests.test_chain_advance import _coder_member

    worker = AnalystReport(summary="w0", tool_request=AnalystToolRequest(
        science_context="c", expected_behavior="b", proposed_tool_name="t",
        validation_data=[{"mission": "IXPE", "obsid": "01002901"}],
    ))
    with pytest.raises(ModelCallFailed) as raised:
        asyncio.run(lead.run_cycle(
            AnalysisTask(question="q"), [worker], sources=["analyst_worker_0"], rounds_left=1,
            author_budget=3, toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
            coder=_coder_member("fake-model"),
        ))
    assert raised.value.cycle_outcome is not None
    assert lead.tools.cycle is None and lead.tools.report_workspace is None, "the provider is restored"
    assert lead._chain_driver is None

