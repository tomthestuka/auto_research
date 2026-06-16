"""WhatsApp human handler over Twilio (CLAUDE.md §9.8).

Two-way without a public webhook: send the query via the Twilio REST API, then **poll**
``messages.list`` for the human's inbound reply (decision #2 in the step-5 plan). A blocking
``HumanHandler`` — it hangs-and-waits up to ``timeout_s``.

The Twilio ``Client`` is injectable so the send/poll logic is unit-testable with a fake
(no network, no ``twilio`` dependency in tests).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from auto_research.hitl.resolve import response_timeout_s
from auto_research.schemas import HumanQuery, HumanResponse


def _wa(number: str) -> str:
    """Ensure a number carries the Twilio ``whatsapp:`` channel prefix."""
    number = number.strip()
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


class WhatsAppHandler:
    """A ``HumanHandler`` that asks over WhatsApp and polls Twilio for the reply."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        from_number: str | None = None,
        to_number: str | None = None,
        timeout_s: float | None = None,
        poll_interval_s: float = 5.0,
    ) -> None:
        from_number = from_number or os.environ.get("TWILIO_WHATSAPP_FROM")
        to_number = to_number or os.environ.get("WHATSAPP_TO")
        if not from_number or not to_number:
            raise RuntimeError(
                "WhatsApp connector needs TWILIO_WHATSAPP_FROM and WHATSAPP_TO "
                "(and TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN for the Twilio client)."
            )
        self.from_number = _wa(from_number)
        self.to_number = _wa(to_number)
        # Default to the shared cross-channel human-response window (orchestration.yaml).
        self.timeout_s = timeout_s if timeout_s is not None else response_timeout_s()
        self.poll_interval_s = poll_interval_s
        self.client = client if client is not None else _build_twilio_client()

    def _body(self, query: HumanQuery) -> str:
        head = f"[auto_research · {query.kind} · {query.sender_label}]"
        ctx = f"\n\ncontext: {query.context}" if query.context else ""
        return f"{head}\n\n{query.question}{ctx}\n\n(reply to this message)"

    async def __call__(self, query: HumanQuery) -> HumanResponse:
        # A small back-dated cutoff avoids clock-skew dropping a fast reply.
        sent_at = datetime.now(UTC) - timedelta(seconds=2)
        await asyncio.to_thread(
            self.client.messages.create,
            from_=self.from_number,
            to=self.to_number,
            body=self._body(query),
        )

        deadline = asyncio.get_event_loop().time() + self.timeout_s
        while asyncio.get_event_loop().time() < deadline:
            reply = await asyncio.to_thread(self._latest_inbound, sent_at)
            if reply is not None:
                return HumanResponse(query_id=query.id, answer=reply)
            await asyncio.sleep(self.poll_interval_s)
        raise TimeoutError(
            f"no WhatsApp reply from {self.to_number} within {self.timeout_s:.0f}s"
        )

    def _latest_inbound(self, after: datetime) -> str | None:
        """Return the body of the newest inbound message after ``after``, if any."""
        # Inbound = sent by the human (their number is our ``to_number``) to our Twilio
        # number (``from_number``). ``date_sent_after`` is a coarse server-side filter.
        messages = self.client.messages.list(
            from_=self.to_number, to=self.from_number, date_sent_after=after, limit=20
        )
        best: tuple[datetime, str] | None = None
        for msg in messages:
            if getattr(msg, "direction", "inbound") not in ("inbound", "incoming"):
                continue
            when = getattr(msg, "date_created", None) or getattr(msg, "date_sent", None)
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when is None or when < after:
                continue
            body = (getattr(msg, "body", "") or "").strip()
            if body and (best is None or when > best[0]):
                best = (when, body)
        return best[1] if best else None


def _build_twilio_client() -> Any:
    from twilio.rest import Client  # imported lazily — optional dependency

    # API-key auth (preferred over the root Auth Token): a leaked key is revoked on its own
    # without rotating the account credential. The key still needs its owning Account SID.
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    key_sid = os.environ.get("TWILIO_API_KEY_SID")
    key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    if not account_sid or not key_sid or not key_secret:
        raise RuntimeError(
            "WhatsApp connector needs TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID and "
            "TWILIO_API_KEY_SECRET."
        )
    return Client(key_sid, key_secret, account_sid)
