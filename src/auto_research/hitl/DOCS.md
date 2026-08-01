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
- 2026-06-15 — Phase 2 step 5: package created. `HumanQuery`/`HumanResponse` protocol; injectable `HumanHandler` + `resolve_human` fail-safe; `resolve_handler` CLI-vs-library auto-detect; CLI/Slack/WhatsApp connectors; `make_human_channel_check` launch ping.
- 2026-06-16 -> 2026-06-24 — Connector hardening: `WhatsAppHandler` Twilio API-key auth; shared `response_timeout_s()` (default 1h) every channel waits at most; `SlackHandler` construction deferred to `async start()`, single-open plain-reply resolution, benign Socket Mode rotation ERROR downgraded (`_SocketRotationDowngradeFilter`); connector/ping faults translate to named causes with a `cli`/`none` escape hatch.
- 2026-06-29 — `HumanQuery` carries one Markdown `message` (was `question`+`context`); connectors render per surface (CLI `rich`, Slack `markdown` block with `_chunk` over the 12000-char limit). WhatsApp/Twilio deprecated (bills per message).
- 2026-07-02 — Telegram connector added — the recommended reliable channel (§12/[E4]): `python-telegram-bot` long polling (outbound-only, no public URL, reconnect-lossless), chosen over unreliable Slack Socket Mode; multi-question reply-to routing (`message_id→Future`) with single-open plain-reply fallback; Markdown→Telegram-HTML render (`_md_to_html`, `_chunk_md`); wired into `build_human_handler`/CLI/preflight; opt-in live tests added.
- 2026-07-06 — Telegram fixes (found live): `_md_to_html` blockquote render (escape before the structure pass); plain-reply for chunked queries dedupes by Future identity; [E4] each `message_id→Future` registered immediately after its own send so a fast mid-send reply is not dropped.
- 2026-07-16 — **Credential-leak fix:** `TelegramHandler.start()` pins the `httpx`/`httpcore` loggers to WARNING for the process. Telegram's API embeds the bot token in every request URL and httpx logs full URLs at INFO, so every backend log was writing the credential to disk (observed in a live run's `backend.log`; rotating the exposed token is the human's out-of-band step). Suppression lives where Telegram traffic starts, covering every entry point (CLI, notebook, detached backend, preflight ping).
- 2026-07-22 — **HITL slash-command channel (§12 Stage 3).** New `commands.py` (channel-agnostic
  `dispatch_command` + `CommandContext` + `make_status_answer` + `wait_reply_or_stop`) and the leaf
  `auto_research.run_control.RunControl` (`stop_requested`/`stop_event`, per-role `steer_inbox`,
  `STOP_SENTINEL`). Commands: `/help` (+ live roster), `/status [q]` (LLM via the new light
  `status_monitor` model role, deterministic-digest fallback), `/steer <role|all> <msg>` (cooperative
  per-role inbox, drained at the top of `AgentRole._react` and injected as a `[Human steer]` user
  turn), `/stop` (`RunControl.request_stop` → honest §9.6 wind-down: `_react` breaks, the consensus
  authoring loop skips, the graph review edges route to finalize). **Telegram + Slack** gained
  `attach_commands` + a command receive-path (Telegram a `filters.COMMAND` handler; Slack a `/`-branch
  in `on_message`) + `wait_reply_or_stop` so `/stop` releases a blocked `ask_human`. `run_research`
  builds the `CommandContext` (roster from the resolved roles, run's `monitor.jsonl`, `status_monitor`
  handle) and attaches it. **CLI/WhatsApp are NOT command-wired** — no background listener, so
  anytime-commands don't fit cleanly (§13). Command *dispatch* is deterministically tested
  (`tests/test_hitl_commands.py`, `tests/test_run_control.py`, connector tests in `tests/test_hitl.py`);
  live command behavior is observational during a real run. NOT LIVE TESTED end-to-end.

- 2026-07-29 — **Command surface reworked + commands recorded (live-driven).** A `/steer` that never
  arrived on the B-run could not be diagnosed from the log at all, so: (1) **targets are roster
  ids**, not internal keys — `/help` prints `id · team · role · model` and `/steer 3 <message>` is
  what the human types (a name is rejected *and told its id*), which removes the retype-an-internal-
  key-on-a-phone failure entirely; (2) **every invalid command fails immediately and
  deterministically** with the valid options (`_REJECTED` ⚠️ prefix), never silently queueing;
  (3) the ack says **queued**, not delivered, and names when it will arrive; (4) commands land in the
  run ledger as `hitl.command` (received) + `hitl.command.reply` (accepted/rejected), actor `human`,
  so a rejected command, a queued steer and a delivered steer are now distinguishable after the fact
  — also giving the B-run's B4 check the event it was waiting for. (5) The `/status` system prompt
  moved to `prompts/status_monitor.md` and loads via `load_prompt` (§4: runtime prompts live only in
  `prompts/`, and it now passes the governance-reference check like every other role). Tests:
  `tests/test_hitl_commands.py` (+6), connector tests updated to id targeting.

