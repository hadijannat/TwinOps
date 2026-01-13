"""Agent HTTP server entry point."""

import asyncio
import json
import signal
import time
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from twinops.agent.capabilities import CapabilityIndex
from twinops.agent.llm.factory import create_llm_client
from twinops.agent.orchestrator import AgentOrchestrator
from twinops.agent.safety import AuditLogger, SafetyKernel
from twinops.agent.schema_gen import generate_all_tool_schemas
from twinops.agent.shadow import ShadowTwinManager
from twinops.agent.twin_client import TwinClient
from twinops.common.logging import get_logger, setup_logging
from twinops.common.metrics import metrics_endpoint
from twinops.common.mqtt import MqttClient
from twinops.common.ratelimit import RateLimitMiddleware
from twinops.common.settings import Settings, get_settings

logger = get_logger(__name__)


class GracefulShutdown:
    """Handles graceful shutdown with request draining."""

    def __init__(self, drain_timeout: float = 30.0):
        """
        Initialize graceful shutdown handler.

        Args:
            drain_timeout: Maximum time to wait for in-flight requests
        """
        self._drain_timeout = drain_timeout
        self._shutdown_event = asyncio.Event()
        self._active_requests = 0
        self._start_time: float | None = None

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._shutdown_event.is_set()

    @property
    def active_requests(self) -> int:
        """Get count of active requests."""
        return self._active_requests

    def request_started(self) -> None:
        """Track start of a request."""
        self._active_requests += 1

    def request_finished(self) -> None:
        """Track completion of a request."""
        self._active_requests = max(0, self._active_requests - 1)

    def trigger_shutdown(self) -> None:
        """Trigger graceful shutdown."""
        logger.info("Graceful shutdown triggered")
        self._start_time = time.time()
        self._shutdown_event.set()

    async def wait_for_drain(self) -> None:
        """Wait for in-flight requests to complete."""
        if self._start_time is None:
            self._start_time = time.time()

        while self._active_requests > 0:
            elapsed = time.time() - self._start_time
            if elapsed >= self._drain_timeout:
                logger.warning(
                    "Drain timeout reached, forcing shutdown",
                    active_requests=self._active_requests,
                    timeout=self._drain_timeout,
                )
                break

            logger.info(
                "Waiting for requests to drain",
                active_requests=self._active_requests,
                elapsed=round(elapsed, 1),
            )
            await asyncio.sleep(0.5)

        if self._active_requests == 0:
            logger.info("All requests drained successfully")


