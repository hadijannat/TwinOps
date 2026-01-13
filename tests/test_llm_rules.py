"""Tests for rules-based LLM client."""

import pytest

from twinops.agent.llm.base import Message
from twinops.agent.llm.rules import RulesBasedClient


@pytest.fixture
def rules_client():
    """Create rules-based client."""
    return RulesBasedClient()


@pytest.fixture
def sample_tools():
    """Sample tool definitions."""
    return [
        {
            "name": "StartPump",
            "description": "Start the pump",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "StopPump",
            "description": "Stop the pump",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "SetSpeed",
            "description": "Set pump speed",
            "input_schema": {
                "type": "object",
                "properties": {"RPM": {"type": "number"}},
            },
        },
        {
            "name": "GetStatus",
            "description": "Get pump status",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


class TestRulesBasedClient:
    """Test rules-based LLM client."""

    @pytest.mark.asyncio
    async def test_start_pump_command(self, rules_client, sample_tools):
        """Test parsing start pump command."""
        messages = [Message(role="user", content="start the pump")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "StartPump"

    @pytest.mark.asyncio
    async def test_stop_pump_command(self, rules_client, sample_tools):
        """Test parsing stop pump command."""
        messages = [Message(role="user", content="stop pump")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "StopPump"

    @pytest.mark.asyncio
    async def test_set_speed_command(self, rules_client, sample_tools):
        """Test parsing set speed command with parameter."""
        messages = [Message(role="user", content="set speed to 1200")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "SetSpeed"
        assert response.tool_calls[0].arguments["RPM"] == 1200.0

    @pytest.mark.asyncio
    async def test_set_speed_with_rpm(self, rules_client, sample_tools):
        """Test parsing set speed with RPM unit."""
        messages = [Message(role="user", content="set speed to 2500 RPM")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].arguments["RPM"] == 2500.0

    @pytest.mark.asyncio
    async def test_get_status_command(self, rules_client, sample_tools):
        """Test parsing status command."""
        messages = [Message(role="user", content="get status")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "GetStatus"

    @pytest.mark.asyncio
    async def test_show_status_command(self, rules_client, sample_tools):
        """Test alternative status command."""
        messages = [Message(role="user", content="show status")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "GetStatus"

    @pytest.mark.asyncio
    async def test_simulate_flag(self, rules_client, sample_tools):
        """Test simulation flag extraction."""
        messages = [Message(role="user", content="simulate start pump")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert response.tool_calls[0].arguments["simulate"] is True

    @pytest.mark.asyncio
    async def test_no_simulate_by_default(self, rules_client, sample_tools):
        """Test no simulation by default."""
        messages = [Message(role="user", content="start pump")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert response.tool_calls[0].arguments["simulate"] is False

    @pytest.mark.asyncio
    async def test_unrecognized_command(self, rules_client, sample_tools):
        """Test handling unrecognized commands."""
        messages = [Message(role="user", content="do something random")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 0
        assert response.content is not None
        assert "couldn't understand" in response.content.lower()

    @pytest.mark.asyncio
    async def test_case_insensitive(self, rules_client, sample_tools):
        """Test case insensitivity."""
        messages = [Message(role="user", content="START THE PUMP")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "StartPump"

    @pytest.mark.asyncio
    async def test_safety_reasoning_added(self, rules_client, sample_tools):
        """Test safety reasoning is added to arguments."""
        messages = [Message(role="user", content="start pump")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert "safety_reasoning" in response.tool_calls[0].arguments

    @pytest.mark.asyncio
    async def test_no_user_message(self, rules_client, sample_tools):
        """Test handling no user message."""
        messages = [Message(role="assistant", content="Hello")]

        response = await rules_client.chat(messages, tools=sample_tools)

        assert len(response.tool_calls) == 0
        assert response.content is not None

    @pytest.mark.asyncio
    async def test_close(self, rules_client):
        """Test close is a no-op."""
        await rules_client.close()  # Should not raise
