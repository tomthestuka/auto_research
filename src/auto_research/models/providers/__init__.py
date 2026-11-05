"""Provider adapter factory."""

from dataclasses import dataclass

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
    return vendor.limits if isinstance(vendor, ResponsesVendor) else None


@dataclass(frozen=True)
class MediaCapability:
    """What a member can be sent: its model's entry in the capability table (Z1d,
    ``models.capabilities``), sent over what its adapter's dialect carries.

    ``images``: the model takes image input. ``native_documents``: the model reads a PDF itself and
    the dialect carries an inline document; otherwise the model-input sanitiser sends the local
    parse (``models.sanitise``).
    """

    images: bool
    native_documents: bool


def media_capability(config: RoleModelConfig) -> MediaCapability:
    """The member's media capability: its model's capability-table entry, which the adapters trust
    (user ruling 2026-11-04). A model with no entry raises ``MissingModelCapability`` naming the
    table; the launch preflight refuses such a member before any call."""
    from auto_research.models.capabilities import capability_for

    entry = capability_for(config.provider, config.model)
    vendor = getattr(_ADAPTERS.get(config.provider), "vendor", None)
    # A wire fact, not a capability: a Responses vendor that takes no inline `file_data` (Alibaba:
    # `file_url` only) is sent the local parse even for a model that reads PDFs.
    inline = not (isinstance(vendor, ResponsesVendor) and not vendor.accepts_inline_documents)
    return MediaCapability(images=entry.images, native_documents=entry.documents and inline)


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
