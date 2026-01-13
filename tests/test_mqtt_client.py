"""Tests for MQTT client functionality."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from twinops.common.basyx_topics import TopicSubscription
from twinops.common.mqtt import ExponentialBackoff, MqttClient, MqttMessage


class TestExponentialBackoff:
    """Tests for exponential backoff calculator."""

    def test_initial_delay(self):
        """First delay should be base delay."""
        backoff = ExponentialBackoff(base_delay=5.0)
        assert backoff.next_delay() == 5.0

    def test_exponential_increase(self):
        """Delays should increase exponentially."""
        backoff = ExponentialBackoff(base_delay=1.0, multiplier=2.0)

        # First delay: 1.0 * (2^0) = 1.0
        assert backoff.next_delay() == 1.0
        # Second delay: 1.0 * (2^1) = 2.0
        assert backoff.next_delay() == 2.0
        # Third delay: 1.0 * (2^2) = 4.0
        assert backoff.next_delay() == 4.0

    def test_max_delay_cap(self):
        """Delays should not exceed max_delay."""
        backoff = ExponentialBackoff(base_delay=10.0, max_delay=20.0, multiplier=2.0)

        assert backoff.next_delay() == 10.0  # 10 * 2^0 = 10
        assert backoff.next_delay() == 20.0  # 10 * 2^1 = 20, capped at 20
        assert backoff.next_delay() == 20.0  # 10 * 2^2 = 40, capped at 20

    def test_reset(self):
        """Reset should return to initial delay."""
        backoff = ExponentialBackoff(base_delay=1.0, multiplier=2.0)

        backoff.next_delay()  # 1
        backoff.next_delay()  # 2
        assert backoff.attempt_count == 2

        backoff.reset()
        assert backoff.attempt_count == 0
        assert backoff.next_delay() == 1.0

    def test_attempt_count_property(self):
        """attempt_count should track number of delays requested."""
        backoff = ExponentialBackoff()

        assert backoff.attempt_count == 0
        backoff.next_delay()
        assert backoff.attempt_count == 1
        backoff.next_delay()
        assert backoff.attempt_count == 2


class TestMqttMessage:
    """Tests for MqttMessage dataclass."""

    def test_basic_fields(self):
        """Test basic field access."""
        msg = MqttMessage(
            topic="test/topic",
            payload=b"hello",
            qos=1,
            retain=True,
        )

        assert msg.topic == "test/topic"
        assert msg.payload == b"hello"
        assert msg.qos == 1
        assert msg.retain is True

    def test_payload_str(self):
        """Test payload_str property."""
        msg = MqttMessage(
            topic="test",
            payload=b"hello world",
            qos=0,
            retain=False,
        )

        assert msg.payload_str == "hello world"

    def test_payload_json(self):
        """Test payload_json property."""
        data = {"key": "value", "number": 42}
        msg = MqttMessage(
            topic="test",
            payload=json.dumps(data).encode(),
            qos=0,
            retain=False,
        )

        assert msg.payload_json == data

    def test_payload_json_array(self):
        """Test payload_json with array."""
        data = [1, 2, 3]
        msg = MqttMessage(
            topic="test",
            payload=json.dumps(data).encode(),
            qos=0,
            retain=False,
        )

        assert msg.payload_json == data


class TestMqttClientInit:
    """Tests for MqttClient initialization."""

    def test_default_parameters(self):
        """Test client with default parameters."""
        client = MqttClient(host="localhost")

        assert client._host == "localhost"
        assert client._port == 1883
        assert client._client_id == "twinops"
        assert client._username is None
        assert client._password is None

    def test_custom_parameters(self):
        """Test client with custom parameters."""
        client = MqttClient(
            host="mqtt.example.com",
            port=8883,
            client_id="test-client",
            username="user",
            password="pass",
        )

        assert client._host == "mqtt.example.com"
        assert client._port == 8883
        assert client._client_id == "test-client"
        assert client._username == "user"
        assert client._password == "pass"

    def test_initial_state(self):
        """Test initial client state."""
        client = MqttClient(host="localhost")

        assert client.is_connected is False
        assert client._running is False
        assert client._subscriptions == []
        assert client._handlers == []

    def test_connection_stats_initial(self):
        """Test initial connection stats."""
        client = MqttClient(host="localhost")

        stats = client.connection_stats
        assert stats["connected"] is False
        assert stats["connection_count"] == 0
        assert stats["disconnection_count"] == 0
        assert stats["last_connected"] is None
        assert stats["reconnect_attempts"] == 0


class TestMqttClientHandlers:
    """Tests for MqttClient handler management."""

    def test_add_handler(self):
        """Test adding message handlers."""
        client = MqttClient(host="localhost")

        async def handler1(msg):
            pass

        async def handler2(msg):
            pass

        client.add_handler(handler1)
        assert len(client._handlers) == 1

        client.add_handler(handler2)
        assert len(client._handlers) == 2

    def test_set_subscriptions(self):
        """Test setting subscriptions."""
        client = MqttClient(host="localhost")

        subs = [
            TopicSubscription(topic="test/+/events", qos=0),
            TopicSubscription(topic="alerts/#", qos=1),
        ]

        client.set_subscriptions(subs)
        assert len(client._subscriptions) == 2
        assert client._subscriptions[0].topic == "test/+/events"
        assert client._subscriptions[1].topic == "alerts/#"

    def test_set_subscriptions_replaces_existing(self):
        """Test that set_subscriptions replaces existing subscriptions."""
        client = MqttClient(host="localhost")

        client.set_subscriptions([TopicSubscription(topic="old/topic", qos=0)])
        assert len(client._subscriptions) == 1

        client.set_subscriptions([
            TopicSubscription(topic="new/topic1", qos=0),
            TopicSubscription(topic="new/topic2", qos=1),
        ])
        assert len(client._subscriptions) == 2
        assert client._subscriptions[0].topic == "new/topic1"


class TestMqttClientConnection:
    """Tests for MqttClient connection lifecycle."""

    @pytest.fixture
    def mock_aiomqtt_client(self):
        """Create mock aiomqtt client."""
        mock_client = AsyncMock()
        mock_client.subscribe = AsyncMock()
        mock_client.publish = AsyncMock()
        # Create an async iterator that yields nothing (empty)
        mock_client.messages = AsyncMock()
        mock_client.messages.__aiter__ = lambda self: self
        mock_client.messages.__anext__ = AsyncMock(side_effect=StopAsyncIteration)
        return mock_client

    @pytest.mark.asyncio
    async def test_connect_context_manager_starts_task(self):
        """Test that connect() starts the background task."""
        client = MqttClient(host="localhost")

        # Mock the run loop to just set a flag
        run_called = asyncio.Event()

        async def mock_run_loop():
            run_called.set()
            # Wait until cancelled
            try:
                while True:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

        with patch.object(client, "_run_loop", mock_run_loop):
            async with client.connect():
                # Wait for run loop to start
                await asyncio.wait_for(run_called.wait(), timeout=1.0)
                assert client._running is True
                assert client._task is not None

        # After context exits, task should be cancelled
        assert client._running is False

    @pytest.mark.asyncio
    async def test_is_connected_property(self):
        """Test is_connected property updates correctly."""
        client = MqttClient(host="localhost")

        assert client.is_connected is False

        # Manually set connected
        client._connected = True
        assert client.is_connected is True


class TestMqttClientBackoff:
    """Tests for MqttClient reconnection with backoff."""

    def test_backoff_configured_from_params(self):
        """Test that backoff is configured from constructor params."""
        client = MqttClient(
            host="localhost",
            base_reconnect_delay=10.0,
            max_reconnect_delay=120.0,
        )

        assert client._backoff._base_delay == 10.0
        assert client._backoff._max_delay == 120.0

    def test_connection_stats_after_events(self):
        """Test connection stats update after connection events."""
        client = MqttClient(host="localhost")

        # Simulate connection
        client._connected = True
        client._connection_count = 3
        client._disconnection_count = 2
        client._last_connected_time = 1234567890.0

        # Simulate some reconnection attempts
        client._backoff.next_delay()
        client._backoff.next_delay()

        stats = client.connection_stats
        assert stats["connected"] is True
        assert stats["connection_count"] == 3
        assert stats["disconnection_count"] == 2
        assert stats["last_connected"] == 1234567890.0
        assert stats["reconnect_attempts"] == 2
