"""Opt-in live checks for step-8 role wrappers.

Run with:
    AUTO_RESEARCH_LIVE_AGENT_TESTS=1 pytest tests/test_live_agents.py -s
"""

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    ReviewerAgent,
)
from auto_research.models import load_registry
from auto_research.observations import load_observations_config
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
)
from auto_research.storage import Storage
from tests.dev_logs import dev_log_path, summarize_agent_log


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_AGENT_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_AGENT_TESTS=1 to spend provider tokens",
)


GX339_PDF = Path("/data/fzhou/ar_runtime/papers/GX339_4.pdf")
IXPE_ONLY_BAD_FIT = """
Model constant<1>*TBfeo<2>*plabs<3>(polconst<4>*diskbb<5> + polconst<6>*relxillCp<7>) Source No.: 1   Active/On
Model Model Component  Parameter  Unit     Value
 par  comp
                           Data group: 1
   1    1   constant   factor              1.00000      frozen
   2    2   TBfeo      nH         10^22    0.400000     frozen
   6    3   plabs      index               2.00000      frozen
   8    4   polconst   A                   0.957113     +/-  -1.00000
   9    4   polconst   psi        deg      -53.5693     +/-  -1.00000
  10    5   diskbb     Tin        keV      4.86557E-03  +/-  -1.00000
  11    5   diskbb     norm                5.04638E+19  +/-  -1.00000
  12    6   polconst   A                   2.89757E-02  +/-  2.14536E-03
  13    6   polconst   psi        deg      -27.0000     frozen
  14    7   relxillCp  Incl       deg      37.0000      frozen
  16    7   relxillCp  Rin                 -1.00000     +/-  27.9445
  22    7   relxillCp  gamma               2.10110      +/-  2.05793E-02
  23    7   relxillCp  logxi               3.63421      +/-  0.114024
  27    7   relxillCp  refl_frac           3.98483      +/-  3.52587
  28    7   relxillCp  norm                0.203784     +/-  0.142256
                           Data group: 2
  29    1   constant   factor              0.697197     +/-  5.27040E-03
  34    3   plabs      index               1.32556      +/-  1.52240E-02
                           Data group: 3
  57    1   constant   factor              0.641158     +/-  4.99129E-03
  62    3   plabs      index               1.30002      +/-  1.53049E-02

Response parameters defined:
   1     1    gain     slope             1.12446      +/-  3.07032E-03
   7     4    gain     slope             1.01249      +/-  2.67073E-03
  13     7    gain     slope             1.02101      +/-  2.63719E-03

Fit statistic  : Chi-Squared                 7026.65     using 149 bins, spectrum 1, group 1.
                 Chi-Squared                  174.52     using 149 bins, spectrum 2, group 1.
                 Chi-Squared                  120.99     using 149 bins, spectrum 3, group 1.
                 Chi-Squared                  485.17     using 149 bins, spectrum 4, group 2.
                 Chi-Squared                  126.38     using 149 bins, spectrum 5, group 2.
                 Chi-Squared                  157.44     using 149 bins, spectrum 6, group 2.
                 Chi-Squared                  521.76     using 149 bins, spectrum 7, group 3.
                 Chi-Squared                  180.47     using 149 bins, spectrum 8, group 3.
                 Chi-Squared                  169.30     using 149 bins, spectrum 9, group 3.
Total fit statistic                          8962.67     with 1324 d.o.f.

Test statistic : Chi-Squared                 8962.67     using 1341 bins.
 Null hypothesis probability of 0.00e+00 with 1324 degrees of freedom
""".strip()


