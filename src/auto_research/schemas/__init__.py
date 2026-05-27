"""Pydantic v2 message schemas: the inter-agent protocol."""

from auto_research.schemas.analysis import AnalysisTask, AnalystReport
from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef
from auto_research.schemas.requests import ResearchRequest
from auto_research.schemas.review import ReviewVerdict
from auto_research.schemas.search import FullTextLink, SearchRequest, SearchResult, SearchSource

__all__ = [
    "AnalysisTask",
    "AnalystReport",
    "FigureRef",
    "FullTextLink",
    "MessageModel",
    "ResearchRequest",
    "ReviewVerdict",
    "SearchRequest",
    "SearchResult",
    "SearchSource",
]
