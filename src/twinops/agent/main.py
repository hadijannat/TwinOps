"""Agent HTTP server entry point."""

import asyncio
import json
import os
import signal
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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
from twinops.agent.twin_client import TwinClient, TwinClientError
from twinops.common.auth import AuthMiddleware
from twinops.common.logging import get_logger, setup_logging
from twinops.common.mqtt import MqttClient
from twinops.common.ratelimit import RateLimitMiddleware
from twinops.common.settings import Settings, get_settings

logger = get_logger(__name__)


class DependencyStatus(str, Enum):
    """Status of a dependency check."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class DependencyCheck:
    """Result of a dependency validation check."""

    name: str
    status: DependencyStatus
    message: str
    details: dict | None = None


class StartupValidationError(Exception):
    """Raised when startup validation fails."""

    def __init__(self, checks: list[DependencyCheck]):
        self.checks = checks
        failed = [c for c in checks if c.status != DependencyStatus.OK]
        messages = [f"{c.name}: {c.message}" for c in failed]
        super().__init__(f"Startup validation failed: {'; '.join(messages)}")


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
        try:
            from twinops.common.metrics import update_active_requests

            update_active_requests(self._active_requests)
        except Exception:
            pass

    def request_finished(self) -> None:
        """Track completion of a request."""
        self._active_requests = max(0, self._active_requests - 1)
        try:
            from twinops.common.metrics import update_active_requests

            update_active_requests(self._active_requests)
        except Exception:
            pass

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
        self._exit_stack = AsyncExitStack()

    async def _validate_dependencies(self) -> list[DependencyCheck]:
        """
        Validate all dependencies are available before full startup.

        Returns:
            List of dependency check results
        """
        checks: list[DependencyCheck] = []

        # Check AAS Repository
        try:
            aas_list = await self._twin_client.get_all_aas()
            checks.append(DependencyCheck(
                name="aas_repository",
                status=DependencyStatus.OK,
                message=f"Connected, {len(aas_list)} AAS available",
                details={"aas_count": len(aas_list)},
            ))

            # Check if configured AAS exists
            if self._settings.startup_validate_aas:
                aas_ids = [aas.get("id", "") for aas in aas_list]
                if self._settings.aas_id in aas_ids:
                    checks.append(DependencyCheck(
                        name="configured_aas",
                        status=DependencyStatus.OK,
                        message=f"AAS '{self._settings.aas_id}' found",
                    ))
                else:
                    checks.append(DependencyCheck(
                        name="configured_aas",
                        status=DependencyStatus.NOT_FOUND,
                        message=f"AAS '{self._settings.aas_id}' not found in repository",
                        details={"available_aas": aas_ids[:10]},  # Limit for logging
                    ))

        except TwinClientError as e:
            checks.append(DependencyCheck(
                name="aas_repository",
                status=DependencyStatus.UNAVAILABLE,
                message=f"Cannot connect to AAS repository: {e}",
                details={"url": self._settings.twin_base_url},
            ))
        except Exception as e:
            checks.append(DependencyCheck(
                name="aas_repository",
                status=DependencyStatus.ERROR,
                message=f"Unexpected error: {e}",
            ))

        return checks

    async def _wait_for_dependencies(self) -> None:
        """
        Wait for dependencies with retry logic.

        Raises:
            StartupValidationError: If dependencies don't become available within timeout
        """
        start_time = time.time()
        last_checks: list[DependencyCheck] = []

        while time.time() - start_time < self._settings.startup_timeout:
            last_checks = await self._validate_dependencies()

            # Check if all critical dependencies are OK
            critical_failed = [
                c for c in last_checks
                if c.status != DependencyStatus.OK
                and c.name in ("aas_repository", "configured_aas")
            ]

            if not critical_failed:
                # All critical dependencies OK
                for check in last_checks:
                    logger.info(
                        "Dependency check passed",
                        dependency=check.name,
                        status=check.status.value,
                        message=check.message,
                    )
                return

            # Log retry
            elapsed = time.time() - start_time
            remaining = self._settings.startup_timeout - elapsed
            logger.warning(
                "Dependencies not ready, retrying...",
                failed_checks=[c.name for c in critical_failed],
                elapsed=round(elapsed, 1),
                remaining=round(remaining, 1),
                retry_interval=self._settings.startup_retry_interval,
            )

            await asyncio.sleep(self._settings.startup_retry_interval)

        # Timeout reached
        raise StartupValidationError(last_checks)

    async def startup(self) -> None:
        """Initialize all components with dependency validation."""
        logger.info(
            "Starting agent server...",
            aas_id=self._settings.aas_id,
            twin_url=self._settings.twin_base_url,
            mqtt_host=self._settings.mqtt_broker_host,
        )

        # Create twin client first (needed for validation)
        self._twin_client = TwinClient(self._settings)
        await self._twin_client.__aenter__()

        # Validate dependencies before proceeding
        try:
            await self._wait_for_dependencies()
        except StartupValidationError as e:
            logger.error(
                "Startup validation failed",
                failed_checks=[
                    {"name": c.name, "status": c.status.value, "message": c.message}
                    for c in e.checks if c.status != DependencyStatus.OK
                ],
            )
            # Clean up twin client
            await self._twin_client.__aexit__(None, None, None)
            raise

        # Create MQTT client
        self._mqtt_client = MqttClient(
            host=self._settings.mqtt_broker_host,
            port=self._settings.mqtt_broker_port,
            client_id=self._settings.mqtt_client_id,
            username=self._settings.mqtt_username,
            password=self._settings.mqtt_password,
        )

        # Create shadow twin with separate repo IDs for AAS and Submodel repositories
        self._shadow = ShadowTwinManager(
            twin_client=self._twin_client,
            mqtt_client=self._mqtt_client,
            aas_id=self._settings.aas_id,
            aas_repo_id=self._settings.effective_aas_repo_id,
            submodel_repo_id=self._settings.effective_submodel_repo_id,
        )

        # Initialize shadow (connects MQTT and keeps connection open)
        await self._exit_stack.enter_async_context(self._mqtt_client.connect())
        await self._shadow.initialize()

        # Load operations and build capability index
        operations = await self._shadow.get_operations()
        tools = generate_all_tool_schemas(operations)
        capabilities = CapabilityIndex(tools)

        logger.info("Loaded tools from AAS", count=len(tools))

        # Create safety kernel
        audit = AuditLogger(self._settings.audit_log_path)
        self._safety = SafetyKernel(
            shadow=self._shadow,
            twin_client=self._twin_client,
            audit_logger=audit,
            policy_submodel_id=self._settings.policy_submodel_id,
            require_policy_verification=self._settings.policy_verification_required,
            interlock_fail_safe=self._settings.interlock_fail_safe,
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
        logger.info(
            "Agent server ready",
            tools_loaded=len(tools),
            aas_id=self._settings.aas_id,
        )

    async def shutdown(self) -> None:
        """Clean up resources with graceful draining."""
        self._shutdown.trigger_shutdown()
        await self._shutdown.wait_for_drain()

        await self._exit_stack.aclose()

        if self._twin_client:
            await self._twin_client.__aexit__(None, None, None)

        logger.info("Agent server shutdown complete")

    def _get_roles(self, request: Request) -> tuple[str, ...]:
        auth = getattr(request.state, "auth", None)
        if auth and getattr(auth, "roles", None):
            return auth.roles
        roles_header = request.headers.get("X-Roles", "")
        roles = tuple(r.strip() for r in roles_header.split(",") if r.strip())
        return roles or self._settings.default_roles

    def _get_subject(self, request: Request, fallback: str) -> str:
        auth = getattr(request.state, "auth", None)
        if auth and getattr(auth, "subject", None):
            return auth.subject
        return fallback

    def _auth_method(self, request: Request) -> str:
        auth = getattr(request.state, "auth", None)
        return getattr(auth, "method", "header")

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

            roles = self._get_roles(request)

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
        response_data = {
            "status": "healthy",
            "uptime": round(time.time() - self._start_time, 1),
            "shutting_down": self._shutdown.is_shutting_down,
        }

        # Include shadow twin freshness if initialized
        if self._shadow and self._shadow.is_initialized:
            response_data["shadow_freshness_seconds"] = round(
                self._shadow.freshness_seconds, 1
            )
            response_data["shadow_event_count"] = self._shadow.event_count

        return JSONResponse(response_data)

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

        try:
            from twinops.common.metrics import (
                update_circuit_breaker_state,
                update_mqtt_status,
                update_shadow_freshness,
            )

            update_mqtt_status(checks.get("mqtt_connected", False))
            if self._shadow and self._shadow.is_initialized:
                update_shadow_freshness(self._shadow.freshness_seconds)
            if self._twin_client:
                update_circuit_breaker_state(self._twin_client.circuit_breaker.state.value)
        except Exception:
            pass

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
        return JSONResponse({"status": "ok"})

    async def handle_list_tasks(self, _request: Request) -> JSONResponse:
        """
        List pending approval tasks.

        Returns list of tasks awaiting human approval.
        """
        if not self._safety:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

        try:
            tasks = await self._safety.get_pending_tasks()
            return JSONResponse({
                "tasks": tasks,
                "count": len(tasks),
            })
        except Exception as e:
            logger.error("Failed to list tasks", error=str(e))
            return JSONResponse(
                {"error": "Failed to retrieve tasks"},
                status_code=500,
            )

    async def handle_approve_task(self, request: Request) -> JSONResponse:
        """
        Approve a pending task.

        Requires task_id in URL path. Optionally accepts approver in body.
        """
        if not self._safety:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

        task_id = request.path_params.get("task_id", "")
        if not task_id:
            return JSONResponse(
                {"error": "Missing task_id"},
                status_code=400,
            )

        approver = "unknown"
        if self._auth_method(request) != "mtls":
            try:
                body = await request.json()
                approver = body.get("approver", approver)
            except (json.JSONDecodeError, ValueError):
                pass
            approver = request.headers.get("X-Approver", approver)
        approver = self._get_subject(request, approver)

        try:
            success = await self._safety.approve_task(task_id, approver)
            if success:
                return JSONResponse({
                    "status": "approved",
                    "task_id": task_id,
                    "approved_by": approver,
                })
            else:
                return JSONResponse(
                    {"error": "Task not found or not in pending state"},
                    status_code=404,
                )
        except Exception as e:
            logger.error("Failed to approve task", task_id=task_id, error=str(e))
            return JSONResponse(
                {"error": "Failed to approve task"},
                status_code=500,
            )

    async def handle_reject_task(self, request: Request) -> JSONResponse:
        """
        Reject a pending task.

        Requires task_id in URL path. Optionally accepts rejector and reason in body.
        """
        if not self._safety:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

        task_id = request.path_params.get("task_id", "")
        if not task_id:
            return JSONResponse(
                {"error": "Missing task_id"},
                status_code=400,
            )

        rejector = "unknown"
        reason = ""
        if self._auth_method(request) != "mtls":
            try:
                body = await request.json()
                rejector = body.get("rejector", rejector)
                reason = body.get("reason", reason)
            except (json.JSONDecodeError, ValueError):
                pass
            rejector = request.headers.get("X-Rejector", rejector)
        rejector = self._get_subject(request, rejector)

        try:
            success = await self._safety.reject_task(task_id, rejector, reason)
            if success:
                return JSONResponse({
                    "status": "rejected",
                    "task_id": task_id,
                    "rejected_by": rejector,
                    "reason": reason,
                })
            else:
                return JSONResponse(
                    {"error": "Task not found or not in pending state"},
                    status_code=404,
                )
        except Exception as e:
            logger.error("Failed to reject task", task_id=task_id, error=str(e))
            return JSONResponse(
                {"error": "Failed to reject task"},
                status_code=500,
            )

    async def handle_get_task(self, request: Request) -> JSONResponse:
        """
        Get details of a specific task.

        Returns full task information including status, tool, args, etc.
        """
        if not self._safety:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

        task_id = request.path_params.get("task_id", "")
        if not task_id:
            return JSONResponse(
                {"error": "Missing task_id"},
                status_code=400,
            )

        try:
            task = await self._safety.get_task(task_id)
            if task:
                return JSONResponse({"task": task})
            else:
                return JSONResponse(
                    {"error": "Task not found"},
                    status_code=404,
                )
        except Exception as e:
            logger.error("Failed to get task", task_id=task_id, error=str(e))
            return JSONResponse(
                {"error": "Failed to retrieve task"},
                status_code=500,
            )

    async def handle_execute_task(self, request: Request) -> JSONResponse:
        """
        Execute an approved task.

        This endpoint allows executing a task that has been approved.
        Useful for executing tasks after agent restart or asynchronous approval.
        """
        if not self._orchestrator:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

        task_id = request.path_params.get("task_id", "")
        if not task_id:
            return JSONResponse(
                {"error": "Missing task_id"},
                status_code=400,
            )

        roles = self._get_roles(request)

        self._shutdown.request_started()
        try:
            response = await self._orchestrator.execute_approved_task(task_id, roles)

            return JSONResponse({
                "reply": response.reply,
                "tool_results": [
                    {
                        "tool": r.tool_name,
                        "success": r.success,
                        "result": r.result,
                        "error": r.error,
                        "job_id": r.job_id,
                        "status": r.status,
                    }
                    for r in response.tool_results
                ],
            })
        except Exception as e:
            logger.error("Failed to execute task", task_id=task_id, error=str(e))
            return JSONResponse(
                {"error": "Failed to execute task"},
                status_code=500,
            )
        finally:
            self._shutdown.request_finished()

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
                        "description": "Comma-separated list of user roles (ignored when mTLS auth is enabled).",
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
                "/tasks": {
                    "get": {
                        "summary": "List pending approval tasks",
                        "description": "Get all tasks awaiting human approval.",
                        "operationId": "listTasks",
                        "tags": ["Approval"],
                        "responses": {
                            "200": {
                                "description": "List of pending tasks",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "tasks": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "task_id": {"type": "string"},
                                                            "tool": {"type": "string"},
                                                            "risk": {"type": "string"},
                                                            "status": {"type": "string"},
                                                            "created_at": {"type": "number"},
                                                        },
                                                    },
                                                },
                                                "count": {"type": "integer"},
                                            },
                                        }
                                    }
                                },
                            },
                            "503": {"description": "Service unavailable"},
                        },
                    }
                },
                "/tasks/{task_id}/approve": {
                    "post": {
                        "summary": "Approve a pending task",
                        "description": "Approve a task awaiting human approval, allowing it to proceed.",
                        "operationId": "approveTask",
                        "tags": ["Approval"],
                        "parameters": [
                            {
                                "name": "task_id",
                                "in": "path",
                                "description": "Task identifier",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "X-Approver",
                                "in": "header",
                                "description": "Identity of the approver (ignored when mTLS auth is enabled).",
                                "required": False,
                                "schema": {"type": "string"},
                            },
                        ],
                        "requestBody": {
                            "required": False,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "approver": {
                                                "type": "string",
                                                "description": "Identity of the approver (ignored when mTLS auth is enabled).",
                                            }
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Task approved",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                                "task_id": {"type": "string"},
                                                "approved_by": {"type": "string"},
                                            },
                                        }
                                    }
                                },
                            },
                            "404": {"description": "Task not found or not pending"},
                            "503": {"description": "Service unavailable"},
                        },
                    }
                },
                "/tasks/{task_id}/reject": {
                    "post": {
                        "summary": "Reject a pending task",
                        "description": "Reject a task awaiting human approval, preventing it from proceeding.",
                        "operationId": "rejectTask",
                        "tags": ["Approval"],
                        "parameters": [
                            {
                                "name": "task_id",
                                "in": "path",
                                "description": "Task identifier",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "X-Rejector",
                                "in": "header",
                                "description": "Identity of the rejector (ignored when mTLS auth is enabled).",
                                "required": False,
                                "schema": {"type": "string"},
                            },
                        ],
                        "requestBody": {
                            "required": False,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "rejector": {
                                                "type": "string",
                                                "description": "Identity of the rejector (ignored when mTLS auth is enabled).",
                                            },
                                            "reason": {
                                                "type": "string",
                                                "description": "Reason for rejection",
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Task rejected",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                                "task_id": {"type": "string"},
                                                "rejected_by": {"type": "string"},
                                                "reason": {"type": "string"},
                                            },
                                        }
                                    }
                                },
                            },
                            "404": {"description": "Task not found or not pending"},
                            "503": {"description": "Service unavailable"},
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
                {"name": "Approval", "description": "Human-in-the-loop approval workflow"},
                {"name": "Health", "description": "Health and readiness probes"},
                {"name": "Observability", "description": "Monitoring and metrics"},
            ],
            "components": {
                "securitySchemes": {
                    "mutualTLS": {
                        "type": "mutualTLS",
                        "description": "Client certificate authentication when TWINOPS_AUTH_MODE=mtls.",
                    }
                }
            },
        }
        if self._settings.auth_mode == "mtls":
            openapi_spec["security"] = [{"mutualTLS": []}]
        return JSONResponse(openapi_spec)


def create_app(settings: Settings | None = None) -> Starlette:
    """Create the Starlette application."""
    settings = settings or get_settings()
    _configure_metrics(settings)
    from twinops.common.metrics import MetricsMiddleware, metrics_endpoint
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
        Route("/tasks", server.handle_list_tasks, methods=["GET"]),
        Route("/tasks/{task_id}", server.handle_get_task, methods=["GET"]),
        Route("/tasks/{task_id}/approve", server.handle_approve_task, methods=["POST"]),
        Route("/tasks/{task_id}/reject", server.handle_reject_task, methods=["POST"]),
        Route("/tasks/{task_id}/execute", server.handle_execute_task, methods=["POST"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
        Route("/openapi.json", server.handle_openapi, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)

    app.add_middleware(
        AuthMiddleware,
        settings=settings,
    )

    # Add rate limiting middleware
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=settings.rate_limit_rpm,
        exclude_paths=["/health", "/ready", "/metrics"],
    )

    app.add_middleware(
        MetricsMiddleware,
        exclude_paths=["/health", "/ready", "/metrics"],
    )

    return app


def _configure_metrics(settings: Settings) -> None:
    if settings.agent_workers <= 1:
        return
    if settings.metrics_multiprocess_dir:
        os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", settings.metrics_multiprocess_dir)


def _prepare_multiprocess_dir(settings: Settings) -> None:
    if settings.agent_workers <= 1 or not settings.metrics_multiprocess_dir:
        return
    metrics_dir = Path(settings.metrics_multiprocess_dir)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    for entry in metrics_dir.iterdir():
        if entry.is_file():
            entry.unlink()


def main():
    """Entry point for agent server."""
    setup_logging()
    settings = get_settings()
    workers = max(1, settings.agent_workers)

    if workers > 1 and not settings.metrics_multiprocess_dir:
        logger.warning(
            "metrics_multiprocess_dir not set; /metrics will be per-worker",
            workers=workers,
        )

    _prepare_multiprocess_dir(settings)

    if workers > 1:
        uvicorn.run(
            "twinops.agent.main:create_app",
            host=settings.agent_host,
            port=settings.agent_port,
            log_level="info",
            factory=True,
            workers=workers,
        )
    else:
        app = create_app(settings)
        uvicorn.run(
            app,
            host=settings.agent_host,
            port=settings.agent_port,
            log_level="info",
        )


if __name__ == "__main__":
    main()
