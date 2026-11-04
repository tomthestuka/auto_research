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
                     ├ render_bindings (quote → text, cite → marker, value → display) for a page
                     └ render_values ({{h#/k}} → "2.72 [h#/k]") for a model reading the report
```

- **Receipts are runtime-minted.** The sha256 is computed over the bytes the runtime stored; the
  producer, call id, toolkit commit and args hash are stamped by the caller that ran the work. A
  tool's own claims about its output are never trusted (the gap figure references had).
- **One store per run**, shared by every role, so a handle means the same artifact everywhere and
  survives a resume (the JSONL is replayed on construction). Blobs are content-addressed.
- **Inputs are hashed once per run** (cache key: path, size, mtime); a directory is hashed by its
  listing. An input whose bytes an in-run receipt produced links to it as a parent (the in-toto
  materials→products chain), so ancestry is walkable.
- **References link a call to the results it used** (`refs.py`, 2026-10-26). A toolkit argument
  `{"$ref": "<handle>#<json-pointer>"}` is replaced, before the call runs, by the value at that
  pointer in the JSON the receipt holds; the receipt becomes a parent of everything the call mints.
  Only the caller's own toolkit results resolve (user ruling 2026-11-03, the unit's input
  validator): another unit's result is refused with "reproduce it with a tool run of yours", so no
  tool echo can make a peer's value the caller's evidence; a navigation receipt, a failed result or
  changed bytes is refused with a sentence the model acts on. The ledger and the receipt's
  `args_sha256` keep the references as written; the values they stand for are fixed by the
  parent's hash and the pointer. This replaced the retyping measured in run 32814459, where every
  derived input was typed into the arguments, so no call had a parent and two hand-built plot
  inputs carried altered error bars.
- **The input validator decides which arguments must be references** (`inputs.py`, user rulings
  2026-11-03; CLAUDE.md §9.3.9). Every argument of a toolkit tool declares a `kind` in its
  manifest: `data` (a tool's output: references into what the caller's own tools returned, never
  a call's arguments), `parameter` (a reference to the caller's own result, or a typed
  `{"value": …, "reason": "…"}`, or left out for the declared `default`), `path` (typed, or a
  reference to a file the caller's own tool wrote) and `setting` (typed). `check_arguments` judges
  the arguments as the model wrote them, before the runtime fills mission folders and defaults
  (`calls.resolve_call_args`); an argument the manifest does not declare is refused, and so is a
  reason on any kind but a parameter. `tool_values` then gives the tool plain values. The call's
  arguments, which `args_sha256` hashes, keep each reason and reference, so a chosen value and why
  it was chosen sit behind the handle of the call that used it: `<handle>#/$args/<name>` reaches
  it (`refs.argument_value`), for a later call or a report, and only a typed parameter or a
  reference is reachable that way (a setting, a path or a default carries no source). This
  replaced the copy refusal (`RetypeCheck`, 2026-10-26 → 2026-11-03), which compared typed floats
  with what results held: by ruling, a typed number now enters only with its reason, shown in every
  result built on it, and nothing compares its digits. The ledger studies behind the design (three
  runs, 926 calls) are the case tables of `tests/test_input_cases.py`.
  A toolkit tool prints summary lines with its result and returns the dict beside them (coder
  contract rule 5), and the result text keeps only the print; so `receipt_call(returned=)` stores
  the returned keys as one JSON blob named on the receipt (`returned_sha256`, hash-checked on every
  read), and a reference resolves against that. A brief that names a handle not in the run is
  refused at the author's `turn_end` (`brief_handle_problems`: the lead's `what_to_probe`, the
  reviewer's `follow_up`).
- **View is global, citing is scoped** (user ruling 2026-10-27). Any unit opens any receipt of the
  run (`RunView`: `view_receipt`, a `$ref`, a message's handles); what it may rest a number on is
  its own `ScopedLookup`.
- **Value references** (user ruling 2026-10-27: every value passes as a handle, the validator
  renders it). Report prose writes `{{<handle>#/<json-pointer>}}`; `check_bindings` resolves it
  against the result's returned value (`refs.returned_value`) in the writer's citing scope and
  refuses a flagged result, a text-only one, a missing key or a pointer at a list or dict. The value
  is shown at `DISPLAY_DIGITS` (3) significant digits (`display_value`), the full value one click
  away on the page. A typed number of `COPY_MIN_DIGITS` (3)+ significant digits that a cited
  structured result returned (echoed arguments aside) is refused with its reference, and so is a
  quote of one; an ungrounded number that rounds a returned value is answered with the reference
  that renders it. Text after a quote is accepted only for a paper: in run faraday-refs-20261026,
  `{{h "key": 2.71}}` read `key` as the quote and hid 46 of 83 numbers in the unchecked locator.
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
- **Every number is its own citation** (user ruling 2026-11-03, one citing syntax, replacing the
  2026-09-24 *scoped grounding* under which a `[src:r12]` let any number its receipts held stand
  beside it). Citations are `{{src:r12}}` (a whole receipt), `{{src:r12#/p}}` (a returned value;
  `#/$args/<name>` a value the call was given), `{{src:r12 "…"}}` (a bounded substring of a text
  receipt) and a paper's attribution with its place. A number outside one is refused; the receipts a
  text cites, its scope and its support suggest the citation to write.
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
- `render_bindings(prose, *, own_source, quote, cite, value=None) -> str` — `value(binding)`
  renders a value reference (left as written without one).
- `bindings.VALUE_RE`; `Binding.pointer` (a value reference's JSON pointer, else `None`);
  `bindings.display_value(value) -> str` (an int whole, a float to `DISPLAY_DIGITS` significant
  digits, `e` notation outside 1e-3..1e6); `bindings.referenced_value(lookup, binding)`;
  `bindings.render_values(prose, lookup, *, own_source=None) -> str` — each reference followed by
  its value and source, `2.72 [h#/k]`, for a model (a number with no source was typed); `DISPLAY_DIGITS = 3`, `COPY_MIN_DIGITS = 3`.
- `RunView(store)` — a `ScopedLookup` that sees every receipt of the run (viewing, never citing).
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
  toolkit_root, kind, fields, parents=()) -> CallReceipts` — one tool call to receipts; `parents`
  (the call's `$ref` receipts) go on every receipt it mints; `.header()` is the line the model sees
  (`[receipt h] ← parents`). `capture.toolkit_commit(root)` reads HEAD (read-only).
- `refs.resolve_refs(args, store, usable) -> (resolved_args, parents)` — every `{"$ref": …}` in an
  argument tree replaced by its value; `usable(handle)` is the caller's scope; raises `RefError`
  (its message is the model's) on the first that does not resolve, so nothing runs. A `data`
  receipt the caller's tool wrote resolves to its path while the file hashes to its receipt
  (digest cached by path, size and mtime); `<handle>#/$args/<name>` to an argument of the
  caller's earlier call. A brief (`input:`) is never a source: it is informal, like a message
  (user ruling 2026-11-03); a value it states is typed with its reason.
- `inputs.check_arguments(tool, arguments, schema, *, runtime=frozenset()) -> str | None` — the
  refusal for arguments, as written, that break their kinds (`runtime`: names the runtime consumes,
  `env`); `inputs.tool_values(args, schema, store, usable) -> ToolValues(sent, parents)` — the
  completed arguments as the tool receives them. Both call paths use them: the worker's
  `SandboxToolProvider.execute` and the lead's in-process `execute_data_catalog`.
  `ledger_calls(...).sent_args` keep the references; `report.html.reproduce_code` resolves them
  from the store for a rerun.
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
- `refs.REF_KEY = "$ref"`; `refs.is_ref(value)` (exactly one key, a string); `refs.split_ref(text)
  -> (handle, pointer)` (no `#`: the whole value); `refs.refs_in(args) -> list[str]`.
- `refs.passable(store, handle) -> bool` — a toolkit tool's result (kind `result`, evidence, not
  `input:`); the only kind a reference resolves.
- `refs.returned_value(store, handle)` — the receipt's JSON, its `CONSOLE_SEPARATOR` tail set
  aside (`sandbox.runner.normalize_tool_raw` writes it); `RefError` on changed bytes or plain text.
- `refs.resolve_one(store, text, usable) -> (value, handle)`; RFC 6901 pointers (`~0`, `~1`); a
  miss names the keys or the index bound that exist.
- `refs.handles_named(text) -> list[str]` — every handle-shaped string a text names, bracketed or
  bare, once each (a message's handles).
- `inputs.ARG_KINDS = ("data", "parameter", "path", "setting")`; `inputs.declared_kinds(schema)`,
  `inputs.declared_defaults(schema)` (parameters with a `default`, which `_call_context` records as
  `defaults`); `inputs.manifest_problems(tool, schema) -> list[str]` (no kind, an unknown kind, an
  optional parameter without a default: refused at launch, at the gate and at the call).
- `refs.VALUE_KEY, refs.REASON_KEY = "value", "reason"`; `refs.typed_parameter(value) -> (value,
  reason) | None` (exactly those two keys, a non-empty reason, no reference inside; a JSON string
  of one is read as the REPL would).
- `refs.ARGS_POINTER = "/$args"`; `refs.point(store, handle, pointer)` (the returned value, or the
  arguments under `/$args`); `refs.argument_value(store, handle, pointer)`;
  `refs.argument_source(store, handle, pointer) -> str` ("chosen by X: reason", or "passed from
  <the reference at the pointer>", followed through a `$args` chain to its reason);
  `refs.received_arguments(store, handle) -> dict` (a call's arguments as its tool received them:
  typed parameters unwrapped, references resolved; `leaves()` excludes every number in it, so an
  echo of a value a reference carried is never credited to the echoing call);
  `refs.call_arguments(store, handle)` (the call's recorded arguments, `{}` when absent or changed).
- `refs.unviewable_handles(text, viewable)`, `refs.brief_handle_problems(field, text, viewable)` —
  the handles a brief names that are not receipts of the run, and the `turn_end` refusal naming
  them.
- `ReceiptStore.put_blob(data) -> (path, sha256)` (content-addressed, atomic) and
  `ReceiptStore.blob_path(sha256)`; `receipt_call(returned=)` sets `returned_sha256`.

## Changelog
Compacted 2026-10-07 (the 2026-10-06/07 entries) and 2026-10-19 (every entry); detail in git history.

- 2026-09-24 — Created (P0/P1): `Receipt`, the run store, scoped grounding (normalisation, boundary
  rule, exemptions); `capture.py`, `ReceiptStore.for_run`, `ScopedLookup`; `check_report_fields(scope=)`;
  `ReceiptStore.mint_input` (source `input`, grounds only a text citing `[input:…]`); math-alphanumeric
  folding; `Receipt.source_path`/`source_sha256`; refusal hints; `Cyg X-1`, `2-8` a range; exempt
  names (ordinals, enumerators, DOIs/bibcodes/arXiv ids, HTTP codes, sexagesimal coordinates).
- 2026-09-24 — Ruling: papers cited, not checked; attribution `{{h "value" <locator>}}` for
  `ATTRIBUTION_KINDS` (supersedes `cited_only_kinds`); `paper_attributions` → `PaperAttribution` (alias
  `Attribution`). Laundering closed: store beside the ledger (`record_beside`), shell-typed numbers
  never ground (`Receipt.arg_numbers`, `ReceiptStore.typed_numbers`).
- 2026-09-25 — [G15] `data_products`, `checkable_text`, `ungrounded_numbers`; `call_started_ns` on the
  coarse realtime clock. [R7] `is_evidence` (human ruling); deleted `_Resolved.typed`, `echoes`,
  `ReceiptStore.typed_numbers`, `capture.typed_numbers_in`; `Receipt.arg_numbers` legacy.
- 2026-10-06 — [F22] [F26] only non-symlink, allow-listed files receipted (`receipt_call(reachable=)`,
  `_allow_listed`; `record_beside(..., legacy_ok=False)`). Human ruling 2026-10-06 (alert, still send):
  `store.read_receipted`, `_open_without_symlinks`, `[changed]` note, `receipt.drift`
  (`install_drift_alert`); `turn_end` refuses changed bytes.
- 2026-10-06 — Env plan B1–B3b: `Receipt.env` (`mint(env=)`, `receipt_call(env=)`, `default_env`),
  `Receipt.env_untested` (still evidence); `NOT_EVIDENCE_NAMES['list_env']`; hex ids, `env:<hex>` exempt.
- 2026-10-07 — B5 live cell: input hashing never ends a call (`hash_input` `surrogateescape`,
  `_MAX_LISTED_FILES`; `inputs_before` skips a failure, §9.3.8).
- 2026-10-17 — Provenance replay step 1 (+ two review rounds): `calls.py` — `resolve_call_args`,
  `FIGURE_PATH_ARGS` (from `agents.react`), `args_digest` (stamped by `mint`), `ledger_calls`,
  `calls_by_receipt` (joined by `call_id`, hash as proof), `LedgerCall.sent_args`. Residual:
  `args_digest` uses `default=str`.
- 2026-10-17 — Step 2 (+ two review rounds): obs-db inputs (`hash_obsdb_entry`,
  `inputs_before(observation_paths=, named_folders=)`, `_listing`, `_catalog_entries`); `toolkit_root`
  stamped on toolkit producers; `is_synthetic_path` matches `synth_<name>`; `_receipted(state=)`.
  Closes §13 (a) for catalogued ObsIDs. Residuals: observation hashed before the call; an `obsdb`
  input links no in-run receipt.
- 2026-10-17 — Step 3: `number_sources`/`NumberSource`, `BindingResult.numbers` (`sources=True`),
  `_scanned_numbers`. A non-evidence refusal ends "without a value" (was "in words").
- 2026-10-26 — Bug fix (audit of run 32814459: no call had a parent; retyped plot inputs altered):
  `refs.py` (`resolve_refs`, `RefError`, `passable`, `returned_value`, `handles_in_brief`,
  `RetypeLint`); `receipt_call(parents=)`; `CallReceipts.header` names parents. User rulings
  2026-10-26: a reference may name a receipt the caller can open but not cite; a typed number gets a
  note, not an edge (the "typed numbers are not evidence" rule is a later ruling).
- 2026-10-26 — Adversarial review round 1 (all confirmed, fixed): references resolve against the
  returned keys (`returned_sha256`, `put_blob`, `blob_path`; real toolkit results are print + JSON
  line, which the text-only resolver refused); pointer indexes match `0|[1-9][0-9]{0,8}` (a `²` or a
  5000-digit index raised past the provider); brief handles checked against the author's reach
  (`brief_handle_problems`); resolution and lint off the event loop, lint cache per store, floats
  only; refusal of an inherited `failed` says so.
- 2026-10-26 — Live B1 (claude-sonnet-5): a reference written as a JSON string into a number-typed
  argument now resolves (`refs._ref_text`; also in the reproduce code), and `#/` means the whole
  value. `REF_NOTE` names numbers among the values a reference can fill.
- 2026-10-26 — User ruling (live B2: a peer's scalar typed twice, the note ignored): a typed copy is
  refused before the call runs (`RetypeCheck`, replacing `RetypeLint`); `REF_NOTE` and the worker
  prompt say so.
- 2026-10-26 — Adversarial review round 2 (all confirmed, fixed): `RetypeCheck` ignores floats the
  producing call was given (echoed parameters such as `cl`, `e_edges`; `receipt_call` stores the
  call's arguments as the blob `args_sha256` names, `calls.canonical_args`), names only results a
  reference resolves (no failed ones), scans 4+-digit floats only and skips returned values over
  2 MB; the returned blob keeps the tool's key order; the brief check reads bracketed handles
  only and accepts handles a message showed the author (`viewable=`).
- 2026-10-27 — Batch after run faraday-refs-20261026 (user rulings 2026-10-27). Value references
  `{{h#/pointer}}` in report prose (`VALUE_RE`, `display_value`, `render_values`,
  `referenced_value`); text after a quote refused on a non-paper receipt; typed and quoted copies of
  returned values refused with the reference; rounded numbers answered with it. `RunView`: view is
  global, citing stays scoped; a `$ref` resolves any toolkit result of the run; `handles_in_brief`
  replaced by `handles_named`. `RetypeCheck` refuses sign-flipped and rounded copies and values
  copied from the caller's own reads and shell output.
- 2026-10-27 — Stateless review (all confirmed, fixed): a reference to a flagged result is refused;
  `VALUE_RE` takes full handles only (a `self:` reference would bind to whoever copies it), keys
  with spaces, and `{{h#}}` for a whole return; leftover `{{`/`}}` markup is refused (it was read as
  nothing); a quote overlapping a reference is no binding; the copy check skips integers, exempt
  shapes and values the cited request states; the rounding hint names only a result the text cites,
  and only when one value fits; returned values over 2 MB are not parsed; `render_values` leaves
  quotes and attributions whole (they were stripped to their text). `RetypeCheck`: navigation
  receipts are scanned run-wide (view is global), sign and rounding only over the caller's own
  results, rounding only for 6+ typed digits (a 4-digit parameter collided with peers' values).
- 2026-11-03 — `$ref` resolves only the caller's own results (`SandboxToolProvider._usable_handles`
  = its own source); `resolve_one` names a peer's result as such; `RetypeCheck` refuses a typed
  value a peer's result holds exactly, with "reproduce it". (User ruling: an input validator that
  checks the referenced data's owner is self.)
- 2026-11-03 — Review of the input validator (confirmed, fixed): `RetypeCheck` reads the arguments as
  the tool receives them (`_as_received`: a JSON-container string parsed as the REPL does, a number
  string as its float); a peer's value is refused exactly, sign-flipped or rounded (6+ digits),
  unless the caller chose it before or the request states it; `resolve_one` names the request and
  a place to look by what they are before the owner check; the refusal header no longer says
  "pass" where the line says "reproduce". `render_values` shows `2.72 [h#/k]` (user ruling): a
  number with no source after it was typed.
- 2026-11-03 — Second review of the input validator (confirmed, fixed): a written `[handle#…]`
  source tag is leftover markup (it would pass for a rendered reference); a peer's value is matched
  only at 6+ significant digits, exactly or sign-flipped (a run-wide rounded match refused fresh
  parameters, a short one refused constants); briefs and the request are task texts whose values
  are parameters, not files read; the arguments a result's call was given are read as the tool
  received them (`_chosen_by`, cached); `RetypeCheck.problem(typed_args, handles)` (no
  `own_source`).
- 2026-11-03 — A file the human sent is a file read again for the copy check (not a task text);
  the prose check reads a result's arguments as the tool received them; a written `[handle#…]` is
  named whole in its refusal, with `[handle]` offered for citing a whole result.
- 2026-11-03 — **The input validator's tool-input job** (user rulings 2026-11-03; plan
  `tmp_plan_handles_only.md`, piece A). New `inputs.py`: argument kinds, `check_arguments`,
  `tool_values`, `manifest_problems`, declared defaults. `refs.py`: `RetypeCheck` and its helpers
  deleted (ruling 1); a file receipt resolves to its path (hash cached), a call's
  `#/$args/<name>` (typed parameters and references only);
  `call_arguments`, `typed_parameter`, `argument_value`, `argument_source`. `calls.resolve_call_args`
  fills `defaults`. Stateless review (10 findings): fixed — undeclared arguments refused (the real
  `load_data` takes seven science arguments its schema omits), `$args` limited to typed parameters
  and references, data refuses a call's arguments, a reason on a non-parameter
  refused, `null` is no default, file digests cached, the lead path fills defaults, the refusal
  names no value to wrap, `sent_args` documented. Tests: `test_input_validator.py`,
  `test_input_cases.py` (the ledger studies' cases plus every kind × shape × target; 53 fail with the
  check switched off).
- 2026-11-03 — Brief parameters (a `# parameters` block resolved as `input:hN#/parameters/<name>`,
  briefly built) purged by user ruling: a brief is an informal text, like a message, never citable
  and never a source; in a graph of MTA units only a report filed through `turn_end` is citable.
- 2026-11-03 — A chosen value is stated through its call's argument only (user ruling 2026-11-03):
  `_Resolved.given(handle)` maps each number a call was given as a typed parameter or by reference
  to its `/$args/…` pointer; a value reference into the tool's echo of one, and a typed copy of
  one, are refused with `{{handle#/$args/<name>}}`, the form that carries who chose it and why. A
  data argument naming a task text (`input:`) is refused as a tool's output, with where its data
  comes from.
- 2026-11-03 — **One citing syntax** (user ruling 2026-11-03; build item 9): `CITE_RE` reads
  `{{handle}}`; a written `[handle]` is leftover markup (`_MARKUP_LEFT`), answered with `{{handle}}`.
  Scoped grounding is retired: `check_bindings` refuses every number outside a binding (exempt
  shapes stay), and the receipts a text cites, its scope and its support only suggest the binding
  (`_copies` references and `$args` pointers, `_rounded_from`, `_quotable` quotes for up to three
  holders). The typed-copy and ungrounded-number clauses are one clause. A commit receipt's record
  takes value references. `refs.unviewable_handles` reads a brief's `{{handle}}` citations.
  `ungrounded_numbers` is unchanged (the gate's return check).
- 2026-11-03 — Review of the one citing syntax (11 findings): fixed — a paper's locator holds a
  place only (`LOCATOR_RE`; closes §13 [R8] "numbers inside a quote's locator are not scanned"),
  an ordinal never swallows a decimal (`trial 4.02`), any bracket holding a handle is retired
  markup, a bare short-number quote (`{{h "3"}}`) binds nothing and is never suggested, scope and
  support are read only when a number needs a citation and rounding hints read only the cited
  results (8.6 s → bounded on a 300-result scope), a brief's retired `[h]` is still checked as a
  name. New clauses `locator_not_a_place`, `bare_quote`. Accepted: a number glued to a word
  (`PD4.02`) is not scanned; a librarian gap's numbers stay exempt (a gap states what was not found).

- 2026-11-03 — Live: argument kinds and the one citing syntax pass the lead, librarian, reviewer and worker cells and a coder chain through the kind gate (`logs/dev/live-20261103d/`); only the graph cell waits on the toolkit commit.
