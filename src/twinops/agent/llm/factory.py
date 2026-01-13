"""Factory for creating LLM clients."""

from typing import Literal

from twinops.agent.llm.base import (
    LlmCircuitBreaker,
    LlmClient,
    ResilientLlmClient,
)
from twinops.agent.llm.openai_compat import AnthropicClient, OpenAIClient
from twinops.agent.llm.rules import RulesBasedClient
from twinops.common.logging import get_logger
from twinops.common.settings import Settings

logger = get_logger(__name__)


def create_llm_client(
    settings: Settings | None = None,
    provider: Literal["anthropic", "openai", "rules"] | None = None,
) -> LlmClient:
    """
    Create an LLM client based on configuration.

    Creates a resilient client with circuit breaker protection and optional
    fallback to rules-based client when the primary LLM is unavailable.

    Args:
        settings: Application settings
        provider: Override provider selection

    Returns:
        Configured LLM client (wrapped with circuit breaker if using API)
    """
    if settings is None:
        from twinops.common.settings import get_settings

        settings = get_settings()

    provider = provider or settings.llm_provider

    # Rules-based client doesn't need circuit breaker (local, always available)
    if provider == "rules":
        logger.info("Using rules-based LLM client (no API)")
        return RulesBasedClient()

    # Create primary LLM client
    primary_client: LlmClient
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("TWINOPS_ANTHROPIC_API_KEY not set")
        primary_client = AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_request_timeout,
        )
        logger.info("Created Anthropic LLM client", model=settings.llm_model)

    elif provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("TWINOPS_OPENAI_API_KEY not set")
        primary_client = OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_request_timeout,
        )
        logger.info("Created OpenAI LLM client", model=settings.llm_model)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")

    # Create circuit breaker
    circuit_breaker = LlmCircuitBreaker(
        failure_threshold=settings.llm_circuit_failure_threshold,
        recovery_timeout=settings.llm_circuit_recovery_timeout,
    )

    # Create fallback client if enabled
    fallback_client: LlmClient | None = None
    if settings.llm_fallback_enabled:
        fallback_client = RulesBasedClient()
        logger.info(
            "LLM fallback enabled - will use rules-based client if circuit opens",
            failure_threshold=settings.llm_circuit_failure_threshold,
            recovery_timeout=settings.llm_circuit_recovery_timeout,
        )

    # Wrap with resilient client
    return ResilientLlmClient(
        primary=primary_client,
        fallback=fallback_client,
        circuit_breaker=circuit_breaker,
    )
