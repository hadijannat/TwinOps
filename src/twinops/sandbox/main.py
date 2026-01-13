"""Sandbox AAS server for local development and testing."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from twinops.common.auth import AuthMiddleware
from twinops.common.basyx_topics import (
    append_trace_param,
    b64url_decode_nopad,
    b64url_encode_nopad,
)
from twinops.common.errors import ErrorCode, error_response
from twinops.common.http import RequestIdMiddleware, get_request_id
from twinops.common.logging import get_logger, setup_logging
from twinops.common.metrics import MetricsMiddleware, metrics_endpoint
from twinops.common.mqtt import MqttClient
from twinops.common.ratelimit import RateLimitMiddleware
from twinops.common.settings import Settings, get_settings
from twinops.common.tracing import setup_tracing

logger = get_logger(__name__)


class InMemoryAASRepository:
    """In-memory AAS repository with MQTT event publishing."""

    def __init__(self, mqtt_client: MqttClient | None, repo_id: str):
        """Initialize repository."""
        self._mqtt = mqtt_client
        self._repo_id = repo_id
        self._shells: dict[str, dict[str, Any]] = {}
        self._submodels: dict[str, dict[str, Any]] = {}

    async def _publish_event(
        self,
        repo_type: str,
        entity_id: str | None,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        """Publish MQTT event."""
        if not self._mqtt:
            return

        if entity_id:
            encoded_id = b64url_encode_nopad(entity_id)
            topic = f"{repo_type}/{self._repo_id}/{'shells' if 'aas' in repo_type else 'submodels'}/{encoded_id}/{event}"
        else:
            topic = f"{repo_type}/{self._repo_id}/{'shells' if 'aas' in repo_type else 'submodels'}/{event}"

        try:
            request_id = get_request_id()
            if request_id:
                topic = append_trace_param(topic, request_id)
            await self._mqtt.publish(topic, json.dumps(payload))
            logger.debug("Published event", topic=topic)
        except Exception as e:
            logger.warning("Failed to publish event", error=str(e))

    # === Shell Operations ===

    async def create_shell(self, shell: dict[str, Any]) -> dict[str, Any]:
        """Create a new AAS shell."""
        shell_id = shell.get("id", "")
        self._shells[shell_id] = shell
        await self._publish_event("aas-repository", None, "created", shell)
        return shell

    async def get_shell(self, shell_id: str) -> dict[str, Any] | None:
        """Get a shell by ID."""
        return self._shells.get(shell_id)

    async def get_all_shells(self) -> list[dict[str, Any]]:
        """Get all shells."""
        return list(self._shells.values())

    async def update_shell(self, shell_id: str, shell: dict[str, Any]) -> dict[str, Any] | None:
        """Update a shell."""
        if shell_id not in self._shells:
            return None
        self._shells[shell_id] = shell
        await self._publish_event("aas-repository", shell_id, "updated", shell)
        return shell

    async def delete_shell(self, shell_id: str) -> bool:
        """Delete a shell."""
        if shell_id not in self._shells:
            return False
        del self._shells[shell_id]
        await self._publish_event("aas-repository", shell_id, "deleted", {"id": shell_id})
        return True

    async def get_shell_submodel_refs(self, shell_id: str) -> list[dict[str, Any]]:
        """Get submodel references from a shell."""
        shell = self._shells.get(shell_id)
        if not shell:
            return []
        submodels = shell.get("submodels", [])
        return list(submodels) if isinstance(submodels, list) else []

    # === Submodel Operations ===

    async def create_submodel(self, submodel: dict[str, Any]) -> dict[str, Any]:
        """Create a new submodel."""
        sm_id = submodel.get("id", "")
        self._submodels[sm_id] = submodel
        await self._publish_event("submodel-repository", None, "created", submodel)
        return submodel

    async def get_submodel(self, submodel_id: str) -> dict[str, Any] | None:
        """Get a submodel by ID."""
        return self._submodels.get(submodel_id)

    async def get_all_submodels(self) -> list[dict[str, Any]]:
        """Get all submodels."""
        return list(self._submodels.values())

    async def update_submodel(
        self, submodel_id: str, submodel: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a submodel."""
        if submodel_id not in self._submodels:
            return None
        self._submodels[submodel_id] = submodel
        await self._publish_event("submodel-repository", submodel_id, "updated", submodel)
        return submodel

    async def delete_submodel(self, submodel_id: str) -> bool:
        """Delete a submodel."""
        if submodel_id not in self._submodels:
            return False
        del self._submodels[submodel_id]
        await self._publish_event(
            "submodel-repository", submodel_id, "deleted", {"id": submodel_id}
        )
        return True

    # === SubmodelElement Operations ===

    async def get_element(
        self,
        submodel_id: str,
        path: str,
    ) -> dict[str, Any] | None:
        """Get a submodel element by path."""
        submodel = self._submodels.get(submodel_id)
        if not submodel:
            return None

        return self._find_element(submodel.get("submodelElements", []), path)

    async def get_element_value(
        self,
        submodel_id: str,
        path: str,
    ) -> Any:
        """Get the value of a property element."""
        element = await self.get_element(submodel_id, path)
        if element:
            return element.get("value")
        return None

    async def set_element_value(
        self,
        submodel_id: str,
        path: str,
        value: Any,
    ) -> bool:
        """Set the value of a property element."""
        submodel = self._submodels.get(submodel_id)
        if not submodel:
            return False

        if self._set_element_value(submodel.get("submodelElements", []), path, value):
            await self._publish_event(
                "submodel-repository",
                submodel_id,
                "updated",
                submodel,
            )
            return True
        return False

    def _find_element(
        self,
        elements: list[dict[str, Any]],
        path: str,
    ) -> dict[str, Any] | None:
        """Recursively find an element by path."""
        parts = path.split("/", 1)
        target = parts[0]
        remaining = parts[1] if len(parts) > 1 else None

        for elem in elements:
            if elem.get("idShort") == target:
                if remaining:
                    # Recurse into collection
                    nested = elem.get("value", [])
                    if isinstance(nested, list):
                        return self._find_element(nested, remaining)
                    return None
                return elem

        return None

    def _set_element_value(
        self,
        elements: list[dict[str, Any]],
        path: str,
        value: Any,
    ) -> bool:
        """Recursively set element value."""
        parts = path.split("/", 1)
        target = parts[0]
        remaining = parts[1] if len(parts) > 1 else None

        for elem in elements:
            if elem.get("idShort") == target:
                if remaining:
                    nested = elem.get("value", [])
                    if isinstance(nested, list):
                        return self._set_element_value(nested, remaining, value)
                    return False
                elem["value"] = value
                return True

        return False

    def load_from_file(self, path: str) -> None:
        """Load AAS environment from JSON file."""
        with open(path) as f:
            data = json.load(f)

        # Load shells
        for shell in data.get("assetAdministrationShells", []):
            self._shells[shell.get("id", "")] = shell

        # Load submodels
        for submodel in data.get("submodels", []):
            self._submodels[submodel.get("id", "")] = submodel

        logger.info(
            "Loaded AAS environment",
            shells=len(self._shells),
            submodels=len(self._submodels),
        )


