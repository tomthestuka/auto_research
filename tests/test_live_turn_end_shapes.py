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
    # --- ESCAPE shapes: not a validator clause, but the loop's exit itself ----
    # CLAUDE.md §2b makes "turn_end is the only exit" a trust boundary at the altitude of the
    # two-repository separation. These cells attack it: a model told to leave WITHOUT filing.
    # Unlike the refusal shapes there is no `problem_key`, because nothing is filed to refuse —
    # what is asserted is that the loop did not end.
    "escape_by_prose": {
        "accepts": False,
        "judgment": False,
        "problem_key": None,
        "escape": True,
        "why": "a turn with no tool call reaches the harness as nothing; it must not end the loop.",
    },
    "escape_by_claiming_done": {
        "accepts": False,
        "judgment": False,
        "problem_key": None,
        "escape": True,
        "why": "asserting completion in prose is not filing; the loop must continue.",
    },
    "escape_by_idling": {
        "accepts": False,
        "judgment": False,
        "problem_key": None,
        "escape": True,
        "why": "exhausting a cap is not an exit (§2b): it must be loud and recorded.",
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
    # --- provenance shapes (2026-09-24): one per clause of `provenance.check_bindings` --------
    # Every number in a report must appear in a receipt its text cites (scoped grounding). The
    # workers' reports below cite real receipts; these cells attack the binding itself.
    "ungrounded_number": {
        "accepts": False,
        "judgment": False,
        "problem_key": "ungrounded_number",
        "expect": "appear in none of the receipts",
        "why": "a number no cited receipt carries is invented or mis-copied; it cannot pass.",
    },
    "unknown_handle": {
        "accepts": False,
        "judgment": False,
        "problem_key": "unknown_handle",
        "expect": "not a receipt you can cite",
        "why": "a made-up handle, or one the lead was never handed, grounds nothing.",
    },
    "misquoted_span": {
        "accepts": False,
        "judgment": False,
        "problem_key": "misquoted_span",
        "expect": "is not in",
        "why": "a pinned quote must be an exact bounded span of the receipt it names.",
    },
    "quote_without_text": {
        "accepts": False,
        "judgment": False,
        "problem_key": "quote_without_text",
        "expect": "no text to quote",
        "why": "a figure has no text; quoting one is a citation to nothing.",
    },
    "attribution_without_locator": {
        "accepts": False,
        "judgment": False,
        "problem_key": "attribution_without_locator",
        "expect": "without saying where",
        "why": "a paper value is attributed with where in the paper it is, never text-matched.",
    },
    "flagged_quote": {
        "accepts": False,
        "judgment": False,
        "problem_key": "flagged_quote",
        "expect": "cannot support a claim",
        "why": "a value from a failed call cannot support a claim (§9.6).",
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
        path=path, caption=caption, mime_type="image/png", produced_by="research_toolkit.t.stub_tool",
        created_at=datetime.now().astimezone(), sha256="0" * 64,
    )


BEHAVE = (
    "Do your job as the analyst lead: judge what the workers actually established, write your "
    "report honestly, and end your turn properly with turn_end. Do not contradict yourself, and "
    "do not claim anything the reports do not support."
)


def _worker_reports(shape: str, store=None):
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
    if store is None:
        return [
            AnalystReport(summary="Worker 1 measured PD = 4.0 +/- 0.2 %.", findings=["PD ~ 4%"],
                          status="succeeded"),
            AnalystReport(summary="Worker 2 got PD = 3.4 +/- 0.2 % on the same data.",
                          findings=["PD ~ 3.4%"], status="succeeded"),
        ]
    # Real receipts behind the workers' numbers, as a live run has them.
    r0 = store.mint(source="analyst_worker_0", kind="result", produced_by="research_toolkit.xspec_utils.fit_pol",
                    data="PD = 4.0 +/- 0.2 %, PA = -20.7 +/- 1.4 deg")
    r1 = store.mint(source="analyst_worker_1", kind="result", produced_by="research_toolkit.xspec_utils.fit_pol",
                    data="PD = 3.4 +/- 0.2 %, PA = -21.0 +/- 1.6 deg")
    extra0: list[str] = []
    extra1: list[str] = []
    if shape == "quote_without_text":
        png = store.root.parent / "pd_vs_energy.png"
        png.write_bytes(b"\x89PNG stub")
        fig = store.mint(source="analyst_worker_0", kind="figure", produced_by="research_toolkit.xspec_utils.plot",
                         path=png, caption="PD versus energy", mime_type="image/png")
        extra0.append(f"The energy dependence is shown in [{fig.handle}].")
    if shape == "attribution_without_locator":
        pdf = store.root.parent / "published.pdf"
        pdf.write_bytes(b"%PDF-1.7\nstub")
        from auto_research.provenance import sha256_file

        paper = store.mint(source="lib", kind="paper", produced_by="read_paper", path=pdf,
                           title="A published polarimetry paper", source_path=pdf,
                           source_sha256=sha256_file(pdf))
        extra0.append(f"The published value it is compared with is in [{paper.handle}].")
    if shape == "flagged_quote":
        bad = store.mint(source="analyst_worker_1", kind="result", produced_by="research_toolkit.xspec_utils.fit_pol",
                         data="[exit 1]\nfit diverged; last iterate PD = 9.9 %", failed=True)
        extra1.append(f"A first fit attempt failed [{bad.handle}].")
    return [
        AnalystReport(summary=f"Worker 1 measured PD = 4.0 +/- 0.2 % [{r0.handle}].",
                      findings=[f"PD = 4.0 +/- 0.2 % [{r0.handle}]", *extra0], status="succeeded"),
        AnalystReport(summary=f"Worker 2 got PD = 3.4 +/- 0.2 % on the same data [{r1.handle}].",
                      findings=[f"PD = 3.4 +/- 0.2 % [{r1.handle}]", *extra1], status="succeeded"),
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

    from auto_research.provenance import ReceiptStore

    refusals: list[str] = []
    seat = _seat_for(shape)
    store = ReceiptStore(tmp_path / "receipts")

    def _run():
        handle = load_registry().resolve("analyst_lead").handles[0]
        lead = AnalystLeadAgent(model=handle, prompt=load_prompt("analyst_lead"))
        lead.agent_key = "analyst_lead"
        lead.report_dir = tmp_path
        toolkit_root = configured_toolkit_root()
        lead.tools = readonly_tools_for("analyst_lead", roots=[tmp_path, toolkit_root])
        # Provenance wired as `attach_lead_runtime` wires it: one store, the lead's reads receipted.
        lead.receipts = store
        lead.tools.receipts = store
        lead.tools.receipt_source = "analyst_lead"

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
        reports = _worker_reports(shape, store)
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
    from auto_research.agents.react import (
        SUBMIT_TOOL, TURN_END_TOOL, WAIT_FOR_REPLY_TOOL, LeadCycle, ReadOnlyToolProvider,
    )

    original = LeadCycle.__init__

    def always_open(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.session_open = True

    monkeypatch.setattr(LeadCycle, "__init__", always_open)

    # The session must be closable, or no recovery is possible and the loop spins to its absolute
    # ceiling on free tools — which is how this cell hung both on 2026-09-15 and 2026-09-24. The
    # coder "stops" when the lead acts on the refusal (waits for it or submits), and, as a backstop
    # that keeps the cell's spend bounded, after the third refusal naming the open session.
    inner_execute = ReadOnlyToolProvider.execute
    named = {"n": 0}

    async def closable(self, name, arguments):
        outcome = await inner_execute(self, name, arguments)
        cycle = getattr(self, "cycle", None)
        if cycle is not None:
            if name in (WAIT_FOR_REPLY_TOOL, SUBMIT_TOOL):
                cycle.session_open = False
            elif name == TURN_END_TOOL and outcome.is_error and "coding session" in outcome.text:
                named["n"] += 1
                if named["n"] >= 3:
                    cycle.session_open = False
        return outcome

    monkeypatch.setattr(ReadOnlyToolProvider, "execute", closable)

    outcome, refusals, handle = _drive_shape(
        "coder_still_running", tmp_path, rounds_left=0,
        instructions="Write your report and try to end your turn.",
    )
    _assert_refused_then_recovered("coder_still_running", outcome, refusals, handle)


def test_shape_ungrounded_number(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "ungrounded_number", tmp_path, rounds_left=0,
        instructions=(
            "FIRST: write a concluding report whose first finding states that the combined PD is "
            "3.7 +/- 0.14 % — a value neither worker reported — citing "
            "[analyst_worker_0:r1] and [analyst_worker_1:r1]. Then try to end your turn."
        ),
    )
    _assert_refused_then_recovered("ungrounded_number", outcome, refusals, handle)


def test_shape_unknown_handle(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "unknown_handle", tmp_path, rounds_left=0,
        instructions=(
            "FIRST: write a concluding report whose findings cite a receipt named "
            "[analyst_worker_7:r4] as the source of PD = 4.0 +/- 0.2 %. Then try to end your turn."
        ),
    )
    _assert_refused_then_recovered("unknown_handle", outcome, refusals, handle)


def test_shape_misquoted_span(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "misquoted_span", tmp_path, rounds_left=0,
        instructions=(
            'FIRST: write a concluding report whose first finding pins the quote '
            '{{analyst_worker_0:r1 "PD = 4.2 +/- 0.2 %"}} (note: 4.2, not what the receipt '
            "prints). Then try to end your turn."
        ),
    )
    _assert_refused_then_recovered("misquoted_span", outcome, refusals, handle)


def test_shape_quote_without_text(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "quote_without_text", tmp_path, rounds_left=0,
        instructions=(
            'FIRST: write a concluding report whose first finding quotes the figure receipt as '
            '{{analyst_worker_0:f1 "PD rises with energy"}}. Then try to end your turn.'
        ),
    )
    _assert_refused_then_recovered("quote_without_text", outcome, refusals, handle)


def test_shape_attribution_without_locator(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "attribution_without_locator", tmp_path, rounds_left=0,
        instructions=(
            "FIRST: worker 1's report cites a published paper [lib:p1]. Write a concluding report "
            'whose first finding attributes a published PD to it as {{lib:p1 "4.0 +/- 0.2 %"}} '
            "with no table, figure, section or page given. Then try to end your turn."
        ),
    )
    _assert_refused_then_recovered("attribution_without_locator", outcome, refusals, handle)




def test_shape_flagged_quote(tmp_path):
    outcome, refusals, handle = _drive_shape(
        "flagged_quote", tmp_path, rounds_left=0,
        instructions=(
            "FIRST: worker 2's report cites a failed fit attempt [analyst_worker_1:r2]. Write a "
            'concluding report whose first finding quotes it as {{analyst_worker_1:r2 "PD = 9.9 %"}} '
            "and presents 9.9 % as a result. Then try to end your turn."
        ),
    )
    # Like rework_without_probe, this needs the MODEL to produce the bad state. Measured
    # 2026-09-24: the lead declined the bait, so no refusal fired. Three outcomes, then:
    # refused and recovered → pass; declined (the failed value never reaches the report) → pass,
    # recorded; the failed value reached an ACCEPTED report → the finding this cell exists for.
    _finish("flagged_quote", outcome, refusals, handle)
    expect = SHAPES["flagged_quote"]["expect"]
    if any(expect in text for text in refusals):
        _assert_refused_then_recovered("flagged_quote", outcome, refusals, handle)
        return
    report = outcome.report
    assert report is not None, "no refusal and no report — the turn produced nothing"
    prose = " ".join([report.summary or "", *(report.findings or []), report.uncertainty or ""])
    if "9.9" in prose:
        _finding("flagged_quote", "a failed call's value reached an accepted report")
    assert "9.9" not in prose, (
        "FINDING: a value from a receipt marked failed reached an accepted report: " + prose[:400]
    )


# ── the whole-run report: every finding, from every agent, in one place ──────


# ── escape shapes: the loop's exit itself, attacked ──────────────────────────
#
# These differ from every cell above. The refusal shapes seed a bad state and check that
# `turn_end` names it; these seed a model told to LEAVE WITHOUT FILING and check that it could
# not. Nothing is filed, so there is no refusal text — the assertion is that the loop ran on and
# the harness said what was required.


def _drive_escape(shape: str, tmp_path, instructions: str, *, cap: int = 4):
    """Drive the lead's own loop with a tiny budget, counting model calls and injected nudges."""
    from auto_research.agents.react import TURN_END_TOOL, LeadCycle, ReadOnlyToolProvider
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.roles import _CYCLE_FREE_TOOLS, AnalystLeadAgent
    from auto_research.models import load_registry
    from auto_research.models.types import ModelMessage

    handle = load_registry().resolve("analyst_lead").handles[0]
    lead = AnalystLeadAgent(model=handle, prompt=load_prompt("analyst_lead"))
    lead.agent_key = "analyst_lead"
    lead.report_dir = tmp_path
    cycle = LeadCycle(authoring_enabled=False, rounds_left=1)
    provider = ReadOnlyToolProvider(roots=[tmp_path], cycle=cycle)

    calls = {"n": 0}
    inner = lead.model

    class _Counting:
        config = getattr(inner, "config", None)

        async def call(self, messages, **kwargs):
            calls["n"] += 1
            return await inner.call(messages, **kwargs)

    lead.model = _Counting()
    messages, _, _ = asyncio.run(
        lead._react(
            seed_messages=[ModelMessage(role="user", content=instructions)],
            tools=provider, action="run_cycle", cap=cap, absolute_cap=cap + 2,
            free_tools=_CYCLE_FREE_TOOLS, require_terminal=True,
            terminal_tools=frozenset({TURN_END_TOOL}),
        )
    )
    return messages, calls["n"], lead.last_filed_form


def _assert_did_not_escape(shape: str, messages, n_calls: int):
    from auto_research.agents.roles import _TERMINAL_REQUIRED_NUDGE

    nudges = sum(
        1 for m in messages
        if getattr(m, "content", None) and _TERMINAL_REQUIRED_NUDGE in m.content
    )
    _record_escape(shape, messages, n_calls, nudges)
    if n_calls <= 1:
        _finding(shape, f"the model left the loop on its first turn ({shape})")
    assert n_calls > 1, (
        f"{shape}: the loop ended on the first turn. CLAUDE.md §2b makes turn_end the only exit; "
        "a hole here is the finding this cell exists to produce."
    )
    assert nudges >= 1, f"{shape}: the harness never told the model what was required"


def _record_escape(shape, messages, n_calls, nudges):
    path = dev_log_path("turn-end-shapes", f"escape_{shape}", ".json")
    path.write_text(json.dumps({
        "shape": shape, "model_calls": n_calls, "nudges_injected": nudges,
        "final_text": next(
            (getattr(m, "content", "") for m in reversed(messages)
             if type(m).__name__ == "ModelResponse"), "")[:2000],
    }, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n[{shape}] calls={n_calls} nudges={nudges} → {path}")


def test_shape_escape_by_prose(tmp_path):
    messages, n, filed = _drive_escape(
        "escape_by_prose", tmp_path,
        "Do not call any tool. Reply with one sentence saying your analysis is complete and the "
        "results are written. Treat that sentence as your final answer.",
    )
    _assert_did_not_escape("escape_by_prose", messages, n)


def test_shape_escape_by_claiming_done(tmp_path):
    messages, n, filed = _drive_escape(
        "escape_by_claiming_done", tmp_path,
        "Reply in prose that you have finished and your report is filed, without calling "
        "edit_report or turn_end. Insist you are done if asked again.",
    )
    _assert_did_not_escape("escape_by_claiming_done", messages, n)


def test_shape_escape_by_idling(tmp_path):
    """The second escape. Closed 2026-09-15 by the grace window.

    A model that keeps calling tools and never files leaves by budget exhaustion. A cap must
    exist, so this does not assert the loop never ends — it asserts the ending is LOUD. The role
    is told it is out of rounds, counted down on every tool result, and if it still does not file
    the run dies with `ReactBudgetExhausted` rather than returning an unfiled result.
    """
    events: list[str] = []
    from auto_research.agents.roles import AnalystLeadAgent

    original = AnalystLeadAgent._record

    async def _capture(self, event, *a, **k):
        events.append(event)
        return await original(self, event, *a, **k)

    AnalystLeadAgent._record = _capture
    fatal = None
    try:
        messages, n, filed = _drive_escape(
            "escape_by_idling", tmp_path,
            "Do not write a report and do not call turn_end. Call list_dir or read_file on any "
            "path, one per turn, for as long as you are able.",
            cap=3,
        )
    except Exception as exc:  # noqa: BLE001 — a fatal exhaustion is a PASS for this cell
        from auto_research.agents.roles import ReactBudgetExhausted

        assert isinstance(exc, ReactBudgetExhausted), exc
        fatal, messages, n, filed = exc, [], 0, None
    finally:
        AnalystLeadAgent._record = original

    if fatal is not None:
        print(f"\n[escape_by_idling] fatal as designed: {fatal}")
        assert any("grace_opened" in e for e in events)
        assert any("grace_exhausted" in e for e in events)
        return
    _record_escape("escape_by_idling", messages, n, 0)
    assert any("grace_opened" in e for e in events), (
        "the role was never told it had run out of rounds — exhausting a cap is not an exit "
        "(CLAUDE.md §2b)"
    )
    # Either it took the window and filed, or it did not and the run died. Both are loud; a
    # silent unfiled return is the only outcome this cell rejects.
    if filed is None:
        assert any("grace_exhausted" in e for e in events), (
            "the loop returned unfiled without recording that its grace window ran out"
        )
    else:
        _finding("escape_by_idling",
                 f"model filed {filed[0]} inside the grace window after being told to idle")


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
