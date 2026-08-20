"""Serve ``ask_lead`` and ``ask_human`` to an author harness that connects over HTTP.

The Claude Agent SDK can host these two tools in-process, so its backend hands them straight to
the SDK. Codex takes MCP servers by command or by URL, and the handlers must not move out of this
process: ``ask_lead`` calls the live analyst-lead object mid-run, and ``ask_human`` reaches the
run's actual HITL connector. A subprocess server would need both proxied back, so instead the
server runs here and the author is given a localhost URL.

Bound to the loopback interface on an ephemeral port, and gated on a per-session bearer token
handed to the harness through an environment variable, so a co-tenant process on the same host
cannot pose as the author and ask the human questions.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any


_MESSAGE_SCHEMA_DESCRIPTION = (
    "Your question, in Markdown — the ask first, then any needed facts."
)


class EscalationServer:
    """A running Streamable-HTTP MCP endpoint exposing the two escalation tools.

    ``url`` and ``token_env`` are what a harness needs: the endpoint to connect to, and the name
    of the environment variable its process will read the bearer token from.
    """

    def __init__(
        self,
        ask_lead: Callable[..., Awaitable[str]],
        ask_human: Callable[..., Awaitable[str]],
        *,
        host: str = "127.0.0.1",
        token_env: str = "AR_ESCALATION_TOKEN",
    ) -> None:
        self._ask_lead = ask_lead
        self._ask_human = ask_human
        self._host = host
        self.token_env = token_env
        self.token = secrets.token_urlsafe(32)
        self.port: int | None = None
        self._server: Any | None = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("EscalationServer.url read before start()")
        return f"http://{self._host}:{self.port}/mcp"

    def env(self) -> dict[str, str]:
        """The environment entry the harness needs to authenticate."""
        return {self.token_env: self.token}

    def _build_app(self) -> Any:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("escalation")

        @mcp.tool(description="Ask the analyst lead a code/convention/which-lib-function question.")
        async def ask_lead(message: str) -> str:
            f"""{_MESSAGE_SCHEMA_DESCRIPTION}"""
            return await self._ask_lead(message)

        @mcp.tool(description="Escalate a genuine expert-science unknown to the real human.")
        async def ask_human(message: str) -> str:
            f"""{_MESSAGE_SCHEMA_DESCRIPTION}"""
            return await self._ask_human(message)

        app = mcp.streamable_http_app()
        return _with_bearer_auth(app, self.token)

    async def start(self) -> None:
        """Bind an ephemeral loopback port and serve until :meth:`aclose`."""
        import uvicorn

        config = uvicorn.Config(
            self._build_app(), host=self._host, port=0, log_level="warning", access_log=False
        )
        server = uvicorn.Server(config)
        self._server = server
        self._task = asyncio.create_task(server.serve())
        # uvicorn publishes its bound sockets once startup completes; port 0 means the kernel
        # chose it, so the URL is only knowable after that.
        while not getattr(server, "started", False):
            if self._task.done():  # startup failed — surface its exception rather than hang
                await self._task
                raise RuntimeError("escalation server exited during startup")
            await asyncio.sleep(0.01)
        socket = server.servers[0].sockets[0]
        self.port = socket.getsockname()[1]

    async def aclose(self) -> None:
        """Stop serving. Idempotent, and reached from interrupt cleanup as well as a finally."""
        server, self._server = self._server, None
        task, self._task = self._task, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.TimeoutError):
                task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — teardown never masks the authoring result
                pass


def _with_bearer_auth(app: Any, token: str) -> Any:
    """Wrap an ASGI app so every request must carry the session's bearer token."""

    async def guarded(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        if headers.get("authorization", "") != f"Bearer {token}":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await app(scope, receive, send)

    return guarded