class AgentServer:
    """HTTP server wrapper for the agent."""

    def __init__(self, settings: Settings):
        """Initialize server components."""
        self._settings = settings
        self._orchestrator: AgentOrchestrator | None = None
        self._twin_client: TwinClient | None = None
        self._mqtt_client: MqttClient | None = None
        self._shadow: ShadowTwinManager | None = None
        self._safety: SafetyKernel | None = None
        self._shutdown = GracefulShutdown(drain_timeout=30.0)
        self._initialized = False
        self._start_time = time.time()

    async def startup(self) -> None:
        """Initialize all components."""
        logger.info("Starting agent server...")

        # Create clients
        self._twin_client = TwinClient(self._settings)
        await self._twin_client.__aenter__()

        self._mqtt_client = MqttClient(
            host=self._settings.mqtt_broker_host,
            port=self._settings.mqtt_broker_port,
            client_id=self._settings.mqtt_client_id,
            username=self._settings.mqtt_username,
            password=self._settings.mqtt_password,
        )

        # Create shadow twin
        self._shadow = ShadowTwinManager(
            twin_client=self._twin_client,
            mqtt_client=self._mqtt_client,
            aas_id=self._settings.aas_id,
            repo_id=self._settings.repo_id,
        )

        # Initialize shadow (connects MQTT)
        async with self._mqtt_client.connect():
            await self._shadow.initialize()

            # Load operations and build capability index
            operations = await self._shadow.get_operations()
            tools = generate_all_tool_schemas(operations)
            capabilities = CapabilityIndex(tools)

            logger.info("Loaded tools", count=len(tools))

            # Create safety kernel
            audit = AuditLogger(self._settings.audit_log_path)
            self._safety = SafetyKernel(
                shadow=self._shadow,
                twin_client=self._twin_client,
                audit_logger=audit,
                policy_submodel_id="urn:example:submodel:policy",  # TODO: from settings
                require_policy_verification=self._settings.policy_verification_required,
            )

            # Create LLM client
            llm = create_llm_client(self._settings)

            # Create orchestrator
            self._orchestrator = AgentOrchestrator(
                llm=llm,
                shadow=self._shadow,
                twin_client=self._twin_client,
                safety=self._safety,
                capability_index=capabilities,
                settings=self._settings,
            )

            self._initialized = True
            logger.info("Agent server ready")

    async def shutdown(self) -> None:
        """Clean up resources with graceful draining."""
        self._shutdown.trigger_shutdown()
        await self._shutdown.wait_for_drain()

        if self._twin_client:
            await self._twin_client.__aexit__(None, None, None)

        logger.info("Agent server shutdown complete")

    async def handle_chat(self, request: Request) -> JSONResponse:
        """Handle chat endpoint."""
        # Reject requests during shutdown
        if self._shutdown.is_shutting_down:
            return JSONResponse(
                {"error": "Server is shutting down"},
                status_code=503,
            )

        if not self._orchestrator:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

        self._shutdown.request_started()
        try:
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return JSONResponse(
                    {"error": "Invalid JSON"},
                    status_code=400,
                )

            message = body.get("message", "")
            if not message:
                return JSONResponse(
                    {"error": "Missing 'message' field"},
                    status_code=400,
                )

            # Extract roles from header
            roles_header = request.headers.get("X-Roles", "")
            roles = tuple(r.strip() for r in roles_header.split(",") if r.strip())
            if not roles:
                roles = self._settings.default_roles

            # Process message
            response = await self._orchestrator.process_message(message, roles)

            return JSONResponse({
                "reply": response.reply,
                "tool_results": [
                    {
                        "tool": r.tool_name,
                        "success": r.success,
                        "result": r.result,
                        "error": r.error,
                        "simulated": r.simulated,
                        "status": r.status,
                    }
                    for r in response.tool_results
                ],
                "pending_approval": response.pending_approval,
                "task_id": response.task_id,
            })
        finally:
            self._shutdown.request_finished()

    async def handle_health(self, _request: Request) -> JSONResponse:
        """
        Liveness probe endpoint.

        Returns healthy if the process is running.
        Used by Kubernetes liveness probes.
        """
        return JSONResponse({
            "status": "healthy",
            "uptime": round(time.time() - self._start_time, 1),
            "shutting_down": self._shutdown.is_shutting_down,
        })

    async def handle_ready(self, _request: Request) -> JSONResponse:
        """
        Readiness probe endpoint.

        Checks if all dependencies are ready to serve traffic.
        Used by Kubernetes readiness probes.
        """
        checks = {
            "initialized": self._initialized,
            "orchestrator": self._orchestrator is not None,
            "mqtt_connected": (
                self._mqtt_client.is_connected if self._mqtt_client else False
            ),
            "shadow_initialized": self._shadow is not None,
            "twin_client_circuit": (
                self._twin_client.circuit_breaker.state.value
                if self._twin_client else "unknown"
            ),
        }

        # Ready if initialized and not shutting down
        all_ready = (
            self._initialized
            and not self._shutdown.is_shutting_down
            and checks.get("mqtt_connected", False)
        )

        return JSONResponse(
            {
                "status": "ready" if all_ready else "not_ready",
                "checks": checks,
                "active_requests": self._shutdown.active_requests,
            },
            status_code=200 if all_ready else 503,
        )

    async def handle_reset(self, _request: Request) -> JSONResponse:
        """Reset conversation history."""
        if self._orchestrator:
            self._orchestrator.reset_conversation()
        return JSONResponse({"status": "conversation reset"})

    async def handle_openapi(self, _request: Request) -> JSONResponse:
        """
        OpenAPI specification endpoint.

        Returns the OpenAPI 3.1 specification for this API.
        """
        openapi_spec = {
            "openapi": "3.1.0",
            "info": {
                "title": "TwinOps Agent API",
                "version": "1.0.0",
                "description": (
                    "AI Agent API for BaSyx Digital Twin operations. "
                    "Provides natural language interface for interacting with "
                    "Asset Administration Shell (AAS) digital twins."
                ),
                "contact": {
                    "name": "RWTH Aachen University",
                    "email": "ias@rwth-aachen.de",
                },
                "license": {
                    "name": "MIT",
                    "url": "https://opensource.org/licenses/MIT",
                },
            },
            "servers": [
                {
                    "url": f"http://{self._settings.agent_host}:{self._settings.agent_port}",
                    "description": "Local development server",
                }
            ],
            "paths": {
                "/chat": {
                    "post": {
                        "summary": "Process natural language command",
                        "description": "Send a natural language command to interact with the digital twin.",
                        "operationId": "chat",
                        "tags": ["Agent"],
                        "parameters": [
                            {
                                "name": "X-Roles",
                                "in": "header",
                                "description": "Comma-separated list of user roles",
                                "required": False,
                                "schema": {"type": "string", "example": "operator,viewer"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "message": {
                                                "type": "string",
                                                "description": "Natural language command",
                                                "example": "What is the current pump speed?",
                                            }
                                        },
                                        "required": ["message"],
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "reply": {"type": "string"},
                                                "tool_results": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "tool": {"type": "string"},
                                                            "success": {"type": "boolean"},
                                                            "result": {},
                                                            "error": {"type": "string"},
                                                            "simulated": {"type": "boolean"},
                                                            "status": {"type": "string"},
                                                        },
                                                    },
                                                },
                                                "pending_approval": {"type": "boolean"},
                                                "task_id": {"type": "string"},
                                            },
                                        }
                                    }
                                },
                            },
                            "400": {"description": "Invalid request"},
                            "429": {"description": "Rate limit exceeded"},
                            "503": {"description": "Service unavailable"},
                        },
                    }
                },
                "/health": {
                    "get": {
                        "summary": "Liveness probe",
                        "description": "Check if the service process is running.",
                        "operationId": "health",
                        "tags": ["Health"],
                        "responses": {
                            "200": {
                                "description": "Service is alive",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                                "uptime": {"type": "number"},
                                                "shutting_down": {"type": "boolean"},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/ready": {
                    "get": {
                        "summary": "Readiness probe",
                        "description": "Check if the service is ready to accept traffic.",
                        "operationId": "ready",
                        "tags": ["Health"],
                        "responses": {
                            "200": {
                                "description": "Service is ready",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                                "checks": {"type": "object"},
                                                "active_requests": {"type": "integer"},
                                            },
                                        }
                                    }
                                },
                            },
                            "503": {"description": "Service not ready"},
                        },
                    }
                },
                "/reset": {
                    "post": {
                        "summary": "Reset conversation",
                        "description": "Reset the conversation history.",
                        "operationId": "reset",
                        "tags": ["Agent"],
                        "responses": {
                            "200": {
                                "description": "Conversation reset",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/metrics": {
                    "get": {
                        "summary": "Prometheus metrics",
                        "description": "Prometheus-formatted metrics for monitoring.",
                        "operationId": "metrics",
                        "tags": ["Observability"],
                        "responses": {
                            "200": {
                                "description": "Metrics in Prometheus format",
                                "content": {
                                    "text/plain": {
                                        "schema": {"type": "string"},
                                    }
                                },
                            }
                        },
                    }
                },
            },
            "tags": [
                {"name": "Agent", "description": "AI agent operations"},
                {"name": "Health", "description": "Health and readiness probes"},
                {"name": "Observability", "description": "Monitoring and metrics"},
            ],
        }
        return JSONResponse(openapi_spec)


def create_app(settings: Settings | None = None) -> Starlette:
    """Create the Starlette application."""
    settings = settings or get_settings()
    server = AgentServer(settings)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # Setup signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()

        def handle_signal() -> None:
            logger.info("Received shutdown signal")
            server._shutdown.trigger_shutdown()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_signal)

        await server.startup()
        yield
        await server.shutdown()

    routes = [
        Route("/chat", server.handle_chat, methods=["POST"]),
        Route("/health", server.handle_health, methods=["GET"]),
        Route("/ready", server.handle_ready, methods=["GET"]),
        Route("/reset", server.handle_reset, methods=["POST"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
        Route("/openapi.json", server.handle_openapi, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)

    # Add rate limiting middleware
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=settings.rate_limit_rpm,
        exclude_paths=["/health", "/ready", "/metrics"],
    )

    return app


def main():
    """Entry point for agent server."""
    setup_logging()
    settings = get_settings()
    app = create_app(settings)

    uvicorn.run(
        app,
        host=settings.agent_host,
        port=settings.agent_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
