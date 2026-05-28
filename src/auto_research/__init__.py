"""auto_research — orchestration package for the automated research assistant.

This package is the build-time-managed half of the two-repository system. The
runtime cannot modify it (see CLAUDE.md §2). Domain tools live in the sibling
``research_toolkit`` package.
"""

__version__ = "0.0.0"

from auto_research.graph import ResearchRun, run_research, run_request
from auto_research.monitor import RunMonitor

__all__ = ["ResearchRun", "RunMonitor", "__version__", "run_research", "run_request"]
