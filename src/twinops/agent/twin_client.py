"""HTTP client for AAS repository operations."""

import json
import time
from enum import Enum
from typing import Any

import aiohttp
from urllib.parse import quote

from twinops.common.basyx_topics import b64url_encode_nopad
from twinops.common.logging import get_logger
from twinops.common.settings import Settings

logger = get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


class CircuitBreakerOpen(Exception):
    """Exception raised when circuit breaker is open."""

    def __init__(self, message: str = "Circuit breaker is open"):
        super().__init__(message)


class CircuitBreaker:
    """Circuit breaker pattern implementation for resilience."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
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

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        """Get current circuit state, transitioning if needed."""
        if (
            self._state == CircuitState.OPEN
            and time.time() - self._last_failure_time > self._recovery_timeout
        ):
            logger.info("Circuit breaker transitioning to half-open")
            self._state = CircuitState.HALF_OPEN
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
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_calls += 1
            if self._half_open_calls >= self._half_open_max_calls:
                logger.info(
                    "Circuit breaker closing after successful recovery",
                    successful_calls=self._half_open_calls,
                )
                self._state = CircuitState.CLOSED
                self._failure_count = 0
        elif self._state == CircuitState.CLOSED:
            # Reset failure count on success in closed state
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            logger.warning("Circuit breaker reopening after failure in half-open state")
            self._state = CircuitState.OPEN
        elif self._failure_count >= self._failure_threshold:
            logger.warning(
                "Circuit breaker opening",
                failure_count=self._failure_count,
                threshold=self._failure_threshold,
            )
            self._state = CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if an operation can be executed."""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return self._half_open_calls < self._half_open_max_calls
        return False  # OPEN

    def ensure_can_execute(self) -> None:
        """Raise exception if circuit is open."""
        if not self.can_execute():
            raise CircuitBreakerOpen(
                f"Circuit breaker is {self.state.value}, "
                f"retry after {self._recovery_timeout}s"
            )


