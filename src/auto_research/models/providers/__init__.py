"""Provider adapter factory."""

from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.google import GoogleProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.types import ModelProvider, ProviderName


def provider_for(name: ProviderName) -> ModelProvider:
    """Return the adapter for a configured provider name."""

    providers: dict[ProviderName, ModelProvider] = {
        "anthropic": AnthropicProvider(),
        "google": GoogleProvider(),
        "openai": OpenAIProvider(),
    }
    return providers[name]
