"""Pydantic v2 message schemas: the inter-agent protocol."""

from auto_research.schemas.analysis import AnalysisTask, AnalystReport
from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import DocumentRef, FigureRef, document_from_file
from auto_research.schemas.hitl import HumanQuery, HumanQueryKind, HumanResponse
from auto_research.schemas.merge import (
    CheckResult,
    GateReport,
    MergeFlowResult,
    ToolReviewVerdict,
)
from auto_research.schemas.report import LatexReport, Reference, ReportSection, ResearchReport
from auto_research.schemas.requests import ResearchRequest
from auto_research.schemas.review import ReviewVerdict
from auto_research.schemas.search import FullTextLink, SearchRequest, SearchResult, SearchSource
from auto_research.schemas.tools import ToolCallRequest, ToolCallReview, ToolKind, ToolResult
from auto_research.schemas.toolspec import (
    AnalystToolRequest,
    AuthoringDecision,
    ProposedCheck,
    ToolSpec,
    ValidationDataset,
)

__all__ = [
    "AnalysisTask",
    "AnalystReport",
    "AnalystToolRequest",
    "AuthoringDecision",
    "CheckResult",
    "FigureRef",
    "DocumentRef",
    "FullTextLink",
    "GateReport",
    "HumanQuery",
    "HumanQueryKind",
    "HumanResponse",
    "LatexReport",
    "MergeFlowResult",
    "MessageModel",
    "Reference",
    "ReportSection",
    "ResearchReport",
    "ResearchRequest",
    "ReviewVerdict",
    "SearchRequest",
    "SearchResult",
    "SearchSource",
    "ToolCallRequest",
    "ToolCallReview",
    "ToolKind",
    "ToolResult",
    "ToolReviewVerdict",
    "ToolSpec",
    "ProposedCheck",
    "ValidationDataset",
    "document_from_file",
]
