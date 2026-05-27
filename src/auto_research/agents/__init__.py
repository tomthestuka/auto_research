"""Per-role agent support utilities."""

from auto_research.agents.prompts import (
    FORBIDDEN_RUNTIME_REFERENCES,
    PROMPT_FILENAMES,
    PromptRole,
    load_prompt,
    prompts_dir,
)
from auto_research.agents.roles import (
    AgentRole,
    AnalystLeadAgent,
    AnalystWorkerAgent,
    CallableModel,
    LibrarianIOAgent,
    LibrarianSearcherAgent,
    ReviewerAgent,
)

__all__ = [
    "AgentRole",
    "AnalystLeadAgent",
    "AnalystWorkerAgent",
    "CallableModel",
    "FORBIDDEN_RUNTIME_REFERENCES",
    "LibrarianIOAgent",
    "LibrarianSearcherAgent",
    "PROMPT_FILENAMES",
    "PromptRole",
    "ReviewerAgent",
    "load_prompt",
    "prompts_dir",
]
