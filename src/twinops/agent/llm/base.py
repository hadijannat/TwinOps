"""Base classes for LLM integration."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Message:
    """Chat message."""

    role: Literal["user", "assistant", "system"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ToolCall:
    """LLM tool call request."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmResponse:
    """Response from LLM."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


class LlmClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """
        Send a chat completion request.

        Args:
            messages: Conversation history
            tools: Available tools in LLM format
            system: System prompt

        Returns:
            LlmResponse with content and/or tool calls
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        pass
