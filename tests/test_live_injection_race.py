"""Opt-in LIVE check that an injected turn never lands where the provider forbids it (A2/L1).

    AUTO_RESEARCH_LIVE_INJECTION_RACE=1 pytest tests/test_live_injection_race.py -s

**Why this exists.** On 2026-09-10 a run died after 3h34m and $42.11 — four tools already
committed — on a 400 naming a server-tool call with no result block. Anthropic documents the rule
it broke: when an assistant turn leaves a provider-executed tool call unresolved, *"the follow-up
user message must contain nothing except the `tool_result` blocks. A block added after the results
… tells the API that the assistant turn is over."* Our loop appended a human steer in exactly that
position, and from B onward a background coder event will want the same slot.

The deterministic half is in `tests/test_run_control.py` (the hold rule asserted on the built
message array) and `tests/test_providers.py` (the predicate, against the real 09-10 turn frozen
out of the prompt dump). What only a live model can supply is the **precondition**: an assistant
turn that genuinely carries an unresolved `server_tool_use` beside a client tool call. That shape
is produced by the provider's own tool-search machinery, not by us, so it cannot be faked into
existence and then trusted.

**It has to be a WORKER, and that was a correction.** This was first written against the lead and
recorded six turns with **no server-tool block at all** — because `ReadOnlyToolProvider` sets
`defer_loading` on nothing, so a lead's tools are all resident and the provider's search tool is
never needed. Only `SandboxToolProvider` defers (every *registry* tool), so the worker is the only
role that can produce the shape — and the 09-10 turn that died was a worker's. The drain under test
lives in `_react`, which every role shares, so driving the worker exercises the same code.

**Wiring.** The worker's provider is the production `SandboxToolProvider` over the real
`ToolkitRegistry` — which reads each package's pure-data `TOOL_MANIFEST` and needs no science
stack, by §9.2's design. No toolkit tool is executed: the model only has to *search* for one,
which is a provider-side step, and its other call goes to a resident harness tool. Models resolve
through the configuration pointer (`--seats` / `AUTO_RESEARCH_SEATS`), never a hand-built handle
(2026-08-06).

**What is stubbed:** nothing on the provider path. The injected text is a stand-in for a coder
event rather than a real coding session, because the queue is what is under test and a real link
costs $27-45.

**Assertions stay on our side of the boundary:** where the injected turn sits in the request we
built, that the vendor accepted it, and that it is delivered exactly once. What the model *says*
about it is recorded for hand reading, never asserted.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_INJECTION_RACE") != "1",
    reason="set AUTO_RESEARCH_LIVE_INJECTION_RACE=1 to spend provider tokens on the race",
)

load_dotenv()


class _RecordingHandle:
    """Wraps the real handle and keeps every request array we built, in order."""

    def __init__(self, handle):
        self._handle = handle
        self.config = getattr(handle, "config", None)
        self.sent: list[list] = []
        self.responses: list = []

    async def call(self, messages, **kwargs):
        self.sent.append(list(messages))
        response = await self._handle.call(messages, **kwargs)
        self.responses.append(response)
        return response


class _RefusingSandbox:
    """A sandbox that refuses to execute, honestly, instead of being ``None``.

    The stimulus asks the model not to run a toolkit tool and a model may do it anyway — one did,
    and `sandbox=None` turned that into an `AttributeError` that read as a harness defect. The
    search is what this cell needs; executing what it finds is not, and a refusal the model can
    read keeps the loop going instead of crashing it.
    """

    async def call_tool(self, module, fn, args, timeout_s=None):
        return {
            "stdout": "",
            "stderr": (
                f"{fn} is not available in this probe (no analysis environment is attached). "
                "You have what you need from the search itself — answer from that."
            ),
            "exit_code": 1,
            "figures": [],
        }


def _worker(folder):
    """The production worker surface: registry tools DEFERRED, harness tools resident."""
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.react import SandboxToolProvider
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.models import load_registry
    from auto_research.sandbox.registry import ToolkitRegistry

    handle = _RecordingHandle(load_registry().resolve("analyst_worker").handles[0])
    worker = AnalystWorkerAgent(model=handle, prompt=load_prompt("analyst_worker"))
    worker.agent_key = "analyst_worker_0"
    worker.report_dir = folder
    toolkit_root = configured_toolkit_root()
    registry = ToolkitRegistry(toolkit_root)
    registry.load()
    worker.tools = SandboxToolProvider(registry, sandbox=_RefusingSandbox(), cwd=folder)
    deferred = sum(
        1 for d in worker.tools.tool_definitions() if d.get("defer_loading")
    )
    assert deferred, (
        "no tool is deferred, so the provider's tool-search step will never run and this cell "
        "cannot produce the shape it exists for — check config tool_search_enabled"
    )
    return worker, handle, toolkit_root


def _position_of(sent, needle: str) -> int | None:
    from auto_research.models import ModelMessage

    for index, message in enumerate(sent):
        if isinstance(message, ModelMessage) and needle in (message.content or ""):
            return index
    return None


def _record(cell: str, handle, extra: dict) -> None:
    from auto_research.models import ModelResponse

    path = dev_log_path("injection-race", cell, ".json")
    turns = []
    for response in handle.responses:
        turns.append(
            {
                "stop": response.stop,
                "has_unresolved_server_tool": response.has_unresolved_server_tool,
                "part_types": [p.get("type") for p in (response.provider_parts or [])],
                "tool_calls": [c.name for c in response.tool_calls],
                "text": (response.content or "")[:2000],
            }
        )
    body = {
        "cell": cell,
        "model": getattr(handle.config, "model", "?"),
        "requests": len(handle.sent),
        "turns": turns,
        **extra,
    }
    path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n[{cell}] {len(handle.sent)} request(s) → {path}")
    for index, turn in enumerate(turns, 1):
        print(f"  turn {index}: stop={turn['stop']} pending={turn['has_unresolved_server_tool']} "
              f"parts={turn['part_types']}")


STEER = "[Human steer] Check the DU3 exposure before you go further."

# Stimuli, in increasing order of how hard they push for ONE turn carrying both a server-tool call
# and a client tool call. Anthropic documents that shape as the unresolved case: *"If Claude calls
# one of your client tools at the same time, the `server_tool_use` block appears without its
# result"*. Which one provokes it is stochastic — the first attempt produced three searches, each
# resolved in-turn — so the cell hunts for the precondition rather than reporting a skip after one
# draw. One draw from a stochastic process is not validation (2026-08-20).
_STIMULI = (
    "Two things, in this one turn, using your tools:\n"
    "1. Search your available tools for one that reduces IXPE event files to a PHA spectrum. "
    "Most of your tools are not loaded yet, so you must use the tool-search facility.\n"
    "2. In the SAME turn, also call list_dir on {root}.\n"
    "Issue BOTH calls together in one turn. Do not run any toolkit tool.",

    "Do all of these in a single turn, issuing every call at once without waiting for results:\n"
    "- search your tool catalogue for an IXPE PHA reduction tool\n"
    "- search it again for a polarization-cube tool\n"
    "- list_dir on {root}\n"
    "- read_file on {root}/README.md (first 20 lines)\n"
    "Emit all four calls in the same assistant turn.",

    "You have deferred tools you cannot see yet. In ONE turn: start a tool search for "
    "'polarization cube' AND simultaneously call list_dir on {root}. Do not wait for the search "
    "to come back before making the list_dir call — issue them together.",
)


def _attempt(tmp_path, stimulus: str):
    """One paid draw. Returns (handle, control, queued, steer_key)."""
    from auto_research.models import ModelMessage
    from auto_research.run_control import RunControl

    worker, handle, toolkit_root = _worker(tmp_path)
    control = RunControl()
    worker.run_control = control
    worker.agent_key = "w"

    queued = {"done": False}
    inner = handle.call

    async def call_then_steer(messages, **kwargs):
        response = await inner(messages, **kwargs)
        # Queue the steer the instant a pending-server-tool turn arrives — the exact race.
        if response.has_unresolved_server_tool and not queued["done"]:
            control.add_steer("w", "Check the DU3 exposure before you go further.")
            queued["done"] = True
        return response

    handle.call = call_then_steer
    seed = [
        ModelMessage(role="system", content=worker.prompt),
        ModelMessage(role="user", content=stimulus.format(root=toolkit_root)),
    ]
    asyncio.run(
        worker._react(seed_messages=seed, tools=worker.tools, action="race_probe", cap=6)
    )
    return handle, queued["done"]


def test_an_injected_turn_never_follows_an_unresolved_server_tool(tmp_path):
    """The regression, end to end.

    A steer is queued the moment a turn carrying an unresolved server-tool call arrives; the
    harness must hold it until that call resolves. Asserted: no user turn is ever appended after
    the tool results of such a turn, the vendor accepted every request we sent, and the steer
    arrives exactly once. Whether the model *acts* on it is quality — recorded, not asserted.
    """
    from auto_research.models import ModelMessage, ModelResponse, ToolResultMessage

    handles: list = []
    queued = False
    for index, stimulus in enumerate(_STIMULI, 1):
        handle, queued = _attempt(tmp_path, stimulus)
        handles.append(handle)
        pending = sum(1 for r in handle.responses if r.has_unresolved_server_tool)
        _record(f"attempt_{index}", handle, {
            "stimulus": stimulus[:200],
            "pending_turns": pending,
            "queued_steer": queued,
        })
        if pending:
            break

    # EVERY request from EVERY attempt is checked, whether or not the precondition fired: the
    # invariant is "no user turn after an unresolved turn's results", and a run that never went
    # pending still must not violate it.
    checked_pending = 0
    for handle in handles:
        assert handle.sent, "no request was made"
        for sent in handle.sent:
            for index, message in enumerate(sent):
                if not isinstance(message, ModelResponse):
                    continue
                if not message.has_unresolved_server_tool:
                    continue
                checked_pending += 1
                # The window is this turn's ANSWER — everything up to the next assistant turn.
                # Only tool results may appear in it. Beyond that boundary the vendor has
                # resolved the call (it returns the result as the first block of the following
                # response, which is its documented two-response flow), so a user turn there is
                # perfectly legal and is where a held steer is supposed to land.
                window = []
                for following in sent[index + 1:]:
                    if isinstance(following, ModelResponse):
                        break
                    window.append(following)
                offenders = [m for m in window if isinstance(m, ModelMessage)]
                assert not offenders, (
                    "a user turn was appended inside the answer to a turn holding an unresolved "
                    "server-tool call — the exact shape that 400'd and killed run 8c75990e at "
                    f"$42.11 with four tools already committed. Offending content: "
                    f"{[m.content[:80] for m in offenders]}"
                )

    last = handles[-1]
    if queued:
        # The positive half: the steer was queued while a pending turn was in flight, so it must
        # NOT be in the request that answered that turn, and must still arrive later.
        pending_request = next(
            (i for i, sent in enumerate(last.sent)
             if any(isinstance(m, ModelResponse) and m.has_unresolved_server_tool for m in sent)),
            None,
        )
        if pending_request is not None:
            assert _position_of(last.sent[pending_request], STEER) is None, (
                "the steer rode the very request that answered the pending turn"
            )
        # Held is not the same as dropped, and only the live path can show both.
        carrying = [
            i for i, sent in enumerate(last.sent) if _position_of(sent, STEER) is not None
        ]
        assert carrying, "a steer was queued and never delivered — held is not dropped"
        assert sum(
            1 for m in last.sent[-1]
            if isinstance(m, ModelMessage) and STEER in (m.content or "")
        ) == 1, "the steer was delivered more than once"

    if not checked_pending:
        pytest.skip(
            f"no unresolved server-tool turn occurred across {len(handles)} attempt(s). The "
            "vendor resolved every search in-turn, which is its documented hosted behaviour; the "
            "unresolved case needs a client tool call issued alongside one. The invariant above "
            "held on every request regardless. See the recorded turns."
        )
