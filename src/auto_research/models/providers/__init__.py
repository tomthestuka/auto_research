"""Provider adapter factory."""

from auto_research.models.providers._harness_only import HarnessOnlyProvider
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.google import GoogleProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.types import CHAT_PROVIDERS, ModelProvider, ProviderName


def provider_for(name: ProviderName) -> ModelProvider:
    """Return the adapter for a configured provider name.

    A vendor outside ``CHAT_PROVIDERS`` is reached only through a coder harness, so it gets a
    :class:`HarnessOnlyProvider`: the role still resolves (for stamps, decorrelation and the
    launch checks) and a chat call against it raises with the reason.
    """

    providers: dict[str, ModelProvider] = {
        "anthropic": AnthropicProvider(),
        "google": GoogleProvider(),
        "openai": OpenAIProvider(),
    }
    if name in CHAT_PROVIDERS:
        return providers[name]
    return HarnessOnlyProvider(name)
