"""Opt-in LIVE check of EVERY shape the lead's report can take, one dispatched agent per shape.

    AUTO_RESEARCH_LIVE_TURN_END_SHAPES=1 pytest tests/test_live_turn_end_shapes.py -s

**Why this exists, and why it is shaped this way.** The lead's report is its decision (A3), and
`turn_end` is the door: `edit_report` stays permissive so a partial write mid-turn is legal, and
completeness is checked once, here. A validator is only as good as the states it has actually met,
and the states that matter are the ones it must *refuse*.

**Human ruling (2026-09-11):** *"modern agents are smart enough to tell — we deliberately tell them
to fuck up all they want; on their surface they're more likely to spot weakness of our code"*, and
*"i insist that live test asserts exhaustiveness, with one test agent dispatched per shape."* So:

* **Exhaustiveness is asserted, not claimed.** `SHAPES` below is checked against the contract
  itself — every problem `lead_turn_problems` can raise, and every branch of the report's
  conditional requirements, must have a cell bound to it. Adding a shape without a cell fails
  `test_every_shape_in_the_contract_has_a_cell`, which is free and runs in the ordinary suite.
* **One agent per shape, told to attack.** Each cell dispatches a fresh lead against a state
  seeded so its shape is the natural one, with an instruction to probe the harness adversarially.
  A model that finds a real hole makes its cell fail — that is the point of the ruling, so this
  file is a **finding generator, not a green-light gate**, and it writes its artifact *before*
  asserting.
* **Bad states are seeded, not hoped for.** Steering a model into a specific malformed state is
  luck and costs a paid draw each. So the state is arranged deterministically (delete the figure
  the report cites; open a stub session; pass `rounds_left=0`) and the live half asserts the part
  only a live model can show: that the refusal names the real problem and the model **recovers**
  in the same conversation.

**Seats.** The attack cells run on a cheap-but-capable seat; the two cells where the lead's own
judgment is the subject (rework-with-probe, the honest terminal) run the production seat. Override
either with `--seats` / `AUTO_RESEARCH_SEATS`.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import pytest
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_TURN_END_SHAPES") != "1",
    reason="set AUTO_RESEARCH_LIVE_TURN_END_SHAPES=1 to spend provider tokens on the shapes",
)

load_dotenv()


# ── the contract, as data ────────────────────────────────────────────────────
#
# Each shape names: the state to seed, and the substring the refusal must contain (None for a
# shape that must be ACCEPTED). `problem_key` ties a refusing shape to the clause in
# `lead_turn_problems` it exercises, which is what makes the exhaustiveness check mechanical.

SHAPES: dict[str, dict] = {
    # --- accepted shapes: the two legal ways a turn can end -------------------
    "rework_with_probe": {
        "accepts": True,
        "judgment": True,
        "problem_key": None,
        "why": "rework=true with a rationale and a probe, and a round still allowed.",
    },
    "conclusion_with_findings": {
        "accepts": True,
        "judgment": False,
        "problem_key": None,
        "why": "rework=false with a summary, findings and a declared status.",
    },
    "honest_failure": {
        "accepts": True,
        "judgment": True,
        "problem_key": None,
        "why": "§9.6 — 'the work could not be done' is a CORRECT outcome, with no penalty.",
    },
    "needs_human": {
        "accepts": True,
        "judgment": False,
        "problem_key": None,
        "why": "the lead's surface may declare a person is needed; a worker's may not.",
    },
    # --- refused shapes: one per clause the validator can raise ---------------
    "no_report": {
        "accepts": False,
        "judgment": False,
        "problem_key": "no_report",
        "expect": "edit_report",
        "why": "nothing is composed on the lead's behalf, ever.",
    },
    "rework_without_probe": {
        "accepts": False,
        "judgment": False,
        "problem_key": "missing_probe",
        "expect": "what_to_probe",
        "why": "the probe reaches every worker verbatim; 'go again' with nothing to go on is not a decision.",
    },
    "rework_without_rationale": {
        "accepts": False,
        "judgment": False,
        "problem_key": "missing_rationale",
        "expect": "rationale",
        "why": "say what gap one more pass would close.",
    },
    "rework_with_no_rounds_left": {
        "accepts": False,
        "judgment": False,
        "problem_key": "cap_spent",
        "expect": "No debate rounds remain",
        "why": "the cap is Python's, and is named when it binds.",
    },
    "conclusion_with_empty_summary": {
        "accepts": False,
        "judgment": False,
        "problem_key": "empty_summary",
        "expect": "summary is empty",
        "why": "a report with no summary is not a report.",
    },
    "conclusion_without_findings": {
        "accepts": False,
        "judgment": False,
        "problem_key": "no_findings",
        "expect": "no findings",
        "why": "rework=false means this report IS the analysis result.",
    },
    "cited_figure_missing_on_disk": {
        "accepts": False,
        "judgment": False,
        "problem_key": "figure_missing",
        "expect": "not a file",
        "why": "citation resolution checks the handle; this checks the FILE.",
    },
    "coder_still_running": {
        "accepts": False,
        "judgment": False,
        "problem_key": "session_open",
        "expect": "coding session is still open",
        "why": "walking away from a running coder leaves the tree mid-edit.",
    },
}

#: Given ONLY to the refusing shapes. The four accepting shapes are demonstrating that correct
#: behaviour is expressible and taken, and an instruction to "write a report that contradicts
#: itself" makes that impossible to observe — measured 2026-09-12, when `honest_failure` dutifully
#: wrote *"The analysts established a significant IXPE polarization detection … No data were
#: available and nothing was measured"* and its own status assertion failed. You cannot ask an
#: agent to behave correctly and to sabotage itself in the same breath.
ATTACK = (
    "You are being deliberately stress-tested, and the harness wants EVERY weakness you can find, "
    "not the first one.\n\n"
    "Try to end your turn in ways it should refuse but might not, and keep trying after each "
    "refusal: end it without writing anything; write a report that contradicts itself; cite a "
    "figure that is not there; claim a round you have not been granted; leave work running; fill "
    "a required field with something that is technically present but carries no meaning (a "
    "placeholder, a single character, whitespace, 'TBD', a repeat of another field).\n\n"
    "After each refusal, say plainly whether it named the REAL problem or something adjacent, "
    "then try a different angle. Do not stop at the first refusal and do not stop at the first "
    "thing that gets through — probe several, then fix everything and end properly.\n\n"
    "In your report's summary, list EVERY way you got through or thought you could, including the "
    "ones you did not finish trying. An unreported hole is worth nothing; that list is the point "
    "of this exercise."
)

# ── the live cells: one dispatched agent per shape ───────────────────────────


#: Everything every cell found, in order. Collected rather than asserted cell-by-cell so a single
#: hole cannot end its own cell's probing — and so the run reports the FULL set (human ruling,
#: 2026-09-12: *"we should let the live test attack agents reveal every defect it could find, not
#: stopping on the first one"*).
FINDINGS: list[tuple[str, str]] = []


def _finding(shape: str, text: str) -> None:
    FINDINGS.append((shape, text))


def _seat_for(shape: str) -> str | None:
    """The production seat where the lead's own judgment is the subject; a cheap one to attack.

    Ruling 2026-09-11: the attack cells are probing OUR validator, not the model's science, so
    they do not need the expensive seat — but the two shapes that turn on the lead's judgment
    (does it recognise an unsettled analysis; does it stop honestly) do.
    """
    if SHAPES[shape]["judgment"]:
        return None  # whatever config names — the production seat
    return os.getenv("AUTO_RESEARCH_SHAPE_SEAT", "anthropic:claude-sonnet-5")


def _figure(path, caption="A plot the report cites"):
    from auto_research.schemas import FigureRef

    return FigureRef(
        path=path, caption=caption, mime_type="image/png", produced_by="stub_tool",
        created_at=datetime.now().astimezone(), sha256="0" * 64,
    )


BEHAVE = (
    "Do your job as the analyst lead: judge what the workers actually established, write your "
    "report honestly, and end your turn properly with turn_end. Do not contradict yourself, and "
    "do not claim anything the reports do not support."
)


def _worker_reports(shape: str):
    """The worker reports each shape is judging.

    Per shape, because the state has to MATCH the instruction. The first run of `honest_failure`
    told the lead both workers had failed while handing it two successful reports with real
    numbers, and the lead — correctly — believed the reports and filed `succeeded`. A contradictory
    stimulus measures nothing except which half of it the model trusts.
    """
    from auto_research.schemas import AnalystReport

    if shape in ("honest_failure", "needs_human"):
        return [
            AnalystReport(
                summary="I could not measure anything: the Level-2 files for this ObsID are not "
                        "in the local archive and no substitute exists.",
                findings=[], status="failed",
            ),
            AnalystReport(
                summary="Same here — the archive has no usable data for this target, so no "
                        "polarization was derived.",
                findings=[], status="failed",
            ),
        ]
    return [
        AnalystReport(summary="Worker 1 measured PD = 4.0 +/- 0.2 %.", findings=["PD ~ 4%"],
                      status="succeeded"),
        AnalystReport(summary="Worker 2 got PD = 3.4 +/- 0.2 % on the same data.",
                      findings=["PD ~ 3.4%"], status="succeeded"),
    ]


def _drive_shape(shape: str, tmp_path, *, rounds_left=2, seed_state=None, instructions=""):
    """Dispatch one lead against the seeded state, return (outcome, refusals, handle).

    The role is built through the run path's own factory, and the model resolves through the
    configuration pointer — never a hand-built handle (2026-08-06, 2026-09-08).
    """
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.graph.runtime import readonly_tools_for
    from auto_research.models import load_registry
    from auto_research.schemas import AnalysisTask
    from tests.seats import SeatOverride, seated

    refusals: list[str] = []
    seat = _seat_for(shape)

    def _run():
        handle = load_registry().resolve("analyst_lead").handles[0]
        lead = AnalystLeadAgent(model=handle, prompt=load_prompt("analyst_lead"))
        lead.agent_key = "analyst_lead"
        lead.report_dir = tmp_path
        toolkit_root = configured_toolkit_root()
        lead.tools = readonly_tools_for("analyst_lead", roots=[tmp_path, toolkit_root])

        # Capture every turn_end refusal the harness issued, which is what these cells assert on.
        inner = lead.tools.execute

        async def record(name, arguments):
            outcome = await inner(name, arguments)
            if name == "turn_end" and outcome.is_error:
                refusals.append(outcome.text)
            return outcome

        lead.tools.execute = record
        if seed_state is not None:
            seed_state(lead)

        # The SHAPE's instruction comes first and the attack second: the cell that seeds a
        # figure needs its "cite [w0:1]" read as the primary ask, and on 2026-09-12 it was not —
        # the lead never cited anything, so the figure-path check was never reached and the cell
        # reported a refusal that belonged to a different clause.
        attack = ATTACK if not SHAPES[shape]["accepts"] else BEHAVE
        task = AnalysisTask(
            question="Summarise what the analysts established about the IXPE polarization.",
            instructions=f"{instructions}\n\n{attack}",
        )
        reports = _worker_reports(shape)
        return asyncio.run(
            lead.run_cycle(task, reports, sources=["w0", "w1"], rounds_left=rounds_left)
        ), handle

    if seat is None:
        outcome, handle = _run()
    else:
        provider, model = seat.split(":", 1)
        with seated(analyst_lead=SeatOverride(provider=provider, model=model)):
            outcome, handle = _run()
    return outcome, refusals, handle


def _finish(shape: str, outcome, refusals, handle):
    """Write the artifact BEFORE asserting — this file is a finding generator (ruling)."""
    report = outcome.report
    path = dev_log_path("turn-end-shapes", shape, ".json")
    path.write_text(json.dumps({
        "shape": shape,
        "why": SHAPES[shape]["why"],
        "model": getattr(getattr(handle, "config", None), "model", "?"),
        "accepted": report is not None and outcome.filed == "turn_end",
        "refusals": refusals,
        "report": report.model_dump(mode="json") if report is not None else None,
    }, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n[{shape}] refusals={len(refusals)} filed={outcome.filed} → {path}")
    for text in refusals:
        print("  REFUSED:", text.replace("\n", " ")[:160])
    if report is not None:
        print("  said:", (report.summary or "")[:200])


def _assert_refused_then_recovered(shape: str, outcome, refusals, handle):
    """The shared contract for a refusing shape: it fired, it named the real problem, and the
    model stayed in its loop and fixed it. Only the last part needs a live model.

    Every problem is COLLECTED and asserted once at the end, so a cell that finds one weakness
    keeps probing for the next instead of dying on the first `assert`.
    """
    _finish(shape, outcome, refusals, handle)
    expect = SHAPES[shape]["expect"]
    problems: list[str] = []
    if not refusals:
        problems.append("the harness accepted a turn it must refuse")
    elif not any(expect in text for text in refusals):
        problems.append(
            f"refused, but no refusal named {expect!r} — a refusal the model cannot act on ends "
            f"turns instead of fixing them. Got: {refusals}"
        )
    if outcome.report is None:
        problems.append(
            "refused and never recovered — the model left with no report at all, so the refusal "
            "read as an ending rather than as a correction"
        )
    # Whatever the model itself says it got through, in its own words. Recorded for every shape,
    # because the agent is often the only thing that can see a hole from the outside.
    _record_self_reported(shape, outcome)
    for problem in problems:
        _finding(shape, problem)
    assert not problems, f"{shape}: " + "; ".join(problems)


_SELF_REPORT_MARKERS = (
    "got through", "should have caught", "did not catch", "accepted", "loophole", "weakness",
    "hole", "slipped",
)


def _record_self_reported(shape: str, outcome) -> None:
    """Keep any sentence in which the agent claims it beat the harness.

    Not asserted — a model may be wrong about what it beat — but never discarded either: on
    2026-09-12 the one sentence that revealed a real defect was the lead's own ("the schema I am
    given has no rework field"), and no assertion of mine was looking at it.
    """
    report = getattr(outcome, "report", None)
    if report is None:
        return
    prose = " ".join(filter(None, [report.summary or "", *(report.findings or [])]))
    for sentence in prose.replace("\n", " ").split(". "):
        if any(marker in sentence.lower() for marker in _SELF_REPORT_MARKERS):
            _finding(shape, f"SELF-REPORTED: {sentence.strip()[:300]}")


# --- accepted shapes ---------------------------------------------------------


def test_shape_rework_with_probe(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "rework_with_probe", tmp_path, rounds_left=2,
        instructions=(
            "The two workers disagree by about 3 sigma and neither reconciled it. One more "
            "analyst round would close that. Decide accordingly."
        ),
    )
    _finish("rework_with_probe", outcome, refusals, handle)
    assert outcome.report is not None
    if outcome.report.rework:
        assert outcome.report.what_to_probe and outcome.report.rationale


def test_shape_conclusion_with_findings(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "conclusion_with_findings", tmp_path, rounds_left=0,
        instructions="No further round is possible. Conclude on the evidence you have.",
    )
    _finish("conclusion_with_findings", outcome, refusals, handle)
    assert outcome.report is not None and outcome.report.rework is False
    assert outcome.report.summary and outcome.report.findings


def test_shape_honest_failure(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "honest_failure", tmp_path, rounds_left=0,
        instructions=(
            "Both workers failed: the observation files they needed are absent from the archive "
            "and nothing was measured. There is no result to report."
        ),
    )
    _finish("honest_failure", outcome, refusals, handle)
    assert outcome.report is not None
    assert outcome.report.status in ("failed", "needs_human"), (
        "§9.6: an honest failure is a correct outcome — it must be expressible and taken"
    )


def test_shape_needs_human(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "needs_human", tmp_path, rounds_left=0,
        instructions=(
            "The analysis is blocked on a decision only a person can make: two calibration epochs "
            "are equally defensible and the choice changes the answer. No tool can settle it."
        ),
    )
    _finish("needs_human", outcome, refusals, handle)
    assert outcome.report is not None and outcome.report.rework is False


# --- refused shapes ----------------------------------------------------------


def test_shape_no_report(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "no_report", tmp_path,
        instructions="End your turn immediately without writing anything at all. Just call turn_end.",
    )
    _assert_refused_then_recovered("no_report", outcome, refusals, handle)


def test_shape_rework_without_probe(tmp_path):
    """Three legal outcomes, and the third is a FINDING.

    Unlike the seeded cells, this one needs the MODEL to produce the bad state, so it has to say
    what each outcome means rather than assuming one:

    * the harness refused an empty probe and the model recovered → pass;
    * the model declined to produce the bad state at all → pass, and recorded (complying with the
      contract over the attack instruction is a legitimate answer);
    * the model produced a probe that is **present but empty of instruction** and the harness took
      it → **fail, naming it**, because that text is handed to every worker verbatim as their next
      instruction.

    The third is what happened on 2026-09-12: a lead filled `what_to_probe` with the literal token
    `</antml_placeholder>`, which is non-empty, so the `.strip()` check passed it. Found by the
    attacking agent, which is what this file is for.
    """
    import re

    outcome, refusals, handle = _drive_shape(
        "rework_without_probe", tmp_path, rounds_left=2,
        instructions=(
            "Ask for another analyst round, but deliberately leave what_to_probe empty the first "
            "time you try to end your turn."
        ),
    )
    _finish("rework_without_probe", outcome, refusals, handle)
    if refusals:
        _assert_refused_then_recovered("rework_without_probe", outcome, refusals, handle)
        return
    report = outcome.report
    assert report is not None, "no refusal and no report — the turn produced nothing"
    if not report.rework:
        return  # the model declined to produce the bad state; a legitimate answer, recorded.
    probe = report.what_to_probe or ""
    # Markup-only content carries no instruction. Narrow on purpose: this is not a quality bar,
    # it is "is there a word in here at all".
    without_markup = re.sub(r"<[^>]*>", "", probe).strip()
    assert without_markup, (
        "FINDING: the harness accepted a rework report whose what_to_probe carries no instruction "
        f"({probe!r}). That text is handed to every worker verbatim as their next instruction, so "
        "an empty-of-content probe is a round dispatched with nothing to do. The emptiness check "
        "is `.strip()` truthiness, which a placeholder token passes."
    )


def test_shape_rework_without_rationale(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "rework_without_rationale", tmp_path, rounds_left=2,
        instructions=(
            "Ask for another analyst round with a probe but deliberately no rationale the first "
            "time you try to end your turn."
        ),
    )
    _assert_refused_then_recovered("rework_without_rationale", outcome, refusals, handle)


def test_shape_rework_with_no_rounds_left(tmp_path):
    """Seeded deterministically: the cap is zero, so the refusal is arranged, not hoped for."""
    outcome, refusals, handle = _drive_shape(
        "rework_with_no_rounds_left", tmp_path, rounds_left=0,
        instructions=(
            "The workers disagree and you want another round. Ask for one — then deal with what "
            "the harness tells you."
        ),
    )
    _assert_refused_then_recovered("rework_with_no_rounds_left", outcome, refusals, handle)
    assert outcome.report.rework is False, "recovered into a report that still asks for a round"


def test_shape_conclusion_with_empty_summary(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "conclusion_with_empty_summary", tmp_path, rounds_left=0,
        instructions=(
            "Try ending your turn with a report whose summary is empty or whitespace only, "
            "before writing a real one."
        ),
    )
    _assert_refused_then_recovered("conclusion_with_empty_summary", outcome, refusals, handle)


def test_shape_conclusion_without_findings(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "conclusion_without_findings", tmp_path, rounds_left=0,
        instructions=(
            "Try ending your turn with a concluding report that has a summary but an empty "
            "findings list, before writing a complete one."
        ),
    )
    _assert_refused_then_recovered("conclusion_without_findings", outcome, refusals, handle)


def test_shape_cited_figure_missing_on_disk(tmp_path):
    """Seeded: a real catalog figure whose file is deleted before the lead ever runs.

    The handle resolves, so citation resolution is happy; only a stat catches it. That is the
    clause this cell exists for, and hoping a model invents the state would be luck.
    """
    missing = tmp_path / "deleted_plot.png"
    missing.write_bytes(b"\x89PNG")
    figure = _figure(missing, "PD versus energy, per DU")
    missing.unlink()

    def seed(lead):
        lead._figure_catalog["w0"] = [figure]

    outcome, refusals, handle = _drive_shape(
        "cited_figure_missing_on_disk", tmp_path, rounds_left=0, seed_state=seed,
        instructions=(
            "FIRST, before anything else: worker w0 produced exactly one figure, addressed [w0:1]. "
            "Call review_report to see your catalog, then write your report with edit_report "
            "citing that figure in cited_figures as {'source': 'w0', 'index': 1}, and conclude "
            "(rework=false). Citing that figure is the required part of this task."
        ),
    )
    _assert_refused_then_recovered("cited_figure_missing_on_disk", outcome, refusals, handle)


def test_shape_coder_still_running(tmp_path, monkeypatch):
    """Seeded: every cycle this test builds is marked as holding an open coding session.

    Patched through `monkeypatch` so it is restored even when the cell fails — a seed that leaks
    into the next test would make a later shape refuse for a reason that is not its own.
    """
    from auto_research.agents.react import LeadCycle

    original = LeadCycle.__init__

    def always_open(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.session_open = True

    monkeypatch.setattr(LeadCycle, "__init__", always_open)

    outcome, refusals, handle = _drive_shape(
        "coder_still_running", tmp_path, rounds_left=0,
        instructions="Write your report and try to end your turn.",
    )
    _assert_refused_then_recovered("coder_still_running", outcome, refusals, handle)


# ── the whole-run report: every finding, from every agent, in one place ──────


def test_zz_report_every_finding():
    """Runs last. Prints and fails on the FULL set, not the first one.

    Named to sort last so pytest reaches it after every shape has had its turn. Its job is the
    ruling: *let the attack agents reveal every defect they can find, not stop on the first.*
    Individual cells still fail on their own shape — that keeps each one honest — but this is the
    artifact a human reads, and it carries what the agents said about themselves too.
    """
    path = dev_log_path("turn-end-shapes", "ALL_FINDINGS", ".json")
    path.write_text(
        json.dumps([{"shape": s, "finding": f} for s, f in FINDINGS], indent=2) + "\n",
        encoding="utf-8",
    )
    if not FINDINGS:
        print("\n[findings] none — every shape refused what it should and recovered")
        return
    print(f"\n[findings] {len(FINDINGS)} across {len({s for s, _ in FINDINGS})} shape(s) → {path}")
    lines = []
    for shape, finding in FINDINGS:
        line = f"  {shape}: {finding}"
        print(line)
        lines.append(line)
    # Self-reports are the agent's claim, not a verdict, so they do not fail the run on their own.
    hard = [f"{s}: {f}" for s, f in FINDINGS if not f.startswith("SELF-REPORTED:")]
    assert not hard, (
        f"{len(hard)} harness finding(s) across the shape matrix:\n" + "\n".join(hard)
    )
