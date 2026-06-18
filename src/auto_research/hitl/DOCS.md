# `auto_research.hitl` — raise-to-human clarification channel

## Purpose

The cross-cutting **raise-to-human** channel (CLAUDE.md §9.7/§9.8): a single injectable
`HumanHandler` every role uses when blocked, plus pluggable connector backends (CLI stdin,
Slack, WhatsApp), an auto-detecting resolver, and the launch-time channel ping. It is a
**clarification** channel, fully decoupled from toolkit merging — the runtime never asks a
human to merge.

## Architecture

A blocked role calls its `ask_human` tool (in-graph roles via
`auto_research.agents.react`; the SDK author via `sandbox.merge_flow`). The call builds a
`HumanQuery` and **blocks** on the resolved `HumanHandler` until the connector returns a
`HumanResponse` (decision #1 — blocking direct-handler; no LangGraph checkpointer / durable
suspend-resume, which stays the §12 TODO). No fabrication, no silent hang: with no handler
wired, `resolve_human` raises `HumanInputRequired` (the §9.8 headless fail-safe).

- **`base.py`** — `HumanHandler` type, `HumanInputRequired`, and `resolve_human` (the one
  chokepoint that calls a handler, normalizes sync/async + bare-`str` returns, and enforces
  the fail-safe). `HumanInputRequired` is re-exported from `sandbox.merge_flow` for
  back-compat.
