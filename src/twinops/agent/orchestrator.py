"""Agent Orchestrator - Tool execution loop with job monitoring."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from twinops.agent.capabilities import CapabilityIndex
from twinops.agent.llm.base import LlmClient, LlmResponse, Message
from twinops.agent.safety import SafetyKernel, SafetyDecision
from twinops.agent.schema_gen import ToolSpec, tool_spec_to_llm_format
from twinops.agent.shadow import ShadowTwinManager
from twinops.agent.twin_client import TwinClient, TwinClientError
from twinops.common.logging import get_logger
from twinops.common.settings import Settings

logger = get_logger(__name__)


@dataclass
class ToolResult:
    """Result of a tool execution."""

    tool_name: str
    success: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    simulated: bool = False
    job_id: str | None = None
    status: str = "completed"


@dataclass
class AgentResponse:
    """Complete response from agent."""

    reply: str | None
    tool_results: list[ToolResult] = field(default_factory=list)
    pending_approval: bool = False
    task_id: str | None = None


class AgentOrchestrator:
    """
    Main agent orchestration loop.

    Coordinates:
    - LLM interaction for intent understanding
    - Capability index for tool selection
    - Safety kernel for authorization and governance
    - Tool execution with simulation and approval
    - Job monitoring for async operations
    """

    SYSTEM_PROMPT = """You are an AI assistant controlling industrial equipment through a digital twin interface.

You have access to operations that control real equipment. Follow these guidelines:
1. Always provide safety_reasoning explaining why an action is appropriate
2. For high-risk operations, consider using simulate=true first
3. If an interlock or safety check fails, explain the issue to the user
4. Monitor job status for long-running operations

Be concise and focus on the task at hand."""

    def __init__(
        self,
        llm: LlmClient,
        shadow: ShadowTwinManager,
        twin_client: TwinClient,
        safety: SafetyKernel,
        capability_index: CapabilityIndex,
        settings: Settings,
    ):
        """
        Initialize the orchestrator.

        Args:
            llm: LLM client for intent understanding
            shadow: Shadow twin for state access
            twin_client: HTTP client for operations
            safety: Safety kernel for authorization
            capability_index: Tool index for retrieval
            settings: Application settings
        """
        self._llm = llm
        self._shadow = shadow
        self._twin = twin_client
        self._safety = safety
        self._capabilities = capability_index
        self._settings = settings
        self._conversation: list[Message] = []

    async def process_message(
        self,
        user_message: str,
        roles: tuple[str, ...],
    ) -> AgentResponse:
        """
        Process a user message through the full agent loop.

        Args:
            user_message: Natural language input
            roles: User's authorization roles

        Returns:
            AgentResponse with reply and tool results
        """
        logger.info("Processing message", roles=roles)

        # Add user message to conversation
        self._conversation.append(Message(role="user", content=user_message))

        # Retrieve relevant tools
        tools = self._capabilities.search(user_message, top_k=self._settings.capability_top_k)
        tool_schemas = [tool_spec_to_llm_format(hit.tool) for hit in tools]

        logger.debug("Retrieved tools", count=len(tools))

        # Get LLM response
        response = await self._llm.chat(
            messages=self._conversation,
            tools=tool_schemas,
            system=self.SYSTEM_PROMPT,
        )

        # Handle text-only response
        if not response.tool_calls:
            self._conversation.append(Message(role="assistant", content=response.content or ""))
            return AgentResponse(reply=response.content)

        # Execute tool calls
        tool_results: list[ToolResult] = []
        pending_approval = False
        task_id = None

        for call in response.tool_calls:
            result = await self._execute_tool(
                call.name,
                call.arguments,
                roles,
            )
            tool_results.append(result)

            # Check for pending approval
            if result.status == "pending_approval":
                pending_approval = True
                task_id = result.job_id

        # Build response message
        reply = self._build_reply(response.content, tool_results)
        self._conversation.append(Message(role="assistant", content=reply))

        return AgentResponse(
            reply=reply,
            tool_results=tool_results,
            pending_approval=pending_approval,
            task_id=task_id,
        )

    async def _execute_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        roles: tuple[str, ...],
    ) -> ToolResult:
        """Execute a single tool with safety checks."""
        # Find tool spec
        tool = self._capabilities.get_tool_by_name(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

        # Safety evaluation
        decision = await self._safety.evaluate(
            tool_name=tool_name,
            tool_risk=tool.risk_level,
            roles=roles,
            params=params,
        )

        if not decision.allowed:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=decision.reason,
                status="denied",
            )

        # Force simulation if required
        if decision.force_simulation and not params.get("simulate"):
            logger.info("Forcing simulation", tool=tool_name, risk=tool.risk_level)
            params = {**params, "simulate": True}

        # Execute (possibly in simulation)
        try:
            result = await self._invoke_operation(tool, params)
        except Exception as e:
            self._safety.log_error(tool_name, roles, str(e))
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
            )

        simulated = params.get("simulate", False)
        self._safety.log_execution(tool_name, tool.risk_level, roles, result, simulated)

        # Check if approval is required (after simulation)
        if decision.require_approval and not simulated:
            task_id = await self._safety.create_approval_task(
                tool_name=tool_name,
                tool_risk=tool.risk_level,
                roles=roles,
                params=params,
                simulation_result=result if simulated else None,
            )
            return ToolResult(
                tool_name=tool_name,
                success=True,
                result={"message": "Awaiting human approval"},
                job_id=task_id,
                status="pending_approval",
                simulated=simulated,
            )

        # For simulation, indicate it was simulation-only
        if simulated:
            return ToolResult(
                tool_name=tool_name,
                success=True,
                result=result,
                simulated=True,
                status="simulated_only",
            )

        # Check for async job
        job_id = result.get("jobId") or result.get("job_id")
        if job_id:
            # Monitor job completion
            final_result = await self._monitor_job(job_id)
            return ToolResult(
                tool_name=tool_name,
                success=final_result.get("status") == "COMPLETED",
                result=final_result,
                job_id=job_id,
            )

        return ToolResult(
            tool_name=tool_name,
            success=True,
            result=result,
        )

    async def _invoke_operation(
        self,
        tool: ToolSpec,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke an AAS operation."""
        # Filter out safety fields
        input_args = []
        for key, value in params.items():
            if key not in ("simulate", "safety_reasoning"):
                input_args.append({
                    "idShort": key,
                    "value": value,
                })

        simulate = params.get("simulate", False)

        # Use delegation URL if available
        if tool.delegation_url:
            return await self._twin.invoke_delegated_operation(
                tool.delegation_url,
                input_args,
                simulate=simulate,
            )

        # Otherwise use standard invocation
        return await self._twin.invoke_operation(
            tool.submodel_id,
            tool.operation_path,
            input_args,
            client_context={"simulate": simulate},
            async_mode=True,
        )

    async def _monitor_job(self, job_id: str) -> dict[str, Any]:
        """Monitor an async job until completion."""
        policy = await self._safety.load_policy()
        start_time = time.time()

        while time.time() - start_time < self._settings.job_timeout:
            # Get job status from shadow twin
            job_status = await self._shadow.get_property_value(
                policy.job_status_submodel_id,
                policy.job_status_property_path,
            )

            if job_status:
                if isinstance(job_status, str):
                    job_status = json.loads(job_status)

                jobs = job_status.get("jobs", [])
                for job in jobs:
                    if job.get("job_id") == job_id:
                        status = job.get("status", "")
                        if status in ("COMPLETED", "FAILED", "CANCELLED"):
                            return job

            await asyncio.sleep(self._settings.job_poll_interval)

        return {"job_id": job_id, "status": "TIMEOUT"}

    def _build_reply(
        self,
        llm_content: str | None,
        results: list[ToolResult],
    ) -> str:
        """Build a response message from tool results."""
        parts = []

        if llm_content:
            parts.append(llm_content)

        for result in results:
            if result.success:
                if result.simulated:
                    parts.append(
                        f"Simulation completed for '{result.tool_name}'. "
                        "To execute for real, re-issue the command with simulate=false."
                    )
                elif result.status == "pending_approval":
                    parts.append(
                        f"Operation '{result.tool_name}' requires human approval. "
                        f"Task ID: {result.job_id}"
                    )
                else:
                    parts.append(f"Executed '{result.tool_name}' successfully.")
            else:
                parts.append(f"Failed to execute '{result.tool_name}': {result.error}")

        return " ".join(parts) if parts else "No response generated."

    async def wait_for_approval(self, task_id: str) -> AgentResponse:
        """
        Wait for a pending approval task and execute if approved.

        Args:
            task_id: Task identifier

        Returns:
            AgentResponse with result
        """
        approved, reason = await self._safety.wait_for_approval(
            task_id,
            timeout=self._settings.approval_timeout,
        )

        if approved:
            # Task was approved - would need to re-execute
            # For now, return approval confirmation
            return AgentResponse(
                reply=f"Task {task_id} was approved. Operation can proceed.",
            )
        else:
            return AgentResponse(
                reply=f"Task {task_id} was not approved: {reason}",
            )

    def reset_conversation(self) -> None:
        """Clear conversation history."""
        self._conversation = []


