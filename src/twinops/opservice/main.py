"""Operation Service - Handles delegated AAS operations."""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from twinops.common.auth import AuthMiddleware
from twinops.common.logging import get_logger, setup_logging
from twinops.common.metrics import MetricsMiddleware, metrics_endpoint
from twinops.common.mqtt import MqttClient
from twinops.common.ratelimit import RateLimitMiddleware
from twinops.common.settings import Settings, get_settings
from twinops.common.tracing import setup_tracing

logger = get_logger(__name__)


@dataclass
class Job:
    """Async operation job."""

    job_id: str
    operation: str
    status: str = "INITIATED"
    progress: int = 0
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class OperationExecutor:
    """Simulated operation executor."""

    def __init__(self):
        """Initialize executor."""
        self._jobs: dict[str, Job] = {}
        self._state = {
            "pump_running": False,
            "pump_speed": 0.0,
            "temperature": 25.0,
        }

    async def execute(
        self,
        operation: str,
        input_args: list[dict[str, Any]],
        simulate: bool = False,
    ) -> dict[str, Any]:
        """
        Execute an operation.

        Args:
            operation: Operation name
            input_args: Input arguments
            simulate: Whether to run in simulation mode

        Returns:
            Result or job reference
        """
        # Convert args to dict
        args = {arg["idShort"]: arg["value"] for arg in input_args}

        if simulate:
            return await self._simulate(operation, args)

        # Create async job
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        job = Job(job_id=job_id, operation=operation)
        self._jobs[job_id] = job

        # Start async execution
        asyncio.create_task(self._execute_async(job, args))

        return {
            "executionState": "Initiated",
            "jobId": job_id,
        }

    async def _simulate(
        self,
        operation: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Simulate an operation without affecting state."""
        result = {
            "executionState": "Completed",
            "simulationResult": {},
        }

        if operation == "StartPump":
            result["simulationResult"] = {
                "predictedState": "Running",
                "estimatedTime": 2.5,
                "confidence": 0.95,
                "warnings": [] if not self._state["pump_running"] else ["Pump is already running"],
            }

        elif operation == "StopPump":
            result["simulationResult"] = {
                "predictedState": "Stopped",
                "estimatedTime": 1.5,
                "confidence": 0.98,
                "warnings": [] if self._state["pump_running"] else ["Pump is already stopped"],
            }

        elif operation == "SetSpeed":
            target_rpm = args.get("RPM", 0)
            current_rpm = self._state["pump_speed"]
            ramp_time = abs(target_rpm - current_rpm) / 500  # 500 RPM/s ramp rate
            result["simulationResult"] = {
                "predictedState": f"Running at {target_rpm} RPM",
                "estimatedTime": ramp_time,
                "currentSpeed": current_rpm,
                "targetSpeed": target_rpm,
                "confidence": 0.92,
                "warnings": ["High speed operation" if target_rpm > 3000 else None],
            }
            result["simulationResult"]["warnings"] = [w for w in result["simulationResult"]["warnings"] if w]

        elif operation == "GetStatus":
            result["simulationResult"] = {
                "status": self._state,
                "confidence": 1.0,
            }

        else:
            result["simulationResult"] = {
                "message": f"Simulation not implemented for {operation}",
                "confidence": 0.5,
            }

        return result

    async def _execute_async(self, job: Job, args: dict[str, Any]) -> None:
        """Execute operation asynchronously."""
        try:
            job.status = "RUNNING"

            if job.operation == "StartPump":
                await self._start_pump(job)

            elif job.operation == "StopPump":
                await self._stop_pump(job)

            elif job.operation == "SetSpeed":
                await self._set_speed(job, args.get("RPM", 0))

            elif job.operation == "GetStatus":
                job.result = {"status": dict(self._state)}
                job.status = "COMPLETED"

            else:
                job.error = f"Unknown operation: {job.operation}"
                job.status = "FAILED"

            job.completed_at = time.time()

        except Exception as e:
            job.status = "FAILED"
            job.error = str(e)
            job.completed_at = time.time()
            logger.error("Operation failed", job_id=job.job_id, error=str(e))

    async def _start_pump(self, job: Job) -> None:
        """Simulate pump startup sequence."""
        for progress in [20, 40, 60, 80, 100]:
            job.progress = progress
            await asyncio.sleep(0.5)

        self._state["pump_running"] = True
        self._state["pump_speed"] = 1000.0  # Default speed
        job.result = {"state": "Running", "speed": 1000.0}
        job.status = "COMPLETED"

    async def _stop_pump(self, job: Job) -> None:
        """Simulate pump shutdown sequence."""
        initial_speed = self._state["pump_speed"]
        steps = 5
        for i in range(steps):
            job.progress = (i + 1) * 100 // steps
            self._state["pump_speed"] = initial_speed * (1 - (i + 1) / steps)
            await asyncio.sleep(0.3)

        self._state["pump_running"] = False
        self._state["pump_speed"] = 0.0
        job.result = {"state": "Stopped", "speed": 0.0}
        job.status = "COMPLETED"

    async def _set_speed(self, job: Job, target_rpm: float) -> None:
        """Simulate speed change."""
        current = self._state["pump_speed"]
        diff = target_rpm - current
        steps = max(5, int(abs(diff) / 200))

        for i in range(steps):
            job.progress = (i + 1) * 100 // steps
            self._state["pump_speed"] = current + diff * (i + 1) / steps
            await asyncio.sleep(0.2)

        self._state["pump_speed"] = target_rpm
        job.result = {"state": "Running", "speed": target_rpm}
        job.status = "COMPLETED"

    def get_job(self, job_id: str) -> Job | None:
        """Get job by ID."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[Job]:
        """Get all jobs."""
        return list(self._jobs.values())


class OperationServer:
    """HTTP server for operation service."""

    def __init__(self, settings: Settings):
        """Initialize server."""
        self._settings = settings
        self._executor = OperationExecutor()

    async def startup(self) -> None:
        """Initialize components."""
        logger.info("Starting operation service...")

    async def shutdown(self) -> None:
        """Clean up resources."""
        pass

    async def handle_invoke(self, request: Request) -> JSONResponse:
        """Handle operation invocation."""
        operation = request.path_params.get("operation", "")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        input_args = body.get("inputArguments", [])
        client_context = body.get("clientContext", {})
        simulate = client_context.get("simulate", False)

        logger.info(
            "Operation invoked",
            operation=operation,
            simulate=simulate,
            args=input_args,
        )

        result = await self._executor.execute(operation, input_args, simulate)

        status_code = 200 if simulate else 202
        return JSONResponse(result, status_code=status_code)

    async def handle_get_job(self, request: Request) -> JSONResponse:
        """Get job status."""
        job_id = request.path_params.get("job_id", "")
        job = self._executor.get_job(job_id)

        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)

        return JSONResponse({
            "job_id": job.job_id,
            "operation": job.operation,
            "status": job.status,
            "progress": job.progress,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "result": job.result,
            "error": job.error,
        })

    async def handle_list_jobs(self, request: Request) -> JSONResponse:
        """List all jobs."""
        jobs = self._executor.get_all_jobs()
        return JSONResponse({
            "jobs": [
                {
                    "job_id": j.job_id,
                    "operation": j.operation,
                    "status": j.status,
                    "progress": j.progress,
                    "started_at": j.started_at,
                }
                for j in jobs
            ]
        })

    async def handle_health(self, request: Request) -> JSONResponse:
        """Health check."""
        return JSONResponse({"status": "healthy"})


def create_app(settings: Settings | None = None) -> Starlette:
    """Create the Starlette application."""
    settings = settings or get_settings()
    server = OperationServer(settings)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await server.startup()
        yield
        await server.shutdown()

    routes = [
        Route("/operations/{operation}", server.handle_invoke, methods=["POST"]),
        Route("/jobs", server.handle_list_jobs, methods=["GET"]),
        Route("/jobs/{job_id}", server.handle_get_job, methods=["GET"]),
        Route("/health", server.handle_health, methods=["GET"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)

    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=settings.rate_limit_rpm,
        exclude_paths=["/health", "/metrics"],
    )
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(
        MetricsMiddleware,
        exclude_paths=["/health", "/metrics"],
    )

    return app


def main():
    """Entry point for operation service."""
    setup_logging()
    settings = get_settings()
    if settings.tracing_enabled or settings.tracing_otlp_endpoint or settings.tracing_console:
        setup_tracing(
            service_name=settings.tracing_service_name or "twinops-opservice",
            otlp_endpoint=settings.tracing_otlp_endpoint,
            enable_console=settings.tracing_console,
        )
    app = create_app(settings)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.opservice_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
