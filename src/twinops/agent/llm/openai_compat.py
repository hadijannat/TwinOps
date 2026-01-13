"""OpenAI and Anthropic compatible LLM clients."""

import json
from typing import Any

from twinops.agent.llm.base import LlmClient, LlmResponse, Message, ToolCall
from twinops.common.logging import get_logger

logger = get_logger(__name__)


class AnthropicClient(LlmClient):
    """Anthropic Claude API client."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        timeout: float = 30.0,
    ):
        """
        Initialize Anthropic client.

        Args:
            api_key: Anthropic API key
            model: Model identifier
            max_tokens: Maximum tokens in response
            timeout: Request timeout in seconds
        """
        import anthropic

        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout,
        )
        self._model = model
        self._max_tokens = max_tokens

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """Send chat completion request to Anthropic."""
        # Convert messages to Anthropic format
        anthropic_messages = []
        for msg in messages:
            if msg.role == "system":
                continue  # Handled separately
            anthropic_messages.append(
                {
                    "role": msg.role,
                    "content": msg.content,
                }
            )

        # Convert tools to Anthropic format
        anthropic_tools = None
        if tools:
            anthropic_tools = []
            for t in tools:
                anthropic_tools.append(
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "input_schema": t.get("input_schema", t.get("parameters", {})),
                    }
                )

        # Build request
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": anthropic_messages,
        }

        if system:
            kwargs["system"] = system

        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        # Send request
        response = await self._client.messages.create(**kwargs)

        # Parse response
        content = None
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        return LlmResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=response.stop_reason,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()


class OpenAIClient(LlmClient):
    """OpenAI API client."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4-turbo-preview",
        max_tokens: int = 4096,
        timeout: float = 30.0,
    ):
        """
        Initialize OpenAI client.

        Args:
            api_key: OpenAI API key
            model: Model identifier
            max_tokens: Maximum tokens in response
            timeout: Request timeout in seconds
        """
        import openai

        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            timeout=timeout,
        )
        self._model = model
        self._max_tokens = max_tokens

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LlmResponse:
        """Send chat completion request to OpenAI."""
        # Convert messages to OpenAI format
        openai_messages = []

        if system:
            openai_messages.append({"role": "system", "content": system})

        for msg in messages:
            openai_messages.append(
                {
                    "role": msg.role,
                    "content": msg.content,
                }
            )

        # Convert tools to OpenAI format
        openai_tools = None
        if tools:
            openai_tools = []
            for t in tools:
                openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", t.get("input_schema", {})),
                        },
                    }
                )

        # Build request
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": openai_messages,
        }

        if openai_tools:
            kwargs["tools"] = openai_tools

        # Send request
        response = await self._client.chat.completions.create(**kwargs)

        # Parse response
        choice = response.choices[0]
        content = choice.message.content
        tool_calls = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Invalid tool arguments JSON from OpenAI",
                            tool_name=tc.function.name,
                            tool_call_id=tc.id,
                        )
                        args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                    )
                )

        usage = None
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }

        return LlmResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()
