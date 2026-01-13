"""Agent HTTP server entry point."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from twinops.agent.capabilities import CapabilityIndex
from twinops.agent.llm.factory import create_llm_client
from twinops.agent.orchestrator import AgentOrchestrator
from twinops.agent.safety import AuditLogger, SafetyKernel
from twinops.agent.schema_gen import generate_all_tool_schemas
from twinops.agent.shadow import ShadowTwinManager
from twinops.agent.twin_client import TwinClient
from twinops.common.logging import get_logger, setup_logging
from twinops.common.mqtt import MqttClient
from twinops.common.settings import Settings, get_settings

logger = get_logger(__name__)


class AgentServer:
    """HTTP server wrapper for the agent."""

    def __init__(self, settings: Settings):
        """Initialize server components."""
        self._settings = settings
        self._orchestrator: AgentOrchestrator | None = None
        self._twin_client: TwinClient | None = None
        self._mqtt_client: MqttClient | None = None
        self._shadow: ShadowTwinManager | None = None

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
            safety = SafetyKernel(
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
                safety=safety,
                capability_index=capabilities,
                settings=self._settings,
            )

            logger.info("Agent server ready")

    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._twin_client:
            await self._twin_client.__aexit__(None, None, None)

    async def handle_chat(self, request: Request) -> JSONResponse:
        """Handle chat endpoint."""
        if not self._orchestrator:
            return JSONResponse(
                {"error": "Server not initialized"},
                status_code=503,
            )

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

    async def handle_health(self, request: Request) -> JSONResponse:
        """Health check endpoint."""
        return JSONResponse({
            "status": "healthy",
            "initialized": self._orchestrator is not None,
            "shadow_events": self._shadow.event_count if self._shadow else 0,
        })

    async def handle_reset(self, request: Request) -> JSONResponse:
        """Reset conversation history."""
        if self._orchestrator:
            self._orchestrator.reset_conversation()
        return JSONResponse({"status": "conversation reset"})


def create_app(settings: Settings | None = None) -> Starlette:
    """Create the Starlette application."""
    settings = settings or get_settings()
    server = AgentServer(settings)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await server.startup()
        yield
        await server.shutdown()

    routes = [
        Route("/chat", server.handle_chat, methods=["POST"]),
        Route("/health", server.handle_health, methods=["GET"]),
        Route("/reset", server.handle_reset, methods=["POST"]),
    ]

    return Starlette(routes=routes, lifespan=lifespan)


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
