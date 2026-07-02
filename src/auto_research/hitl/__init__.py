"""Human-in-the-loop clarification channel (CLAUDE.md §9.7/§9.8).

The injectable ``HumanHandler`` interface plus pluggable connector backends (CLI stdin,
Slack, Telegram, WhatsApp), the auto-detecting resolver, and the launch-time channel ping.
"""

from auto_research.hitl.base import HumanHandler, HumanInputRequired, resolve_human
from auto_research.hitl.cli import cli_stdin_handler
from auto_research.hitl.resolve import (
    build_human_handler,
    make_human_channel_check,
    ping_timeout_s,
    resolve_handler,
    response_timeout_s,
)
from auto_research.hitl.slack import SlackHandler
from auto_research.hitl.telegram import TelegramHandler
from auto_research.hitl.whatsapp import WhatsAppHandler

__all__ = [
    "HumanHandler",
    "HumanInputRequired",
    "SlackHandler",
    "TelegramHandler",
    "WhatsAppHandler",
    "build_human_handler",
    "cli_stdin_handler",
    "make_human_channel_check",
    "ping_timeout_s",
    "resolve_handler",
    "resolve_human",
    "response_timeout_s",
]
