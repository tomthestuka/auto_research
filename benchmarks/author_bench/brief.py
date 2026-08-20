"""The fixed brief every trial authors.

The instrument is a *real* brief, not one written for the benchmark: the ``ToolSpec`` the Opus
lead produced in run ``46ed6671`` (analysis #2), recovered verbatim from that run's ledger and
frozen beside this module. Two properties follow. The paper's §3 walkthrough is then trial 0 of
the same distribution rather than a separate anecdote, and the brief carries a real lead's
ambiguities — its proposed checks name two synthetic fixture ObsIDs that the original authoring
resolved by writing controlled CSVs into the test's own ``tmp_path`` instead. How each trial
resolves that is part of what is being measured, so the brief is never edited to smooth it.
"""

from __future__ import annotations

import json
from pathlib import Path

from auto_research.schemas import ToolSpec

BRIEF_PATH = Path(__file__).resolve().parent / "brief_ixpe_pa_pd_stability.json"

# The toolkit state the original authoring started from: the parent of the commit that landed
# this tool (`a6e427c`, "Add IXPE PA/PD stability analysis tool to xspec_utils"). A trial must
# begin where the real one began, with the tool absent and everything it builds on present.
BASE_SHA = "fa99ef8"

# The brief's validation observations, all already local. Checked before a run rather than
# discovered by K coders one at a time.
VALIDATION_OBSIDS = ("01002901", "01250401", "02001901", "03002599", "03005801", "03006001")


def load_brief() -> ToolSpec:
    """The frozen brief as a validated ``ToolSpec``."""
    data = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
    return ToolSpec.model_validate(data["tool_spec"])


def brief_provenance() -> dict:
    """Where the frozen brief came from, for the run stamps."""
    return json.loads(BRIEF_PATH.read_text(encoding="utf-8"))["_provenance"]
