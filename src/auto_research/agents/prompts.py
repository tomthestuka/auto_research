"""Runtime prompt loading for agent roles."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

PromptRole = Literal[
    "reviewer",
    "librarian_io",
    "analyst_worker",
    "analyst_lead",
]

PROMPT_FILENAMES: dict[PromptRole, str] = {
    "reviewer": "reviewer.md",
    "librarian_io": "librarian_io.md",
    "analyst_worker": "analyst_worker.md",
    "analyst_lead": "analyst_lead.md",
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
