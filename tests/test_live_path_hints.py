"""Env-gated LIVE test: the coder's structure context actually conveys the absolute toolkit
root + the obsdb validation-data paths, so a real Claude Agent SDK author can ``ls`` them
cleanly on the first try — no wrong-root guessing, no ``find /`` hunting (todo #2).

Activate with:
    AUTO_RESEARCH_LIVE_PATH_HINT_TESTS=1 pytest tests/test_live_path_hints.py -q

What it proves: we build the **real** ``_build_structure_context`` (the exact block the runtime
injects into the author's first turn) and ask the coder to ``ls`` (a) its research_toolkit
working directory and (b) every mission observation directory — WITHOUT repeating any absolute
path in the task prompt. The coder must read the paths out of the structure context. We then
assert on the recorded tool stream (``ToolUseBlock`` commands + their ``ToolResultBlock``
outcomes), not the model's narration: each target was ``ls``'d at its absolute path and the
result was clean (no error), and the author never hunted (no ``find /``, no ``/root/`` guess).

Read-only by construction: the coder is told not to author/edit anything, runs against a
throwaway clone of ``toolkit_root`` (never the live checkout), and the obsdb is only listed.
Requires the Claude Agent SDK CLI + a resolvable coder model + the analyst env.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from auto_research.agents.prompts import load_prompt
from auto_research.graph.runtime import load_orchestration_config
from auto_research.sandbox.merge_flow import (
    _author_env,
    _build_structure_context,
    _observation_paths,
    _resolve_coder_model,
)

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_PATH_HINT_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_PATH_HINT_TESTS=1 to run the live path-hint test (spends tokens)",
)


def _scratch_clone(tmp_path) -> Path:
    root = Path(load_orchestration_config().get("toolkit_root", "")).expanduser()
    if not (root / ".git").exists():
        pytest.skip(f"toolkit_root {root} is not a git checkout")
    clone = tmp_path / "toolkit_clone"
    subprocess.run(["git", "clone", "--depth", "1", str(root), str(clone)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-q", "-B", "ai"], cwd=str(clone), check=True,
                   capture_output=True, text=True)
    return clone


def test_live_coder_knows_toolkit_and_obsdb_paths(tmp_path):
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    clone = _scratch_clone(tmp_path)
    obs = _observation_paths()
    if not obs:
        pytest.skip("no mission observation paths configured (config/observations.yaml)")

    # The exact structure context the runtime injects — built for the clone as the author's cwd.
    structure_context = _build_structure_context(clone)
    # The paths the coder must discover from that context (NOT restated in the task below).
    toolkit_abs = str(clone)
    obs_abs = sorted({str(p) for p in obs.values()})

    message = (
        "## Local structure context\n" + structure_context + "\n\n"
        "## Task (read-only locate check — author/edit nothing, run only the probes below)\n"
        "Confirm you can locate your workspace and validation data using ONLY the absolute "
        "paths given in the structure context above. Run these Bash probes IN ORDER, attempting "
        "each even if one errors, and use each absolute path VERBATIM from the context — do not "
        "search or guess:\n"
        "1. `ls -la` your research_toolkit working directory (your cwd / toolkit root).\n"
        "2. `ls -la` each mission observation directory where your validation ObsIDs live "
        f"({len(obs_abs)} of them).\n"
        "After the last probe, reply DONE. Author nothing."
    )

    options = ClaudeAgentOptions(
        cwd=str(clone),
        model=_resolve_coder_model(),
        setting_sources=[],
        permission_mode="acceptEdits",
        system_prompt=load_prompt("coder"),
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        max_turns=16,
        **({"env": _author_env(clone)} if _author_env(clone) else {}),
    )

    # Record the real tool stream: tool_use (id, name, input) + its paired result (is_error).
    uses: dict[str, tuple[str, dict]] = {}
    errored: dict[str, bool] = {}

    async def run():
        async with ClaudeSDKClient(options=options) as client:
            await client.query(message)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, ToolUseBlock):
                            uses[block.id] = (block.name, dict(block.input or {}))
                elif isinstance(msg, UserMessage):
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            errored[block.tool_use_id] = bool(block.is_error)

    asyncio.run(run())

    bash = [
        (str(inp.get("command", "")), errored.get(uid))
        for uid, (name, inp) in uses.items()
        if name == "Bash"
    ]
    print(f"\n[path-hint live] {len(bash)} Bash calls:")
    for cmd, err in bash:
        print(f"  {'ERR ' if err else 'ok  '} {cmd[:100]}")

    def cleanly_listed(target: str) -> bool:
        # An ls command naming the absolute target whose result was not an error.
        return any(target in cmd and "ls" in cmd and err is False for cmd, err in bash)

    # 1. Clean ls of the toolkit root at its absolute path.
    assert cleanly_listed(toolkit_abs), (
        f"coder did not cleanly `ls` the toolkit root {toolkit_abs!r}; bash={bash}"
    )
    # 2. Clean ls of every mission observation directory at its absolute path.
    missed = [p for p in obs_abs if not cleanly_listed(p)]
    assert not missed, f"coder did not cleanly `ls` obsdb path(s) {missed}; bash={bash}"
    # 3. No hunting: it used the given paths instead of searching for the checkout.
    hunted = [cmd for cmd, _ in bash if "find /" in cmd or "/root/" in cmd]
    assert not hunted, f"coder hunted for paths instead of using the hints: {hunted}"
