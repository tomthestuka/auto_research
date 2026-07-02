# `auto_research.hitl` — raise-to-human clarification channel

## Purpose

The cross-cutting **raise-to-human** channel (CLAUDE.md §9.7/§9.8): a single injectable
`HumanHandler` every role uses when blocked, plus pluggable connector backends (CLI stdin,
Slack, Telegram, WhatsApp), an auto-detecting resolver, and the launch-time channel ping. It is a
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
- **Connectors** (`cli.py`, `slack.py`, `telegram.py`, `whatsapp.py`) — each a `HumanHandler`.
  The Slack/Telegram/Twilio clients are injectable so the send/reply logic is unit-tested without
  those libraries or any network; the real clients are imported lazily inside their `_build_*`
  helpers, so `import auto_research.hitl` never requires `python-telegram-bot`/`twilio`/`slack_bolt`.

This package stays independent of `auto_research.graph` (it reads `orchestration.yaml`
directly) so the preflight can import it without pulling the graph/LLM stack.

## Interfaces / Contracts

- `HumanHandler = Callable[[HumanQuery], HumanResponse | str | Awaitable[...]]` — the
  injectable interface (`run_research(..., human_handler=fn)`).
- `async resolve_human(handler, query) -> HumanResponse` — call + normalize; raises
  `HumanInputRequired` when `handler is None`.
- `resolve_handler(human_handler, human_channel) -> HumanHandler | None` — the CLI-vs-library
  decision; explicit handler/channel override the `isatty` auto-detect.
- `build_human_handler(name, **cfg) -> HumanHandler | None` — `cli|slack|telegram|whatsapp|none`;
  raises `ValueError` on an unknown name and a clear `RuntimeError` when a selected
  connector's env keys are missing.
- `make_human_channel_check(handler, *, timeout_s=None) -> Callable[[], Awaitable[None]]` —
  the preflight ping: passes only when the channel returns a non-empty reply within the
  timeout; raises otherwise. Registered by `auto_research.preflight` only when a handler is
  resolved.
- `cli_stdin_handler(query) -> HumanResponse` — attended-terminal handler: renders the query's
  Markdown `message` to stderr with `rich` (a titled rule + `rich.markdown.Markdown`; degrades to
  plain text on a non-tty), then reads stdin off-thread. Waits at most `response_timeout_s()` then
  raises `TimeoutError` — an attended terminal no longer hangs the run forever.
- `SlackHandler(*, app=None, socket_handler=None, channel=None, timeout_s=None)` — Socket
  Mode; posts to `SLACK_HUMAN_CHANNEL`, awaits a threaded reply via a Future resolved by
  `on_message`. The query's Markdown `message` is rendered as one or more `markdown` blocks
  (`_blocks`); `_chunk` splits a body over the 12000-char block limit at paragraph/line
  boundaries (the per-message ceiling is 50 blocks), and the notification `text` carries
  `query.headline`. `start()`/`aclose()` manage the socket. `start()` also installs
  `_SocketRotationDowngradeFilter` on the `slack_sdk.socket_mode.aiohttp` logger, demoting the
  benign websocket-rotation ERROR ("Cannot write to closing transport") to WARNING while leaving
  genuine errors untouched. `timeout_s` defaults to the shared `response_timeout_s()` when unset.
  Built per spec; **not live-tested**.
- `TelegramHandler(*, app=None, token=None, chat_id=None, timeout_s=None)` — **the recommended
  reliable channel** (CLAUDE.md §12/[E4]): long polling (`getUpdates`) — **outbound-only**, no public
  URL, and Telegram retains updates ≤24h so a reconnect is lossless. Posts to `TELEGRAM_CHAT_ID`
  (auth: `TELEGRAM_BOT_TOKEN` from BotFather) and awaits a reply via a Future resolved by `on_message`.
  **Multi-question ready:** `_pending` maps every posted `message_id → Future`, so a Telegram
  **reply-to** (`reply_to_message.message_id`) routes to the right pending query even with several open
  (future parallel analysts); a plain reply resolves the single open one. The Markdown `message` is
  rendered to Telegram's HTML subset (`_md_to_html`: bold/italic, inline+fenced code, links, headings→
  bold, bullets→`•`, escaping `< > &`) and `_chunk_md` splits a long body across ≤4096-char messages;
  `_send` falls back to plain text if Telegram rejects the HTML. `start()`/`aclose()` manage the
  `Application` + polling; `timeout_s` defaults to the shared `response_timeout_s()`. Live-tested
  via `tests/test_live_telegram.py` (opt-in, `AUTO_RESEARCH_LIVE_TELEGRAM=1`) and the cross-channel
  `tests/test_live_hitl.py`.
- `WhatsAppHandler(*, client=None, from_number=None, to_number=None, timeout_s=None,
  poll_interval_s=5)` — **deprecated** (Twilio bills per message; prefer `slack`/`cli` — emits a
  `DeprecationWarning` on construction, CLAUDE.md §13). Sends the Markdown `message` body raw (no
  per-channel polish). `timeout_s` defaults to the shared `response_timeout_s()` when unset.
  Twilio: sends, then **polls** `messages.list` for the inbound reply
  (no public webhook). Auth is via a Twilio **API key** (`TWILIO_ACCOUNT_SID` +
  `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET`), not the root Auth Token, so a leaked key is
  revoked on its own. Live-tested via `tests/test_live_whatsapp.py` (opt-in).

