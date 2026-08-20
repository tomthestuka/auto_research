"""Placeholder adapter for a vendor reached only through a coder harness.

Some ``coder`` members name a vendor that has no chat adapter here: their model is driven by
an author harness (the Claude Agent SDK, the Codex app-server), which speaks to the vendor
itself and never passes through ``ModelHandle.call``. The registry still resolves the role, so
that it can report the member's model, provider and harness for stamps, decorrelation and the
launch checks (CLAUDE.md §6/§9.3).

Resolving such a member therefore yields this object instead of a real adapter. It answers the
one question the registry needs (which provider serves it) and raises with a specific message if
anything ever tries to hold a chat conversation with it, rather than failing later inside a
provider SDK with an opaque error.
"""

from typing import Any


class HarnessOnlyProvider:
    """A resolvable stand-in whose ``call`` states why there is no chat adapter."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def call(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            f"provider {self.provider!r} is reached only through a coder harness and has no "
            "chat adapter: its model is driven by the author harness named on the member's "
            "`harness` field, so it can back the `coder` role only. Give any chat role a member "
            "whose provider is anthropic, google or openai."
        )
