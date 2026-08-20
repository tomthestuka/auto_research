"""Author harnesses: the coder processes that write toolkit code (CLAUDE.md §9.3).

One harness drives one model, named together on the ``coder`` role in ``config/models.yaml``.
:mod:`harness` holds which pairings exist and the launch checks over them; the backends
themselves live beside it.
"""

from auto_research.sandbox.coder.harness import (
    HARNESS_ENDPOINTS,
    check_coder_harness_available,
    check_coder_harness_supported,
    coder_member,
)

__all__ = [
    "HARNESS_ENDPOINTS",
    "check_coder_harness_available",
    "check_coder_harness_supported",
    "coder_member",
]
