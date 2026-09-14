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
}

FORBIDDEN_RUNTIME_REFERENCES = ("CLAUDE.md", "DOCS.md", "AGENTS.md")


def prompts_dir(root: Path | None = None) -> Path:
    """Return the directory containing runtime prompt files."""

    if root is not None:
        return root
    return Path(__file__).resolve().parents[3] / "prompts"


def load_prompt(role: PromptRole, *, root: Path | None = None) -> str:
    """Load one runtime prompt and reject build-time governance references."""

    prompt_path = prompts_dir(root) / PROMPT_FILENAMES[role]
    content = prompt_path.read_text(encoding="utf-8").strip()
    forbidden = [name for name in FORBIDDEN_RUNTIME_REFERENCES if name in content]
    if forbidden:
        names = ", ".join(forbidden)
        raise ValueError(f"Runtime prompt {prompt_path} references forbidden file(s): {names}")
    return content
