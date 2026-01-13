"""Rules-based LLM fallback for operation without API keys."""

import re
import uuid
from collections.abc import Callable
from typing import Any

from twinops.agent.llm.base import LlmClient, LlmResponse, Message, ToolCall
from twinops.common.logging import get_logger

logger = get_logger(__name__)


# Common prefixes and suffixes to strip from user messages
STRIP_PREFIXES = [
    r"^(?:please\s+)?(?:can\s+you\s+)?(?:could\s+you\s+)?(?:would\s+you\s+)?",
    r"^(?:i\s+want\s+(?:you\s+)?to\s+)?",
    r"^(?:i\s+need\s+(?:you\s+)?to\s+)?",
    r"^(?:i'd\s+like\s+(?:you\s+)?to\s+)?",
]


def normalize_message(msg: str) -> str:
    """Normalize user message by stripping common prefixes."""
    result = msg.lower().strip()
    for prefix in STRIP_PREFIXES:
        result = re.sub(prefix, "", result)
    return result.strip()


def fuzzy_match_tool(tool_name: str, available_tools: dict[str, Any]) -> str | None:
    """
    Try to fuzzy match a tool name against available tools.

    Returns the matched tool name or None.
    """
    # Direct match
    if tool_name in available_tools:
        return tool_name

    # Case-insensitive match
    tool_lower = tool_name.lower()
    for name in available_tools:
        if name.lower() == tool_lower:
            return name

    # Partial match (tool name contains or is contained in available tool)
    for name in available_tools:
        name_lower = name.lower()
        if tool_lower in name_lower or name_lower in tool_lower:
            return name

    # Word-based match
    tool_words = set(re.findall(r"[a-z]+", tool_lower))
    best_match = None
    best_score = 0
    for name in available_tools:
        name_words = set(re.findall(r"[a-z]+", name.lower()))
        overlap = len(tool_words & name_words)
        if overlap > best_score:
            best_score = overlap
            best_match = name

    if best_score > 0:
        return best_match

    return None


