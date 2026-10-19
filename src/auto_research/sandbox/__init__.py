"""Sandbox execution, tool registry, MCP client, and run workspace for research_toolkit."""

from auto_research.sandbox.mcp_client import MCPToolClient
from auto_research.sandbox.merge_flow import propose_and_pr
from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.sandbox.registry import ToolkitRegistry, ToolSpec
from auto_research.sandbox.review import (
    build_diff_bundle,
    check_author_reviewer_decorrelation,
    make_review_hook,
    select_decorrelated_member,
)
from auto_research.sandbox.runner import run_tool
from auto_research.sandbox.workspace import (
    create_run_workspace,
    figures_dir,
    run_dir,
)

__all__ = [
    "MCPToolClient",
    "PersistentSandbox",
    "ToolkitRegistry",
    "ToolSpec",
    "build_diff_bundle",
    "check_author_reviewer_decorrelation",
    "create_run_workspace",
    "figures_dir",
    "make_review_hook",
    "propose_and_pr",
    "run_dir",
    "run_tool",
    "select_decorrelated_member",
]
