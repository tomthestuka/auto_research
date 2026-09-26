"""Deterministic tests for step-8 agent role wrappers."""

import asyncio
import json

import pytest

from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    ReviewerAgent,
)
from auto_research.models import ModelResponse, ToolCall
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    SearchResult,
)


class FakeModel:
    """Stand-in model returning a fixed structured object via native structured output."""

    def __init__(self, content):
        self.obj = content
        self.messages = None
        self.images = None
        self.schema = None

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto", images=(), documents=(), cache_prefix=False):
        self.messages = messages
        self.images = images
        self.documents = documents
        self.schema = schema
        return ModelResponse(parsed=self.obj)


class FakeSource:
    async def search(self, query: str, *, limit: int = 10):
        assert query == "Cyg X-1 polarization"
        assert limit == 1
        return [
            SearchResult(
                source="arxiv",
                title="Cyg X-1 paper",
                url="https://example.org/cyg",
                abstract="A relevant paper.",
            )
        ]


def _user_payload(fake_model: FakeModel):
    # The user message is now just the task-data JSON (the output schema is
    # enforced by the provider, not injected into the prompt).
    return json.loads(fake_model.messages[1].content)


class _ScriptedModel:
    """Issues a fixed sequence of tool calls, then prose."""

    def __init__(self, script):
        self.script = list(script)
        self.messages = []
        self.tools = None
        self.turn = 0

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        self.messages = list(messages)
        self.tools = tools
        self.turn += 1
        if self.turn > len(self.script):
            return ModelResponse(content="done")
        step = self.script[self.turn - 1]
        batch = step if isinstance(step, list) else [step]  # a list is one turn's batch of calls
        return ModelResponse(tool_calls=[
            ToolCall(id=f"c{self.turn}_{i}", name=name, arguments=arguments)
            for i, (name, arguments) in enumerate(batch)
        ])


def _reviewer(tmp_path, script):
    from auto_research.agents.react import ReadOnlyToolProvider
    from auto_research.provenance import ReceiptStore

    fake = _ScriptedModel(script)
    agent = ReviewerAgent(model=fake, prompt="reviewer prompt")
    agent.report_dir = tmp_path / "reviewer"
    agent.receipts = ReceiptStore(tmp_path / "receipts")
    agent.tools = ReadOnlyToolProvider(roots=[tmp_path], include_open_paper=False)
    return agent, fake


def test_reviewer_writes_and_files_one_report_in_its_loop(tmp_path):
    from auto_research.agents.react import EDIT_REPORT_TOOL, TURN_END_TOOL

    agent, fake = _reviewer(tmp_path, [
        (EDIT_REPORT_TOOL, {"rationale": "The literature is adequate.", "assign_to": "analysts",
                            "follow_up": "Fit the data."}),
        (TURN_END_TOOL, {"summary": "dispatched"}),
    ])
    report = asyncio.run(agent.review(ResearchRequest(question="Check this."),
                                      budgets={"librarian": 1, "analysts": 1}))
    assert report.follow_up == "Fit the data." and report.assign_to == "analysts"
    assert fake.messages[0].content == "reviewer prompt"
    assert _user_payload(fake)["arrived"] == "request"
    names = {t["name"] for t in fake.tools}
    assert {"edit_report", "review_report", "turn_end", "read_paper"} <= names
    assert "open_paper" not in names
    assert (tmp_path / "reviewer" / "review1.jsonl").is_file()


def test_reviewer_turn_end_refuses_until_the_report_is_complete(tmp_path):
    from auto_research.agents.react import EDIT_REPORT_TOOL, TURN_END_TOOL

    agent, fake = _reviewer(tmp_path, [
        (TURN_END_TOOL, {"summary": "nothing written"}),
        (EDIT_REPORT_TOOL, {"rationale": "Adequate.", "assign_to": "analysts", "follow_up": ""}),
        (TURN_END_TOOL, {"summary": "no dispatch"}),
        (EDIT_REPORT_TOOL, {"rationale": "Adequate.", "assign_to": "analysts", "follow_up": "Fit it."}),
        (TURN_END_TOOL, {"summary": "ok"}),
    ])
    report = asyncio.run(agent.review(ResearchRequest(question="q"),
                                      budgets={"librarian": 1, "analysts": 1}))
    assert report.follow_up == "Fit it." and fake.turn == 5


