# provenance — DOCS

## Purpose

Bind every claim a graph role makes to the artifact it rests on. The runtime mints a **receipt**
for each artifact (tool output, figure, data file, paper, web page, human answer, commit), and a
role's report prose cites receipts by handle. The check that a report's numbers actually come
from the receipts it cites runs at the role's single exit, `turn_end` (CLAUDE.md §2b).

## Architecture

```
tool call / fetch / ask_human ──► ReceiptStore.mint ──► receipts.jsonl (append-only) + blobs/<sha>
                                         │ handle "<source>:<letter><n>"
report prose ──► check_bindings / check_report_fields ──► problems (turn_end refuses) + bindings
                     └ render_bindings (quote → text, cite → marker) for the finished view
```

- **Receipts are runtime-minted.** The sha256 is computed over the bytes the runtime stored; the
  producer, call id, toolkit commit and args hash are stamped by the caller that ran the work. A
  tool's own claims about its output are never trusted (the gap figure references had).
- **One store per run**, shared by every role, so a handle means the same artifact everywhere and
  survives a resume (the JSONL is replayed on construction). Blobs are content-addressed.
- **Inputs are hashed once per run** (cache key: path, size, mtime); a directory is hashed by its
  listing. An input whose bytes an in-run receipt produced links to it as a parent (the in-toto
  materials→products chain), so ancestry is walkable.