- **`resolve.py`** — the auto-detection chokepoint (decision #4): `resolve_handler` picks a
  concrete handler by precedence (explicit handler → explicit channel → **auto**, where
  `sys.stdin.isatty()` → CLI else the `orchestration.yaml: human_channel` default).
  `build_human_handler` is the name→connector factory. `make_human_channel_check` builds the
  launch-time preflight ping (decision #5).
- **Connectors** (`cli.py`, `slack.py`, `whatsapp.py`) — each a `HumanHandler`. The Slack/
  Twilio clients are injectable so the send/poll logic is unit-tested without those libraries
  or any network; the real clients are imported lazily inside their `_build_*` helpers, so
  `import auto_research.hitl` never requires `twilio`/`slack_bolt`.

This package stays independent of `auto_research.graph` (it reads `orchestration.yaml`
directly) so the preflight can import it without pulling the graph/LLM stack.

## Interfaces / Contracts

- `HumanHandler = Callable[[HumanQuery], HumanResponse | str | Awaitable[...]]` — the
  injectable interface (`run_research(..., human_handler=fn)`).
- `async resolve_human(handler, query) -> HumanResponse` — call + normalize; raises
  `HumanInputRequired` when `handler is None`.
- `resolve_handler(human_handler, human_channel) -> HumanHandler | None` — the CLI-vs-library
  decision; explicit handler/channel override the `isatty` auto-detect.
- `build_human_handler(name, **cfg) -> HumanHandler | None` — `cli|slack|whatsapp|none`;
  raises `ValueError` on an unknown name and a clear `RuntimeError` when a selected
  connector's env keys are missing.
- `make_human_channel_check(handler, *, timeout_s=None) -> Callable[[], Awaitable[None]]` —
  the preflight ping: passes only when the channel returns a non-empty reply within the
  timeout; raises otherwise. Registered by `auto_research.preflight` only when a handler is
  resolved.
- `cli_stdin_handler(query) -> HumanResponse` — attended-terminal handler (prints to stderr,
  reads stdin off-thread). Waits at most `response_timeout_s()` then raises `TimeoutError` — an
  attended terminal no longer hangs the run forever.
- `SlackHandler(*, app=None, socket_handler=None, channel=None, timeout_s=None)` — Socket
  Mode; posts to `SLACK_HUMAN_CHANNEL`, awaits a threaded reply via a Future resolved by
  `on_message`. `start()`/`aclose()` manage the socket. `timeout_s` defaults to the shared
  `response_timeout_s()` when unset. Built per spec; **not live-tested**.
- `WhatsAppHandler(*, client=None, from_number=None, to_number=None, timeout_s=None,
  poll_interval_s=5)` — `timeout_s` defaults to the shared `response_timeout_s()` when unset.
  Twilio: sends, then **polls** `messages.list` for the inbound reply
  (no public webhook). Auth is via a Twilio **API key** (`TWILIO_ACCOUNT_SID` +
  `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET`), not the root Auth Token, so a leaked key is
  revoked on its own. Live-tested via `tests/test_live_whatsapp.py` (opt-in).

The schemas `HumanQuery` (`id`, `kind ∈ {clarify_science, clarify_code, blocked}`, `question`,
`context`, `asked_by`, `created_at`) and `HumanResponse` (`query_id`, `answer`, `created_at`)
live in `auto_research.schemas` (the protocol, §7).

## Dependencies

- Internal: `auto_research.schemas` (`HumanQuery`/`HumanResponse`); reads
  `config/orchestration.yaml` directly. Imported by `auto_research.preflight` (channel check),
  `auto_research.graph.runtime` (resolve + thread), `auto_research.agents.react`
  (`ask_human` tool), `auto_research.sandbox.merge_flow` (author `ask_human`).
- External (optional, lazy): `twilio` (WhatsApp), `slack_bolt` (Slack). The CLI handler needs
  nothing.

## Dictionary-level reference

- `HumanHandler` — injectable `(HumanQuery) -> HumanResponse` (sync/async; bare str ok).
- `HumanInputRequired(RuntimeError)` — headless fail-safe.
- `resolve_human(handler, query)` — call + normalize + fail-safe.
- `resolve_handler(human_handler, human_channel)` — precedence resolver / auto-detect.
- `build_human_handler(name, **cfg)` — name→connector factory.
- `ping_timeout_s()` — preflight ping timeout from `orchestration.yaml`.
- `response_timeout_s()` — the shared mid-run human-response window from `orchestration.yaml`
  (`human_response_timeout_s`, default 3600s/1h); every channel (CLI/Slack/WhatsApp) waits at
  most this long for a reply before raising. Decoupled from the LLM prompt-cache TTL on purpose
  (the default cache is ~5min — far too short to govern a human deadline, and overshooting it
  only costs one cheap cold prefix re-prefill on resume).
- `make_human_channel_check(handler, *, timeout_s=None)` — preflight ping check factory.
- `cli_stdin_handler(query)` — attended-terminal handler.
- `SlackHandler` / `WhatsAppHandler` — connector classes (see contracts above).

## Changelog

- 2026-06-15 — Phase 2 step 5: package created. `HumanQuery`/`HumanResponse` protocol; the
  injectable `HumanHandler` + `resolve_human` fail-safe; the `resolve_handler` CLI-vs-library
  auto-detect; CLI/Slack/WhatsApp connectors; and the `make_human_channel_check` launch ping.
- 2026-06-16 — `WhatsAppHandler` now authenticates with a Twilio **API key**
  (`TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET` + `TWILIO_ACCOUNT_SID`) instead of the root
  Auth Token, so a leaked credential is revoked without rotating the account. `.env.example`,
  README, and the live-test docstring updated to match.
- 2026-06-16 — all three connector bodies (CLI/Slack/WhatsApp) now render the sender via
  `HumanQuery.sender_label` → `"<role> (<model>)"` so the human sees which model is asking.
- 2026-06-16 — added `response_timeout_s()` (`orchestration.yaml: human_response_timeout_s`,
  default 3600s/1h): one shared mid-run human-response window read by every channel. Slack and
  WhatsApp `timeout_s` now default to it (was a hardcoded 1800s); the CLI handler, previously an
  indefinite block, now waits at most this long then raises `TimeoutError`. Deliberately
  decoupled from the LLM prompt-cache TTL (default ~5min is too short to govern a human deadline;
  overshooting only costs one cheap cold prefix re-prefill).
- 2026-06-18 — Polished the channel-ping preflight error into an actionable directive. The
  WhatsApp connector now catches a failed Twilio send and translates it: an HTTP-401 names the
  exact creds to check (`TWILIO_API_KEY_SID`/`TWILIO_API_KEY_SECRET`/`TWILIO_ACCOUNT_SID` — the
  API Key SID is the basic-auth username, so "invalid username" is a credential fault), other
  failures point at the from/to numbers + connectivity, and both offer the escape hatch
  (`human_channel='cli'`/`'none'`). `make_human_channel_check` now wraps any non-timeout ping
  failure and the timeout/empty-reply cases with the same escape-hatch directive.
- 2026-06-18 — `SlackHandler.on_message` no longer requires the reply to be **in-thread**: a
  plain channel reply (no `thread_ts`) now resolves the most recent pending question (the common
  case — people reply in the channel, not the message thread). Still ignores our own posts,
  events with a `subtype` (joins/edits), empty text, and other channels. Threaded replies match
  their own thread as before. Covered by `tests/test_hitl.py`
  (`test_slack_resolves_plain_channel_reply`, `test_slack_on_message_ignores_other_channel_and_subtype`).
  Concurrency guard: a bare channel reply is only auto-attributed when **exactly one** question
  is open; with 2+ pending it's ambiguous, so it's ignored and an in-thread reply (always
  unambiguous) is required. The orchestration is sequential today (≤1 question ever open), so this
  is future-proofing. Both the in-thread and channel-reply round-trips were live-verified 2026-06-18
  (`test_slack_channel_reply_ambiguous_when_multiple_pending` covers the guard).
