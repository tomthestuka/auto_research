"""The author-harness interface the authoring flow drives.

An :class:`AuthoringSession` needs exactly three things from a coder: bring the author up, send
it one instruction and hand back what it said, and take it down. Everything else about a harness
(how it is configured, how its permission gate works, what its event stream looks like, how its
spend is priced) belongs to the backend and is invisible here.

That narrowness is what makes a second harness a drop-in rather than a fork of the flow: the
gate, the review, the commit, the per-attempt base ref and the failure reset are all defined over
the working tree, not over the process that edited it.

Two obligations every backend carries, because they are what the run's record depends on:

* **Stream to the ledger** while a turn runs, under the harness-independent ``coder.*`` event
  names in :mod:`auto_research.sandbox.coder.events`, so a run reads the same either way.
* **Book its own spend** on the run's cost meter. Each harness reports usage differently (dollars
  in one case, token counts in the other), and converting that into one recorded figure is the
  backend's job.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CoderBackend(Protocol):
    """One warm author session over one working tree."""

    async def connect(self) -> None:
        """Bring the author process up, ready to receive instructions."""

    async def drive(self, message: str) -> str:
        """Send one instruction, drain the response, and return the author's text."""

    async def disconnect(self) -> None:
        """Take the author process down. Called from a ``finally`` and from interrupt cleanup,
        so it tolerates being called on a session that never connected."""
