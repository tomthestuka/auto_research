"""Runtime prompt loading for agent roles."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

# Role names here MUST mirror the role keys in config/models.yaml (one name per role,
# no divergent aliases) so the two configs stay maintainable together. Not every model
# role needs a prompt.
PromptRole = Literal[
    "reviewer",
    "librarian_io",
    "analyst_worker",
    "analyst_lead",
    "analyst_colead",
    "coder",
    "status_monitor",
    "compactor",
    "session_compactor",
]

PROMPT_FILENAMES: dict[PromptRole, str] = {
    "reviewer": "reviewer.md",
    "librarian_io": "librarian_io.md",
    "analyst_worker": "analyst_worker.md",
    "analyst_lead": "analyst_lead.md",
    # Phase 2: the provider-decorrelated GPT co-lead's code-review system prompt
    # (CLAUDE.md §9.3 step 4). Loaded by the review hook for the structured verdict.
    "analyst_colead": "analyst_colead.md",
    # Phase 2: the Sonnet author's system prompt (CLAUDE.md §9.3). Loaded by merge_flow
    # and passed to the Claude Agent SDK; same governance check as the other prompts.
    "coder": "coder.md",
    # The light role answering the HITL `/status` command over the run's event log. It is a
    # runtime LLM like any other, so its prompt lives here rather than inline in `hitl/commands.py`
    # (§4: runtime agents are configured only from `prompts/`), which also puts it under
    # `load_prompt`'s governance-reference check.
    "status_monitor": "status_monitor.md",
    # Summarises one finished authoring link into the outcome sentence the lead reads in place
    # of the coding transcript. A runtime LLM like any other, so its instructions live here.
    "compactor": "compactor.md",
    # Condenses the middle of a long turn (B6/B7): the instructions of Anthropic's server-side
    # compaction and the system prompt of the runtime's own summariser for every other provider.
    "session_compactor": "session_compactor.md",
}

FORBIDDEN_RUNTIME_REFERENCES = ("CLAUDE.md", "DOCS.md", "AGENTS.md")

#: The roles that are team units: each one's system prompt is the shared conventions, then its own.
UNIT_ROLES: frozenset[PromptRole] = frozenset({"reviewer", "librarian_io", "analyst_worker", "analyst_lead"})

#: What every unit follows (receipts, evidence, how to write a number, the channel, the one exit).
CONVENTIONS_FILENAME = "conventions.md"


def prompts_dir(root: Path | None = None) -> Path:
    """Return the directory containing runtime prompt files."""

    if root is not None:
        return root
    return Path(__file__).resolve().parents[3] / "prompts"


def load_prompt(role: PromptRole, *, root: Path | None = None) -> str:
    """Load one runtime prompt and reject build-time governance references.

    A unit role's prompt is ``conventions.md`` followed by the role's own file: what every unit
    follows, then its job and the scope it owns in the graph.
    """

    names = ([CONVENTIONS_FILENAME] if role in UNIT_ROLES else []) + [PROMPT_FILENAMES[role]]
    parts = []
    for name in names:
        prompt_path = prompts_dir(root) / name
        content = prompt_path.read_text(encoding="utf-8").strip()
        forbidden = [ref for ref in FORBIDDEN_RUNTIME_REFERENCES if ref in content]
        if forbidden:
            raise ValueError(f"Runtime prompt {prompt_path} references forbidden file(s): {', '.join(forbidden)}")
        parts.append(content)
    return "\n\n".join(parts)
