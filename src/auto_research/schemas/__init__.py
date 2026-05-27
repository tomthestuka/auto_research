"""Pydantic v2 message schemas: the inter-agent protocol."""

from auto_research.schemas.analysis import AnalysisTask, AnalystReport
from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import DocumentRef, FigureRef, document_from_file
from auto_research.schemas.requests import ResearchRequest
from auto_research.schemas.review import ReviewVerdict
from auto_research.schemas.search import FullTextLink, SearchRequest, SearchResult, SearchSource
from auto_research.schemas.tools import ToolCallRequest, ToolCallReview, ToolKind

__all__ = [
    "AnalysisTask",
    "AnalystReport",
    "FigureRef",
    "DocumentRef",
    "FullTextLink",
    "MessageModel",
    "ResearchRequest",
    "ReviewVerdict",
    "SearchRequest",
    "SearchResult",
    "SearchSource",
    "ToolCallRequest",
    "ToolCallReview",
    "ToolKind",
    "document_from_file",
]
