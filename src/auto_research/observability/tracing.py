"""LangSmith tracing wrapper for provider calls."""

from collections.abc import Awaitable, Callable
import os

from auto_research.models.types import ModelResponse, RoleModelConfig
from auto_research.observability.costs import append_cost_entry, cost_entry_for


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
            },
        )
        async def _call() -> ModelResponse:
            return await provider_call()

        response = await _call()
    else:
        response = await provider_call()
    append_cost_entry(cost_entry_for(role, config, response.usage))
    return response
