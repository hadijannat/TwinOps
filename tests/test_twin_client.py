"""Tests for twin client with circuit breaker."""

import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest

from twinops.agent.twin_client import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
    TwinClient,
    TwinClientError,
)


class TestCircuitBreaker:
    """Tests for circuit breaker functionality."""

    def test_initial_state_closed(self):
        """Circuit breaker starts in closed state."""
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    def test_stays_closed_on_success(self):
        """Circuit stays closed on successful operations."""
        cb = CircuitBreaker(failure_threshold=3)

        for _ in range(10):
            cb.record_success()

        assert cb.state == CircuitState.CLOSED

    def test_opens_after_failure_threshold(self):
        """Circuit opens after reaching failure threshold."""
        cb = CircuitBreaker(failure_threshold=3)

        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False

    def test_transitions_to_half_open(self):
        """Circuit transitions to half-open after recovery timeout."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)

        # Open the circuit
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(0.15)

        # Should transition to half-open
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.can_execute() is True

    def test_closes_from_half_open_on_success(self):
        """Circuit closes from half-open after successful calls."""
        cb = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.1,
            half_open_max_calls=2,
        )

        # Open and transition to half-open
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        # Record successes
        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_reopens_from_half_open_on_failure(self):
        """Circuit reopens from half-open on failure."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)

        # Open and transition to half-open
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        # Failure in half-open
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_ensure_can_execute_raises_when_open(self):
        """ensure_can_execute raises exception when circuit is open."""
        cb = CircuitBreaker(failure_threshold=2)

        cb.record_failure()
        cb.record_failure()

        with pytest.raises(CircuitBreakerOpen) as exc_info:
            cb.ensure_can_execute()

        assert "open" in str(exc_info.value).lower()

    def test_success_resets_failure_count(self):
        """Success resets failure count in closed state."""
        cb = CircuitBreaker(failure_threshold=3)

        cb.record_failure()
        cb.record_failure()
        assert cb._failure_count == 2

        cb.record_success()
        assert cb._failure_count == 0

    def test_stats_property(self):
        """Stats property returns current state."""
        cb = CircuitBreaker()

        cb.record_success()
        cb.record_failure()

        stats = cb.stats
        assert stats["state"] == "closed"
        assert stats["success_count"] == 1
        assert stats["failure_count"] == 1


class TestTwinClientCircuitBreaker:
    """Tests for TwinClient with circuit breaker integration."""

    @pytest.fixture
    def twin_client(self, settings):
        """Create twin client for testing."""
        return TwinClient(settings)

    @pytest.mark.asyncio
    async def test_circuit_breaker_property(self, twin_client):
        """Client exposes circuit breaker."""
        assert twin_client.circuit_breaker is not None
        assert isinstance(twin_client.circuit_breaker, CircuitBreaker)

    @pytest.mark.asyncio
    async def test_circuit_opens_on_server_errors(self, settings):
        """Circuit opens after repeated 5xx errors."""
        cb = CircuitBreaker(failure_threshold=2)
        client = TwinClient(settings, circuit_breaker=cb)

        async with client:
            # Mock failed requests
            with patch.object(
                client, "_protected_request", new_callable=AsyncMock
            ) as mock_request:
                # Simulate 500 errors
                mock_response = AsyncMock()
                mock_response.status = 500
                mock_response.__aenter__ = AsyncMock(return_value=mock_response)
                mock_response.__aexit__ = AsyncMock(return_value=None)
                mock_response.text = AsyncMock(return_value="Server Error")
                mock_request.return_value = mock_response

                # These should record failures
                with contextlib.suppress(TwinClientError):
                    await client.get_all_aas()

                # After 2 failures, circuit should open
                assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_requests_rejected_when_circuit_open(self, settings):
        """Requests are rejected when circuit is open."""
        cb = CircuitBreaker(failure_threshold=1)
        client = TwinClient(settings, circuit_breaker=cb)

        # Open the circuit
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        async with client:
            with pytest.raises(CircuitBreakerOpen):
                await client.get_aas("test-id")

    @pytest.mark.asyncio
    async def test_4xx_errors_dont_open_circuit(self, settings):
        """Client errors (4xx) don't open the circuit."""
        cb = CircuitBreaker(failure_threshold=2)
        client = TwinClient(settings, circuit_breaker=cb)

        async with client:
            with patch.object(
                client._ensure_session(), "request", new_callable=AsyncMock
            ) as mock_request:
                # Simulate 404 errors
                mock_response = AsyncMock()
                mock_response.status = 404
                mock_response.__aenter__ = AsyncMock(return_value=mock_response)
                mock_response.__aexit__ = AsyncMock(return_value=None)
                mock_response.text = AsyncMock(return_value="Not Found")
                mock_request.return_value = mock_response

                with contextlib.suppress(TwinClientError):
                    await client.get_aas("nonexistent")

                with contextlib.suppress(TwinClientError):
                    await client.get_aas("nonexistent2")

                # Circuit should still be closed (4xx are client errors)
                # Note: The actual implementation records success for < 500
                assert cb.state == CircuitState.CLOSED


class TestTwinClientOperations:
    """Tests for TwinClient HTTP operations."""

    @pytest.fixture
    def twin_client(self, settings):
        """Create twin client for testing."""
        return TwinClient(settings)

    @pytest.mark.asyncio
    async def test_get_aas_success(self, twin_client):
        """Test successful AAS retrieval."""
        async with twin_client:
            with patch.object(
                twin_client, "_protected_request", new_callable=AsyncMock
            ) as mock_request:
                mock_response = AsyncMock()
                mock_response.status = 200
                mock_response.json = AsyncMock(return_value={"id": "test-aas"})
                mock_response.__aenter__ = AsyncMock(return_value=mock_response)
                mock_response.__aexit__ = AsyncMock(return_value=None)
                mock_request.return_value = mock_response

                result = await twin_client.get_aas("test-aas-id")

                assert result == {"id": "test-aas"}

    @pytest.mark.asyncio
    async def test_get_aas_not_found(self, twin_client):
        """Test AAS not found handling."""
        async with twin_client:
            with patch.object(
                twin_client, "_protected_request", new_callable=AsyncMock
            ) as mock_request:
                mock_response = AsyncMock()
                mock_response.status = 404
                mock_response.text = AsyncMock(return_value="Not found")
                mock_response.__aenter__ = AsyncMock(return_value=mock_response)
                mock_response.__aexit__ = AsyncMock(return_value=None)
                mock_request.return_value = mock_response

                with pytest.raises(TwinClientError) as exc_info:
                    await twin_client.get_aas("nonexistent")

                assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_invoke_operation(self, twin_client):
        """Test operation invocation."""
        async with twin_client:
            with patch.object(
                twin_client, "_protected_request", new_callable=AsyncMock
            ) as mock_request:
                mock_response = AsyncMock()
                mock_response.status = 202
                mock_response.json = AsyncMock(
                    return_value={"executionState": "Running", "jobId": "job-123"}
                )
                mock_response.__aenter__ = AsyncMock(return_value=mock_response)
                mock_response.__aexit__ = AsyncMock(return_value=None)
                mock_request.return_value = mock_response

                result = await twin_client.invoke_operation(
                    submodel_id="test-sm",
                    operation_path="TestOp",
                    input_arguments=[],
                    async_mode=True,
                )

                assert result["executionState"] == "Running"
                assert result["jobId"] == "job-123"
