"""Opt-in live checks for configured model strings.

Run with:
    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_models.py
"""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.models import ModelMessage, load_registry


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)


def _unique_handles():
    seen = set()
    registry = load_registry()
    for role in sorted(registry.config.roles):
        group = registry.resolve(role)
        for handle in group.handles:
            key = (
                handle.config.provider,
                handle.config.model,
                json.dumps(handle.config.parameters, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            yield handle


def test_configured_models_accept_minimal_call():
    load_dotenv()
    prompt = [
        ModelMessage(
            role="user",
            content="Reply with only the exact model string you are running as.",
        )
    ]

    async def run_all():
        results = []
        failures = []
        for handle in _unique_handles():
            print(f"checking {handle.config.provider}:{handle.config.model}", flush=True)
            try:
                response = await asyncio.wait_for(handle.call(prompt), timeout=45)
            except Exception as exc:
                failures.append((handle.config.provider, handle.config.model, repr(exc)))
                continue
            served_model = getattr(response.raw, "model", None)
            results.append((handle.config.provider, handle.config.model, response.content))
            print(
                f"ok {handle.config.provider}:{handle.config.model}"
                f" raw.model={served_model!r} text={response.content!r}",
                flush=True,
            )
        return results, failures

    results, failures = asyncio.run(run_all())

    assert results
    for provider, model, content in results:
        assert content.strip(), f"{provider}:{model} returned an empty response"
    assert not failures, "\n".join(f"{provider}:{model} failed with {error}" for provider, model, error in failures)