- 2026-07-30 — **`/stop` is forceful, with no modifier to remember (user ruling).** A stop that waits
  for a "safe point" is not a stop: on the 2026-07-29 run the next safe point sat behind a batch of
  25-minute downloads, so an acknowledged stop could not take effect for hours. `_stop` now also
  awaits `RunControl.abort_all()`, which cancels each role's in-flight tool call through an aborter
  the run registered (the worker's `PersistentSandbox.abort`), and the reply says how many calls it
  cancelled — or that nothing was mid-call. Cancelling is honest rather than violent: killing the REPL
  child makes the in-flight `call_tool` return its ordinary non-zero "sandbox subprocess exited
  unexpectedly" result, so every tool call still gets a result and the transcript stays valid, and the
  next call spawns a fresh REPL at entry. Tests: `tests/test_hitl_commands.py` (+2),
  `tests/test_run_control.py` (+2).

- 2026-07-30 — **Buttons replace the flag vocabulary (design decided against `-f/-p/-c`).** A chat
  channel surfaces *registered commands* in an autocomplete menu and offers *buttons* for follow-up
  choices — flags are invisible in both, and a dash is exactly what a phone keyboard mangles when a
  human is in a hurry. So the typed surface stays four verbs and the two escalations became taps on
  the steer ack: `[⚡ Deliver now]` and `[🗑 Clear]`. `dispatch_command` now returns `CommandReply`, a
  **`str` subclass** carrying `.buttons` — additive by construction, so CLI/WhatsApp/Slack keep
  sending plain text untouched and only Telegram reads them. New `dispatch_callback` handles a tap:
  payload `steer:<now|clear>:<id>:<run_id>:<boot>` carries no message and needs no side store (the
  text is already in the inbox), while the two trailing fields answer "is this button still mine?" —
  a foreign `run_id` is another run's history, and a foreign boot nonce is an **earlier session of
  this run**, which `run_id` cannot detect because it is deliberately stable across a resume.
  `⚡` aborts the target's in-flight call so the queued steer lands at once; `🗑` drains its queue.
  Telegram gained a `CallbackQueryHandler` (a tap is a third channel, never a reply), inline-keyboard
  markup on `_send`, and `setMyCommands` registration from the same list `/help` prints, so the menu
  cannot drift from the help text. Taps record as `hitl.command.button`. Tests:
  `tests/test_hitl_commands.py` (+7), `tests/test_hitl.py` (+2, and the Telegram fake gained the
  `reply_markup`/`set_my_commands` shape it was missing).

- 2026-07-31 — **`/stop` is Ctrl-C.** `_stop` now calls `RunControl.interrupt()` — flag the run,
  then cancel its task — instead of setting a flag and aborting in-flight tool calls for a
  pipeline that then wound itself down over ~14 more model calls. The cancellation is *scheduled*
  (`call_soon`), not delivered inside the handler, so the acknowledgement gets in flight before
  cleanup closes the connector. The reply says what actually happens: work cancelled, sandbox
  processes killed, authoring branch reset if a tool was being written, **no report composed**, and
  `resume=True` continues from the last completed step. `/help` and the README match. `abort_all`
  stays — the ⚡ deliver-now button still cancels one role's in-flight call so a queued steer lands
  at once. Tests: `tests/test_hitl_commands.py` (the three wind-down tests replaced by cancellation
  + reply-ordering + no-task-registered).

- 2026-07-31 — **`/status` investigates instead of being handed a digest.** `make_status_answer`
  now builds a `StatusMonitorAgent` (a real ReAct role) over `MonitorToolProvider`, and both
  compaction paths are deleted: `_status_digest` (last 80 events at any level, flattened to
  `[Lx] team · event — summary`, capped at 8k chars) and `_deterministic_status`. The digest was
  the limit — it dropped every event's `data`, so the answers to the questions people actually
  ask ("what did the reviewer decide?", "why did the gate reject it?") were exactly the part
  thrown away, and its window was the tail at *any* level (44 L7 + 18 L10 against 6 significant
  events on a live run, with `cost.call` now claiming ~16 more of those slots). No fallback
  replaces it, by ruling: `status_monitor` is a required role so a misconfigured run fails at
  launch; transient provider errors are already retried at the model layer; and a run whose
  provider is genuinely down cannot advance, so a second, lesser view of the same log would
  report on a stalled run as if it were fine. `_status` therefore lets a reporter failure
  surface. `CommandContext.status_answer` is always wired now. Tests:
  `tests/test_hitl_commands.py` (the two digest tests replaced).
