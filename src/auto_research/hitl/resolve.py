"""Handler resolution + the preflight channel check (CLAUDE.md §9.8, step-5 plan #4/#5).

``resolve_handler`` is the single place "am I a CLI or a library?" is decided; it picks a
concrete ``HumanHandler`` by precedence (explicit handler → explicit channel → auto-detect).
``make_human_channel_check`` is the launch-time ping registered into the preflight: it flags
green only when the resolved channel actually replies.

This module stays independent of ``auto_research.graph`` (it reads orchestration.yaml
directly) so the preflight can import it without pulling the graph stack.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from auto_research.hitl.base import HumanHandler, resolve_human
from auto_research.schemas import HumanQuery


@lru_cache(maxsize=1)
def _orchestration_config() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def build_human_handler(name: str, **cfg: Any) -> HumanHandler | None:
    """Construct a connector-backed handler by name (``cli|slack|whatsapp|none``).

    Reads credentials from the environment inside each connector; a selected connector with
    missing keys raises a clear error. ``none`` returns ``None`` (headless).
    """
    key = (name or "none").lower()
    if key == "none":
        return None
    if key == "cli":
        from auto_research.hitl.cli import cli_stdin_handler

        return cli_stdin_handler
    if key == "slack":
        from auto_research.hitl.slack import SlackHandler

        return SlackHandler(**cfg)
    if key == "whatsapp":
        from auto_research.hitl.whatsapp import WhatsAppHandler

        return WhatsAppHandler(**cfg)
    raise ValueError(f"unknown human channel {name!r}; expected cli|slack|whatsapp|none")


def resolve_handler(
    human_handler: HumanHandler | None, human_channel: str | None
) -> HumanHandler | None:
    """Resolve the effective ``HumanHandler`` (decision #4).

    Precedence: an explicit ``human_handler`` wins; else an explicit ``human_channel``; else
    **auto** — an interactive stdin (``isatty``) resolves to the CLI handler, otherwise
    (library/notebook/server) the ``human_channel`` default from ``orchestration.yaml`` (or
    ``none``).
    """
    if human_handler is not None:
        return human_handler
    channel = (human_channel or "auto").lower()
    if channel == "auto":
        if _stdin_is_interactive():
            channel = "cli"
        else:
            channel = str(_orchestration_config().get("human_channel", "none"))
    return build_human_handler(channel)


def _stdin_is_interactive() -> bool:
    stdin = getattr(sys, "stdin", None)
    try:
        return bool(stdin is not None and stdin.isatty())
    except (ValueError, OSError):
        return False


def ping_timeout_s() -> float:
    """Preflight channel-ping timeout (``orchestration.yaml: human_channel_ping_timeout_s``)."""
    return float(_orchestration_config().get("human_channel_ping_timeout_s", 300))


def response_timeout_s() -> float:
    """How long a blocked role waits for a human reply before failing, shared by every HITL
    channel (``orchestration.yaml: human_response_timeout_s``, default 3600s = 1h).

    A suspend is in-process blocking (§9.8), so this caps how long the run holds the process
    waiting on a person. Default 1h is a humane response window; it is deliberately *not* tied
    to the LLM prompt-cache TTL — the default cache (5 min on Anthropic, 5-10 min on OpenAI) is
    far too short to govern a human deadline, and exceeding it only costs one cheap cold prefix
    re-prefill on resume, not a re-run.
    """
    return float(_orchestration_config().get("human_response_timeout_s", 3600))


def make_human_channel_check(
    handler: HumanHandler, *, timeout_s: float | None = None
) -> Callable[[], Awaitable[None]]:
    """Build the launch-time preflight check that the human channel round-trips (#5).

    Returns an async zero-arg check that pings ``handler`` and **passes only when a non-empty
    reply comes back** within ``timeout_s``; raises otherwise so the launch fails before any
    model spend. Registered by the preflight only when a handler is resolved (non-headless).
    """
    limit = timeout_s if timeout_s is not None else ping_timeout_s()

    async def check() -> None:
        query = HumanQuery(
            kind="blocked",
            asked_by="preflight",
            question="Reply to confirm this run's human-response channel is live.",
        )
        try:
            response = await asyncio.wait_for(resolve_human(handler, query), timeout=limit)
        except TimeoutError as exc:
            raise RuntimeError(
                f"the human-response channel did not reply within {limit:.0f}s. Confirm the "
                "channel is reachable and someone replies to the launch ping; or set "
                "human_channel='none' (headless) to skip the channel."
            ) from exc
        except Exception as exc:  # noqa: BLE001 — keep the connector's directive, add an escape hatch
            raise RuntimeError(
                f"the human-response channel ping failed: {exc} "
                "(or set human_channel='none' for a headless run, 'cli' for an attended terminal)."
            ) from exc
        if not response.answer.strip():
            raise RuntimeError(
                "the human-response channel returned an empty reply; reply with any text to "
                "confirm it is live, or set human_channel='none' to skip the channel."
            )

    return check
