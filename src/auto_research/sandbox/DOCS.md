# `auto_research.sandbox` — tool-authoring & isolated execution

## Purpose
Two responsibilities, kept together because they share trust assumptions:
1. **Tool-authoring flow** — analysts propose new/changed tools in
   `research_toolkit`; the analyst lead reviews; **a human approves the merge** in
   Phase 1 ([CLAUDE.md §8](../../../CLAUDE.md#8-runtime-tool-authoring--sandbox)).
2. **Execution sandbox** — every tool, new or existing (`xspec_utils` included),
   runs in an **isolated subprocess** with timeout, confined workdir, and no
   network unless explicitly whitelisted.

## Architecture
- `merge_flow.py` — drives the propose → branch → lead-review → human-approve
  flow against the `research_toolkit` working tree. The git ops here are
  *application code*, acting on `research_toolkit` only; this is **not** the
  build-time coding-agent prohibition on git ([CLAUDE.md §3](../../../CLAUDE.md#3-coding-agent-contract-read-this-every-session)).
- `runner.py` — subprocess sandbox. Spawns Python with the tool's entry function,
  applies timeout, restricts working directory, blocks network by default.

Deeper hardening (containers, seccomp, egress control, autonomous-merge toggle)
is documented as a deferred Phase 2 task and is **not built now**
([CLAUDE.md §13](../../../CLAUDE.md#13-phase-2-readiness)).

## Interfaces / Contracts
*To be populated in step 11.* Planned:
- `async propose_tool(spec: ToolProposal) -> ProposalId`.
- `async review_proposal(pid: ProposalId) -> LeadReview`.
- `async approve_and_merge(pid: ProposalId)` — refuses without explicit human
  approval in Phase 1.
- `async run_tool(name: str, args: dict, *, timeout_s: int, workdir: Path) -> ToolResult`.

## Dependencies
- External: stdlib `subprocess`, `asyncio`, plus `gitpython` (or `subprocess` to
  `git`) for the merge flow.
- Internal: `auto_research.schemas`, `auto_research.agents` (analyst lead).

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.
