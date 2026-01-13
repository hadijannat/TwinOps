"""HTTP client for AAS repository operations."""

import json
from typing import Any

import aiohttp

from twinops.common.basyx_topics import b64url_encode_nopad
from twinops.common.logging import get_logger
from twinops.common.settings import Settings

logger = get_logger(__name__)


class TwinClientError(Exception):
    """Error communicating with AAS repository."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TwinClient:
    """
    HTTP client for BaSyx AAS/Submodel repository operations.

    Supports both combined (single server) and split (separate AAS/SM servers)
    repository configurations.
    """

    def __init__(self, settings: Settings):
        """
        Initialize the twin client.

        Args:
            settings: Application settings
        """
        self._aas_base = settings.twin_base_url.rstrip("/")
        self._sm_base = (settings.submodel_base_url or settings.twin_base_url).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=settings.http_timeout)
        self._session: aiohttp.ClientSession | None = None

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

    # === AAS Operations ===

    async def get_aas(self, aas_id: str) -> dict[str, Any]:
        """
        Retrieve an Asset Administration Shell by ID.

        Args:
            aas_id: AAS identifier

        Returns:
            AAS JSON structure
        """
        session = self._ensure_session()
        aas_id_encoded = b64url_encode_nopad(aas_id)
        url = f"{self._aas_base}/shells/{aas_id_encoded}"

        logger.debug("Fetching AAS", aas_id=aas_id, url=url)

        async with session.get(url) as response:
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
        session = self._ensure_session()
        url = f"{self._aas_base}/shells"

        async with session.get(url) as response:
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
        session = self._ensure_session()
        aas_id_encoded = b64url_encode_nopad(aas_id)
        url = f"{self._aas_base}/shells/{aas_id_encoded}/submodel-refs"

        async with session.get(url) as response:
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
        session = self._ensure_session()
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        url = f"{self._sm_base}/submodels/{sm_id_encoded}"

        logger.debug("Fetching submodel", submodel_id=submodel_id, url=url)

        async with session.get(url) as response:
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
        session = self._ensure_session()
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{id_short_path}"

        async with session.get(url) as response:
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
        session = self._ensure_session()
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{id_short_path}/$value"

        async with session.get(url) as response:
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
        session = self._ensure_session()
        sm_id_encoded = b64url_encode_nopad(submodel_id)
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{id_short_path}/$value"

        async with session.put(
            url,
            json=value,
            headers={"Content-Type": "application/json"},
        ) as response:
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
        session = self._ensure_session()
        sm_id_encoded = b64url_encode_nopad(submodel_id)

        endpoint = "$invoke-async" if async_mode else "$invoke"
        url = f"{self._sm_base}/submodels/{sm_id_encoded}/submodel-elements/{operation_path}/{endpoint}"

        payload = {
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

        async with session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as response:
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
        session = self._ensure_session()

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

        async with session.post(
            delegation_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as response:
            if response.status not in (200, 202):
                text = await response.text()
                raise TwinClientError(f"Delegated operation failed: {text}", response.status)
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
            if isinstance(value, str):
                data = json.loads(value)
            else:
                data = value
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
