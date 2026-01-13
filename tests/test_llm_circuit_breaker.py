"""Tests for LLM circuit breaker functionality."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from twinops.agent.llm.base import (
    LlmCircuitBreaker,
    LlmCircuitBreakerOpen,
    LlmCircuitState,
    LlmResponse,
    Message,
    ResilientLlmClient,
)
from twinops.agent.llm.rules import RulesBasedClient


class TestLlmCircuitBreaker:
    """Tests for LLM circuit breaker state machine."""

    def test_initial_state_closed(self):
        """Circuit breaker starts in closed state."""
        cb = LlmCircuitBreaker()
        assert cb.state == LlmCircuitState.CLOSED
        assert cb.can_execute() is True

    def test_stays_closed_on_success(self):
        """Circuit stays closed on successful operations."""
        cb = LlmCircuitBreaker(failure_threshold=3)

        for _ in range(10):
            cb.record_success()

        assert cb.state == LlmCircuitState.CLOSED

    def test_opens_after_failure_threshold(self):
        """Circuit opens after reaching failure threshold."""
        cb = LlmCircuitBreaker(failure_threshold=3)

        cb.record_failure()
        assert cb.state == LlmCircuitState.CLOSED

        cb.record_failure()
        assert cb.state == LlmCircuitState.CLOSED

        cb.record_failure()
        assert cb.state == LlmCircuitState.OPEN
        assert cb.can_execute() is False

    def test_transitions_to_half_open(self):
        """Circuit transitions to half-open after recovery timeout."""
        cb = LlmCircuitBreaker(failure_threshold=2, recovery_timeout=0.1)

        # Open the circuit
        cb.record_failure()
        cb.record_failure()
        assert cb.state == LlmCircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(0.15)

        # Should transition to half-open
        assert cb.state == LlmCircuitState.HALF_OPEN
        assert cb.can_execute() is True

    def test_closes_from_half_open_on_success(self):
        """Circuit closes from half-open after successful calls."""
        cb = LlmCircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.1,
            half_open_max_calls=2,
        )

        # Open and transition to half-open
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == LlmCircuitState.HALF_OPEN

        # Record successes
        cb.record_success()
        assert cb.state == LlmCircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == LlmCircuitState.CLOSED

    def test_reopens_from_half_open_on_failure(self):
        """Circuit reopens from half-open on failure."""
        cb = LlmCircuitBreaker(failure_threshold=2, recovery_timeout=0.1)

        # Open and transition to half-open
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == LlmCircuitState.HALF_OPEN

        # Failure in half-open
        cb.record_failure()
        assert cb.state == LlmCircuitState.OPEN

    def test_success_resets_failure_count(self):
        """Success resets failure count in closed state."""
        cb = LlmCircuitBreaker(failure_threshold=3)

        cb.record_failure()
        cb.record_failure()
        assert cb._failure_count == 2

        cb.record_success()
        assert cb._failure_count == 0

    def test_stats_property(self):
        """Stats property returns current state."""
        cb = LlmCircuitBreaker()

        cb.record_success()
        cb.record_failure()

        stats = cb.stats
        assert stats["state"] == "closed"
        assert stats["success_count"] == 1
        assert stats["failure_count"] == 1

    def test_is_open_property(self):
        """is_open returns correct value."""
        cb = LlmCircuitBreaker(failure_threshold=1)

        assert cb.is_open() is False

        cb.record_failure()
        assert cb.is_open() is True


class TestResilientLlmClient:
    """Tests for ResilientLlmClient with circuit breaker."""

    @pytest.fixture
    def mock_primary(self):
        """Create mock primary LLM client."""
        client = AsyncMock()
        client.chat = AsyncMock(
            return_value=LlmResponse(content="Primary response", finish_reason="stop")
        )
        client.close = AsyncMock()
        return client

    @pytest.fixture
    def mock_fallback(self):
        """Create mock fallback LLM client."""
        client = AsyncMock()
        client.chat = AsyncMock(
            return_value=LlmResponse(content="Fallback response", finish_reason="stop")
        )
        client.close = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_uses_primary_when_healthy(self, mock_primary, mock_fallback):
        """Uses primary client when circuit is closed."""
        cb = LlmCircuitBreaker(failure_threshold=3)
        client = ResilientLlmClient(
            primary=mock_primary, fallback=mock_fallback, circuit_breaker=cb
        )

        messages = [Message(role="user", content="Hello")]
        response = await client.chat(messages)

        assert response.content == "Primary response"
        mock_primary.chat.assert_called_once()
        mock_fallback.chat.assert_not_called()
        assert client.is_using_fallback is False

    @pytest.mark.asyncio
    async def test_switches_to_fallback_when_circuit_opens(
        self, mock_primary, mock_fallback
    ):
        """Switches to fallback when circuit breaker opens."""
        cb = LlmCircuitBreaker(failure_threshold=2)
        client = ResilientLlmClient(
            primary=mock_primary, fallback=mock_fallback, circuit_breaker=cb
        )

        # Make primary fail
        mock_primary.chat.side_effect = Exception("API Error")

        messages = [Message(role="user", content="Hello")]

        # First failure - circuit stays closed, exception raised
        with pytest.raises(Exception):
            await client.chat(messages)
        assert cb.state == LlmCircuitState.CLOSED
        assert cb._failure_count == 1

        # Second failure opens circuit and immediately falls back
        response = await client.chat(messages)
        assert cb.state == LlmCircuitState.OPEN
        assert response.content == "Fallback response"
        assert client.is_using_fallback is True

        # Subsequent calls should use fallback
        response = await client.chat(messages)
        assert response.content == "Fallback response"
        assert client.is_using_fallback is True

    @pytest.mark.asyncio
    async def test_raises_when_circuit_open_no_fallback(self, mock_primary):
        """Raises exception when circuit is open and no fallback."""
        cb = LlmCircuitBreaker(failure_threshold=1)
        client = ResilientLlmClient(
            primary=mock_primary, fallback=None, circuit_breaker=cb
        )

        # Open the circuit
        mock_primary.chat.side_effect = Exception("API Error")
        messages = [Message(role="user", content="Hello")]

        with pytest.raises(Exception):
            await client.chat(messages)

        # Next call should raise LlmCircuitBreakerOpen
        with pytest.raises(LlmCircuitBreakerOpen):
            await client.chat(messages)

    @pytest.mark.asyncio
    async def test_recovers_from_fallback(self, mock_primary, mock_fallback):
        """Returns to primary when circuit recovers."""
        cb = LlmCircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        client = ResilientLlmClient(
            primary=mock_primary, fallback=mock_fallback, circuit_breaker=cb
        )

        messages = [Message(role="user", content="Hello")]

        # Fail primary to open circuit - falls back immediately when circuit opens
        mock_primary.chat.side_effect = Exception("API Error")
        response = await client.chat(messages)
        assert cb.state == LlmCircuitState.OPEN
        assert response.content == "Fallback response"
        assert client.is_using_fallback is True

        # Subsequent calls use fallback
        response = await client.chat(messages)
        assert response.content == "Fallback response"

        # Wait for recovery timeout and fix primary
        time.sleep(0.15)
        mock_primary.chat.side_effect = None
        mock_primary.chat.return_value = LlmResponse(
            content="Primary recovered", finish_reason="stop"
        )

        # Should try primary again (half-open) and recover
        response = await client.chat(messages)
        assert response.content == "Primary recovered"
        assert client.is_using_fallback is False

    @pytest.mark.asyncio
    async def test_records_success_on_primary(self, mock_primary, mock_fallback):
        """Records success when primary succeeds."""
        cb = LlmCircuitBreaker(failure_threshold=3)
        client = ResilientLlmClient(
            primary=mock_primary, fallback=mock_fallback, circuit_breaker=cb
        )

        messages = [Message(role="user", content="Hello")]
        await client.chat(messages)

        assert cb.stats["success_count"] == 1
        assert cb.stats["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_property(self, mock_primary):
        """Circuit breaker is accessible via property."""
        cb = LlmCircuitBreaker()
        client = ResilientLlmClient(primary=mock_primary, circuit_breaker=cb)

        assert client.circuit_breaker is cb

    @pytest.mark.asyncio
    async def test_close_both_clients(self, mock_primary, mock_fallback):
        """Close method closes both clients."""
        client = ResilientLlmClient(primary=mock_primary, fallback=mock_fallback)

        await client.close()

        mock_primary.close.assert_called_once()
        mock_fallback.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_primary_only(self, mock_primary):
        """Close works with no fallback."""
        client = ResilientLlmClient(primary=mock_primary, fallback=None)

        await client.close()

        mock_primary.close.assert_called_once()


class TestRulesBasedClientAsFallback:
    """Tests verifying RulesBasedClient works as a fallback."""

    @pytest.mark.asyncio
    async def test_rules_client_handles_set_speed(self):
        """Rules-based client can handle set speed command."""
        client = RulesBasedClient()

        messages = [Message(role="user", content="Set speed to 1500")]
        tools = [{"name": "SetSpeed", "description": "Set pump speed"}]

        response = await client.chat(messages, tools)

        assert response.tool_calls
        assert response.tool_calls[0].name == "SetSpeed"
        assert response.tool_calls[0].arguments.get("RPM") == 1500.0

    @pytest.mark.asyncio
    async def test_rules_client_handles_start_pump(self):
        """Rules-based client can handle start pump command."""
        client = RulesBasedClient()

        messages = [Message(role="user", content="Start the pump")]
        tools = [{"name": "StartPump", "description": "Start the pump"}]

        response = await client.chat(messages, tools)

        assert response.tool_calls
        assert response.tool_calls[0].name == "StartPump"

    @pytest.mark.asyncio
    async def test_rules_client_returns_help_on_unknown(self):
        """Rules-based client returns help message for unknown commands."""
        client = RulesBasedClient()

        messages = [Message(role="user", content="do something random")]
        tools = [{"name": "SetSpeed", "description": "Set pump speed"}]

        response = await client.chat(messages, tools)

        assert response.content is not None
        assert "couldn't understand" in response.content.lower()
        assert not response.tool_calls
