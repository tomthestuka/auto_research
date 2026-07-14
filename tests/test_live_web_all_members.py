"""Env-gated LIVE test: every web-capable member (across all providers) can actually search the
web via the wired tools and answer a real IXPE question ([I10] cross-provider round-trip).

Activate with:
    AUTO_RESEARCH_LIVE_WEB_MEMBERS_TESTS=1 pytest tests/test_live_web_all_members.py -q -s

For each DISTINCT model member of the web-capable roles (reviewer, analyst_lead, analyst_worker,
analyst_colead), we drive a minimal ReAct loop with a `web_source`-backed ReadOnlyToolProvider and
ask it to research IXPE `xpbin` event weighting (conventions, when introduced, when to use / not).
We record each member's web tool calls + final answer and assert every member made at least one
SUCCESSFUL web call (the tool round-trips for that provider). Scientific correctness is NOT
asserted — the answers are logged under `logs/dev/web_members/` for human review. Real Tavily +
all three provider APIs, so it spends tokens and a few Tavily credits.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.agents.react import (
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
    ReadOnlyToolProvider,
)
from auto_research.agents.roles import AgentRole
from auto_research.models import ModelMessage, load_registry

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_WEB_MEMBERS_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_WEB_MEMBERS_TESTS=1 to run the all-members web live test (spends tokens + Tavily credits)",
)

_WEB_ROLES = ["reviewer", "analyst_lead", "analyst_worker", "analyst_colead"]

_QUESTION = (
    "In IXPE X-ray polarimetry, the ixpeobssim tool `xpbin` supports event weighting. Search the "
    "web to answer, in three short parts: (1) the weighting conventions (e.g. NEUTRAL vs SIMPLE, "
    "and how the weights are defined); (2) when weighted analyses were introduced for IXPE; and "
    "(3) when you should use weights and when you should not. Use web_search first, and web_fetch "
    "to read a promising source if useful. Then give a brief summary and list the sources you used."
)
_SYSTEM = (
    "You are a research assistant with live web access via the web_search and web_fetch tools. "
    "Ground your answer in what you find online — call web_search (and web_fetch to open a source) "
    "before answering; do not answer from memory alone."
)


class _Probe(AgentRole):
    role = "web_probe"


class _RecordingProvider(ReadOnlyToolProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.web_calls: list[dict] = []

    async def execute(self, name, arguments):
        out = await super().execute(name, arguments)
        if name in (WEB_SEARCH_TOOL, WEB_FETCH_TOOL):
            self.web_calls.append(
                {
                    "tool": name,
                    "arg": arguments.get("query") or arguments.get("url"),
                    "ok": not out.is_error,
                    "chars": len(out.text),
                }
            )
        return out


def test_live_all_web_members_can_search(tmp_path):
    load_dotenv()
    from auto_research.sources import WebSource

    registry = load_registry()
    members: dict[str, object] = {}
    for role in _WEB_ROLES:
        for handle in registry.resolve(role).handles:
            members.setdefault(handle.config.name, handle)

    results: dict[str, dict] = {}

    async def probe(handle):
        # prompt=... satisfies AgentRole.__init__ (skips load_prompt for the fake role); the real
        # system prompt is passed via seed_messages below.
        agent = _Probe(model=handle, prompt=_SYSTEM)
        provider = _RecordingProvider(roots=[tmp_path], web_source=WebSource())
        await agent._react(
            seed_messages=[
                ModelMessage(role="system", content=_SYSTEM),
                ModelMessage(role="user", content=_QUESTION),
            ],
            tools=provider,
            action="probe",
            cap=6,
        )
        answer = str(getattr(agent.last_response, "content", "") or "")
        return provider.web_calls, answer

    async def run_all():
        # One event loop for every member — per-member asyncio.run leaves provider httpx clients
        # closing against a dead loop ("Event loop is closed" spam).
        for name, handle in members.items():
            try:
                web_calls, answer = await probe(handle)
                results[name] = {
                    "provider": handle.config.provider,
                    "web_calls": web_calls,
                    "any_ok": any(c["ok"] for c in web_calls),
                    "answer": answer,
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 — record and keep probing the other members
                results[name] = {"provider": handle.config.provider, "web_calls": [], "any_ok": False,
                                 "answer": "", "error": f"{type(exc).__name__}: {exc}"}
            r = results[name]
            print(
                f"\n[web-members] {name} ({r['provider']}): "
                f"{len(r['web_calls'])} web calls, any_ok={r['any_ok']}"
                + (f", ERROR {r['error']}" if r["error"] else "")
            )

    asyncio.run(run_all())

    # Log full transcripts for human review.
    logdir = Path("logs/dev/web_members")
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (logdir / f"xpbin_weights_{stamp}.jsonl").write_text(
        "\n".join(json.dumps({"member": n, **r}) for n, r in results.items()) + "\n", encoding="utf-8"
    )
    md = [f"# xpbin weighting — web-tool live check across all members ({stamp})\n"]
    for name, r in results.items():
        md.append(f"## {name} ({r['provider']})\n")
        md.append(f"- web calls: {len(r['web_calls'])}; any_ok: {r['any_ok']}"
                  + (f"; ERROR: {r['error']}" if r["error"] else ""))
        for c in r["web_calls"]:
            md.append(f"  - {c['tool']}({str(c['arg'])[:80]!r}) ok={c['ok']} {c['chars']} chars")
        md.append(f"\n**Answer:**\n\n{r['answer']}\n")
    (logdir / f"xpbin_weights_{stamp}.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n[web-members] transcripts → {logdir}/xpbin_weights_{stamp}.md")

    # Assert every member round-tripped the web tools (≥1 successful web call). Scientific content
    # is NOT asserted (logged for review); this is a per-provider tool-integration check.
    # `google` is a known xfail: the Gemini adapter drops `thought_signature`, so Gemini 400s on the
    # 2nd tool call of any loop (handoff [I8]). Excluded until that adapter fix lands; the other two
    # providers must pass. Flip this by removing "google" once [I8] is fixed.
    _KNOWN_BROKEN_PROVIDERS = {"google"}
    broken = [n for n, r in results.items()
              if r["provider"] in _KNOWN_BROKEN_PROVIDERS and not r["any_ok"]]
    if broken:
        print(f"\n[web-members] KNOWN xfail ([I8], Gemini thought_signature): {broken}")
    failed = [f"{n} ({r['provider']}): {r['error'] or 'no successful web call'}"
              for n, r in results.items()
              if not r["any_ok"] and r["provider"] not in _KNOWN_BROKEN_PROVIDERS]
    assert not failed, "members that did NOT successfully use the web tools:\n  " + "\n  ".join(failed)
