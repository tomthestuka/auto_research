"""Retrieval tools: text search now, vector search stubbed for Phase 2."""

from auto_research.retrieval.backend import MemoryHit, RetrievalBackend, SqliteRetrievalBackend
from auto_research.retrieval.text_search import text_search_paths
from auto_research.retrieval.vector_search import vector_search_paths

__all__ = [
    "MemoryHit",
    "RetrievalBackend",
    "SqliteRetrievalBackend",
    "text_search_paths",
    "vector_search_paths",
]
