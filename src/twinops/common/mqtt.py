"""MQTT client wrapper for async operation."""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiomqtt

from twinops.common.basyx_topics import TopicSubscription
from twinops.common.logging import get_logger

logger = get_logger(__name__)


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


class MqttClient:
    """Async MQTT client with automatic reconnection."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: str = "twinops",
        username: str | None = None,
        password: str | None = None,
        reconnect_interval: float = 5.0,
    ):
        """
        Initialize MQTT client.

        Args:
            host: Broker hostname
            port: Broker port
            client_id: Client identifier
            username: Optional username
            password: Optional password
            reconnect_interval: Seconds between reconnection attempts
        """
        self._host = host
        self._port = port
        self._client_id = client_id
        self._username = username
        self._password = password
        self._reconnect_interval = reconnect_interval
        self._subscriptions: list[TopicSubscription] = []
        self._handlers: list[MessageHandler] = []
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def add_handler(self, handler: MessageHandler) -> None:
        """Add a message handler."""
        self._handlers.append(handler)

    def set_subscriptions(self, subscriptions: list[TopicSubscription]) -> None:
        """Set topics to subscribe to."""
        self._subscriptions = list(subscriptions)

    @asynccontextmanager
    async def connect(self):
        """Context manager for connection lifecycle."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        try:
            yield self
        finally:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

    async def _run_loop(self) -> None:
        """Main connection loop with auto-reconnect."""
        while self._running:
            try:
                await self._connect_and_listen()
            except aiomqtt.MqttError as e:
                if not self._running:
                    break
                logger.warning(
                    "MQTT connection lost, reconnecting...",
                    error=str(e),
                    interval=self._reconnect_interval,
                )
                await asyncio.sleep(self._reconnect_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.error("Unexpected MQTT error", error=str(e))
                await asyncio.sleep(self._reconnect_interval)

    async def _connect_and_listen(self) -> None:
        """Establish connection and process messages."""
        logger.info(
            "Connecting to MQTT broker",
            host=self._host,
            port=self._port,
            client_id=self._client_id,
        )

        async with aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=self._client_id,
            username=self._username,
            password=self._password,
        ) as client:
            # Subscribe to all topics
            for sub in self._subscriptions:
                await client.subscribe(sub.topic, qos=sub.qos)
                logger.debug("Subscribed to topic", topic=sub.topic, qos=sub.qos)

            logger.info(
                "MQTT connected and subscribed",
                subscription_count=len(self._subscriptions),
            )

            # Process incoming messages
            async for msg in client.messages:
                message = MqttMessage(
                    topic=str(msg.topic),
                    payload=bytes(msg.payload) if isinstance(msg.payload, (bytes, bytearray)) else msg.payload.encode() if isinstance(msg.payload, str) else bytes(msg.payload),
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
        async with aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=f"{self._client_id}-pub",
            username=self._username,
            password=self._password,
        ) as client:
            await client.publish(topic, payload, qos=qos, retain=retain)
