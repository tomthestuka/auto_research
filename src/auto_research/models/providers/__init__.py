"""Provider adapter factory."""

from auto_research.models.providers._harness_only import HarnessOnlyProvider
from auto_research.models.providers._responses import ResponsesVendor
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.dashscope import DashscopeProvider
from auto_research.models.providers.deepseek import DeepSeekProvider
from auto_research.models.providers.google import GoogleProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.providers.zai import ZaiProvider
from auto_research.models.providers._media import MediaLimits
from auto_research.models.types import CHAT_PROVIDERS, ModelProvider, ProviderName, RoleModelConfig


#: Every adapter, by the provider name a config member declares. The four vendors speaking the
#: OpenAI Responses dialect share one implementation and differ only in their descriptors
#: (:mod:`auto_research.models.providers._responses`).
_ADAPTERS: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "zai": ZaiProvider,
    "dashscope": DashscopeProvider,
}


def provider_for(name: ProviderName) -> ModelProvider:
    """Return the adapter for a configured provider name.

    A vendor with no chat adapter is reached only through a coder harness, so it gets a
    :class:`HarnessOnlyProvider`: the role still resolves (for stamps, decorrelation and the
    launch checks) and a chat call against it raises with the reason.
    """

    if name in CHAT_PROVIDERS:
        return _ADAPTERS[name]()
    return HarnessOnlyProvider(name)


def media_limits(config: RoleModelConfig) -> MediaLimits | None:
    """The documented media ceilings a member's requests meet (``_media.MediaLimits``), read from
    its adapter without building a client; ``None`` for a provider with no chat adapter."""
    from auto_research.models.providers import google
    from auto_research.models.providers.anthropic import _limits_for

    if config.provider == "anthropic":
        return _limits_for(config)
    if config.provider == "google":
        return google._LIMITS
    vendor = getattr(_ADAPTERS.get(config.provider), "vendor", None)
    return vendor.limits_for(config.model) if isinstance(vendor, ResponsesVendor) else None


def responses_vendors() -> tuple[ResponsesVendor, ...]:
    """Descriptors of every vendor served over the OpenAI Responses dialect.

    Exposed so a check can hold a property across all of them at once rather than per adapter:
    the set grows, and a guarantee asserted against one module does not travel to the next.
    """

    return tuple(
        adapter.vendor
        for adapter in _ADAPTERS.values()
        if isinstance(getattr(adapter, "vendor", None), ResponsesVendor)
    )