class AgentOrchestratorBuilder:
    """Builder for AgentOrchestrator with dependency injection."""

    def __init__(self, settings: Settings):
        """Initialize builder with settings."""
        self._settings = settings
        self._llm: LlmClient | None = None
        self._shadow: ShadowTwinManager | None = None
        self._twin: TwinClient | None = None
        self._safety: SafetyKernel | None = None
        self._capabilities: CapabilityIndex | None = None

    def with_llm(self, llm: LlmClient) -> "AgentOrchestratorBuilder":
        """Set LLM client."""
        self._llm = llm
        return self

    def with_shadow(self, shadow: ShadowTwinManager) -> "AgentOrchestratorBuilder":
        """Set shadow twin manager."""
        self._shadow = shadow
        return self

    def with_twin_client(self, twin: TwinClient) -> "AgentOrchestratorBuilder":
        """Set twin client."""
        self._twin = twin
        return self

    def with_safety(self, safety: SafetyKernel) -> "AgentOrchestratorBuilder":
        """Set safety kernel."""
        self._safety = safety
        return self

    def with_capabilities(self, capabilities: CapabilityIndex) -> "AgentOrchestratorBuilder":
        """Set capability index."""
        self._capabilities = capabilities
        return self

    def build(self) -> AgentOrchestrator:
        """Build the orchestrator."""
        if not all([self._llm, self._shadow, self._twin, self._safety, self._capabilities]):
            raise ValueError("All dependencies must be set before building")

        return AgentOrchestrator(
            llm=self._llm,
            shadow=self._shadow,
            twin_client=self._twin,
            safety=self._safety,
            capability_index=self._capabilities,
            settings=self._settings,
        )
