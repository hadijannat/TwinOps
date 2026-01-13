"""MQTT client wrapper for async operation."""

import asyncio
import contextlib
import ssl
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self

import aiomqtt

from twinops.common.basyx_topics import TopicSubscription
from twinops.common.logging import get_logger

logger = get_logger(__name__)


class ExponentialBackoff:
    """Exponential backoff calculator with jitter."""

    def __init__(
        self,
        base_delay: float = 5.0,
        max_delay: float = 60.0,
        multiplier: float = 2.0,
    ):
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._multiplier = multiplier
        self._attempt = 0

    def reset(self) -> None:
        """Reset backoff to initial state."""
        self._attempt = 0

    def next_delay(self) -> float:
        """Calculate next delay with exponential backoff."""
        delay = min(
            self._base_delay * (self._multiplier**self._attempt),
            self._max_delay,
        )
        self._attempt += 1
        return delay

    @property
    def attempt_count(self) -> int:
        """Current attempt count."""
        return self._attempt


@dataclass
class MqttMessage:
    """Wrapper for incoming MQTT messages."""

    topic: str
    payload: bytes
    qos: int
    retain: bool

    @property
    def payload_str(self) -> str:
        """Decode payload as UTF-8 string."""
        return self.payload.decode("utf-8")

    @property
    def payload_json(self) -> Any:
        """Parse payload as JSON."""
        import json

        return json.loads(self.payload_str)


MessageHandler = Callable[[MqttMessage], Coroutine[Any, Any, None]]
ReconnectHandler = Callable[[], Coroutine[Any, Any, None]]


class MqttClient:
    """Async MQTT client with automatic reconnection and exponential backoff."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: str = "twinops",
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
        tls_ca_cert: str | None = None,
        tls_client_cert: str | None = None,
        tls_client_key: str | None = None,
        base_reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
    ):
        """
        Initialize MQTT client.

        Args:
            host: Broker hostname
            port: Broker port
            client_id: Client identifier
            username: Optional username
            password: Optional password
            base_reconnect_delay: Initial delay between reconnection attempts
            max_reconnect_delay: Maximum delay between reconnection attempts
        """
        self._host = host
        self._port = port
        self._client_id = client_id
        self._username = username
        self._password = password
        self._tls = tls
        self._tls_ca_cert = tls_ca_cert
        self._tls_client_cert = tls_client_cert
        self._tls_client_key = tls_client_key
        self._backoff = ExponentialBackoff(
            base_delay=base_reconnect_delay,
            max_delay=max_reconnect_delay,
        )
        self._subscriptions: list[TopicSubscription] = []
        self._handlers: list[MessageHandler] = []
        self._reconnect_handlers: list[ReconnectHandler] = []
        self._running = False
        self._connected = False
        self._last_connected_time: float | None = None
        self._connection_count = 0
        self._disconnection_count = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to MQTT broker."""
        return self._connected

    @property
    def connection_stats(self) -> dict[str, Any]:
        """Get connection statistics."""
        return {
            "connected": self._connected,
            "connection_count": self._connection_count,
            "disconnection_count": self._disconnection_count,
            "last_connected": self._last_connected_time,
            "reconnect_attempts": self._backoff.attempt_count,
        }

    def add_handler(self, handler: MessageHandler) -> None:
        """Add a message handler."""
        self._handlers.append(handler)

    def add_reconnect_handler(self, handler: ReconnectHandler) -> None:
        """
        Add a handler to be called on reconnection.

        This is useful for triggering resync operations when the MQTT connection
        is restored after being offline.
        """
        self._reconnect_handlers.append(handler)

    def set_subscriptions(self, subscriptions: list[TopicSubscription]) -> None:
        """Set topics to subscribe to."""
        self._subscriptions = list(subscriptions)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Self]:
        """Context manager for connection lifecycle."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        try:
            yield self
        finally:
            self._running = False
            if self._task:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task

    async def _run_loop(self) -> None:
        """Main connection loop with auto-reconnect and exponential backoff."""
        while self._running:
            try:
                await self._connect_and_listen()
            except aiomqtt.MqttError as e:
                self._connected = False
                self._disconnection_count += 1
                if not self._running:
                    break
                delay = self._backoff.next_delay()
                logger.warning(
                    "MQTT connection lost, reconnecting with backoff...",
                    error=str(e),
                    delay=delay,
                    attempt=self._backoff.attempt_count,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self._connected = False
                break
            except Exception as e:
                self._connected = False
                self._disconnection_count += 1
                if not self._running:
                    break
                delay = self._backoff.next_delay()
                logger.error(
                    "Unexpected MQTT error, reconnecting...",
                    error=str(e),
                    delay=delay,
                )
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        """Establish connection and process messages."""
        logger.info(
            "Connecting to MQTT broker",
            host=self._host,
            port=self._port,
            client_id=self._client_id,
        )

        tls_context = None
        if self._tls:
            tls_context = ssl.create_default_context(cafile=self._tls_ca_cert)
            if self._tls_client_cert and self._tls_client_key:
                tls_context.load_cert_chain(self._tls_client_cert, self._tls_client_key)

        async with aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=self._client_id,
            username=self._username,
            password=self._password,
            tls_context=tls_context,
        ) as client:
            # Mark as connected and reset backoff
            self._connected = True
            self._connection_count += 1
            self._last_connected_time = time.time()
            self._backoff.reset()

            # Subscribe to all topics
            for sub in self._subscriptions:
                await client.subscribe(sub.topic, qos=sub.qos)
                logger.debug("Subscribed to topic", topic=sub.topic, qos=sub.qos)

            logger.info(
                "MQTT connected and subscribed",
                subscription_count=len(self._subscriptions),
                connection_number=self._connection_count,
            )

            # Call reconnect handlers on reconnection (not initial connection)
            if self._connection_count > 1:
                logger.info(
                    "Triggering reconnect handlers",
                    handler_count=len(self._reconnect_handlers),
                )
                for reconnect_handler in self._reconnect_handlers:
                    try:
                        await reconnect_handler()
                    except Exception as e:
                        logger.error(
                            "Reconnect handler error",
                            error=str(e),
                        )

            # Process incoming messages
            async for msg in client.messages:
                message = MqttMessage(
                    topic=str(msg.topic),
                    payload=bytes(msg.payload)
                    if isinstance(msg.payload, (bytes, bytearray))
                    else msg.payload.encode()
                    if isinstance(msg.payload, str)
                    else bytes(msg.payload),
                    qos=msg.qos,
                    retain=msg.retain,
                )

                for handler in self._handlers:
                    try:
                        await handler(message)
                    except Exception as e:
                        logger.error(
                            "Message handler error",
                            topic=message.topic,
                            error=str(e),
                        )

    async def publish(
        self,
        topic: str,
        payload: str | bytes,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        """
        Publish a message.

        Note: This creates a new connection for publishing.
        For frequent publishing, consider maintaining a persistent connection.
        """
        tls_context = None
        if self._tls:
            tls_context = ssl.create_default_context(cafile=self._tls_ca_cert)
            if self._tls_client_cert and self._tls_client_key:
                tls_context.load_cert_chain(self._tls_client_cert, self._tls_client_key)

        async with aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=f"{self._client_id}-pub",
            username=self._username,
            password=self._password,
            tls_context=tls_context,
        ) as client:
            await client.publish(topic, payload, qos=qos, retain=retain)
