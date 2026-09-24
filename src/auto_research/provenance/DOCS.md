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

## Changelog

- 2026-09-24 — **Created (provenance P0).** Receipt schema (`schemas/artifacts.py`), the run
  store, and the scoped-grounding check with its normalisation, boundary rule and exemptions,
  agreed across the four peer sessions (board `tmp_coord_provenance.md`). `tests/test_provenance.py`
  (28). Not yet wired into any role: minting (P1) and the `turn_end` wiring (P2) follow.
- 2026-09-24 — **P1: capture, the per-run store and the scoped view.** `capture.py` turns a tool
  call into receipts (inputs hashed before the call from the paths its arguments name; result,
  figure — hash recomputed from the file — and data-product receipts after). `ReceiptStore.for_run`
  is the process-wide accessor; `ScopedLookup` restricts citation to what an agent produced or was
  handed. `check_report_fields` gains `scope`, shares one resolution cache per report (each handle
  verified and normalised once), and scope receipts no longer produce problems (both asked for by
  the reviewer session, whose scope is every readable receipt). Grammar fixes from real cases: a
  digit after letter+hyphen is not a number (`Cyg X-1`, `DU-3`); a `-` with a digit before it is a
  range dash, so `2-8` grounds against `2-8` (the sign guard still stops `0.51` in `-0.51`, and the
  exponent guard `3` in `1.2e-3`). **Stated residual:** inputs are the files a call's arguments
  name; a file a tool locates itself from an identifier (an ObsID under a data root) is not
  hashed. Tests 28 → 34.
- 2026-09-24 — **Inputs are receipts (found by the reviewer session).** A research request puts
  numbers (an ObsID, a band) into every role's task, and no receipt carried them, so a role that
  repeated its own task was refused. `ReceiptStore.mint_input(text, kind=, produced_by=)` mints each
  distinct input once under source `input` (the request: `kind="human",
  produced_by="research_request"`; a dispatched task: the defaults). `ScopedLookup` makes `input`
  visible to every unit. **Visible is not in scope:** a number the task stated grounds a text only
  when that text cites `[input:…]`, so a role cannot "confirm" a value it was handed without the
  citation saying where the value came from.
- 2026-09-24 — **Math alphanumerics fold (from the librarian session's live run).** Docling emits
  math-italic `𝜎` where a model types `σ`; 4 of 8 refused quotes in a real run were such honest
  copies (the other half: HTML entities, fixed at mint time by the librarian). `normalise` now folds
  the U+1D400–U+1D7FF block only, per code point through NFKC; superscripts stay distinct.
- 2026-09-24 — **`Receipt.source_path` and the closest-passage hint (for the librarian's native-PDF
  reading).** A paper receipt now names its original file (`source_path`, hashed as
  `source_sha256`) beside the checked text in `path`; `verify()` refuses a missing or changed
  original. Because the model may read the PDF itself rather than the checked extraction, a failed
  quote's refusal quotes the closest passage of the checked text (longest common run, widened to
  the quote's length), so an honest near-miss can be re-copied.
- 2026-09-24 — **Ordinals that name things are exempt** (`worker 0`, `DU 2`, `round 3`, `step 4`,
  `link 1`). Measured in the first live lead run: "Worker 0 … worker 1" was refused as ungrounded
  numbers in four of ten cells, and likely drove one cell's nine-refusal detour (the lead dropped its
  findings to escape the note, then was refused for having none).
- 2026-09-24 — **Closest-passage hint made quotable; line-broken words rejoin on both sides.** Found
  live by the librarian session: a hint ending in `respec-` (then, once collapsed, containing
  `respec- tively`) was copied verbatim and refused twice. The hint now snaps to whole words, drops a
  broken word at either end, and is trimmed until it passes the check itself (property-tested); for
  paper/web, `normalise` rejoins `letter-<whitespace>lowercase` on both sides. Trade-off: a compound
  broken exactly at its own hyphen (`X-\nray`) reads `Xray`.
- 2026-09-24 — **A signed value in a PDF table row is not a range** (found live by the reviewer: a
  row `0.2 −22 ± 2` closed up to `0.2-22`, which the range guard reads as a dash, so an exact quote of
  `−22 ± 2` was refused). For paper/web, whitespace before a sign glued to its own number, after a
  number, stays one separator; a sign with space on both sides (`2 - 8`) still closes up.
