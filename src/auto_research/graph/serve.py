"""LangGraph Studio entry point.

Import-time graph construction for ``langgraph dev``. langgraph-cli loads
``.env`` (via ``langgraph.json``) before importing this module, so API keys
are available when ``_default_graph()`` constructs the agents.

Usage::

    langgraph dev          # starts server at http://localhost:2024
    # open https://smith.langchain.com/studio/?baseUrl=http://localhost:2024
"""

from auto_research.graph.runtime import _default_graph

graph = _default_graph()
