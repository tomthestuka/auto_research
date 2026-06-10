"""Tool and shell-execution request messages."""

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef


NonEmptyStr = Annotated[str, Field(min_length=1)]
ToolKind = str


class ToolCallRequest(MessageModel):
    """A structured request for a tool action that the sandbox may later execute.

    Built from a provider-native tool call: ``kind`` is the tool name and
    ``arguments`` is the provider-validated, per-tool-schema-constrained argument
    map. ``requested_by``/``purpose`` are runtime metadata (the model authors a
    native tool call, not this envelope), so both carry defaults.
    """

    requested_by: str = "analyst_worker"
    kind: NonEmptyStr = Field(
        description=(
            "Exact tool kind string — e.g. 'mcp.builtin.bash_exec' or "
            "'research_toolkit.xspec_utils.plot_fit'. Set from the native tool call name."
        )
    )
    purpose: str = ""
    command: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
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
