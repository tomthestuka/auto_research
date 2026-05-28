"""Tool and shell-execution request messages."""

from pathlib import Path
from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef


NonEmptyStr = Annotated[str, Field(min_length=1)]
ToolKind = str


class ToolCallRequest(MessageModel):
    """A structured request for a tool action that the sandbox may later execute."""

    requested_by: NonEmptyStr
    kind: NonEmptyStr
    purpose: NonEmptyStr
    command: str | None = None
    arguments: dict[str, str] = Field(default_factory=dict)
    working_dir: Path | None = None
    timeout_s: int = Field(default=300, ge=1, le=3600)
    network_required: bool = False


class ToolCallReview(MessageModel):
    """Lead review of a proposed tool action."""

    approved: bool
    rationale: NonEmptyStr
    revised_request: ToolCallRequest | None = None


class ToolResult(MessageModel):
    """Result of a sandboxed tool execution."""

    tool_name: NonEmptyStr
    stdout: str
    stderr: str
    exit_code: int
    figures: list[FigureRef] = Field(default_factory=list)
    elapsed_s: float
