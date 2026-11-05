"""D1c — `run_research` closes the human-channel connector at a run's normal end and on a failed
launch, last in its teardown (leaf to root), as a /stop's cleanup does.

Runs the real `run_research` on the hermetic wiring the stop tests use (`tests.test_react_ceiling`):
no model is called; the reviewer is a stand-in that reports to the human at once, and the
connector is a stand-in recording what reached it.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.test_react_ceiling import _hermetic_run


class _Connector:
    def __init__(self):
        self.closed = 0
        self.queries = []

    async def __call__(self, query):
        from auto_research.schemas import HumanResponse

        self.queries.append(query)
        return HumanResponse(query_id=query.id, answer="done")

    async def aclose(self):
        self.closed += 1


def _run(tmp_path, connector):
    from auto_research import run_research
    from auto_research.schemas import ResearchRequest, ReviewerReport
    from auto_research.schemas.review import ReviewerSection

    class Reviewer:
        async def review(self, request, **_):
            return ReviewerReport(rationale="r", assign_to="human", title="T", abstract="A",
                                  sections=[ReviewerSection(heading="H", body="B")],
                                  key_findings=["K"])

    class Never:
        last_receipts: dict = {}

        async def research(self, request, *, follow_up=None):
            raise AssertionError("never routed to")

        async def run(self, workers, lead, task):
            raise AssertionError("never routed to")

    return asyncio.run(asyncio.wait_for(run_research(
        ResearchRequest(question="q"), reviewer=Reviewer(), io_agent=Never(), retrieval=None,
        workers=[], lead=object(), consensus=Never(), max_iterations=1, chatter=10,
        monitor_jsonl=tmp_path / "monitor.jsonl", report_formats=[], human_handler=connector,
    ), timeout=30))


def test_a_finished_run_closes_its_connector_once(tmp_path, monkeypatch):
    _hermetic_run(tmp_path, monkeypatch)
    connector = _Connector()
    _run(tmp_path, connector)
    assert connector.queries and connector.closed == 1


def test_a_failed_launch_closes_its_connector(tmp_path, monkeypatch):
    _hermetic_run(tmp_path, monkeypatch)

    async def _refused(*, human_handler=None, monitor=None):
        raise RuntimeError("the human-response channel did not start")

    monkeypatch.setattr("auto_research.graph.runtime.run_preflight", _refused)
    connector = _Connector()
    with pytest.raises(RuntimeError, match="did not start"):
        _run(tmp_path, connector)
    assert connector.closed == 1 and not connector.queries


def test_a_run_that_fails_still_closes_its_connector(tmp_path, monkeypatch):
    from auto_research import run_research
    from auto_research.schemas import ResearchRequest

    _hermetic_run(tmp_path, monkeypatch)

    class FailingReviewer:
        async def review(self, request, **_):
            raise RuntimeError("the reviewer broke")

    class Never:
        last_receipts: dict = {}

        async def research(self, request, *, follow_up=None):
            raise AssertionError("never routed to")

        async def run(self, workers, lead, task):
            raise AssertionError("never routed to")

    connector = _Connector()
    with pytest.raises(RuntimeError, match="the reviewer broke"):
        asyncio.run(asyncio.wait_for(run_research(
            ResearchRequest(question="q"), reviewer=FailingReviewer(), io_agent=Never(),
            retrieval=None, workers=[], lead=object(), consensus=Never(), max_iterations=1,
            chatter=10, monitor_jsonl=tmp_path / "monitor.jsonl", report_formats=[],
            human_handler=connector,
        ), timeout=30))
    assert connector.closed == 1


def test_a_connector_with_a_plain_close_is_closed(tmp_path, monkeypatch):
    _hermetic_run(tmp_path, monkeypatch)

    class _SyncClose(_Connector):
        aclose = None

        def close(self):
            self.closed += 1

    connector = _SyncClose()
    _run(tmp_path, connector)
    assert connector.closed == 1