class TwinClientError(Exception):
    """Error communicating with AAS repository."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TwinClient:
    """
    HTTP client for BaSyx AAS/Submodel repository operations.

    Supports both combined (single server) and split (separate AAS/SM servers)
    repository configurations. Includes circuit breaker for resilience.
    """

    def __init__(
        self,
        settings: Settings,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        """
        Initialize the twin client.

        Args:
            settings: Application settings
            circuit_breaker: Optional circuit breaker instance
        """
        self._aas_base = settings.twin_base_url.rstrip("/")
        self._sm_base = (settings.submodel_base_url or settings.twin_base_url).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=settings.http_timeout)
        self._session: aiohttp.ClientSession | None = None
        self._circuit_breaker = circuit_breaker or CircuitBreaker()

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get the circuit breaker instance."""
        return self._circuit_breaker

    async def __aenter__(self) -> "TwinClient":
        """Enter async context."""
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context."""
        if self._session:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure session exists."""
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _protected_request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """
        Execute HTTP request with circuit breaker protection.

        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            url: Request URL
            **kwargs: Additional arguments for the request

        Returns:
            aiohttp response object

        Raises:
            CircuitBreakerOpen: If circuit breaker is open
            TwinClientError: On request failure
        """
        self._circuit_breaker.ensure_can_execute()
        session = self._ensure_session()

        try:
            response = await session.request(method, url, **kwargs)
            # Record success for 2xx and 4xx (client errors are not server failures)
            if response.status < 500:
                self._circuit_breaker.record_success()
            else:
                self._circuit_breaker.record_failure()
            return response
        except aiohttp.ClientError as e:
            self._circuit_breaker.record_failure()
            raise TwinClientError(f"Request failed: {e}") from e

    # === AAS Operations ===

    async def get_aas(self, aas_id: str) -> dict[str, Any]:
        """
        Retrieve an Asset Administration Shell by ID.

        Args:
            aas_id: AAS identifier

        Returns:
            AAS JSON structure
        """
        aas_id_encoded = b64url_encode_nopad(aas_id)
        url = f"{self._aas_base}/shells/{aas_id_encoded}"

        logger.debug("Fetching AAS", aas_id=aas_id, url=url)

        response = await self._protected_request("GET", url)
        async with response:
            if response.status == 404:
                raise TwinClientError(f"AAS not found: {aas_id}", 404)
            if response.status != 200:
                text = await response.text()
                raise TwinClientError(f"Failed to get AAS: {text}", response.status)
            return await response.json()

    async def get_all_aas(self) -> list[dict[str, Any]]:
        """
        Retrieve all Asset Administration Shells.

        Returns:
            List of AAS JSON structures
        """
        url = f"{self._aas_base}/shells"

        response = await self._protected_request("GET", url)
        async with response:
            if response.status != 200:
                text = await response.text()
                raise TwinClientError(f"Failed to list AAS: {text}", response.status)
            data = await response.json()
            # BaSyx returns paged results
            return data.get("result", data) if isinstance(data, dict) else data

    async def get_aas_submodel_refs(self, aas_id: str) -> list[dict[str, Any]]:
        """
        Get submodel references from an AAS.

        Args:
            aas_id: AAS identifier

        Returns:
            List of submodel reference structures
        """
        aas_id_encoded = b64url_encode_nopad(aas_id)
        url = f"{self._aas_base}/shells/{aas_id_encoded}/submodel-refs"

        response = await self._protected_request("GET", url)
        async with response:
            if response.status != 200:
                text = await response.text()
                raise TwinClientError(f"Failed to get submodel refs: {text}", response.status)
            data = await response.json()
            return data.get("result", data) if isinstance(data, dict) else data

    # === Submodel Operations ===

    async def get_submodel(self, submodel_id: str) -> dict[str, Any]:
        """
        Retrieve a Submodel by ID.

        Args:
            submodel_id: Submodel identifier

        Returns:
            Submodel JSON structure
        """
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        url = f"{self._sm_base}/submodels/{sm_id_encoded}"

        logger.debug("Fetching submodel", submodel_id=submodel_id, url=url)

        response = await self._protected_request("GET", url)
        async with response:
            if response.status == 404:
                raise TwinClientError(f"Submodel not found: {submodel_id}", 404)
            if response.status != 200:
                text = await response.text()
                raise TwinClientError(f"Failed to get submodel: {text}", response.status)
            return await response.json()

    async def get_submodel_element(
        self,
        submodel_id: str,
        id_short_path: str,
    ) -> dict[str, Any]:
        """
        Retrieve a specific submodel element.

        Args:
            submodel_id: Submodel identifier
            id_short_path: Path to element (e.g., "Property1" or "Collection/Nested")

        Returns:
            SubmodelElement JSON structure
        """
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        encoded_path = quote(id_short_path, safe="/")
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{encoded_path}"

        response = await self._protected_request("GET", url)
        async with response:
            if response.status == 404:
                raise TwinClientError(f"Element not found: {id_short_path}", 404)
            if response.status != 200:
                text = await response.text()
                raise TwinClientError(f"Failed to get element: {text}", response.status)
            return await response.json()

    async def get_property_value(
        self,
        submodel_id: str,
        id_short_path: str,
    ) -> Any:
        """
        Get the value of a Property element.

        Args:
            submodel_id: Submodel identifier
            id_short_path: Path to property

        Returns:
            Property value
        """
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        encoded_path = quote(id_short_path, safe="/")
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{encoded_path}/$value"

        response = await self._protected_request("GET", url)
        async with response:
            if response.status != 200:
                text = await response.text()
                raise TwinClientError(f"Failed to get value: {text}", response.status)
            return await response.json()

    async def set_property_value(
        self,
        submodel_id: str,
        id_short_path: str,
        value: Any,
    ) -> None:
        """
        Set the value of a Property element.

        Args:
            submodel_id: Submodel identifier
            id_short_path: Path to property
            value: New value
        """
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        encoded_path = quote(id_short_path, safe="/")
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{encoded_path}/$value"

        response = await self._protected_request(
            "PUT",
            url,
            json=value,
            headers={"Content-Type": "application/json"},
        )
        async with response:
            if response.status not in (200, 204):
                text = await response.text()
                raise TwinClientError(f"Failed to set value: {text}", response.status)

    # === Operation Invocation ===

    async def invoke_operation(
        self,
        submodel_id: str,
        operation_path: str,
        input_arguments: list[dict[str, Any]],
        client_context: dict[str, Any] | None = None,
        async_mode: bool = True,
    ) -> dict[str, Any]:
        """
        Invoke an AAS operation.

        Args:
            submodel_id: Submodel identifier
            operation_path: Path to operation
            input_arguments: List of input argument structures
            client_context: Optional context (e.g., simulate flag)
            async_mode: If True, use async invocation endpoint

        Returns:
            Operation result or job reference
        """
        sm_id_encoded = b64url_encode_nopad(submodel_id)

        endpoint = "$invoke-async" if async_mode else "$invoke"
        encoded_path = quote(operation_path, safe="/")
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{encoded_path}/{endpoint}"

        payload: dict[str, Any] = {
            "inputArguments": input_arguments,
        }
        if client_context:
            payload["clientContext"] = client_context

        logger.debug(
            "Invoking operation",
            submodel_id=submodel_id,
            operation=operation_path,
            async_mode=async_mode,
        )

        response = await self._protected_request(
            "POST",
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        async with response:
            if response.status not in (200, 202):
                text = await response.text()
                raise TwinClientError(f"Operation failed: {text}", response.status)
            return await response.json()

    async def invoke_delegated_operation(
        self,
        delegation_url: str,
        input_arguments: list[dict[str, Any]],
        simulate: bool = False,
    ) -> dict[str, Any]:
        """
        Invoke an operation via its delegation URL.

        Args:
            delegation_url: HTTP endpoint for the operation
            input_arguments: List of input argument structures
            simulate: Whether to run in simulation mode

        Returns:
            Operation result
        """
        payload = {
            "inputArguments": input_arguments,
            "clientContext": {
                "simulate": simulate,
            },
        }

        logger.debug(
            "Invoking delegated operation",
            url=delegation_url,
            simulate=simulate,
        )

        response = await self._protected_request(
            "POST",
            delegation_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        async with response:
            if response.status not in (200, 202):
                text = await response.text()
                raise TwinClientError(f"Delegated operation failed: {text}", response.status)
            return await response.json()

    async def get_job_status(
        self,
        submodel_id: str,
        operation_path: str,
        job_id: str,
    ) -> dict[str, Any]:
        """
        Get job status directly via HTTP.

        This provides a fallback for checking job status when MQTT-based
        shadow twin updates are unavailable or stale.

        Args:
            submodel_id: Submodel identifier
            operation_path: Path to the operation
            job_id: Job identifier from async invocation

        Returns:
            Job status including state and result (if complete)
        """
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        encoded_path = quote(operation_path, safe="/")
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{encoded_path}/$result"

        logger.debug(
            "Fetching job status via HTTP",
            submodel_id=submodel_id,
            operation_path=operation_path,
            job_id=job_id,
        )

        response = await self._protected_request(
            "GET",
            url,
            params={"jobId": job_id},
        )
        async with response:
            if response.status == 404:
                # Job not found or expired
                raise TwinClientError(f"Job not found: {job_id}", response.status)
            if response.status not in (200, 202):
                text = await response.text()
                raise TwinClientError(f"Failed to get job status: {text}", response.status)
            return await response.json()

    # === Batch Operations ===

    async def get_full_twin(self, aas_id: str) -> dict[str, Any]:
        """
        Retrieve complete twin state: AAS + all referenced submodels.

        Args:
            aas_id: AAS identifier

        Returns:
            Dictionary with 'aas' and 'submodels' keys
        """
        aas = await self.get_aas(aas_id)
        submodel_refs = await self.get_aas_submodel_refs(aas_id)

        submodels: dict[str, dict[str, Any]] = {}
        for ref in submodel_refs:
            # Extract submodel ID from reference
            keys = ref.get("keys", [])
            if keys:
                sm_id = keys[0].get("value", "")
                if sm_id:
                    try:
                        submodels[sm_id] = await self.get_submodel(sm_id)
                    except TwinClientError as e:
                        logger.warning(
                            "Failed to fetch referenced submodel",
                            submodel_id=sm_id,
                            error=str(e),
                        )

        return {
            "aas": aas,
            "submodels": submodels,
        }

    # === Task Management ===

    async def get_tasks(self, submodel_id: str, property_path: str) -> list[dict[str, Any]]:
        """
        Get pending tasks from TasksJson property.

        Args:
            submodel_id: Submodel containing tasks
            property_path: Path to TasksJson property

        Returns:
            List of task objects
        """
        try:
            value = await self.get_property_value(submodel_id, property_path)
            data = json.loads(value) if isinstance(value, str) else value
            return data.get("tasks", [])
        except TwinClientError:
            return []

    async def update_task_status(
        self,
        submodel_id: str,
        property_path: str,
        task_id: str,
        new_status: str,
        reason: str | None = None,
    ) -> bool:
        """
        Update a task's status.

        Args:
            submodel_id: Submodel containing tasks
            property_path: Path to TasksJson property
            task_id: Task identifier
            new_status: New status value
            reason: Optional reason for status change

        Returns:
            True if task was found and updated
        """
        tasks = await self.get_tasks(submodel_id, property_path)
        updated = False

        for task in tasks:
            if task.get("task_id") == task_id:
                task["status"] = new_status
                if reason:
                    task["status_reason"] = reason
                updated = True
                break

        if updated:
            await self.set_property_value(
                submodel_id,
                property_path,
                json.dumps({"tasks": tasks}),
            )

        return updated

    async def add_task(
        self,
        submodel_id: str,
        property_path: str,
        task: dict[str, Any],
    ) -> None:
        """
        Add a new task to TasksJson.

        Args:
            submodel_id: Submodel containing tasks
            property_path: Path to TasksJson property
            task: Task object to add
        """
        tasks = await self.get_tasks(submodel_id, property_path)
        tasks.append(task)
        await self.set_property_value(
            submodel_id,
            property_path,
            json.dumps({"tasks": tasks}),
        )

    async def update_tasks(
        self,
        submodel_id: str,
        property_path: str,
        tasks: list[dict[str, Any]],
    ) -> None:
        """
        Replace all tasks in TasksJson.

        Args:
            submodel_id: Submodel containing tasks
            property_path: Path to TasksJson property
            tasks: Complete list of tasks to store
        """
        await self.set_property_value(
            submodel_id,
            property_path,
            json.dumps({"tasks": tasks}),
        )