def test_a_call_after_an_accepted_turn_end_in_the_same_batch_is_not_run(tmp_path):
    # Found 2026-09-25 by an adversarial review: an edit after turn_end in one batch replaced the
    # filed report with an unvalidated one. The shared loop answers it and runs nothing.
    from auto_research.agents.react import EDIT_REPORT_TOOL, TURN_END_TOOL

    agent, fake = _reviewer(tmp_path, [[
        (EDIT_REPORT_TOOL, {"rationale": "Adequate.", "assign_to": "analysts", "follow_up": "Fit it."}),
        (TURN_END_TOOL, {"summary": "ok"}),
        (EDIT_REPORT_TOOL, {"rationale": "PD is 9.99.", "assign_to": "librarian", "follow_up": "12.345"}),
    ]])
    report = asyncio.run(agent.review(ResearchRequest(question="q"),
                                      budgets={"librarian": 0, "analysts": 1}))
    assert report.assign_to == "analysts" and report.follow_up == "Fit it."
    assert fake.turn == 1


def test_a_reviewer_that_never_files_raises_rather_than_returning(tmp_path):
    agent, _ = _reviewer(tmp_path, [])
    with pytest.raises(Exception):
        asyncio.run(agent.review(ResearchRequest(question="q")))


def test_call_fails_loudly_when_no_structured_output():
    """No salvage: a provider that returns no structured output raises, never fabricates."""

    class _NullModel:
        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            return ModelResponse(parsed=None)

    agent = AnalystWorkerAgent(model=_NullModel(), prompt="p")
    with pytest.raises(ValueError):
        asyncio.run(agent.analyze(AnalysisTask(question="q")))


def test_call_fails_loudly_on_schema_mismatch():
    """No salvage: an off-schema structured result raises a validation error."""

    class _BadModel:
        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            return ModelResponse(parsed={"unexpected": "shape"})

    agent = AnalystWorkerAgent(model=_BadModel(), prompt="p")
    with pytest.raises(Exception):
        asyncio.run(agent.analyze(AnalysisTask(question="q")))


def test_analyst_worker_agent_exposes_toolkit_as_available_context_only():
    fake = FakeModel({"summary": "Analysis complete.", "findings": ["One finding."]})
    agent = AnalystWorkerAgent(model=fake, prompt="worker prompt")

    report = asyncio.run(agent.analyze(AnalysisTask(question="Reproduce a spectral result.")))

    assert report.summary == "Analysis complete."
    payload = _user_payload(fake)
    assert payload["available_toolkits"] == ["research_toolkit.xspec_utils"]
    assert "xspec_utils" not in fake.messages[0].content


def test_analyst_lead_agent_adjudicates_reports(tmp_path):
    """The lead's cycle: it writes its report with edit_report and ends its turn — no compose.

    The synthesis used to be a structured compose over one payload, then a form filed inside the
    loop. It is now the report itself (A3): the lead writes it and calls `turn_end`, which the
    harness validates. What this checks is unchanged in intent — the worker reports reach it, and
    the report it wrote is what comes back.
    """
    from auto_research.agents.react import (
        EDIT_REPORT_TOOL,
        TURN_END_TOOL,
        ReadOnlyToolProvider,
    )

    class FormsModel:
        """Writes a report, then files it."""

        def __init__(self):
            self.messages = []
            self.turn = 0

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.messages = list(messages)
            self.turn += 1
            script = [
                (EDIT_REPORT_TOOL, {
                    "summary": "Final synthesis.",
                    "findings": ["Workers agree."],
                    "status": "succeeded",
                }),
                (TURN_END_TOOL, {"summary": "done"}),
            ]
            if self.turn > len(script):
                return ModelResponse(content="done")
            name, arguments = script[self.turn - 1]
            return ModelResponse(
                tool_calls=[ToolCall(id=f"c{self.turn}", name=name, arguments=arguments)]
            )

    fake = FormsModel()
    agent = AnalystLeadAgent(model=fake, prompt="lead prompt")
    agent.report_dir = tmp_path
    agent.tools = ReadOnlyToolProvider(roots=[tmp_path], include_text_search=False)
    report = AnalystReport(summary="Worker result.")

    outcome = asyncio.run(agent.run_cycle(AnalysisTask(question="Synthesize."), [report]))

    assert outcome.filed == TURN_END_TOOL
    assert outcome.report.findings == ["Workers agree."]
    payload = _user_payload(fake)
    assert payload["worker_reports"][0]["summary"] == "Worker result."
