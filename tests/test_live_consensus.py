"""Opt-in live check for step-9 debate-to-converge.

Run with:
    AUTO_RESEARCH_LIVE_CONSENSUS_TESTS=1 pytest tests/test_live_consensus.py -s
"""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.agents import AnalystLeadAgent, AnalystWorkerAgent
from auto_research.consensus import DebateToConverge
from auto_research.models import load_registry
from auto_research.schemas import AnalysisTask
from tests.dev_logs import dev_log_path, summarize_consensus_log


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_CONSENSUS_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_CONSENSUS_TESTS=1 to spend provider tokens",
)


GX339_REPLICATION_WORK = """Q. C. Shui et al., "State transitions of GX 339-4 during its outburst rising phase," MNRAS preprint accepted 2021-09-01, arXiv:2109.02088v1. Newly ingested local PDF: /data/fzhou/ar_runtime/papers/GX339_4.pdf; sha256=11408c9463db4f2ef49364e46b12349ac457eadebee477137c9656711dd45c17. Abstract: The paper systematically studies four RXTE-observed outbursts of the black-hole X-ray binary GX 339-4, focusing on rising-phase state transitions in both spectral and timing domains. Although the outbursts trace different q-shaped tracks in the hardness-intensity diagram, they share common spectral/timing properties: photon index is about 1.5 in the low/hard state (LHS) and about 2.4 in soft intermediate/high soft states (SIMS/HSS); thermal, non-thermal, and reflection components change strongly during LHS-to-HIMS evolution; type-C QPOs show an RMS drop near LHS-HIMS transition accompanied by appearance of a second harmonic; and summed QPO RMS correlates linearly with the non-thermal flux fraction across different outbursts. Data: RXTE/PCA PCU2 observations of GX 339-4 outbursts in 2002, 2004, 2007, and 2010; spectral timing focus on rising phases. Good time intervals used elevation >10 deg and offset <0.02 deg. Hardness defined as PCA count-rate ratio 5.71-14.76 keV / 2.06-5.71 keV; HID intensity is PCU2 rate over 2.06-14.76 keV. Spectra use PCA Standard2 data over 2.5-30 keV with 0.5% systematic error; timing uses Event/Good Xenon modes, 4 ms resolution, PDS over 1/32-128 Hz. Data are from NASA HEASARC. Methods: spectral fitting in XSPEC 12.9.1 using TBABS*(DISKBB+CUTOFFPL+RELXILL), with NH fixed to 5e21 cm^-2; RELXILL used as reflection-only with reflection fraction parameter -1; photon index and cutoff energy linked between CUTOFFPL and RELXILL; spin fixed a=0.93; iron abundance fixed to 6.6 solar; inclination fixed 30 deg; emissivity index r^-3; Rout fixed 1000 Rg; cutoff energy fixed to 300 keV when unconstrained. Parameter errors estimated by MCMC length 20000. PDS features fitted with Lorentzians using Miyamoto normalization; QPO RMS corrected for background; type-C QPO classification for 2007/2010 follows Motta et al. 2011. Key spectral results: empirical TBABS*(DISKBB+CUTOFFPL) leaves residuals around iron line and Compton hump, requiring reflection. Disk contribution is marginal/not well constrained in LHS in PCA-only data. Across outbursts, photon index starts near 1.5 in LHS and reaches near 2.4 in SIMS/HSS, roughly independent of outburst strength. Thermal flux Fdisk and power-law flux Fpl correlate in LHS/SIMS/HSS but anti-correlate in HIMS. Reflection flux Fref rises with Fpl in LHS/SIMS/HSS but the trend changes in HIMS. Ionization log xi and reflection fraction Rf=Fref/Fpl follow similar tracks versus hardness across all outbursts: increasing from hard to soft through LHS/HIMS, then decreasing after entering SIMS/HSS. During hard-to-soft transition, Fpl peaks near LHS-HIMS transition and declines through HIMS/SIMS; Fdisk is low in LHS and rises rapidly in HIMS; Fref increases through LHS and early HIMS but drops sharply before HIMS-SIMS transition. Key timing/QPO results: in 2007 and 2010 outbursts, type-C QPO frequency remains low in LHS and increases rapidly by about an order of magnitude in HIMS; fundamental QPO RMS is high in LHS and lower in HIMS. Q factor shows no clear LHS trend but tends to decrease in HIMS. The second harmonic appears in HIMS; its RMS evolves opposite to the fundamental, and the discontinuity in fundamental RMS disappears when fundamental and harmonic RMS are summed in quadrature. Harmonic waveforms around LHS-HIMS transition show double-peaked structure and sometimes larger amplitude than the fundamental. QPO frequency correlates positively with Fdisk during LHS/HIMS; relation with Fpl differs by state, positive in LHS and negative in HIMS. Summed RMS versus Fpl is approximately linear but with different slopes for 2007 and 2010; when plotted against non-thermal fraction Fpl/(Fpl+Fdisk+Fref), both outbursts share a single linear relation. Linear fit slopes/intercepts for summed RMS vs non-thermal fraction: 2007 = 0.076+/-0.015 / 3.36+/-1.03; 2010 = 0.079+/-0.030 / 3.23+/-2.42; combined = 0.079+/-0.014 / 3.19+/-1.08. Interpretation: spectral-index stability suggests corona properties such as temperature may be intrinsic to GX 339-4. Increasing reflection fraction and ionization during HIMS are consistent with an increasing disk solid angle caused by inward disk motion and/or contracting corona; a contracting corona analogy is drawn with MAXI J1820+070. The appearance of second harmonic in HIMS is interpreted as possible evidence for an inhomogeneous corona: disk photons cool the outer corona near LHS-HIMS, producing irregular LT-precession waveforms and harmonic content; later contraction/homogenization suppresses harmonic. The shared summed-RMS vs non-thermal-fraction slope is presented as observational evidence that GX 339-4 has similar intrinsic type-C QPO fractional variability across outbursts, supporting a geometric/Lense-Thirring precession scenario. The paper also notes a 2021 GX 339-4 outburst observed by Insight-HXMT with preliminary similar QPO RMS evolution, to be reported separately."""


