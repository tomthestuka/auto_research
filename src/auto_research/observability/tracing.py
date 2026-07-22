"""LangSmith tracing wrapper for provider calls.

Every model call funnels through here, so this is where per-call cost is recorded — to the local
``cost_ledger.jsonl`` (always) and, when tracing is on, onto the LangSmith run as ``usage_metadata``
+ cost metadata so the LangSmith dashboard shows spend without a provider admin panel ([I5]).
"""

from collections.abc import Awaitable, Callable
import os

from auto_research.models.types import ModelResponse, RoleModelConfig
from auto_research.observability.costs import append_cost_entry, cost_entry_for


def _annotate_langsmith_usage(role: str, config: RoleModelConfig, response: ModelResponse) -> None:
    """Attach token usage + cost to the current LangSmith run (best-effort, never fatal).

    Reports the LangSmith-standard ``usage_metadata`` (``input_tokens`` is the TOTAL prompt incl.
    cache; the cached/created subsets go in ``input_token_details`` so LangSmith's native cost can
    key off ``ls_model_name``/``ls_provider``), plus our own computed ``cost_usd`` so spend is
    visible/filterable even for models LangSmith's price map doesn't know.
    """
    try:
        from langsmith import get_current_run_tree

        run = get_current_run_tree()
        if run is None:
            return
        usage = response.usage
        entry = cost_entry_for(role, config, usage)
        total_input = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        run.add_metadata(
            {
                "usage_metadata": {
                    "input_tokens": total_input,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": total_input + usage.output_tokens,
                    "input_token_details": {
                        "cache_read": usage.cache_read_tokens,
                        "cache_creation": usage.cache_write_tokens,
                    },
                    "output_token_details": {"reasoning": usage.reasoning_tokens},
                },
                "cost_usd": entry.total_cost_usd,
                "cost_breakdown_usd": {
                    "input": entry.input_cost_usd,
                    "output": entry.output_cost_usd,
                    "cache_read": entry.cache_read_cost_usd,
                    "cache_write": entry.cache_write_cost_usd,
                },
                "price_source": entry.price_source,
            }
        )
    except Exception:  # noqa: BLE001 — observability must never break a real model call
        pass


async def traced_model_call(
    *,
    role: str,
    config: RoleModelConfig,
    provider_call: Callable[[], Awaitable[ModelResponse]],
) -> ModelResponse:
    """Run a provider call under LangSmith tracing and append a cost row."""

    if os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true":
        from langsmith import traceable

        @traceable(
            name="llm_call",
            run_type="llm",
            metadata={
                "role": role,
                "member": config.name,
                "provider": config.provider,
                "model": config.model,
                # LangSmith keys its native cost computation off these two.
                "ls_provider": config.provider,
                "ls_model_name": config.model,
            },
        )
        async def _call() -> ModelResponse:
            response = await provider_call()
            _annotate_langsmith_usage(role, config, response)
            return response

        response = await _call()
    else:
        response = await provider_call()
    append_cost_entry(cost_entry_for(role, config, response.usage))
    return response
