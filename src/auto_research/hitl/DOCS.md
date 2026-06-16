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
  reads stdin off-thread).
- `SlackHandler(*, app=None, socket_handler=None, channel=None, timeout_s=1800)` — Socket
  Mode; posts to `SLACK_HUMAN_CHANNEL`, awaits a threaded reply via a Future resolved by
  `on_message`. `start()`/`aclose()` manage the socket. Built per spec; **not live-tested**.
- `WhatsAppHandler(*, client=None, from_number=None, to_number=None, timeout_s=1800,
  poll_interval_s=5)` — Twilio: sends, then **polls** `messages.list` for the inbound reply
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
