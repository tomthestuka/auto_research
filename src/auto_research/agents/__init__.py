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
    AnalystColeadAgent,
    AnalystLeadAgent,
    AnalystWorkerAgent,
    CallableModel,
    LibrarianIOAgent,
    ReviewerAgent,
)

__all__ = [
    "AgentRole",
    "AnalystColeadAgent",
    "AnalystLeadAgent",
    "AnalystWorkerAgent",
    "CallableModel",
    "FORBIDDEN_RUNTIME_REFERENCES",
    "LibrarianIOAgent",
    "PROMPT_FILENAMES",
    "PromptRole",
    "ReviewerAgent",
    "load_prompt",
    "prompts_dir",
]
