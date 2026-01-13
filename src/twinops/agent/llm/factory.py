"""Factory for creating LLM clients."""

from typing import Literal

from twinops.agent.llm.base import LlmClient
from twinops.agent.llm.openai_compat import AnthropicClient, OpenAIClient
from twinops.agent.llm.rules import RulesBasedClient
from twinops.common.settings import Settings


def create_llm_client(
    settings: Settings | None = None,
    provider: Literal["anthropic", "openai", "rules"] | None = None,
) -> LlmClient:
    """
    Create an LLM client based on configuration.

    Args:
        settings: Application settings
        provider: Override provider selection

    Returns:
        Configured LLM client
    """
    if settings is None:
        from twinops.common.settings import get_settings
        settings = get_settings()

    provider = provider or settings.llm_provider

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("TWINOPS_ANTHROPIC_API_KEY not set")
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
        )

    elif provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("TWINOPS_OPENAI_API_KEY not set")
        return OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
        )

    else:
        return RulesBasedClient()
