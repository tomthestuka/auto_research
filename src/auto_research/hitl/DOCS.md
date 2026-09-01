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
- **`commands.py`** — the channel-agnostic HITL **slash-command** dispatcher (§12 Stage 3). A
  command-capable connector recognizes a leading-`/` message and calls `dispatch_command(text, ctx)`
  instead of routing it as a reply, so a human drives the run *any time*: `/help` (commands +
  live team/role/model roster), `/status [q]` (the `status_monitor` reporter *investigates* the
  run's ledger through `MonitorToolProvider` and answers — no fallback digest), `/steer <id> <msg>`
  (queue a message for a role's next step — addressed by its **roster id**, the number `/help`
  prints; `all` broadcasts), `/stop` (interrupt the run now — see `graph/DOCS.md`). Every outcome is immediate and deterministic: an
  unknown command, an unknown/out-of-range id, a role *name* where an id belongs, or a missing
  message answers with ⚠️ plus the valid options and queues nothing. `CommandContext` carries the run-scoped `run_control`/roster/JSONL/status
  hook, wired by `run_research` via each connector's `attach_commands`. `wait_reply_or_stop` is the
  shared reply-wait so `/stop` releases a role blocked on `ask_human` (returns
  `run_control.STOP_SENTINEL`). **Command-capable connectors are Telegram + Slack** (always
  listening); CLI/WhatsApp have no background listener so commands don't fit cleanly there (§13).
  `RunControl` itself lives in the leaf module `auto_research.run_control` (imported by agents,
  graph, and hitl without cycles); the agents' `_react` loop reads it (steer injection + stop-break)
  and the graph review edges + consensus authoring loop honor `/stop`.

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
Compacted 2026-09-01: entries through 2026-07-31 folded into themed ranges (detail in git history).

- 2026-06-15 -> 2026-06-29 — **Package created and hardened (Phase 2 step 5).**
  `HumanQuery`/`HumanResponse` protocol; injectable `HumanHandler` + `resolve_human` fail-safe;
  `resolve_handler` CLI-vs-library auto-detect; CLI/Slack/WhatsApp connectors;
  `make_human_channel_check` launch ping. Then: Twilio API-key auth, a shared `response_timeout_s()`
  (default 1 h) every channel waits at most, `SlackHandler` construction deferred to `async start()`
  with single-open plain-reply resolution and the benign Socket Mode rotation ERROR downgraded, and
  connector/ping faults translated to named causes with a `cli`/`none` escape hatch. `HumanQuery`
  settled on one Markdown `message` rendered per surface (CLI `rich`, Slack `markdown` block);
  WhatsApp/Twilio deprecated (it bills per message).
- 2026-07-02 -> 2026-07-16 — **Telegram becomes the recommended channel (§12/[E4]), then its
  credential leak is closed.** `python-telegram-bot` long polling (outbound-only, no public URL,
  reconnect-lossless), chosen over unreliable Slack Socket Mode; multi-question reply-to routing
  (`message_id→Future`) with a single-open plain-reply fallback; Markdown→Telegram-HTML render
  (`_md_to_html`, `_chunk_md`); wired into `build_human_handler`/CLI/preflight. Live fixes: blockquote
  rendering (escape before the structure pass), plain-reply dedupe by Future identity, and each
  `message_id→Future` registered immediately after its own send so a fast mid-send reply is not
  dropped. **Credential leak:** `TelegramHandler.start()` pins `httpx`/`httpcore` to WARNING, since
  Telegram embeds the bot token in every request URL and httpx logs full URLs at INFO — every backend
  log had been writing the credential to disk (rotating the exposed token is the human's out-of-band
  step). The suppression lives where Telegram traffic starts, so it covers every entry point.
- 2026-07-22 -> 2026-07-30 — **The slash-command channel (§12 Stage 3), then its live-driven rework.**
  New `commands.py` (channel-agnostic `dispatch_command` + `CommandContext` + `make_status_answer` +
  `wait_reply_or_stop`) over the leaf `auto_research.run_control.RunControl`; **Telegram + Slack** gained
  `attach_commands`, a command receive-path, and `/stop`-aware waiting so a stop releases a blocked
  `ask_human`. **CLI/WhatsApp are NOT command-wired** — no background listener, so anytime-commands do
  not fit cleanly (§13). The rework after the B-run, where a `/steer` that never arrived could not be
  diagnosed from the log at all: **targets are roster ids** (`/help` prints `id · team · role · model`;
  a name is rejected *and told its id*), **every invalid command fails immediately and
  deterministically** with the valid options, the ack says **queued** rather than delivered, and
  commands land in the ledger as `hitl.command` + `hitl.command.reply` (actor `human`) so a rejected,
  a queued and a delivered steer are distinguishable after the fact. The `/status` prompt moved to
  `prompts/status_monitor.md` (§4). Two rulings followed: **`/stop` is forceful with no modifier** — a
  stop that waits for a safe point is not a stop, since on the live run the next safe point sat behind
  25-minute downloads — and **buttons replace a flag vocabulary** (`-f/-p/-c` was designed and dropped:
  a chat platform surfaces registered commands in a menu and buttons for follow-ups, flags are invisible
  in both, and a dash is what a phone keyboard mangles). So the typed surface stays four verbs and the
  escalations became taps on the steer ack (`[⚡ Deliver now]`, `[🗑 Clear]`). `dispatch_command` returns
  `CommandReply`, a **`str` subclass** carrying `.buttons` — additive by construction, so other
  connectors keep sending plain text and only Telegram reads them; a tap carries
  `steer:<now|clear>:<id>:<run_id>:<boot>` and needs no side store, the two trailing fields answering
  "is this button still mine?" (a foreign `run_id` is another run; a foreign boot nonce is an earlier
  segment of *this* run, which `run_id` cannot detect because it is deliberately stable across a
  resume). Telegram registers its menu from the same list `/help` prints, so the two cannot drift.
- 2026-07-31 — **`/stop` is Ctrl-C, and `/status` investigates instead of being handed a digest.**
  `_stop` calls `RunControl.interrupt()` — flag the run, then cancel its task — instead of setting a flag
  and aborting in-flight tool calls for a pipeline that then wound itself down over ~14 more model calls.
  The cancellation is *scheduled* (`call_soon`), not delivered inside the handler, so the acknowledgement
  gets in flight before cleanup closes the connector. The reply says what actually happens: work
  cancelled, sandbox processes killed, authoring branch reset if a tool was being written, **no report
  composed**, and `resume=True` continues from the last completed step. `abort_all` stays — the ⚡ button
  still cancels one role's in-flight call so a queued steer lands at once. **`/status`:**
  `make_status_answer` builds a `StatusMonitorAgent` (a real ReAct role) over `MonitorToolProvider`, and
  both compaction paths are **deleted** (`_status_digest` — last 80 events at any level, flattened, 8k cap
  — and `_deterministic_status`); the digest dropped every event's `data`, i.e. exactly the answers people
  ask for ("what did the reviewer decide?", "why did the gate reject it?"), and windowed the tail at *any*
  level (44 L7 + 18 L10 against 6 significant events on a live run). **No fallback replaces it, by
  ruling:** `status_monitor` is a required role so a misconfigured run fails at launch; transient provider
  errors are already retried at the model layer; and a run whose provider is down cannot advance, so a
  second, lesser view of the same log would report on a stalled run as if it were fine. Tests:
  `tests/test_hitl_commands.py`, `tests/test_hitl.py`, `tests/test_run_control.py`.
