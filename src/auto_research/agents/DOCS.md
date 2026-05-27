# `auto_research.agents` — per-role wrappers

## Purpose
Thin wrappers around the provider layer that bind a role to its prompt
(`prompts/<role>.md`), its allowed tools, and its expected I/O schemas. One class
per role — reviewer, librarian I/O, librarian searcher, analyst worker, analyst
lead ([CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop)).

## Architecture
Each role is a small async class:
1. Construction loads its prompt file and resolves its model via
   `auto_research.models.registry`.
2. Each public method accepts a task schema, returns a result schema.
3. Tool access (sources, retrieval, sandboxed toolkit calls) is injected — not
   imported globally — so swap-outs in tests are trivial.

Critical: **runtime LLMs never see `CLAUDE.md` or any `DOCS.md`**
([CLAUDE.md §4](../../../CLAUDE.md#4-claudemd-and-docsmd-are-build-time-only)).
Only the file in `prompts/` plus task data goes into the model.

Step 7 provides the prompt-loading utility and runtime prompt files. Step 8 will
add the async role wrapper classes on top of this loader.

## Interfaces / Contracts
Prompt loading:
- `load_prompt(role, root=None) -> str` loads `prompts/<role>.md`, strips leading
  and trailing whitespace, and raises `ValueError` if the prompt text references
  build-time governance filenames.
- `prompts_dir(root=None) -> Path` returns the prompt directory, allowing tests to
  inject a temporary prompt root.
- `PROMPT_FILENAMES` maps the five Phase 1 roles to their Markdown filenames.

Role wrappers are still planned for step 8:
- `ReviewerAgent`, `LibrarianIOAgent`, `LibrarianSearcherAgent`,
  `AnalystWorkerAgent`, `AnalystLeadAgent`.

## Dependencies
- Internal: `auto_research.models`, `auto_research.schemas`,
  `auto_research.sources`, `auto_research.retrieval`,
  `auto_research.observability`.

## Dictionary-level reference
- `PromptRole` — literal type for the five Phase 1 runtime roles:
  `reviewer`, `librarian_io`, `librarian_searcher`, `analyst_worker`,
  `analyst_lead`.
- `PROMPT_FILENAMES` — role-to-filename mapping used by the loader.
- `FORBIDDEN_RUNTIME_REFERENCES` — tuple of build-time governance filenames that
  runtime prompts must not reference.
- `prompts_dir(root=None)` — returns an injected prompt root or the repository's
  top-level `prompts/` directory.
- `load_prompt(role, root=None)` — loads and validates one runtime prompt.

## Changelog
- 2026-05-27 — Step 7 completed: added runtime prompt loading utilities and
  governance-reference guards for prompt files.
- 2026-05-26 — Folder seeded during step 0.
