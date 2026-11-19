"""Fixtures every freeze test takes; ``tests/conftest.py``'s isolation fixtures apply as well."""

from __future__ import annotations

import asyncio

import pytest

from tests.invariants import drivers


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model calls are traced to no service: the scripted provider network is the only one."""
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


@pytest.fixture(scope="session")
def _shared_chains() -> dict:
    return {}


@pytest.fixture
def approved_chain(_shared_chains: dict, tmp_path_factory: pytest.TempPathFactory) -> drivers.Chain:
    """One approved coding link (:func:`drivers.run_chain` with its defaults: the gate passes, the
    co-lead approves, one commit), run once per session by the first test that takes it, under that
    test's isolation fixtures, and shared read-only. Take it as a test argument, so it is built
    before any patch of the test's own is active; read it and never change it (its run, receipts and
    actors' records are live objects). A test that varies the chain runs its own (``run_chain``)."""
    if "approved" not in _shared_chains:
        try:
            _shared_chains["approved"] = asyncio.run(drivers.run_chain(tmp_path_factory.mktemp("approved_chain")))
        except (Exception, drivers.ScriptError) as exc:
            _shared_chains["approved"] = exc
            raise
    chain = _shared_chains["approved"]
    if isinstance(chain, BaseException):
        raise RuntimeError("the shared approved chain failed in an earlier test") from chain
    return chain
