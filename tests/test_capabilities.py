"""Tests for capability index."""

import pytest

from twinops.agent.capabilities import CapabilityIndex, HybridCapabilityIndex
from twinops.agent.schema_gen import ToolSpec


@pytest.fixture
def sample_tools() -> list[ToolSpec]:
    """Create sample tools for testing."""
    return [
        ToolSpec(
            name="StartPump",
            description="Start the pump motor to begin fluid transfer",
            input_schema={"type": "object", "properties": {}},
            submodel_id="urn:test:submodel:control",
            operation_path="StartPump",
            risk_level="HIGH",
        ),
        ToolSpec(
            name="StopPump",
            description="Stop the pump motor and halt fluid transfer",
            input_schema={"type": "object", "properties": {}},
            submodel_id="urn:test:submodel:control",
            operation_path="StopPump",
            risk_level="HIGH",
        ),
        ToolSpec(
            name="SetSpeed",
            description="Set the pump rotational speed in RPM",
            input_schema={"type": "object", "properties": {"RPM": {"type": "number"}}},
            submodel_id="urn:test:submodel:control",
            operation_path="SetSpeed",
            risk_level="HIGH",
        ),
        ToolSpec(
            name="GetTemperature",
            description="Read the current bearing temperature",
            input_schema={"type": "object", "properties": {}},
            submodel_id="urn:test:submodel:sensors",
            operation_path="GetTemperature",
            risk_level="LOW",
        ),
        ToolSpec(
            name="GetPressure",
            description="Read the discharge pressure",
            input_schema={"type": "object", "properties": {}},
            submodel_id="urn:test:submodel:sensors",
            operation_path="GetPressure",
            risk_level="LOW",
        ),
    ]


class TestCapabilityIndex:
    """Test capability index functionality."""

    def test_index_creation(self, sample_tools):
        """Test creating an index with tools."""
        index = CapabilityIndex(sample_tools)
        assert index.tool_count == 5

    def test_search_pump_operations(self, sample_tools):
        """Test searching for pump-related operations."""
        index = CapabilityIndex(sample_tools)

        results = index.search("start the pump", top_k=3)

        assert len(results) > 0
        tool_names = [r.tool.name for r in results]
        assert "StartPump" in tool_names

    def test_search_speed(self, sample_tools):
        """Test searching for speed operations."""
        index = CapabilityIndex(sample_tools)

        results = index.search("set speed to 1200 RPM", top_k=3)

        assert len(results) > 0
        assert results[0].tool.name == "SetSpeed"

    def test_search_temperature(self, sample_tools):
        """Test searching for temperature reading."""
        index = CapabilityIndex(sample_tools)

        results = index.search("what is the temperature", top_k=3)

        assert len(results) > 0
        tool_names = [r.tool.name for r in results]
        assert "GetTemperature" in tool_names

    def test_get_tool_by_name(self, sample_tools):
        """Test retrieving tool by exact name."""
        index = CapabilityIndex(sample_tools)

        tool = index.get_tool_by_name("SetSpeed")
        assert tool is not None
        assert tool.name == "SetSpeed"

        tool = index.get_tool_by_name("NonExistent")
        assert tool is None

    def test_get_tools_by_risk(self, sample_tools):
        """Test filtering tools by risk level."""
        index = CapabilityIndex(sample_tools)

        high_risk = index.get_tools_by_risk("HIGH")
        assert len(high_risk) == 3

        low_risk = index.get_tools_by_risk("LOW")
        assert len(low_risk) == 2

    def test_get_tools_for_submodel(self, sample_tools):
        """Test filtering tools by submodel."""
        index = CapabilityIndex(sample_tools)

        control_tools = index.get_tools_for_submodel("urn:test:submodel:control")
        assert len(control_tools) == 3

        sensor_tools = index.get_tools_for_submodel("urn:test:submodel:sensors")
        assert len(sensor_tools) == 2

    def test_empty_index(self):
        """Test empty index behavior."""
        index = CapabilityIndex()

        assert index.tool_count == 0
        results = index.search("anything")
        assert len(results) == 0

    def test_add_tools(self, sample_tools):
        """Test adding tools incrementally."""
        index = CapabilityIndex()
        index.add_tools(sample_tools[:2])

        assert index.tool_count == 2

        index.add_tools(sample_tools[2:])
        assert index.tool_count == 5


class TestHybridCapabilityIndex:
    """Test hybrid index with priority tools."""

    def test_priority_tools_always_included(self, sample_tools):
        """Test that priority tools are always in results."""
        index = HybridCapabilityIndex(
            tools=sample_tools,
            always_include=["GetTemperature"],
        )

        results = index.search("start pump", top_k=3)

        tool_names = [r.tool.name for r in results]
        assert "GetTemperature" in tool_names

    def test_priority_tools_first(self, sample_tools):
        """Test that priority tools appear first."""
        index = HybridCapabilityIndex(
            tools=sample_tools,
            always_include=["GetPressure"],
        )

        results = index.search("start pump", top_k=5)

        assert results[0].tool.name == "GetPressure"
