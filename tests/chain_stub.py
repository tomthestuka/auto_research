"""Stub the authoring chain's coding SESSION, leaving the driver above it real.

A live test of the lead's cycle is a test of the loop — what it briefs, how it reacts to a link
that fails, when it decides to stop. The coder underneath costs $27-45 per link, so it is
stubbed; everything above it runs for real.

**The seam moved down on 2026-09-13, and that is the whole point of this file.** It used to
replace ``_ChainDriver._open`` and ``_close_current`` outright, which meant no test ever executed
the mark, the compaction, or the driver's own advance — the fake `_open` never even assigned
``driver.mark``, so it stayed ``0`` from ``__init__`` and every assertion about where the
conversation was cut was really an assertion about the fake. A mark recorded one message too
early then reached production with a green suite and killed a run on its first committed link.

So the double is now the *session*: `_ChainDriver._build_session` is the one method a test
replaces, because building, connecting and driving a real coder is the only expensive part.
`_open`, `_close_current`, `advance`, the mark and the compaction all run as production runs
them. One definition, here, because several tests reach for it and a stub that drifts from the
driver it replaces measures a topology production does not have.
"""

from __future__ import annotations

from typing import Any


class StubSession:
    """The surface `_ChainDriver` uses of an authoring session, and nothing else.

    Kept deliberately narrow: every attribute here is one the driver actually reads, so a driver
    that grows a new dependency fails loudly against this double instead of silently reading a
    ``Mock``'s auto-attribute.
    """

    def __init__(self, spec: Any, *, status: str, sha: str):
        self.tool_spec = spec
        self.initial_message = f"Brief for {spec.proposed_tool_name}"
        self.branch = "ai"
        self.last_commit_sha = sha if status == "committed" else None
        # `_authoring_result` reads these to explain a failure. Left as None, which is the
        # "lead concluded authoring without a mergeable tool" case — a test wanting a gate or
        # review rejection sets them on the returned session.
        self.last_gate = None
        self.last_review = None
        self.submits_used = 1
        self.submit_cap = 5
        self.connected = False
        self.closed = False
        self.driven: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def drive(self, message: str) -> str:
        self.driven.append(message)
        return "stubbed coder turn"

    async def aclose(self) -> None:
        self.closed = True

    def pending_changes(self) -> bool:
        return False


def stub_chain_driver(
    monkeypatch,
    *,
    status: str = "committed",
    sha: str = "0123456789ab",
    compactor: str | None = "The coder built it and the reviewer agreed.",
) -> list:
    """Replace the driver's session builder; return the specs it was asked to author.

    ``status`` is what every stubbed link reports — ``"committed"`` to exercise the happy chain,
    ``"failed"`` to exercise F9's continuation and the lead's decision to stop.

    A coding span is NOT injected here. In production the span accumulates on later turns, after
    the link is open, so a test that wants one appends to ``driver.messages`` between advances —
    which is also the only way its position relative to the mark is the real one.
    """
    from auto_research.agents.roles import _ChainDriver

    driven: list[Any] = []

    async def _fake_build(self, spec):
        driven.append(spec)
        return StubSession(spec, status=status, sha=sha)

    monkeypatch.setattr(_ChainDriver, "_build_session", _fake_build)
    # The link now closes through the real `_close_current`, which summarises the coding span.
    # There is no span worth summarising behind a stubbed session, so the summary is canned by
    # default rather than spending a live call on nothing. Pass ``compactor=None`` to exercise
    # the real one.
    if compactor is not None:
        stub_compactor(monkeypatch, compactor)
    return driven


def stub_compactor(monkeypatch, text: str = "It built the thing and the reviewer agreed.") -> None:
    """Answer the compactor without a model call.

    Patched at the driver's own narrative method rather than at the registry, so a test that
    wants the deterministic-fallback path can simply not call this and let the resolve fail.
    """
    from auto_research.agents.roles import _ChainDriver

    async def _fake_narrative(self, span):
        return text

    monkeypatch.setattr(_ChainDriver, "_narrative_for", _fake_narrative)
