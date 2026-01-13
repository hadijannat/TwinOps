"""Rules-based LLM fallback for operation without API keys."""

import re
import uuid
from typing import Any

from twinops.agent.llm.base import LlmClient, LlmResponse, Message, ToolCall
from twinops.common.logging import get_logger

logger = get_logger(__name__)


class RulesBasedClient(LlmClient):
    """
    Simple rules-based "LLM" for testing without API keys.

    Parses natural language commands into tool calls using pattern matching.
    Useful for:
    - Local development without API keys
    - Testing the safety/execution pipeline
    - Demonstrating the architecture
    """

    # Patterns for extracting commands
    PATTERNS = [
        # "set speed to 1200 RPM"
        (r"set\s+speed\s+(?:to\s+)?(\d+(?:\.\d+)?)", "SetSpeed", lambda m: {"RPM": float(m.group(1))}),
        # "start pump" / "start the pump"
        (r"start\s+(?:the\s+)?pump", "StartPump", lambda m: {}),
        # "stop pump" / "stop the pump"
        (r"stop\s+(?:the\s+)?pump", "StopPump", lambda m: {}),
        # "set temperature to 75"
        (r"set\s+temp(?:erature)?\s+(?:to\s+)?(\d+(?:\.\d+)?)", "SetTemperature", lambda m: {"Temperature": float(m.group(1))}),
        # "get status" / "show status"
        (r"(?:get|show|check)\s+status", "GetStatus", lambda m: {}),
        # "read temperature"
        (r"(?:read|get|show)\s+temp(?:erature)?", "ReadTemperature", lambda m: {}),
        # "emergency stop"
        (r"emergency\s+stop", "EmergencyStop", lambda m: {}),
    ]

    def __init__(self):
        """Initialize the rules-based client."""
        logger.info("Using rules-based LLM client (no API key)")

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """
        Parse the last user message and generate tool calls.

        Uses pattern matching to extract commands from natural language.
        """
        # Find last user message
        user_msg = None
        for msg in reversed(messages):
            if msg.role == "user":
                user_msg = msg.content.lower()
                break

        if not user_msg:
            return LlmResponse(
                content="I didn't receive a message to process.",
                finish_reason="stop",
            )

        # Check for simulation flag in message
        simulate = "simulate" in user_msg and "simulate=false" not in user_msg.lower()

        # Try to match patterns
        tool_calls = []
        available_tools = {t["name"]: t for t in (tools or [])}

        for pattern, tool_name, extractor in self.PATTERNS:
            match = re.search(pattern, user_msg)
            if match and tool_name in available_tools:
                args = extractor(match)
                args["simulate"] = simulate
                args["safety_reasoning"] = "Matched command pattern in user request"

                tool_calls.append(ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=tool_name,
                    arguments=args,
                ))
                break

        if tool_calls:
            return LlmResponse(
                content=None,
                tool_calls=tool_calls,
                finish_reason="tool_use",
            )

        # No pattern matched - provide helpful response
        available = ", ".join(available_tools.keys()) if available_tools else "none loaded"
        return LlmResponse(
            content=f"I couldn't understand that command. Available operations: {available}. "
                    "Try commands like 'start pump', 'set speed to 1200', or 'stop pump'.",
            finish_reason="stop",
        )

    async def close(self) -> None:
        """No resources to clean up."""
        pass


class EchoClient(LlmClient):
    """
    Echo client that returns the input as output.

    Useful for testing message flow.
    """

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """Echo the last message."""
        last_msg = messages[-1] if messages else None
        content = f"Echo: {last_msg.content}" if last_msg else "No message"
        return LlmResponse(content=content, finish_reason="stop")

    async def close(self) -> None:
        """No resources to clean up."""
        pass
