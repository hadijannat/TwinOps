"""Shadow Twin Manager - Live synchronized copy of AAS state."""

import asyncio
import json
import time
from typing import Any

from twinops.common.basyx_topics import (
    EventType,
    ParsedTopic,
    RepositoryType,
    build_subscriptions_split,
    extract_trace_param,
    parse_topic,
)
from twinops.common.logging import get_logger
from twinops.common.mqtt import MqttClient, MqttMessage
from twinops.agent.twin_client import TwinClient, TwinClientError
from twinops.common.metrics import record_mqtt_event
from twinops.common.tracing import span

logger = get_logger(__name__)


class ShadowTwinManager:
    """
    Maintains a live, synchronized copy of the AAS state.

    Features:
    - Initial HTTP snapshot of full shell + referenced submodels
    - MQTT event patching for incremental updates
    - Fallback re-sync when patching fails
    - Thread-safe access via async lock
    """

    def __init__(
        self,
        twin_client: TwinClient,
        mqtt_client: MqttClient,
        aas_id: str,
        aas_repo_id: str,
        submodel_repo_id: str | None = None,
    ):
        """
        Initialize the Shadow Twin Manager.

        Args:
            twin_client: HTTP client for AAS operations
            mqtt_client: MQTT client for event subscription
            aas_id: AAS identifier to track
            aas_repo_id: Repository ID for AAS repository MQTT topics
            submodel_repo_id: Repository ID for Submodel repository MQTT topics
                             (defaults to aas_repo_id if not specified)
        """
        self._twin_client = twin_client
        self._mqtt_client = mqtt_client
        self._aas_id = aas_id
        self._aas_repo_id = aas_repo_id
        self._submodel_repo_id = submodel_repo_id if submodel_repo_id is not None else aas_repo_id

        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "aas": {},
            "submodels": {},
        }
        self._initialized = False
        self._event_count = 0
        self._last_sync_time: float | None = None
        self._last_update_times: dict[str, float] = {}  # Per-submodel timestamps

    @property
    def is_initialized(self) -> bool:
        """Check if initial sync has completed."""
        return self._initialized

    @property
    def event_count(self) -> int:
        """Number of MQTT events processed."""
        return self._event_count

    @property
    def last_sync_time(self) -> float | None:
        """Timestamp of last full sync."""
        return self._last_sync_time

    @property
    def freshness_seconds(self) -> float:
        """
        Seconds since last sync or update.

        Returns float('inf') if never synced.
        """
        if self._last_sync_time is None:
            return float("inf")
        return time.time() - self._last_sync_time

    def get_submodel_freshness(self, submodel_id: str) -> float:
        """
        Get freshness for a specific submodel in seconds.

        Args:
            submodel_id: Submodel identifier

        Returns:
            Seconds since last update, or inf if never updated
        """
        last_update = self._last_update_times.get(submodel_id)
        if last_update is None:
            return float("inf")
        return time.time() - last_update

    async def initialize(self) -> None:
        """
        Perform initial HTTP sync and start MQTT subscription.

        This must be called before using the shadow twin.
        """
        logger.info(
            "Initializing Shadow Twin",
            aas_id=self._aas_id,
            aas_repo_id=self._aas_repo_id,
            submodel_repo_id=self._submodel_repo_id,
        )

        # Set up MQTT subscriptions for both repositories
        subscriptions = build_subscriptions_split(
            aas_repo_id=self._aas_repo_id,
            submodel_repo_id=self._submodel_repo_id,
        )
        self._mqtt_client.set_subscriptions(subscriptions)
        self._mqtt_client.add_handler(self._handle_mqtt_message)

        # Register reconnect handler for automatic resync after connection loss
        self._mqtt_client.add_reconnect_handler(self._on_mqtt_reconnect)

        # Perform initial HTTP snapshot
        await self._full_sync()
        self._initialized = True

        logger.info(
            "Shadow Twin initialized",
            aas_id=self._aas_id,
            submodel_count=len(self._state["submodels"]),
        )

    async def _full_sync(self) -> None:
        """Fetch complete twin state via HTTP."""
        async with self._lock:
            try:
                with span("shadow_full_sync", {"aas.id": self._aas_id}):
                    full_state = await self._twin_client.get_full_twin(self._aas_id)
                self._state = full_state
                sync_time = time.time()
                self._last_sync_time = sync_time
                # Update timestamps for all submodels
                for sm_id in self._state["submodels"]:
                    self._last_update_times[sm_id] = sync_time
                logger.debug(
                    "Full sync completed",
                    submodel_count=len(self._state["submodels"]),
                )
            except TwinClientError as e:
                logger.error("Full sync failed", error=str(e))
                raise

    async def _on_mqtt_reconnect(self) -> None:
        """
        Handle MQTT reconnection by triggering full resync.

        Called when MQTT connection is re-established after being lost.
        Events may have been missed during the disconnection period.
        """
        logger.info(
            "MQTT reconnected, triggering shadow twin resync",
            aas_id=self._aas_id,
        )
        await self._full_sync()
        logger.info(
            "Shadow twin resync completed",
            submodel_count=len(self._state["submodels"]),
            freshness_seconds=self.freshness_seconds,
        )

    async def _handle_mqtt_message(self, message: MqttMessage) -> None:
        """Process incoming MQTT event."""
        trace_id = extract_trace_param(message.topic)
        span_attrs = {"mqtt.topic": message.topic}
        if trace_id:
            span_attrs["trace_id"] = trace_id
        with span("shadow_mqtt_event", span_attrs):
            parsed = parse_topic(message.topic)
            if not parsed:
                return

            # Only process events for our repositories (AAS or Submodel)
            # Check the appropriate repo_id based on the repository type
            if parsed.repository_type == RepositoryType.AAS:
                if parsed.repo_id != self._aas_repo_id:
                    return
            elif parsed.repository_type == RepositoryType.SUBMODEL:
                if parsed.repo_id != self._submodel_repo_id:
                    return
            else:
                return

            self._event_count += 1
            record_mqtt_event(parsed.event_type.value)

            try:
                await self._apply_event(parsed, message.payload)
            except Exception as e:
                logger.warning(
                    "Failed to apply MQTT event, triggering resync",
                    topic=message.topic,
                    error=str(e),
                )
                await self._full_sync()

    async def _apply_event(self, parsed: ParsedTopic, payload: bytes) -> None:
        """Apply an MQTT event to the shadow state."""
        async with self._lock:
            if parsed.repository_type == RepositoryType.AAS:
                await self._apply_aas_event(parsed, payload)
            elif parsed.repository_type == RepositoryType.SUBMODEL:
                await self._apply_submodel_event(parsed, payload)

    async def _apply_aas_event(self, parsed: ParsedTopic, payload: bytes) -> None:
        """Apply AAS repository event."""
        if not parsed.entity_id:
            # Collection-level event (e.g., new AAS created)
            if parsed.event_type == EventType.CREATED:
                # Check if it's our AAS
                try:
                    data = json.loads(payload.decode("utf-8"))
                    if data.get("id") == self._aas_id:
                        self._state["aas"] = data
                except json.JSONDecodeError:
                    pass
            return

        # Entity-specific event
        if parsed.entity_id != self._aas_id:
            return

        if parsed.event_type == EventType.UPDATED:
            try:
                data = json.loads(payload.decode("utf-8"))
                self._state["aas"] = data
                logger.debug("AAS updated via MQTT", aas_id=self._aas_id)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON in AAS update payload")

        elif parsed.event_type == EventType.DELETED:
            self._state["aas"] = {}
            logger.warning("AAS deleted via MQTT", aas_id=self._aas_id)

    async def _apply_submodel_event(self, parsed: ParsedTopic, payload: bytes) -> None:
        """Apply submodel repository event."""
        if not parsed.entity_id:
            return

        submodel_id = parsed.entity_id

        # Check if this submodel is referenced by our AAS
        if submodel_id not in self._state["submodels"]:
            # Might be a new submodel reference - check if we should track it
            return

        if parsed.event_type == EventType.DELETED:
            del self._state["submodels"][submodel_id]
            self._last_update_times.pop(submodel_id, None)
            logger.debug("Submodel deleted via MQTT", submodel_id=submodel_id)
            return

        if parsed.event_type == EventType.UPDATED:
            try:
                data = json.loads(payload.decode("utf-8"))

                if parsed.element_path:
                    # Element-specific update
                    self._update_element(submodel_id, parsed.element_path, data)
                else:
                    # Full submodel update
                    self._state["submodels"][submodel_id] = data

                # Update timestamp for this submodel
                self._last_update_times[submodel_id] = time.time()
                self._last_sync_time = time.time()

                logger.debug(
                    "Submodel updated via MQTT",
                    submodel_id=submodel_id,
                    element_path=parsed.element_path,
                )
            except json.JSONDecodeError:
                logger.warning("Invalid JSON in submodel update payload")

    def _update_element(
        self,
        submodel_id: str,
        element_path: str,
        new_data: dict[str, Any],
    ) -> None:
        """Update a specific element within a submodel."""
        submodel = self._state["submodels"].get(submodel_id, {})
        elements = submodel.get("submodelElements", [])

        # Navigate path (e.g., "Collection/Nested/Property")
        path_parts = element_path.split("/")
        current = elements

        for i, part in enumerate(path_parts[:-1]):
            found = False
            for elem in current:
                if elem.get("idShort") == part:
                    current = elem.get("value", [])
                    found = True
                    break
            if not found:
                return

        # Update the target element
        target_name = path_parts[-1]
        for j, elem in enumerate(current):
            if elem.get("idShort") == target_name:
                current[j] = new_data
                return

    # === Public Query Interface ===

    async def get_aas(self) -> dict[str, Any]:
        """Get the current AAS state."""
        async with self._lock:
            return dict(self._state["aas"])

    async def get_submodel(self, submodel_id: str) -> dict[str, Any] | None:
        """Get a submodel by ID."""
        async with self._lock:
            return self._state["submodels"].get(submodel_id)

    async def get_all_submodels(self) -> dict[str, dict[str, Any]]:
        """Get all tracked submodels."""
        async with self._lock:
            return dict(self._state["submodels"])

    async def get_property_value(
        self,
        submodel_id: str,
        id_short_path: str,
    ) -> Any:
        """
        Get a property value from the shadow state.

        Args:
            submodel_id: Submodel identifier
            id_short_path: Path to property (e.g., "Temperature" or "Status/Current")

        Returns:
            Property value or None if not found
        """
        async with self._lock:
            submodel = self._state["submodels"].get(submodel_id)
            if not submodel:
                return None

            elements = submodel.get("submodelElements", [])
            path_parts = id_short_path.split("/")

            current = elements
            for part in path_parts:
                found = None
                for elem in current:
                    if elem.get("idShort") == part:
                        found = elem
                        break

                if found is None:
                    return None

                # If this is the target, return its value
                if part == path_parts[-1]:
                    return found.get("value")

                # Otherwise, descend into nested elements
                current = found.get("value", [])
                if not isinstance(current, list):
                    return None

            return None

    async def get_operations(self) -> list[dict[str, Any]]:
        """
        Get all operations from all submodels.

        Returns:
            List of operation elements with their submodel context
        """
        operations = []

        async with self._lock:
            for sm_id, submodel in self._state["submodels"].items():
                elements = submodel.get("submodelElements", [])
                ops = self._extract_operations(elements, sm_id)
                operations.extend(ops)

        return operations

    def _extract_operations(
        self,
        elements: list[dict[str, Any]],
        submodel_id: str,
        path_prefix: str = "",
    ) -> list[dict[str, Any]]:
        """Recursively extract Operation elements."""
        operations = []

        for elem in elements:
            model_type = elem.get("modelType", "")
            id_short = elem.get("idShort", "")
            current_path = f"{path_prefix}/{id_short}" if path_prefix else id_short

            if model_type == "Operation":
                operations.append({
                    **elem,
                    "_submodel_id": submodel_id,
                    "_path": current_path,
                })

            # Recurse into collections
            if model_type == "SubmodelElementCollection":
                nested = elem.get("value", [])
                if isinstance(nested, list):
                    operations.extend(
                        self._extract_operations(nested, submodel_id, current_path)
                    )

        return operations

    async def refresh(self) -> None:
        """Force a full resync from HTTP."""
        await self._full_sync()

    async def get_element_by_path(
        self,
        submodel_id: str,
        path: str,
    ) -> dict[str, Any] | None:
        """
        Get any submodel element by path.

        Args:
            submodel_id: Submodel identifier
            path: idShort path

        Returns:
            Element structure or None
        """
        async with self._lock:
            submodel = self._state["submodels"].get(submodel_id)
            if not submodel:
                return None

            elements = submodel.get("submodelElements", [])
            path_parts = path.split("/")

            current = elements
            for part in path_parts:
                found = None
                for elem in current:
                    if elem.get("idShort") == part:
                        found = elem
                        break

                if found is None:
                    return None

                if part == path_parts[-1]:
                    return found

                current = found.get("value", [])
                if not isinstance(current, list):
                    return None

            return None
