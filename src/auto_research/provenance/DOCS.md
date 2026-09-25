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
- **Flags propagate.** `failed` (the producing call failed) and `synthetic` (an input under a
  marked `synth<YYMMDD>-<sha>` directory, §9.5) pass to every descendant. A flagged receipt can be
  cited to describe what happened; it cannot ground a number.
- **Scoped grounding** (human ruling 2026-09-24, replacing "every number quoted"): a text cites
  `[src:r12]`; every number in that text must appear, on token boundaries, in a receipt it cites.
  Findings are scoped per item; other report fields by everything the report cites. A pinned quote
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
- `ScopedLookup(store, *, own_source, admitted=())` — what one agent may cite: its own receipts
  (live) plus admitted handles and their ancestors; `.admit(handles)`, `.handles()`.
- `capture.inputs_before(store, args, *, cwd, skip_keys) -> CallInputs` and
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
- `bindings.ungrounded_numbers(prose, usable, *, echoes, masked)` — the number-grounding loop, used
  by `check_bindings` and the gate.

## Changelog

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