class RulesBasedClient(LlmClient):
    """
    Enhanced rules-based "LLM" for testing without API keys.

    Parses natural language commands into tool calls using pattern matching.
    Supports:
    - Multiple phrasings (please, can you, etc.)
    - Fuzzy tool matching
    - Generic set/get/call patterns
    - Value extraction with units

    Useful for:
    - Local development without API keys
    - Testing the safety/execution pipeline
    - Demonstrating the architecture
    """

    # Specific patterns for common operations
    SPECIFIC_PATTERNS: list[tuple[str, str, Callable[[re.Match[str]], dict[str, Any]]]] = [
        # Speed control
        (
            r"set\s+(?:the\s+)?(?:pump\s+)?speed\s+(?:to\s+)?(\d+(?:\.\d+)?)",
            "SetSpeed",
            lambda m: {"RPM": float(m.group(1))},
        ),
        (
            r"change\s+(?:the\s+)?speed\s+(?:to\s+)?(\d+(?:\.\d+)?)",
            "SetSpeed",
            lambda m: {"RPM": float(m.group(1))},
        ),
        (r"speed\s+(?:to\s+)?(\d+(?:\.\d+)?)", "SetSpeed", lambda m: {"RPM": float(m.group(1))}),
        # Pump control
        (r"(?:turn\s+on|start|activate|enable)\s+(?:the\s+)?pump", "StartPump", lambda _m: {}),
        (r"(?:turn\s+off|stop|deactivate|disable)\s+(?:the\s+)?pump", "StopPump", lambda _m: {}),
        (r"pump\s+(?:on|start)", "StartPump", lambda _m: {}),
        (r"pump\s+(?:off|stop)", "StopPump", lambda _m: {}),
        # Temperature control
        (
            r"set\s+(?:the\s+)?temp(?:erature)?\s+(?:to\s+)?(\d+(?:\.\d+)?)",
            "SetTemperature",
            lambda m: {"Temperature": float(m.group(1))},
        ),
        (
            r"change\s+(?:the\s+)?temp(?:erature)?\s+(?:to\s+)?(\d+(?:\.\d+)?)",
            "SetTemperature",
            lambda m: {"Temperature": float(m.group(1))},
        ),
        (
            r"temp(?:erature)?\s+(?:to\s+)?(\d+(?:\.\d+)?)",
            "SetTemperature",
            lambda m: {"Temperature": float(m.group(1))},
        ),
        # Status queries
        (
            r"(?:get|show|check|display|what(?:'s|\s+is)?)\s+(?:the\s+)?(?:current\s+)?status",
            "GetStatus",
            lambda _m: {},
        ),
        (r"status\s+(?:report|check|info)", "GetStatus", lambda _m: {}),
        (r"how\s+(?:is|are)\s+(?:things|it)", "GetStatus", lambda _m: {}),
        # Temperature reading
        (
            r"(?:read|get|show|what(?:'s|\s+is)?)\s+(?:the\s+)?(?:current\s+)?temp(?:erature)?",
            "ReadTemperature",
            lambda _m: {},
        ),
        (r"temp(?:erature)?\s+reading", "ReadTemperature", lambda _m: {}),
        # Emergency
        (r"emergency\s+(?:stop|shutdown|halt)", "EmergencyStop", lambda _m: {}),
        (r"e-stop|estop", "EmergencyStop", lambda _m: {}),
        (r"(?:immediate(?:ly)?|urgent)\s+stop", "EmergencyStop", lambda _m: {}),
    ]

    # Generic patterns for any tool
    GENERIC_PATTERNS: list[tuple[str, Callable[[re.Match[str]], tuple[str, dict[str, Any]]]]] = [
        # "call <operation>" or "run <operation>" or "execute <operation>"
        (r"(?:call|run|execute|invoke)\s+(\w+)", lambda m: (m.group(1), {})),
        # "set <property> to <value>"
        (
            r"set\s+(\w+)\s+(?:to\s+)?(\d+(?:\.\d+)?)",
            lambda m: (f"Set{m.group(1).title()}", {m.group(1).title(): float(m.group(2))}),
        ),
        # "get <property>" or "read <property>"
        (r"(?:get|read|show)\s+(\w+)", lambda m: (f"Read{m.group(1).title()}", {})),
    ]

    def __init__(self) -> None:
        """Initialize the rules-based client."""
        logger.info("Using rules-based LLM client (no API key)")

    def _extract_simulate_flag(self, msg: str) -> bool:
        """Extract simulation flag from message."""
        # Explicit simulate=false overrides
        if "simulate=false" in msg.lower() or "real" in msg.lower():
            return False
        # Check for simulate request
        return "simulate" in msg.lower() or "dry run" in msg.lower() or "test" in msg.lower()

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """
        Parse the last user message and generate tool calls.

        Uses enhanced pattern matching to extract commands from natural language.
        Supports:
        - Common phrase normalization (please, can you, etc.)
        - Specific operation patterns
        - Generic set/get/call patterns
        - Fuzzy tool name matching
        """
        # System prompt is not used by the rules-based client.
        _ = system
        # Find last user message
        user_msg = None
        for msg in reversed(messages):
            if msg.role == "user":
                user_msg = msg.content
                break

        if not user_msg:
            return LlmResponse(
                content="I didn't receive a message to process.",
                finish_reason="stop",
            )

        # Normalize the message (strip common prefixes)
        normalized = normalize_message(user_msg)

        # Check for simulation flag
        simulate = self._extract_simulate_flag(user_msg)

        # Build available tools map
        available_tools = {t["name"]: t for t in (tools or [])}
        tool_calls = []

        # Try specific patterns first (highest priority)
        for pattern, tool_name, extractor in self.SPECIFIC_PATTERNS:
            match = re.search(pattern, normalized)
            if match:
                # Check if tool exists (with fuzzy matching)
                matched_tool = fuzzy_match_tool(tool_name, available_tools)
                if matched_tool:
                    args = extractor(match)
                    args["simulate"] = simulate
                    args["safety_reasoning"] = f"Matched specific pattern for {tool_name}"

                    tool_calls.append(
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            name=matched_tool,
                            arguments=args,
                        )
                    )
                    break

        # If no specific pattern matched, try generic patterns
        if not tool_calls:
            for pattern, generic_extractor in self.GENERIC_PATTERNS:
                match = re.search(pattern, normalized)
                if match:
                    tool_name, args = generic_extractor(match)
                    # Try fuzzy matching for the extracted tool name
                    matched_tool = fuzzy_match_tool(tool_name, available_tools)
                    if matched_tool:
                        args["simulate"] = simulate
                        args["safety_reasoning"] = (
                            f"Matched generic pattern, resolved to {matched_tool}"
                        )

                        tool_calls.append(
                            ToolCall(
                                id=f"call_{uuid.uuid4().hex[:8]}",
                                name=matched_tool,
                                arguments=args,
                            )
                        )
                        break

        if tool_calls:
            return LlmResponse(
                content=None,
                tool_calls=tool_calls,
                finish_reason="tool_use",
            )

        # No pattern matched - provide helpful response
        available = ", ".join(sorted(available_tools.keys())) if available_tools else "none loaded"
        return LlmResponse(
            content=f"I couldn't understand that command. Available operations: {available}. "
            "Try commands like 'start pump', 'set speed to 1200', 'get status', or 'stop pump'.",
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
        _ = (tools, system)
        last_msg = messages[-1] if messages else None
        content = f"Echo: {last_msg.content}" if last_msg else "No message"
        return LlmResponse(content=content, finish_reason="stop")

    async def close(self) -> None:
        """No resources to clean up."""
        pass