- 2026-09-24 — **More things that name rather than state are exempt** (found by the librarian's live
  shape matrix): list enumerators (`(1)`, `2)`, `3.` at item position); persistent identifiers —
  DOIs, ADS bibcodes, old-style `astro-ph/…`, and arXiv ids *with* their prefix (a bare
  `dddd.ddddd` could be a measured value, so it stays a number); HTTP status codes named with their
  reason phrase (`401 Unauthorized`, `HTTP 429`), so a role reporting an honest source failure is
  not pushed to hide what failed. Receipting tool *errors* so their numbers can be cited was the
  larger alternative and is not built.
- 2026-09-24 — **Refusal hints that point the right way** (from scanning both peers' live artifacts).
  A quote found verbatim in a *different* visible receipt is answered with that receipt's handle.
  When the checked text lacks a sign the quote carries — a PDF text layer that dropped a table row's
  minus, measured in the reviewer's live run — the hint no longer offers the unsigned text to copy
  (that would put the wrong sign in the report). Sexagesimal coordinates (`00h00m`, `12h34m56.7s`)
  are exempt. **Open:** the model still cannot ground the correct signed value when the extraction
  lost it; the proposed fix is checking against every extraction of the same PDF bytes, pending a
  ruling because it revisits dropping Docling.
- 2026-09-24 — **An input grounds only the text that cites it** (leak found live by the reviewer
  session: a title repeated the request's ObsID unmarked because `follow_up` cited `[input:h1]`, and
  the report-wide union then grounded every field with it). `check_report_fields` keeps `input:`
  handles out of that union and out of `scope`.
- 2026-09-24 — **Papers are cited, not checked (human ruling, option A).** A paper receipt is the PDF
  itself; `store.text()` returns `None` for any bytes starting `%PDF-`, so no PDF text is ever
  quoted or scanned. `check_bindings` / `check_report_fields(cited_only_kinds=("paper",))` lets a
  caller bind a text that cites a paper to that citation as a whole claim (handles still resolved
  and re-hashed; numbers not scanned). Opt-in, because it lets any number through in such a text.
  The paper-specific normalisation (whitespace, hyphen rejoin, table-row signs) now serves web pages
  only.
- 2026-09-24 — **`cited_only_kinds` is sentence-scoped; `support=` backs a whole text.** Only a
  sentence that inline-cites a cited-only receipt skips the number scan (the reviewer's long
  sections need that grain); `check_bindings(support=…)` takes handles backing the text as a whole
  (a finding's support list) — they ground its numbers like scope and, when one is cited-only, bind
  the whole text to it.
- 2026-09-24 — **Paper values are attributed, never exempted (human ruling, superseding the
  sentence-scoped `cited_only_kinds` built earlier the same day).** `{{h "value" <locator>}}`: for a
  kind in `ATTRIBUTION_KINDS` (`paper`) the binding is an attribution — handle resolved and
  verified, locator required, value not matched, its numbers exempt because they sit inside the
  binding. Every other number is checked as usual (papers ground nothing), and a refusal in a text
  citing a paper suggests the attribution form. `paper_attributions(prose, lookup, *, own_source)
  -> list[Attribution(handle, value, locator)]` feeds each report's verify-against-the-PDF list.
  `cited_only_kinds` is removed; `support=` only grounds. For text kinds a locator is ignored.
  Honest limit: an attributed value is unverified by the runtime.
- 2026-09-24 — **`PaperAttribution` (schemas/artifacts.py) is the one verify-list shape** (human
  ruling via the reviewer session): `handle`, `value`, `locator`, a `MessageModel` both
  `LiteratureReport` and `ResearchReport` carry; `paper_attributions()` returns it (`Attribution`
  kept as an alias). Title and identifiers are resolved from the receipt when rendered.
- 2026-09-24 — **Two laundering routes closed (found live by the reviewer session).** (1) The
  run's store moved beside the ledger (`record_beside(run_dir, "receipts")`), outside every role's
  allow-list: a reviewer had read `receipts/blobs` with `read_file` and re-minted the request's text
  as its own receipt, grounding an input's number without citing the input. (2) A number a role
  typed into any shell command (`SHELL_TOOLS`) cannot be grounded or quoted from a shell receipt:
  `echo 3.7`, or writing a number to a file and reading it back in a second call, minted a receipt
  for a number nothing computed. `Receipt.arg_numbers` records a shell call's typed numbers;
  `ReceiptStore.typed_numbers(source)` unions them per role. Computed output (`print(4.0-3.4)` →
  0.6) still grounds; toolkit tools echoing their own parameters are unaffected. **Residual:** a
  number reaching a shell by a route other than its command text (a toolkit tool that writes a
  file whose content the model chose) is not caught.
