"""Stub the authoring chain's SESSIONS while leaving the lead's loop above them real.

A live test of the lead's cycle is a test of the loop — what it briefs, how it reacts to a link
that fails, when it decides to stop. The coder underneath costs $27–45 per link, so it is stubbed;
everything above it (the brief, the chain advancing on a tool result, `turn_end`'s validation, the
lead deciding when to write its report) runs for real.

A7 moved the per-link lifecycle out of `AnalystLeadAgent._drive_one_link` and into
`_ChainDriver`, so the seam moved with it. One definition, here, because three live tests reach
for it and a stub that drifts from the driver it replaces measures a topology production does not
have — the 2026-09-08 wiring lesson at a smaller scale.
"""

from __future__ import annotations

from typing import Any


def stub_chain_driver(monkeypatch, *, status: str = "committed", sha: str = "0123456789ab") -> list:
    """Replace the driver's session open/close; return the list of specs it was asked to author.

    ``status`` is what every stubbed link reports — ``"committed"`` to exercise the happy chain,
    ``"failed"`` to exercise F9's continuation and the lead's decision to stop.
    """
    from auto_research.agents.roles import _ChainDriver
    from auto_research.schemas import MergeFlowResult

    driven: list[Any] = []

    async def _fake_open(self, spec):
        driven.append(spec)
        self.session = object()
        self.spec = spec
        self.cycle.session_open = True

    async def _fake_close(self):
        if self.session is None:
            return
        spec, self.session, self.spec = self.spec, None, None
        self.cycle.session_open = False
        self.outcome.authored.append(
            MergeFlowResult(
                tool_spec_id=spec.id,
                status=status,
                branch="ai",
                commit_sha=sha if status == "committed" else None,
                reason="stubbed coding session (the loop above it is under test)",
            )
        )
        if status == "committed":
            self.outcome.committed += 1

    monkeypatch.setattr(_ChainDriver, "_open", _fake_open)
    monkeypatch.setattr(_ChainDriver, "_close_current", _fake_close)
    return driven
