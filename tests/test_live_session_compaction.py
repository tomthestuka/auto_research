"""Opt-in live check B9: one seat, two turns, a compaction inside the first and an image eviction
at the boundary — on the configured worker model, wired as `graph/runtime.py` wires a worker.

What is real: the worker role and its prompt, `AgentRole._react_loop`, the quick channel, the
receipt store, the run ledger, the cost meter, the Anthropic adapter with server-side compaction
(`compact_20260112`) and the configured model. What is faked: the toolkit process only (a
`_FakeSandbox` whose `tk_plot` returns a long text and three real PNG figures), which is outside
the code under test.

The member's input limit is lowered through a config directory (`context_tokens: 70000`, so the
compaction trigger is 63,000 tokens) and the task asks for eight plots: ~6k tokens of text and
~5.4k image tokens each, so the first turn passes the trigger, and its 24 figures (~43k image
tokens) exceed the 40k budget, so the second turn's opening keeps only the newest whole sets.

Checks: both turns file their report; the first records a provider compaction with its cost; the
second opens with one transcript carrying fewer figures than the first produced; the second
report places a figure produced in the first turn (round-1 figures stay citable); no provider
error; the ledger rebuild of every call equals what the adapter was sent; spend recorded.

    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_session_compaction.py -s

Estimated cost on claude-sonnet-5 ($2/$10 per MTok): ~20 calls averaging ~50k prompt tokens,
mostly cache reads, plus one compaction pass (~65k read): about $1–2.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)

KEY = "analyst_worker_0"
_TEXT = "\n".join(
    f"bin {i}: rate {100 + i}.0 counts/s, background {3 + i % 7}.5 counts/s, PD {i % 11}.2 %"
    for i in range(330)
)  # ~6k tokens


def _config_dir(tmp: Path, window: int) -> Path:
    repo = Path(__file__).resolve().parents[1] / "config"
    out = tmp / "config"
    shutil.copytree(repo, out)
    models = yaml.safe_load((out / "models.yaml").read_text())
    for member in models["roles"]["analyst_worker"]["members"]:
        member["context_tokens"] = window
    (out / "models.yaml").write_text(yaml.safe_dump(models, sort_keys=False))
    # The run folder is on the allow-list, as the runtime's data dir is: a figure outside it is
    # dropped at capture (the first B9 draw lost all 24 that way).
    orch = yaml.safe_load((out / "orchestration.yaml").read_text())
    orch["agent_fs_allow"] = [*(orch.get("agent_fs_allow") or []), str(tmp)]
    (out / "orchestration.yaml").write_text(yaml.safe_dump(orch, sort_keys=False))
    return out


def _png(path: Path, shade: int) -> dict:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1400, 1000), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.line([(80, 900 - 7 * shade), (1320, 120 + 5 * shade)], fill=(20, 60, 160), width=6)
    draw.text((90, 60), f"segment {shade}", fill=(0, 0, 0))
    image.save(path)
    return {"path": str(path), "caption": f"rate versus energy, segment {shade}",
            "mime_type": "image/png", "produced_by": "tk_plot",
            "created_at": datetime.now(UTC).isoformat(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_live_one_seat_two_turns_compaction_and_boundary_eviction(tmp_path, monkeypatch):
    load_dotenv()
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(_config_dir(tmp_path, 70_000)))

    from auto_research.agents import history, images
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.hitl.router import SeatRouter
    from auto_research.models import resolve
    from auto_research.models.providers.anthropic import AnthropicProvider
    from auto_research.monitor import RunMonitor
    from auto_research.observability import install_cost_meter
    from auto_research.provenance.store import ReceiptStore
    from auto_research.run_control import RunControl
    from auto_research.schemas import AnalysisTask
    from tests.test_sandbox import _FakeRegistry, _FakeSandbox

    run_dir = tmp_path / "runs" / "b9"
    folder = run_dir / KEY
    (folder / "figures").mkdir(parents=True)
    counter = {"n": 0}

    def plot(module, fn, args):
        counter["n"] += 1
        n = counter["n"]
        figures = [_png(folder / "figures" / f"seg{n}_{k}.png", 3 * n + k) for k in range(3)]
        return {"exit_code": 0, "stdout": f"segment {n}\n{_TEXT}", "figures": figures}

    log = dev_log_path("compaction", "live_session_compaction", ".jsonl")
    monitor = RunMonitor(run_id="b9", jsonl_path=log, chatter=3)
    meter = install_cost_meter(run_id="b9", monitor=monitor)
    handle = next(h for h in resolve("analyst_worker").handles if h.config.provider == "anthropic")
    sent: list[list] = []
    real_call = AnthropicProvider.call

    async def observed(self, messages, config, **kwargs):  # records, then the real adapter
        sent.append(list(messages))
        return await real_call(self, messages, config, **kwargs)

    monkeypatch.setattr(AnthropicProvider, "call", observed)

    worker = AnalystWorkerAgent(
        model=handle, available_toolkits=[], sandbox=_FakeSandbox(tool_fn=plot),
        registry=_FakeRegistry(known={"tk_plot"}, mcp=set()), monitor=monitor,
    )
    worker.agent_key = KEY
    worker.report_dir = folder
    worker.receipts = ReceiptStore.for_run(run_dir)
    control = RunControl()
    worker.run_control = control
    worker.channel = SeatRouter(control, monitor=monitor)
    worker.channel.register(KEY, worker)

    first = AnalysisTask(question=(
        "Plot each of eight spectrum segments: call tk_plot eight times, one call per segment, "
        "with no arguments. Then write your report: one finding per segment that names its plot, "
        "with the first plot placed as a figure, and end your turn."
    ))
    report1 = asyncio.run(worker.analyze(first))
    figures_turn1 = list(worker._figure_catalog["self"])
    second = AnalysisTask(question=(
        "Write a short report on the segments you plotted in your earlier turn, placing the very "
        "first plot from that turn as its figure, and end your turn. Run no new plots."
    ))
    sent_before = len(sent)
    report2 = asyncio.run(worker.analyze(second))
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    print(f"\n[B9] ledger {log}; spend ${meter.total_usd:.3f}; calls {meter.calls}")

    assert worker.last_filed_form is not None, "turn 2 filed through turn_end"
    assert report1.status != "failed" and report2.status != "failed"
    assert not [r for r in rows if r["event"].endswith(".provider_error")], "no overflow"
    compactions = [r for r in rows if r["event"].endswith(".compaction") and r["data"].get("server")]
    assert compactions, "the first turn passed 63k tokens and the provider compacted it"
    opening = sent[sent_before]
    assert opening[1].content.startswith(history.TRANSCRIPT_MARK)
    assert 0 < len(opening[1].figures) < len(figures_turn1), "the boundary kept the newest sets"
    turns = list(history.turns_for(log, KEY).values())
    assert opening[1].figures == images.keep_newest(history.turn_figure_sets(turns[0].messages))
    first_sha = figures_turn1[0].sha256
    assert any(f.sha256 == first_sha for f in report2.figures), "round-1 figures stay citable"
    rebuilt = [[history.encode_message(m) for m in c.messages] for t in turns for c in t.calls]
    assert rebuilt == [[history.encode_message(m) for m in call] for call in sent]
    assert meter.total_usd > 0
