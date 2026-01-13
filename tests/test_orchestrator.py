"""Tests for agent orchestrator."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from twinops.agent.orchestrator import AgentOrchestrator
from twinops.agent.schema_gen import ToolSpec


@pytest.fixture
def mock_llm():
    """Mock LLM client."""
    llm = AsyncMock()
    llm.chat = AsyncMock()
    return llm


@pytest.fixture
def mock_shadow():
    """Mock shadow twin manager."""
    shadow = MagicMock()
    shadow.get_state = MagicMock(return_value={"aas": {}, "submodels": {}})
    shadow.get_operations = AsyncMock(return_value=[])
    shadow.event_count = 0
    return shadow


@pytest.fixture
def mock_safety():
    """Mock safety kernel."""
    safety = AsyncMock()
    safety.evaluate = AsyncMock()
    safety._pending_approvals = {}
    return safety


@pytest.fixture
def mock_capabilities():
    """Mock capability index."""
    capabilities = MagicMock()
    capabilities.get_relevant_tools = MagicMock(return_value=[])
    capabilities.get_tool_by_name = MagicMock(return_value=None)
    return capabilities


@pytest.fixture
def orchestrator(settings, mock_llm, mock_shadow, mock_twin_client, mock_safety, mock_capabilities):
    """Create orchestrator with mocked dependencies."""
    return AgentOrchestrator(
        llm=mock_llm,
        shadow=mock_shadow,
        twin_client=mock_twin_client,
        safety=mock_safety,
        capability_index=mock_capabilities,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_simple_query_no_tools(orchestrator, mock_llm):
    """Test query that doesn't require tools."""
    # Configure mock for simple response
    mock_response = MagicMock()
    mock_response.content = "The current pump speed is 1000 RPM."
    mock_response.tool_calls = []
    mock_llm.chat.return_value = mock_response

    response = await orchestrator.process_message(
        "What is the pump speed?",
        roles=("operator",)
    )

    assert response.reply is not None
    assert "1000" in response.reply or "speed" in response.reply.lower()
    assert response.tool_results == []
    assert response.pending_approval is False


@pytest.mark.asyncio
async def test_tool_execution_with_safety_allowed(
    orchestrator, mock_llm, mock_safety, mock_capabilities, mock_twin_client
):
    """Test tool execution when safety allows."""
    # Configure tool schema as ToolSpec object
    tool_spec = ToolSpec(
        name="SetSpeed",
        description="Set pump speed",
        input_schema={"properties": {"RPM": {"type": "number"}}, "required": ["RPM"]},
        submodel_id="urn:test:submodel:control",
        operation_path="SetSpeed",
        risk_level="HIGH",
        delegation_url="http://opservice:8087/operations/SetSpeed",
    )
    mock_capabilities.get_tool_by_name.return_value = tool_spec

    # Configure LLM to request tool call
    tool_call = MagicMock()
    tool_call.name = "SetSpeed"
    tool_call.arguments = {"RPM": 1500}

    llm_response = MagicMock()
    llm_response.content = None
    llm_response.tool_calls = [tool_call]
    mock_llm.chat.return_value = llm_response

    # Configure safety to allow with simulation
    safety_result = MagicMock()
    safety_result.allowed = True
    safety_result.force_simulation = True
    safety_result.require_approval = False
    safety_result.reason = None
    mock_safety.evaluate.return_value = safety_result

    # Configure twin client response
    mock_twin_client.invoke_delegated_operation.return_value = {
        "executionState": "Completed",
        "outputArguments": [{"value": {"value": "ok"}}],
    }

    # Second LLM call for final response
    final_response = MagicMock()
    final_response.content = "Speed has been set to 1500 RPM (simulated)."
    final_response.tool_calls = []

    mock_llm.chat.side_effect = [llm_response, final_response]

    response = await orchestrator.process_message(
        "Set the pump speed to 1500 RPM",
        roles=("operator",)
    )

    assert len(response.tool_results) == 1
    assert response.tool_results[0].tool_name == "SetSpeed"
    assert response.tool_results[0].simulated is True