class SandboxServer:
    """HTTP server for sandbox AAS repository."""

    def __init__(self, settings: Settings):
        """Initialize server."""
        self._settings = settings
        self._repo: InMemoryAASRepository | None = None
        self._mqtt: MqttClient | None = None

    async def startup(self) -> None:
        """Initialize components."""
        logger.info("Starting sandbox server...")

        # Create MQTT client
        self._mqtt = MqttClient(
            host=self._settings.mqtt_broker_host,
            port=self._settings.mqtt_broker_port,
            client_id="sandbox-publisher",
            username=self._settings.mqtt_username,
            password=self._settings.mqtt_password,
            tls=self._settings.mqtt_tls_enabled,
            tls_ca_cert=self._settings.mqtt_tls_ca_cert,
            tls_client_cert=self._settings.mqtt_tls_client_cert,
            tls_client_key=self._settings.mqtt_tls_client_key,
        )

        # Create repository
        self._repo = InMemoryAASRepository(self._mqtt, self._settings.repo_id)

        # Load sample data if available
        sample_path = Path(__file__).parent.parent.parent.parent / "models" / "sample_aas_env.json"
        if sample_path.exists():
            self._repo.load_from_file(str(sample_path))

        logger.info("Sandbox server ready")

    async def shutdown(self) -> None:
        """Clean up resources."""
        pass

    # === HTTP Handlers ===

    async def handle_get_shells(self, _request: Request) -> JSONResponse:
        """GET /shells"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        shells = await self._repo.get_all_shells()
        return JSONResponse({"result": shells})

    async def handle_get_shell(self, request: Request) -> JSONResponse:
        """GET /shells/{aasId}"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        aas_id = self._decode_path_id(request.path_params["aas_id"])
        shell = await self._repo.get_shell(aas_id)
        if not shell:
            return error_response(ErrorCode.NOT_FOUND, "Not found", status_code=404)
        return JSONResponse(shell)

    async def handle_get_shell_refs(self, request: Request) -> JSONResponse:
        """GET /shells/{aasId}/submodel-refs"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        aas_id = self._decode_path_id(request.path_params["aas_id"])
        refs = await self._repo.get_shell_submodel_refs(aas_id)
        return JSONResponse({"result": refs})

    async def handle_get_submodels(self, _request: Request) -> JSONResponse:
        """GET /submodels"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        submodels = await self._repo.get_all_submodels()
        return JSONResponse({"result": submodels})

    async def handle_get_submodel(self, request: Request) -> JSONResponse:
        """GET /submodels/{smId}"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        sm_id = self._decode_path_id(request.path_params["sm_id"])
        submodel = await self._repo.get_submodel(sm_id)
        if not submodel:
            return error_response(ErrorCode.NOT_FOUND, "Not found", status_code=404)
        return JSONResponse(submodel)

    async def handle_get_element(self, request: Request) -> JSONResponse:
        """GET /submodels/{smId}/submodel-elements/{path}"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        sm_id = self._decode_path_id(request.path_params["sm_id"])
        path = request.path_params["path"]
        element = await self._repo.get_element(sm_id, path)
        if not element:
            return error_response(ErrorCode.NOT_FOUND, "Not found", status_code=404)
        return JSONResponse(element)

    async def handle_get_value(self, request: Request) -> JSONResponse:
        """GET /submodels/{smId}/submodel-elements/{path}/$value"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        sm_id = self._decode_path_id(request.path_params["sm_id"])
        path = request.path_params["path"]
        value = await self._repo.get_element_value(sm_id, path)
        return JSONResponse(value)

    async def handle_set_value(self, request: Request) -> Response:
        """PUT /submodels/{smId}/submodel-elements/{path}/$value"""
        if not self._repo:
            return error_response(
                ErrorCode.SERVER_NOT_READY,
                "Repository not initialized",
                status_code=503,
            )
        sm_id = self._decode_path_id(request.path_params["sm_id"])
        path = request.path_params["path"]
        value = await request.json()
        if await self._repo.set_element_value(sm_id, path, value):
            return Response(status_code=204)
        return error_response(ErrorCode.NOT_FOUND, "Not found", status_code=404)

    async def handle_health(self, _request: Request) -> JSONResponse:
        """Health check."""
        return JSONResponse({"status": "healthy"})

    def _decode_path_id(self, encoded: str) -> str:
        """Decode base64url encoded ID from path."""
        try:
            return b64url_decode_nopad(encoded)
        except Exception:
            return encoded


def create_app(settings: Settings | None = None) -> Starlette:
    """Create the Starlette application."""
    settings = settings or get_settings()
    server = SandboxServer(settings)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await server.startup()
        yield
        await server.shutdown()

    routes = [
        # AAS Repository
        Route("/shells", server.handle_get_shells, methods=["GET"]),
        Route("/shells/{aas_id}", server.handle_get_shell, methods=["GET"]),
        Route("/shells/{aas_id}/submodel-refs", server.handle_get_shell_refs, methods=["GET"]),
        # Submodel Repository
        Route("/submodels", server.handle_get_submodels, methods=["GET"]),
        Route("/submodels/{sm_id}", server.handle_get_submodel, methods=["GET"]),
        Route(
            "/submodels/{sm_id}/submodel-elements/{path:path}",
            server.handle_get_element,
            methods=["GET"],
        ),
        Route(
            "/submodels/{sm_id}/submodel-elements/{path:path}/$value",
            server.handle_get_value,
            methods=["GET"],
        ),
        Route(
            "/submodels/{sm_id}/submodel-elements/{path:path}/$value",
            server.handle_set_value,
            methods=["PUT"],
        ),
        # Health
        Route("/health", server.handle_health, methods=["GET"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)

    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=settings.rate_limit_rpm,
        exclude_paths=["/health", "/metrics"],
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(
        MetricsMiddleware,
        exclude_paths=["/health", "/metrics"],
    )

    return app


def main() -> None:
    """Entry point for sandbox server."""
    setup_logging()
    settings = get_settings()
    if settings.tracing_enabled or settings.tracing_otlp_endpoint or settings.tracing_console:
        setup_tracing(
            service_name=settings.tracing_service_name or "twinops-sandbox",
            otlp_endpoint=settings.tracing_otlp_endpoint,
            enable_console=settings.tracing_console,
        )
    app = create_app(settings)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.sandbox_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
