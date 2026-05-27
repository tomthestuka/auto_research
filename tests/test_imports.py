"""Smoke test for step-0 scaffolding.

Verifies that the conda env declared in ``environment.yml`` was built correctly
and that the ``auto_research`` package + every submodule import cleanly.
"""

import importlib

import pytest


THIRD_PARTY = [
    "pydantic",
    "yaml",
    "dotenv",
    "anthropic",
    "openai",
    "google.genai",
    "langgraph",
    "langsmith",
    "arxiv",
    "ads",
    "tavily",
]

SUBMODULES = [
    "auto_research",
    "auto_research.schemas",
    "auto_research.models",
    "auto_research.observability",
    "auto_research.storage",
    "auto_research.sources",
    "auto_research.retrieval",
    "auto_research.agents",
    "auto_research.consensus",
    "auto_research.graph",
    "auto_research.sandbox",
    "auto_research.cli",
]


@pytest.mark.parametrize("name", THIRD_PARTY)
def test_third_party_imports(name):
    importlib.import_module(name)


@pytest.mark.parametrize("name", SUBMODULES)
def test_auto_research_imports(name):
    importlib.import_module(name)


def test_package_version():
    import auto_research

    assert auto_research.__version__ == "0.0.0"