"""Base classes for LLM integration."""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from twinops.common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Message:
    """Chat message."""

    role: Literal["user", "assistant", "system"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ToolCall:
    """LLM tool call request."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmResponse:
    """Response from LLM."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


class LlmClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """
        Send a chat completion request.

        Args:
            messages: Conversation history
            tools: Available tools in LLM format
            system: System prompt

        Returns:
            LlmResponse with content and/or tool calls
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        pass


# === Circuit Breaker for LLM Resilience ===


class LlmCircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


class LlmCircuitBreakerOpen(Exception):
    """Exception raised when LLM circuit breaker is open."""

    def __init__(self, message: str = "LLM circuit breaker is open"):
        super().__init__(message)


class LlmCircuitBreaker:
    """Circuit breaker pattern for LLM API resilience."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 2,
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before trying half-open
            half_open_max_calls: Successful calls needed to close circuit
        """
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = LlmCircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0

    @property
    def state(self) -> LlmCircuitState:
        """Get current circuit state, transitioning if needed."""
        if (
            self._state == LlmCircuitState.OPEN
            and time.time() - self._last_failure_time > self._recovery_timeout
        ):
            logger.info("LLM circuit breaker transitioning to half-open")
            self._state = LlmCircuitState.HALF_OPEN
            self._half_open_calls = 0
        return self._state

    @property
    def stats(self) -> dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time,
        }

    def record_success(self) -> None:
        """Record a successful operation."""
        self._success_count += 1
        if self._state == LlmCircuitState.HALF_OPEN:
            self._half_open_calls += 1
            if self._half_open_calls >= self._half_open_max_calls:
                logger.info(
                    "LLM circuit breaker closing after successful recovery",
                    successful_calls=self._half_open_calls,
                )
                self._state = LlmCircuitState.CLOSED
                self._failure_count = 0
        elif self._state == LlmCircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == LlmCircuitState.HALF_OPEN:
            logger.warning("LLM circuit breaker reopening after failure in half-open state")
            self._state = LlmCircuitState.OPEN
        elif self._failure_count >= self._failure_threshold:
            logger.warning(
                "LLM circuit breaker opening",
                failure_count=self._failure_count,
                threshold=self._failure_threshold,
            )
            self._state = LlmCircuitState.OPEN

    def is_open(self) -> bool:
        """Check if circuit is open."""
        return self.state == LlmCircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if an operation can be executed."""
        state = self.state
        if state == LlmCircuitState.CLOSED:
            return True
        if state == LlmCircuitState.HALF_OPEN:
            return self._half_open_calls < self._half_open_max_calls
        return False


class ResilientLlmClient(LlmClient):
    """
    Wrapper that adds circuit breaker and fallback to any LlmClient.

    When the primary LLM fails repeatedly, automatically falls back
    to a secondary client (typically rules-based).
    """

    def __init__(
        self,
        primary: LlmClient,
        fallback: LlmClient | None = None,
        circuit_breaker: LlmCircuitBreaker | None = None,
    ):
        """
        Initialize resilient client.

        Args:
            primary: Primary LLM client to use
            fallback: Fallback client when circuit is open
            circuit_breaker: Circuit breaker instance
        """
        self._primary = primary
        self._fallback = fallback
        self._circuit_breaker = circuit_breaker or LlmCircuitBreaker()
        self._using_fallback = False

    @property
    def circuit_breaker(self) -> LlmCircuitBreaker:
        """Get the circuit breaker instance."""
        return self._circuit_breaker

    @property
    def is_using_fallback(self) -> bool:
        """Check if currently using fallback client."""
        return self._using_fallback

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """
        Send chat request with circuit breaker protection.

        If circuit is open and fallback is available, uses fallback.
        """
        # Check if we should use fallback
        if self._circuit_breaker.is_open():
            if self._fallback:
                if not self._using_fallback:
                    logger.warning(
                        "LLM circuit open, switching to fallback client",
                        circuit_state=self._circuit_breaker.state.value,
                    )
                    self._using_fallback = True
                return await self._fallback.chat(messages, tools, system)
            else:
                raise LlmCircuitBreakerOpen(
                    f"LLM circuit breaker is open, retry after "
                    f"{self._circuit_breaker._recovery_timeout}s"
                )

        # Try primary client
        try:
            response = await self._primary.chat(messages, tools, system)
            self._circuit_breaker.record_success()

            # If we were using fallback, switch back
            if self._using_fallback:
                logger.info("LLM circuit recovered, switching back to primary client")
                self._using_fallback = False

            return response

        except Exception as e:
            logger.warning(
                "LLM API call failed",
                error=str(e),
                failure_count=self._circuit_breaker._failure_count + 1,
            )
            self._circuit_breaker.record_failure()

            # If fallback available and circuit just opened, use it
            if self._fallback and self._circuit_breaker.is_open():
                logger.warning("LLM circuit opened, using fallback client")
                self._using_fallback = True
                return await self._fallback.chat(messages, tools, system)

            raise

    async def close(self) -> None:
        """Close both primary and fallback clients."""
        await self._primary.close()
        if self._fallback:
            await self._fallback.close()