def test_live_role_wrappers_on_research_jobs(tmp_path, monkeypatch):
    load_dotenv()
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    registry = load_registry()
    observations = load_observations_config()
    storage = Storage(tmp_path / "live_agents.sqlite3")
    log_path = dev_log_path("step-8", "live_agents", ".jsonl")

    async def run_all():
        await storage.initialize()
        failures = []
        from auto_research.env import toolkit_root as configured_toolkit_root
        from auto_research.graph.runtime import attach_reviewer_runtime

        reviewer = ReviewerAgent(model=registry.resolve("reviewer").handles[0])
        # Production's wiring: tools, report folder, the run's receipt store.
        attach_reviewer_runtime(reviewer, run_id="live-agents", toolkit_root=configured_toolkit_root())
        await _record(
            log_path,
            "reviewer",
            reviewer,
            reviewer.review(
                stage="literature",
                iterations_left=1,
                request=ResearchRequest(
                    question=(
                        "IXPE has done consecutive observations on Cyg X-1 in its 2024 soft states. "
                        "Perform a spectral fit on one 2024 Cyg X-1 IXPE observation, preferably "
                        "as a joint fit with other missions."
                    ),
                    context=(
                        "Hint: the HEASARC archive is available for you to discover relevant observations."
                    ),
                )
            ),
            failures,
        )

        obs_payload = {
            name: str(config.obs_db_path)
            for name, config in observations.missions.items()
        }
        analyst = AnalystWorkerAgent(model=registry.resolve("analyst_worker").handles[0])
        await _record(
            log_path,
            "analyst_worker/tool_request",
            analyst,
            analyst.request_tools(
                AnalysisTask(
                    question=(
                        "Using the configured observation databases, identify which observations "
                        "are 2024 soft-state observations and perform a spectral fit on one of them."
                    ),
                    instructions=(
                        "Operate the available computer/tool environment as needed. "
                        f"Observation locations: {json.dumps(obs_payload)}"
                    ),
                )
            ),
            failures,
        )

        # The lead's whole turn is one cycle that ends by filing a form, so it needs the two
        # things a cycle writes through: a report folder (edit_report's JSONL) and a tool
        # surface (the forms live on the provider). Without them there is no form to file and
        # the cycle returns the honest failure instead of a report.
        lead = AnalystLeadAgent(model=registry.resolve("analyst_lead").handles[0])
        lead.agent_key = "analyst_lead"
        lead.report_dir = log_path.parent
        lead.tools = ReadOnlyToolProvider(
            roots=[log_path.parent], include_text_search=False
        )
        await _record(
            log_path,
            "analyst_lead/ixpe_only_fit_review",
            lead,
            lead.run_cycle(
                AnalysisTask(
                    question="Review whether this IXPE-only Cyg X-1 fit result is acceptable.",
                    instructions=IXPE_ONLY_BAD_FIT,
                ),
                [
                    AnalystReport(
                        summary="An IXPE-only XSPEC fit result was supplied for review.",
                        findings=[IXPE_ONLY_BAD_FIT],
                        uncertainty="No NICER/NuSTAR joint data, residual plot, or external literature comparison was supplied.",
                    )
                ],
                sources=["analyst_worker_0"],
                rounds_left=0,
            ),
            failures,
        )

        return failures

    failures = asyncio.run(run_all())
    md_path = summarize_agent_log(log_path)
    print(f"wrote dev log {log_path}", flush=True)
    print(f"wrote summary {md_path}", flush=True)
    assert not failures, "\n".join(f"{role} failed with {error}" for role, error in failures)


async def _record(log_path, role, agent, task, failures):
    handle = agent.model
    print(f"running {role} via {handle.config.provider}:{handle.config.model}", flush=True)
    try:
        output = await asyncio.wait_for(task, timeout=180)
    except Exception as exc:
        failures.append((role, repr(exc)))
        row = _row(role, handle, "failed", error=repr(exc), response=agent.last_response)
        output = None
    else:
        row = _row(role, handle, "ok", output=_dump_output(output), response=agent.last_response)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True))
        stream.write("\n")
    print(f"{role} {row['status']}", flush=True)
    return output


def _dump_output(output):
    if hasattr(output, "filed"):  # a LeadCycleOutcome — the filed form is what it produced
        return {
            "filed": output.filed,
            "report": output.report.model_dump(mode="json") if output.report else None,
            "verdict": output.verdict.model_dump(mode="json") if output.verdict else None,
        }
    if isinstance(output, list):
        return [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in output]
    if hasattr(output, "model_dump"):
        return output.model_dump(mode="json")
    return output


def _row(role, handle, status, *, output=None, error=None, response=None):
    usage = response.usage if response is not None else None
    return {
        "role": role,
        "model_member": handle.config.name,
        "provider": handle.config.provider,
        "model": handle.config.model,
        "input_tokens": usage.input_tokens if usage is not None else 0,
        "output_tokens": usage.output_tokens if usage is not None else 0,
        "status": status,
        "output": output,
        "error": error,
        "raw_text": response.content if response is not None and status == "failed" else None,
    }
