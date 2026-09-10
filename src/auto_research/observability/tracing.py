"""LangSmith tracing wrapper for provider calls.

Every model call funnels through here, so this is where per-call cost is recorded — into the run's
:class:`~auto_research.observability.costs.CostMeter`, which accumulates the run total and writes
one ``cost.call`` event into the monitor JSONL (the run's single source of truth), and — when
tracing is on — onto the LangSmith run as ``usage_metadata`` + cost metadata so the LangSmith
dashboard shows spend without a provider admin panel ([I5]).
"""

from collections.abc import Awaitable, Callable
import os

from auto_research.models.types import ModelResponse, RoleModelConfig
from auto_research.observability.costs import cost_for_call, current_cost_meter


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
        entry = cost_for_call(role, config, usage)
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
    """Run a provider call under LangSmith tracing and record its cost on the run's meter.

    With no meter installed (a bare probe, an isolated unit/live test), the call runs unchanged and
    is simply not accumulated — there is no cost log outside a run's monitor ledger.
    """

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

        runner = _call
    else:
        runner = provider_call

    try:
        response = await runner()
    except BaseException as exc:
        # An adapter that raises AFTER a billable response still costs money (N6). The vendor
        # generated those tokens; only our own extraction rejected the turn. Usage rides on the
        # exception as `billed_usage` (see `_responses._raise_billed`), because the exception
        # *type* is the role layer's retry contract and must not change. Measured live
        # 2026-09-08: slot 5's two failing structured attempts (~195k input tokens) appear
        # neither in its ledger nor in its reported $29.23.
        #
        # A genuine provider refusal (a 4xx before generation) carries no `billed_usage` and is
        # correctly recorded as nothing — a request refused before any tokens is never billed.
        usage = getattr(exc, "billed_usage", None)
        if usage is not None:
            meter = current_cost_meter()
            if meter is not None:
                await meter.record_call(role, config, usage)
        raise

    meter = current_cost_meter()
    if meter is not None:
        await meter.record_call(role, config, response.usage)
    return response
