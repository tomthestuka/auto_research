"""Pydantic v2 message schemas: the inter-agent protocol."""

from auto_research.schemas.analysis import AnalysisTask, AnalystReport
from auto_research.schemas.artifacts import InputFile, PaperAttribution, Receipt
from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import (
    DocumentRef,
    FigureCitation,
    FigureRef,
    FiguredReport,
    document_from_file,
)
from auto_research.schemas.hitl import HumanAttachment, HumanQuery, HumanQueryKind, HumanResponse
from auto_research.schemas.merge import (
    CheckResult,
    GateReport,
    MergeFlowResult,
    ToolReviewVerdict,
)
from auto_research.schemas.report import ResearchReport
from auto_research.schemas.requests import ResearchRequest
from auto_research.schemas.review import (
    ReviewerReport,
    ReviewerSection,
    ReviewerTable,
)
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
    "FigureCitation",
    "FiguredReport",
    "DocumentRef",
    "FullTextLink",
    "GateReport",
    "HumanAttachment",
    "HumanQuery",
    "HumanQueryKind",
    "HumanResponse",
    "InputFile",
    "MergeFlowResult",
    "MessageModel",
    "PaperAttribution",
    "Receipt",
    "ResearchReport",
    "ResearchRequest",
    "ReviewerReport",
    "ReviewerSection",
    "ReviewerTable",
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
