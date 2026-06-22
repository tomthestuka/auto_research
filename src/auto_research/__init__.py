"""auto_research — orchestration package for the automated research assistant.

This package is the build-time-managed half of the two-repository system. The
runtime cannot modify it (see CLAUDE.md §2). Domain tools live in the sibling
``research_toolkit`` package.
"""

__version__ = "0.0.0"

# Mute the two chatty, non-actionable Pydantic v2 warning families that the heavy
# model_dump/model_dump_json traffic in this system emits by the hundred: the v2
# deprecation notices and the serializer "Expected X but got Y" UserWarnings. Both
# are scoped narrowly so every other warning stays audible.
import warnings as _warnings

_warnings.filterwarnings("ignore", message=r"Pydantic serializer warnings")
try:  # pragma: no cover - depends on installed pydantic version
    from pydantic import PydanticDeprecatedSince20 as _PydanticDeprecatedSince20

    _warnings.filterwarnings("ignore", category=_PydanticDeprecatedSince20)
except Exception:  # noqa: BLE001 - never let a warning filter break import
    pass

from auto_research.graph import ResearchRun, run_research, run_request
from auto_research.monitor import RunMonitor

__all__ = ["ResearchRun", "RunMonitor", "__version__", "run_research", "run_request"]
