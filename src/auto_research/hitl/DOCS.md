# `auto_research.hitl` — raise-to-human clarification channel

## Purpose

The cross-cutting **raise-to-human** channel (CLAUDE.md §9.7/§9.8): a single injectable
`HumanHandler` every role uses when blocked, plus pluggable connector backends (CLI stdin,
Slack, Telegram, WhatsApp), an auto-detecting resolver, and the launch-time channel ping. It is a
**clarification** channel, fully decoupled from toolkit merging — the runtime never asks a
human to merge.

## Architecture

A role reaches the human with `message(to="human")` (E12: in-graph seats via
`agents.roles.ChannelTools`, the coder via `sandbox.merge_flow`), routed by **`router.py`**'s
`SeatRouter`. The call builds a `HumanQuery` and returns at once; the router awaits the resolved
`HumanHandler` in a background task (the connector still blocks on its reply — decision #1, no
durable suspend-resume, §12) and delivers the `HumanResponse` to the sender's inbox as a nudge. No
fabrication, no silent hang: with no handler wired `human` is not an address, and
`resolve_human` raises `HumanInputRequired` (the §9.8 headless fail-safe) for a direct call.

- **`router.py`** — the quick channel between any two seats, the human one endpoint (E12).
  `SeatRouter(run_control, human_handler=None, *, monitor=None, mirror=True, store=None)`:
  `register(key, seat)`, `directory(sender)` (every other seat, then `human` when attended),
  `seat(key)`, `state(key)` (working / finished / not started, for `list_peers`),
  `record_message(sender, to, text, *, route)` (ledgers the lead's text to its coder),
  `send(sender, to, text, *, handles, scope, receipt_source, reply_dir, kind, model) -> (text,
  is_error)` (never blocks), `ask_human(query, *, deliver, receipt_source)` (the background wait;
  raises `HumanInputRequired` unattended), `viewable(key)`, `outstanding(key)`. Routes: live seat →
  its `RunControl` inbox; a seat with a kept turn → inbox + resume task (registered on
  `RunControl.register_resumed`, a notice to the sender when it ends); not started → inbox; human →
  background wait, reply (`kind="reply"`) or notice (timeout/fault) to the sender's inbox, a filed
  sender resumed reply-only. Texts mint no receipt; handles a text names are recorded as viewable
  (only those the sender itself can see). The human's files are minted with
  `attachments.mint_reply_attachments(..., parent=None)` — the reply text is never a receipt.
  Mirror: seat-to-seat texts are posted with the handler's `notify(text)` when it has one and
  `orchestration.yaml: channel_mirror` is on. Ledger: `channel.message`, `channel.reply`,
  `channel.reply.timeout`/`.error`, `channel.resume`/`.end`/`.error`, `channel.mirror.error`.

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
- **`attachments.py`** — files on the channel, by receipt ([E11]). `resolve_attachments` turns a
  role's receipt handles into `HumanAttachment`s inside that role's citable view, re-hashing each
  one first, so a connector only ever sees paths the runtime vouched for; `mint_reply_attachments`
  turns the files a connector saved from the human's reply into receipts a role can view; they ground
  nothing (`produced_by="human_reply"` is not evidence under `provenance.store.is_evidence`, R7).
  The caller (the human-bound `message` router, R1's human node) owns both calls; connectors never
  touch the receipt store.
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
  shared reply-wait so `/stop` releases a connector awaiting a human reply (returns
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
  The read runs on a **daemon thread** (`_read_stdin_line`), not on `asyncio.to_thread`'s default
  executor: nothing can interrupt a thread parked in a read on stdin, and the executor's workers
  are non-daemon and joined at interpreter exit with no timeout (bpo-39812), so a timed-out prompt
  used to return on schedule and still wedge the **process**. A daemon thread is abandoned
  instead, and an answer typed after the timeout settles a no-op rather than raising on the
  cancelled future.
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
  (future parallel analysts); a plain reply resolves the single open one. **Sending** (`_send_md`):
  the query (`_query_markdown`: header, body, hint) goes as a Bot API 10.1 **rich message**
  (`_send_rich` → `bot.do_api_request("sendRichMessage", rich_message={"markdown": …})`, since
  python-telegram-bot 22.8 does not wrap it), which renders headings, GFM tables, `$…$`/`$$…$$` math and
  `<details>` natively; parts are ≤30000 chars / ≤400 block lines (`_RICH_CHUNK`, `_RICH_MAX_LINES`; `_block_lines`
  counts a fenced block once) under
  the 32768-char / 500-block limits. A `BadRequest` falls back to HTML for that part; a missing method
  (`AttributeError`/`EndPointNotFound`) sets `_rich=False` for the handler's life;
  `orchestration.yaml: telegram_rich_messages` (default `true`) turns the rich path off. **HTML
  fallback** (`_html_chunks`): `_chunk_md(text, size, *, max_lines=None)` packs `_md_blocks` (a fence,
  or a run of non-blank lines, with each heading glued to the block after it) so a cut never orphans a
  heading or splits a fence/table; an oversize block is split by lines with the fence re-opened
  (a split table repeats its header + separator rows in every part); each
  chunk is measured after `_md_to_html` and re-split if it exceeds 4096. `_md_to_html` renders the HTML
  subset (bold/italic, code, links, headings→bold, bullets→`•`), turns a GFM table into an aligned
  `<pre>` (≤60 chars wide) or a bullet list (`_table_to_md`), and groups quoted lines into one
  `<blockquote>`, `expandable` past 3 lines; `_send` falls back to plain text if Telegram rejects the
  HTML. **Edits** are routed apart (`filters.UpdateType.EDITED_MESSAGE` → `handle_edit`): new-message
  handlers match only `UpdateType.MESSAGE`, so an edited command is never re-run, and an edit to a reply
  already taken as an answer (`_answered`, last 200 ids) gets a notice asking for a new message.
  **Attachments** ([E11]): the query's `attachments` go inside its last rich part — a
  `**Attachments**` section of `![](tg://photo?id=aN "handle — caption")` / `tg://document?id=aN`
  links, `rich_message.media` naming each `attach://aN`, and each file uploaded as its own top-level
  `InputFile` field (python-telegram-bot uploads only top-level files). A PNG/JPEG within `sendPhoto`'s
  limits (10 MB, width + height ≤ 10000, ratio ≤ 20; `_is_photo`) goes as a photo, anything else as a
  document; over 50 MB it is not sent and a note goes both into the message and into
  `HumanResponse.notes` (`_sendable`). When the rich part falls back to HTML (or there are more than
  50 media), the files follow as `send_photo`/`send_document` replies to it (`_send_files`; a rejected
  photo is re-sent as a document), each registered so a reply to any of them answers the question.
  **Inbound**: photos and documents route like text (`_route`: reply-to, else the single open
  question; `handle_media`); the caption is the answer; an album (shared `media_group_id`) answers
  once, `_ALBUM_SETTLE_S` after its last item. Files download (`getFile`, ≤ 20 MB) into the query's
  `reply_dir` as `<message_id>_<safe name>` (`_safe_filename`); no `reply_dir`, an oversize file or a
  failed download becomes a note, never an error. The reply hint offers files only when `reply_dir` is set.
  `start()`/`aclose()` manage the `Application` + polling; `timeout_s` defaults to the shared
  `response_timeout_s()`. Live: `tests/test_live_telegram_render.py` (opt-in,
  `AUTO_RESEARCH_LIVE_TELEGRAM=1`; `…_REPLY=1` adds a round trip) and the cross-channel
  `tests/test_live_hitl.py`.
- `WhatsAppHandler(*, client=None, from_number=None, to_number=None, timeout_s=None,
  poll_interval_s=5)` — **deprecated** (Twilio bills per message; prefer `slack`/`cli` — emits a
  `DeprecationWarning` on construction, CLAUDE.md §13). Sends the Markdown `message` body raw (no
  per-channel polish). `timeout_s` defaults to the shared `response_timeout_s()` when unset.
  Twilio: sends, then **polls** `messages.list` for the inbound reply
  (no public webhook). Auth is via a Twilio **API key** (`TWILIO_ACCOUNT_SID` +
  `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET`), not the root Auth Token, so a leaked key is
  revoked on its own. Live-tested via `tests/test_live_whatsapp.py` (opt-in).

The schemas `HumanQuery` (`id`, `kind ∈ {clarify_science, clarify_code, blocked, report}`, Markdown
`message`, `asked_by`, `model`, `attachments`, `reply_dir`, `created_at`; `sender_label`/`headline`
properties), `HumanAttachment`, and `HumanResponse` (`query_id`, `answer`, `attachments`, `notes`, `created_at`) live in `auto_research.schemas` (the
protocol, §7).

## Dependencies

- Internal: `auto_research.schemas` (`HumanQuery`/`HumanResponse`); reads
  `config/orchestration.yaml` directly. Imported by `auto_research.preflight` (channel check),
  `auto_research.graph.runtime` (resolve + thread; `attach_channel` builds the `SeatRouter`),
  `auto_research.agents.roles` (`ChannelTools` → `SeatRouter.send`), `auto_research.sandbox.merge_flow`
  (the coder's `message`). `router.py` imports `auto_research.hitl.attachments` and, lazily,
  `auto_research.schemas`.
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
- `_read_stdin_line() -> str` — one line of stdin, read on a daemon thread so an
  unanswered prompt cannot hold the process open past the timeout.
- `SlackHandler` / `TelegramHandler` / `WhatsAppHandler` — connector classes (see contracts above).
- `resolve_attachments(lookup, handles) -> (list[HumanAttachment], list[str])` — resolve handles in
  the sender's `ScopedLookup`; the second list is one model-readable sentence per handle not attached
  (not citable, or bytes no longer matching). A paper/page sends its original file (`source_path`); a
  text receipt is named `<handle>.txt`.
- `mint_reply_attachments(store, *, source, response, parent) -> list[Receipt]` — one receipt per
  saved file (`FIGURE_MIME_TYPES` → `figure`, else `data`; `produced_by="human_reply"`, parent =
  the reply's receipt); fills each attachment's `handle` in place. Missing files are skipped.
- `TelegramHandler.handle_media(update)` — answer an open question with a photo/document/album.
- `TelegramHandler.notify(text)` — post Markdown as a read-only notice (`disable_notification`, rich
  then HTML); registers no pending question, so a reply to it answers nothing. Called by E12's seat
  router for each seat-to-seat text when `orchestration.yaml: channel_mirror` is on.
- Ambiguous replies — a plain (non-reply-to) text, file or album that matches no single question
  while two or more are open gets one notice (`_ambiguous_notice`) and answers nothing; an album is
  noticed once (`_unrouted_albums`).

## Changelog
Compacted 2026-09-01 and 2026-09-24 (detail in git history).

- 2026-06-15 -> 2026-06-29 — Package built (Phase 2 step 5): `HumanQuery`/`HumanResponse`,
  `HumanHandler` + `resolve_human`, `resolve_handler`, CLI/Slack/WhatsApp connectors,
  `make_human_channel_check`; shared `response_timeout_s()` (1 h); one Markdown `message` rendered
  per surface; WhatsApp/Twilio deprecated.
- 2026-07-02 -> 2026-07-16 — Telegram is the recommended channel ([E4]): long polling, reply-to
  routing (`message_id→Future`), `_md_to_html`/`_chunk_md`; `start()` pins `httpx`/`httpcore` to
  WARNING so the bot token no longer reaches logs.
- 2026-07-22 -> 2026-07-30 — `commands.py` (`dispatch_command`, `CommandContext`,
  `make_status_answer`, `wait_reply_or_stop`) over `RunControl`; Telegram + Slack `attach_commands`
  (CLI/WhatsApp not command-wired); roster-id targets, `hitl.command`/`.reply` ledger events.
  Rulings: `/stop` forceful with no modifier; buttons replace flags (`CommandReply` with `.buttons`).
- 2026-07-31 — `/stop` calls `RunControl.interrupt()` (scheduled; no report composed). `/status`
  runs a `StatusMonitorAgent` over `MonitorToolProvider`; `_status_digest`/`_deterministic_status`
  deleted, no fallback (ruling).
- 2026-09-12 — CLI prompt timeout no longer hangs exit: stdin is read on a daemon thread
  (`_read_stdin_line`, ruling); a late answer is a no-op.
- 2026-09-25 — [E8] Telegram sends queries and command replies as Bot API 10.1 rich messages (native
  tables/math/headings) via `do_api_request`, falling back to HTML chunked at block boundaries with a
  table ladder (`<pre>` → bullets) and grouped/expandable quotes; edits routed apart (an edited command
  no longer re-runs; an edited taken answer gets a notice). `telegram_rich_messages` flag. LIVE-VERIFIED
  (both sends accepted; 34-payload attack corpus `tests/test_live_telegram_attack.py`: 30 rich, 4
  fell back, 0 errors). Slack dropped by ruling (bolt-python#1529).
- 2026-09-25 — [E11] Media on the Telegram channel, both directions: `hitl/attachments.py`
  (`resolve_attachments`, `mint_reply_attachments`); files embedded in the rich message (Bot API 10.2
  `media`), photo/document replies as the fallback, inbound photos/documents/albums saved into
  `reply_dir`, notes for what could not travel. CLI/Slack/WhatsApp unchanged (ruling: Telegram only).
  LIVE: embedded rich message and both replies accepted by the Bot API; the reply leg is by hand.
- 2026-09-25 — [E12] `router.py`: `SeatRouter`, the quick channel between any two seats with the
  human one endpoint; `message` never blocks (the human's reply arrives in the sender's inbox as a
  nudge), a finished seat is resumed, texts mint no receipt, seat-to-seat texts mirrored via
  `notify` (`channel_mirror`). `ask_human` retired everywhere.
- 2026-09-25 — [E12] `SeatRouter.seat`/`state` (for `list_peers`) and `record_message` (the lead's
  `message(to="coder")`, routed by the lead's channel layer, is ledgered as `channel.message`).
- 2026-09-25 — `TelegramHandler.notify` (silent, answers nothing) for the seat-to-seat mirror; a plain
  reply matching no single open question now gets a notice instead of being dropped (rulings: both).
  LIVE: one silent notify accepted.
- 2026-10-06 — [F26] receipted files read through `provenance.store.read_receipted` (read once, no symlink, hash-checked; a changed file is sent with a note and a `receipt.drift` alert). See `provenance/DOCS.md`.
- 2026-10-17 — Ledger fields recorded whole (user ruling): `channel.message` / `channel.reply` text and answers (were 4,000 chars), `hitl.command*` text and replies (were 400/200); one-line summaries keep their 80-char preview.
