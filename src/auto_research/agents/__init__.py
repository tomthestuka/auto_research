"""Per-role agent support utilities."""

from auto_research.agents.prompts import (
    FORBIDDEN_RUNTIME_REFERENCES,
    PROMPT_FILENAMES,
    PromptRole,
    load_prompt,
    prompts_dir,
)

__all__ = [
    "FORBIDDEN_RUNTIME_REFERENCES",
    "PROMPT_FILENAMES",
    "PromptRole",
    "load_prompt",
    "prompts_dir",
]