The schemas `HumanQuery` (`id`, `kind ∈ {clarify_science, clarify_code, blocked}`, Markdown
`message`, `asked_by`, `model`, `created_at`; `sender_label`/`headline` properties) and
`HumanResponse` (`query_id`, `answer`, `created_at`) live in `auto_research.schemas` (the
protocol, §7).

## Dependencies

- Internal: `auto_research.schemas` (`HumanQuery`/`HumanResponse`); reads
  `config/orchestration.yaml` directly. Imported by `auto_research.preflight` (channel check),
  `auto_research.graph.runtime` (resolve + thread), `auto_research.agents.react`
  (`ask_human` tool), `auto_research.sandbox.merge_flow` (author `ask_human`).
- External: `rich` (CLI Markdown rendering — a hard dep, in `environment.yml`). Optional, lazy:
  `python-telegram-bot` (Telegram — the recommended channel), `slack_bolt` (Slack), `twilio`
  (WhatsApp — deprecated).

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
- `SlackHandler` / `TelegramHandler` / `WhatsAppHandler` — connector classes (see contracts above).

## Changelog
- 2026-06-15 — Phase 2 step 5: package created. `HumanQuery`/`HumanResponse` protocol; injectable
  `HumanHandler` + `resolve_human` fail-safe; `resolve_handler` CLI-vs-library auto-detect;
  CLI/Slack/WhatsApp connectors; `make_human_channel_check` launch ping.
- 2026-06-16 -> 2026-06-24 — **Connector hardening.** `WhatsAppHandler` → Twilio API-key auth;
  shared `response_timeout_s()` every channel waits at most (default 1h, decoupled from the
  prompt-cache TTL). `SlackHandler` construction deferred to `async start()`; `on_message` resolves
  a plain channel reply when exactly one question is open; a benign Socket Mode rotation ERROR is
  downgraded to WARNING (`_SocketRotationDowngradeFilter`). Twilio/ping faults translate to named
  causes + the `cli`/`none` escape hatch. Covered by `tests/test_hitl.py`.
- 2026-06-29 — **Markdown `message` + per-channel rendering; WhatsApp deprecated.** `HumanQuery`
  carries one Markdown `message` (was `question`+`context`); connectors *render* per surface (LLMs
  emit Markdown natively): CLI via `rich`; Slack via the dedicated `markdown` block with `_chunk`
  over the 12000-char limit (replacing the 2900-char section `_truncate`), notification text =
  `query.headline`; fail-safe/preflight use `message`/`headline`. **WhatsApp/Twilio deprecated**
  (bills per message; warns on construction, raw passthrough; prefer `slack`/`cli`, CLAUDE.md §13).
  Tests `tests/test_hitl.py`; opt-in live render across channels `tests/test_live_hitl.py`
  (`AUTO_RESEARCH_LIVE_HITL=1`, each channel try/except, logs `logs/dev/hitl/`).
- 2026-07-02 — **Telegram connector added — the recommended reliable channel (CLAUDE.md §12/[E4]).**
  `telegram.py` `TelegramHandler`: `python-telegram-bot` long polling (outbound-only, no public URL,
  24h server-side retention → reconnect-lossless), chosen over Slack Socket Mode (chronically stale)
  and Slack-HTTP (a public inbound endpoint = cluster-policy blocker). Auth = `TELEGRAM_BOT_TOKEN`
  (BotFather) + `TELEGRAM_CHAT_ID`. **Multi-question-ready** via reply-to routing (`message_id→Future`
  map) for future parallel analysts, single-open plain-reply fallback. Markdown→Telegram-HTML render
  (`_md_to_html`) with a plain-text fallback; `_chunk_md` splits over the 4096-char cap. Wired into
  `build_human_handler` (`telegram`), the CLI `--human` choices, and the generic preflight ping.
  `python-telegram-bot>=22` added to `environment.yml`. Tests: `tests/test_hitl.py` (fake
  Application/bot — reply-to routing, concurrent-question routing, plain-reply fallback, chat/bot/empty
  filtering, timeout, MD→HTML, chunking). WhatsApp stays deprecated.
- 2026-07-02 — **Telegram live-test stubs.** Added the dedicated opt-in blocking round-trip
  `tests/test_live_telegram.py` (`AUTO_RESEARCH_LIVE_TELEGRAM=1`; gates on the two Telegram vars
  only — decoupled from the LLM/ADS keys `check_required_env()` requires, since a Telegram send
  needs neither; logs the exchange under `logs/dev/`) and wired the recommended channel into the
  cross-channel send-only `tests/test_live_hitl.py` (`_emit_telegram` starts the `Application`,
  sends the HTML-rendered chunks, `aclose()`s; best-effort like the other external channels). No
  connector code changed.