@pytest.mark.asyncio
async def test_rbac_denial(orchestrator, mock_llm, mock_safety, mock_capabilities):
    """Test that unauthorized access is denied."""
    # Configure tool schema as ToolSpec object
    tool_spec = ToolSpec(
        name="SetSpeed",
        description="Set pump speed",
        input_schema={"properties": {"RPM": {"type": "number"}}, "required": ["RPM"]},
        submodel_id="urn:test:submodel:control",
        operation_path="SetSpeed",
        risk_level="HIGH",
        delegation_url="http://opservice:8087/operations/SetSpeed",
    )
    mock_capabilities.get_tool_by_name.return_value = tool_spec

    # Configure LLM to request tool call
    tool_call = MagicMock()
    tool_call.name = "SetSpeed"
    tool_call.arguments = {"RPM": 1500}

    llm_response = MagicMock()
    llm_response.content = None
    llm_response.tool_calls = [tool_call]
    mock_llm.chat.return_value = llm_response

    # Configure safety to deny
    safety_result = MagicMock()
    safety_result.allowed = False
    safety_result.force_simulation = False
    safety_result.require_approval = False
    safety_result.reason = "Role viewer not authorized for SetSpeed"
    mock_safety.evaluate.return_value = safety_result

    # Final LLM response
    final_response = MagicMock()
    final_response.content = "Access denied: Role viewer not authorized for SetSpeed."
    final_response.tool_calls = []

    mock_llm.chat.side_effect = [llm_response, final_response]

    response = await orchestrator.process_message(
        "Set the pump speed to 1500 RPM",
        roles=("viewer",)
    )

    assert len(response.tool_results) == 1
    assert response.tool_results[0].success is False
    assert "not authorized" in response.tool_results[0].error


@pytest.mark.asyncio
async def test_approval_required(orchestrator, mock_llm, mock_safety, mock_capabilities):
    """Test that critical operations require approval."""
    # Configure tool schema as ToolSpec object
    tool_spec = ToolSpec(
        name="EmergencyStop",
        description="Emergency stop operation",
        input_schema={"properties": {}, "required": []},
        submodel_id="urn:test:submodel:control",
        operation_path="EmergencyStop",
        risk_level="CRITICAL",
        delegation_url="http://opservice:8087/operations/EmergencyStop",
    )
    mock_capabilities.get_tool_by_name.return_value = tool_spec

    # Configure LLM to request tool call
    tool_call = MagicMock()
    tool_call.name = "EmergencyStop"
    tool_call.arguments = {}

    llm_response = MagicMock()
    llm_response.content = None
    llm_response.tool_calls = [tool_call]
    mock_llm.chat.return_value = llm_response

    # Configure safety to require approval
    safety_result = MagicMock()
    safety_result.allowed = True
    safety_result.force_simulation = False
    safety_result.require_approval = True
    safety_result.reason = "Critical operation requires human approval"
    mock_safety.evaluate.return_value = safety_result

    # Mock create_approval_task to return a task ID
    mock_safety.create_approval_task = AsyncMock(return_value="task-123")

    # Final LLM response
    final_response = MagicMock()
    final_response.content = "Emergency stop requires approval. Task ID: task-123"
    final_response.tool_calls = []

    mock_llm.chat.side_effect = [llm_response, final_response]

    response = await orchestrator.process_message(
        "Execute emergency stop",
        roles=("operator",)
    )

    assert response.pending_approval is True
    assert len(response.tool_results) == 1
    assert response.tool_results[0].status == "pending_approval"


@pytest.mark.asyncio
async def test_conversation_reset(orchestrator):
    """Test conversation history reset."""
    from twinops.agent.llm.base import Message

    # Add some history
    orchestrator._conversation.append(Message(role="user", content="test"))
    assert len(orchestrator._conversation) == 1

    # Reset
    orchestrator.reset_conversation()

    assert len(orchestrator._conversation) == 0


@pytest.mark.asyncio
async def test_invalid_tool_name(orchestrator, mock_llm, mock_capabilities):
    """Test handling of invalid tool names."""
    mock_capabilities.get_tool_by_name.return_value = None

    # Configure LLM to request invalid tool
    tool_call = MagicMock()
    tool_call.name = "NonExistentTool"
    tool_call.arguments = {}

    llm_response = MagicMock()
    llm_response.content = None
    llm_response.tool_calls = [tool_call]

    final_response = MagicMock()
    final_response.content = "Tool not found."
    final_response.tool_calls = []

    mock_llm.chat.side_effect = [llm_response, final_response]

    response = await orchestrator.process_message(
        "Run the non-existent tool",
        roles=("operator",)
    )

    assert len(response.tool_results) == 1
    assert response.tool_results[0].success is False
    assert "unknown tool" in response.tool_results[0].error.lower()