- **Evidence vs record** (human ruling 2026-09-25: "citing in a report must be to sth it has
  produced, not just read"). Every receipt is a record; only `store.is_evidence(receipt)` receipts
  back a number: a toolkit tool's return and its files/figures (producer `research_toolkit.*`), a
  `commit`, a paper (attributed), and the request (`input:`, `research_request`, cite-only).
  `_Resolved.entry` is the one gate: a cited non-evidence receipt is refused by name
  (`NOT_EVIDENCE_NAMES`: "a file you read", "a web page", "shell output", "a task brief", …); in
  scope it is skipped silently. This closes line-number grounding (`read_file`'s `cat -n`) and the
  existence-over-everything-read coincidence class, and makes the shell typed-number rule
  unnecessary (deleted).
- **Flags propagate.** `failed` (the producing call failed) and `synthetic` (an input under a
  marked `synth<YYMMDD>-<sha>` directory, §9.5) pass to every descendant. A flagged receipt can be
  cited to describe what happened; it cannot ground a number.
- **Scoped grounding** (human ruling 2026-09-24, replacing "every number quoted"): a text cites
  `[src:r12]`; every number in that text must appear, on token boundaries, in a receipt it cites.
  Findings are scoped per item; other report fields by all the evidence the report cites. A pinned quote
  `{{src:r12 "…"}}` must be a bounded substring of that receipt. Measured before the ruling over
  231 real reports: 98% of decimals already appear verbatim in the run's tool outputs; quoting
  every number would have meant hundreds of quotes per report.
- **Normalisation** applies to both sides of a comparison and never to what is rendered: NFC (not
  NFKC, which turns `10²³` into `1023`); U+2212 before a digit → `-` (sign-preserving); whitespace
  collapses; paper/web additionally rejoin line-end hyphens, expand ligatures, and drop whitespace
  except between two digits (PDF extraction splits `4 . 0 ± 0 . 2`; measured by the librarian
  session on the Docling cache).
- **Boundary rule** closes the sign-flip hole found in data-to-paper: a needle starting with a
  digit may not follow a digit, `.`, or a sign (`+/-` excepted, it is an error bar); one starting
  with a sign may not follow a digit or `.` (a range, not a minus); one ending in a digit may not
  precede a digit, `.digit` or an exponent.
- **Exemptions** (numbers that name things): Figure/Table/Eq. references, 4-digit years (covering
  "et al. 2022"), astronomical source designations (`GRS 1915+105`). A markup caller passes
  further `exempt_spans` (e.g. a math index).

## Interfaces / Contracts

- `ReceiptStore(root)`: `mint(...) -> Receipt`, `hash_input(path) -> InputFile`, `get(handle)`,
  `text(handle)`, `verify(handle)`, `ancestors(handle)`, `receipts(source=, kind=)`.
  Implements `ReceiptLookup`. Thread-safe minting (one lock).
- `check_bindings(prose, lookup, *, own_source, scope, exempt_spans, label) -> BindingResult`.
- `check_report_fields(fields, lookup, *, own_source, itemised=("findings",)) -> BindingResult`.
- `render_bindings(prose, *, own_source, quote, cite) -> str`.
- `ReceiptStore.for_run(run_dir) -> ReceiptStore` — the one store per run in this process; every
  role gets its store here (two instances on one directory would mint duplicate handles).
- `store.is_evidence(receipt) -> bool`; `TOOLKIT_PRODUCER_PREFIX = "research_toolkit."`,
  `REQUEST_PRODUCER = "research_request"`, `NOT_EVIDENCE_NAMES` (producer → how a refusal names it).
- `calls.resolve_call_args(arguments, context) -> dict` — the arguments a worker's call runs with
  (the model's, completed by the runtime's context); `calls.args_digest(args)` — the
  `Receipt.args_sha256` hash; `calls.ledger_calls(path) -> list[LedgerCall]` — every call in a
  ledger that minted receipts, with its rebuilt `ran_args` (what the receipts hash) and `sent_args`
  (what the tool received: without the runtime-only `env` / `shell_env`); a call answered without
  running, or one that does not rebuild, is left out; `calls.calls_by_receipt(calls, receipts) ->
  {handle: LedgerCall}` — a receipt matched to its call by identity (source = actor, `call_id`
  named on the `tool_result` line) and proven by the rebuilt-args hash; unmatched otherwise. Any
  subset of receipts in any order. `calls.FIGURE_PATH_ARGS`.
- `ScopedLookup(store, *, own_source, admitted=())` — what one agent may cite: its own receipts
  (live) plus admitted handles and their ancestors; `.admit(handles)`, `.handles()`.
- `capture.inputs_before(store, args, *, cwd, skip_keys, observation_paths=None) -> CallInputs` and
  `capture.receipt_call(store, *, source, produced_by, args, text, failed, state, figures, cwd,
  toolkit_root, kind, fields) -> CallReceipts` — one tool call to receipts; `.header()` is the line
  the model sees. `capture.toolkit_commit(root)` reads HEAD (read-only).
- Invariant: every problem string names what is wrong and what to do, because it is returned to
  the model as a `turn_end` refusal. Only a receipt the text itself cites produces a problem; a
  scope receipt that is missing, stale or flagged silently grounds nothing.

## Dependencies

`schemas.artifacts` (Receipt, InputFile, handle pattern), `schemas.figures` (FigureRef, via
`Receipt.to_figure_ref`). Standard library only otherwise. Used by `agents.react` (minting in the
tool providers, the report tools, `turn_end`), the librarian tools, the reviewer tools and the
report renderer.

## Dictionary-level reference

- `ReceiptStore.mint(*, source, kind, produced_by, data=None, path=None, call_id=None,
  toolkit_commit=None, args=None, inputs=(), parents=(), failed=False, **fields)` — exactly one of
  `data` (stored as a blob) / `path` (hashed in place). `fields` carries the kind-specific receipt
  fields (`caption`, `mime_type`, `title`, `ids`, `source_sha256`, `parser`, `commit_sha`).
- `ReceiptStore.hash_input(path) -> InputFile` — cached per (path, size, mtime); links `receipt`
  when the bytes match an in-run receipt.
- `ReceiptStore.hash_obsdb_entry(mission_dir, obsid) -> InputFile | None` — an observation a tool
  located from its ObsID (user ruling 2026-10-16: what it is and a hash, never a copy): basis
  `obsdb`, sha256 over the catalog entry (`catalog.json`) plus the folder listing; read fresh each
  call; an uncatalogued ObsID-like folder is its `listing`; `None` otherwise. `capture.inputs_before(observation_paths=)`
  records one per string any argument holds (through lists and dicts, e.g. `load_data`'s
  `epoch={"IXPE": [...]}`) that a mission folder's catalog lists; a string naming a subfolder the
  catalog does not list, and that holds a digit, is that folder's `listing` (fresh each call; a
  mission folder never is); with no observation named in a folder the caller named itself
  (`named_folders`), that folder's `catalog.json` is the input (`data_catalog`); a folder the runtime
  filled in records nothing. Integer ObsIDs count. A `synth<YYMMDD>-<sha>` or hand-authored `synth_<name>`
  folder flags the result synthetic.
  Relative folders resolve against the call's `cwd`.
- `ReceiptStore.text(handle) -> str | None` — the text a quote is checked against (result, web,
  paper, human; a data file when it decodes as UTF-8).
- `ReceiptStore.verify(handle) -> str | None` — re-hash; a problem sentence or `None`.
- `ReceiptStore.ancestors(handle) -> list[Receipt]` — transitive parents.
- `normalise(text, kind) -> str`; `bounded_find(hay, needle) -> bool`.
- `parse_bindings(prose, *, own_source) -> list[Binding]` — `self:` resolves to `own_source`.
- `Binding(start, end, handle, quote)`; `BindingResult(problems, bindings)`, `.cited`.
- `is_synthetic_path(path)`, `sha256_file(path)`.
- `capture.data_products(state, text, *, failed, figure_paths, stat, is_file)` — the one statement
  of the data-product rule, used by `receipt_call` and the gate.
- `capture.call_started_ns()` — call start on `CLOCK_REALTIME_COARSE` so it compares with mtimes.
- `store.checkable_text(raw)` — the decode rule behind `ReceiptStore.text`.
- `bindings.number_sources(prose, usable, *, masked) -> list[NumberSource]` — every number the
  grounding rule checks, with every usable receipt holding it (`NumberSource(start, end, value,
  handles)`); `check_bindings(sources=True)` / `check_report_fields(sources=True)` fill
  `BindingResult.numbers` keyed by text (what a report page shows as each number's source).
- `bindings.ungrounded_numbers(prose, usable, *, masked)` — the number-grounding loop, used
  by `check_bindings` and the gate.

## Changelog
Compacted 2026-10-07 (the 2026-10-06/07 entries); detail in git history.

- 2026-09-24 — Created (P0): `Receipt` schema, the run store, and the scoped-grounding check with its
  normalisation, boundary rule and exemptions. P1: `capture.py` (inputs hashed before a call; result,
  figure — re-hashed from the file — and data receipts after), `ReceiptStore.for_run`, `ScopedLookup`.
- 2026-09-24 — `check_report_fields` gains `scope` and one resolution cache per report. Grammar: a digit
  after letter+hyphen is not a number (`Cyg X-1`); `2-8` is a range. Residual: a file a tool locates
  itself from an identifier is not hashed.
- 2026-09-24 — Inputs are receipts: `ReceiptStore.mint_input(text, kind=, produced_by=)` under source
  `input`, visible to every unit, but grounding only a text that cites `[input:…]` (kept out of the
  report-wide union and `scope`).
- 2026-09-24 — `normalise` folds math alphanumerics (U+1D400–U+1D7FF only) and, for paper/web, rejoins
  line-broken words and keeps a table-row sign glued to its number. `Receipt.source_path`/`source_sha256`
  name a paper's original file (`verify()` refuses a missing or changed one).
- 2026-09-24 — Refusal hints: the closest passage (whole words, itself quotable), the other visible
  receipt holding the quote, never unsigned text for a signed quote.
- 2026-09-24 — Exempt as names, not values: ordinals (`worker 0`, `DU 2`, `round 3`), list enumerators,
  DOIs/bibcodes/`astro-ph/…`/prefixed arXiv ids, HTTP status codes with reason phrase, sexagesimal
  coordinates.
- 2026-09-24 — Ruling: papers are cited, not checked — `store.text()` is `None` for `%PDF-` bytes. The
  interim `cited_only_kinds` (sentence-scoped) was superseded the same day by attribution
  (ruling): `{{h "value" <locator>}}` for `ATTRIBUTION_KINDS` (`paper`), value unmatched.
- 2026-09-24 — `paper_attributions(prose, lookup, *, own_source)` returns `PaperAttribution` (alias
  `Attribution`), the one verify-list shape; `check_bindings(support=…)` only grounds. Limit: an
  attributed value is unverified.
- 2026-09-24 — Laundering closed: the store lives beside the ledger (`record_beside(run_dir,
  "receipts")`), outside every allow-list; numbers typed into a `SHELL_TOOLS` command
  (`Receipt.arg_numbers`, `ReceiptStore.typed_numbers`) never ground. Residual: via a toolkit-written file.
- 2026-09-25 — [G15] `data_products`, `checkable_text`, `ungrounded_numbers` factored out for the
  gate's return check; `call_started_ns` moved to the coarse realtime clock (200/200 fast writes had
  read as older than their own call, so were never receipted).
- 2026-09-25 — [R7] `is_evidence` (human ruling): only toolkit returns, commits, papers and the
  request ground; `_Resolved.entry` refuses a cited record by name and skips it in scope. Deleted
  the shell typed-number machinery (`_Resolved.typed`, `echoes`, `ReceiptStore.typed_numbers`,
  `capture.typed_numbers_in`); `Receipt.arg_numbers` kept as a legacy field. `store.text` reads
  `commit` receipts.
- 2026-10-06 — **[F22] [F26] Receipted files.** A figure or data file is receipted only if it is not
  a symlink and resolves inside the allow-list (`capture.receipt_call(reachable=)`, default
  `_allow_listed`; the host `/tmp` excluded while jailed). `ReceiptStore.for_run` never uses a
  legacy in-run-folder store (`record_beside(..., legacy_ok=False)`). Human ruling 2026-10-06
  (alert, still send): `store.read_receipted(path, sha256)` opens component by component without
  following links (`_open_without_symlinks`), reads once and hashes what it returns; changed bytes
  come back with a model-visible `[changed]` note and a `receipt.drift` ledger event
  (`install_drift_alert`, once per file and bytes per run). Used by the media layer, report figure
  staging, human attachments, `store.text` and `text_with_note` (`view_receipt`); citations at
  `turn_end` refuse changed bytes.
- 2026-10-06 — **Env plan B1–B3b: every receipt names its environment.** `Receipt.env`
  (fingerprint) via `ReceiptStore.mint(env=)` / `capture.receipt_call(env=)`;
  `ReceiptStore.default_env` (the orchestrator's, set by `run_research`) for a call naming none;
  with `env_default=`, a call whose environment cannot be fingerprinted, or a configured
  non-builtin MCP server's call, stays unstamped. Worker sandbox calls stamp the REPL's
  fingerprint, its lock under `<runtime>/conda/locks/`. `Receipt.env_untested` /
  `receipt_call(env_untested=)`: run outside the package's tested environment, still evidence.
  `NOT_EVIDENCE_NAMES['list_env']`. Grounding exempts a hex identifier (12+ lowercase hex with a
  letter a–d or f) and `env:<hex>`.
- 2026-10-07 — **Input hashing never ends a call (B5 live cell).** `ReceiptStore.hash_input` lists a
  folder with `surrogateescape` (a non-UTF-8 name had ended a run) and leaves one of more than
  `_MAX_LISTED_FILES` (20,000) files unhashed; `capture.inputs_before` skips an input whose hashing
  fails (§9.3.8).
- 2026-10-17 — [provenance replay, step 1] `calls.py`: the call-argument rewrite moved here from `agents.react` (`resolve_call_args`, `FIGURE_PATH_ARGS`), `args_digest` (now what `ReceiptStore.mint` stamps), `ledger_calls` and `calls_by_receipt`. A receipt's exact arguments are rebuilt from the ledger, never stored; the hash proves the rebuild.
- 2026-10-17 — Step-1 review fixes: `calls_by_receipt` joins by identity (the receipt's `call_id`, recorded on the `tool_result` line) and keeps the hash as proof — order pairing had matched a receipt to a not-run twin of another tool; `ledger_calls` drops calls that minted nothing and skips malformed lines; `LedgerCall.sent_args`.
- 2026-10-17 — Step-1 re-review fixes: `ledger_calls` pairs a `tool_call` with its `tool_result` by actor, loop (`<role>.<action>`), tool and id (two overlapping loops of one actor reusing an id had crossed), streams the file, and skips a call whose context does not parse; `sent_args` documents the shell's activation wrapper. Residual: `args_digest` uses `default=str` (a non-JSON argument value hashes as its string; adapters pass JSON values).
- 2026-10-17 — [provenance replay, step 2] Obs-db inputs (`hash_obsdb_entry`, `inputs_before(observation_paths=)`, store helpers `_listing`/`_catalog_entries`); `receipt_call` stamps `toolkit_root` beside `toolkit_commit`. Closes §13 (a) for ObsIDs in an obs-db catalog.
- 2026-10-17 — Step-2 review fixes: ObsIDs are found through dicts (the real `load_data` passes `epoch={...}`, so none had been recorded), relative mission folders resolve against `cwd`, an uncatalogued observation folder is a `listing` input, `obs_db_path` is a mission folder (never listed whole: the IXPE folder took 2.9 s per call and then failed the 20,000-file limit); `receipt_call` stamps `toolkit_root` (resolved) on toolkit producers only; `_listing` stats each file once and skips a file removed while listed. Residuals: the observation is hashed before the call (a reduction writing into it during the call is not seen); an `obsdb` input links no in-run receipt that wrote the folder; the lead's in-process `data_catalog` records no input.
- 2026-10-17 — [provenance replay, step 3] `number_sources` / `NumberSource` and `BindingResult.numbers` (`sources=True`): the grounding check's own per-number answer, shared with `ungrounded_numbers` through `_scanned_numbers`.
- 2026-10-17 — Step-2 re-review fixes: `is_synthetic_path` also matches the obs-db's hand-authored fixtures (`synth_<name>`); the uncatalogued fallback lives in `hash_obsdb_entry` (fresh listing, ObsID-like names only, never a mission folder) and the catalog is read once per folder; a call naming no observation records `catalog.json` (the lead's in-process `data_catalog` too, through `_receipted(state=)`); integer ObsIDs and `./x`, `x/` forms; a NUL or loop in a path records nothing instead of ending the call; mission folders in sorted order; `_listing` skips any unreadable entry.
- 2026-10-17 — `inputs_before(named_folders=)`: `catalog.json` is recorded only for a mission folder the caller named (an auto-filled `<mission>_path` had put every mission's catalog into every call's inputs).
- 2026-10-17 — A cited non-evidence receipt's refusal ends "state anything else without a value" (it had said "in words", inviting a spelled-out number).