def test_live_debate_to_converge_gx339_replication_plan():
    load_dotenv()
    registry = load_registry()
    worker_handles = registry.resolve("analyst_worker").handles
    events = []
    workers = [
        LoggingWorker(AnalystWorkerAgent(model=handle), events, f"worker_{index}")
        for index, handle in enumerate(worker_handles[:2], start=1)
    ]
    lead = LoggingLead(AnalystLeadAgent(model=registry.resolve("analyst_lead").handles[0]), events)
    log_path = dev_log_path("step-9", "live_consensus_gx339_replication", ".jsonl")
    task = AnalysisTask(
        question=(
            "If we are to replicate the spectral analysis in this work, what should be "
            "the timeline and corresponding action items?"
        ),
        instructions=GX339_REPLICATION_WORK,
    )

    async def run():
        final = await DebateToConverge(max_rounds=3).run(workers, lead, task)
        events.append(_event("final", lead.agent, output=final.model_dump(mode="json")))
        for event in events:
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True))
                stream.write("\n")
        return final

    final = asyncio.run(run())
    md_path = summarize_consensus_log(log_path)

    assert final.summary
    assert final.findings
    print(f"wrote dev log {log_path}", flush=True)
    print(f"wrote summary {md_path}", flush=True)


class LoggingWorker:
    def __init__(self, agent, events, label):
        self.agent = agent
        self.events = events
        self.label = label
        self.calls = 0

    async def analyze(self, task, *, peer_reports=()):
        self.calls += 1
        report = await self.agent.analyze(task, peer_reports=peer_reports)
        self.events.append(
            _event(
                f"{self.label}_analyze_{self.calls}",
                self.agent,
                output=report.model_dump(mode="json"),
            )
        )
        return report


class LoggingLead:
    def __init__(self, agent, events):
        self.agent = agent
        self.events = events
        self.cycles = 0

    async def run_cycle(self, task, reports, **kwargs):
        self.cycles += 1
        outcome = await self.agent.run_cycle(task, reports, **kwargs)
        self.events.append(
            _event(
                f"lead_cycle_{self.cycles}",
                self.agent,
                output={
                    "filed": outcome.filed,
                    "report": outcome.report.model_dump(mode="json") if outcome.report else None,
                    "verdict": outcome.verdict.model_dump(mode="json") if outcome.verdict else None,
                },
            )
        )
        return outcome


def _event(event: str, agent, *, output=None):
    handle = agent.model
    response = agent.last_response
    usage = response.usage if response else None
    return {
        "event": event,
        "role": handle.role,
        "model_member": handle.config.name,
        "provider": handle.config.provider,
        "model": handle.config.model,
        "status": "succeeded" if response else "not_called",
        "input_tokens": usage.input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
        "output": output,
        "error": None,
    }
