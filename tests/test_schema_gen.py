"""Tests for AAS-to-Tool schema generation."""

import pytest

from twinops.agent.schema_gen import (
    ToolSpec,
    build_input_schema,
    build_property_schema,
    extract_qualifier_value,
    generate_tool_schema,
    tool_spec_to_llm_format,
)


class TestQualifierExtraction:
    """Test qualifier value extraction."""

    def test_extract_existing_qualifier(self):
        """Test extracting an existing qualifier."""
        element = {
            "qualifiers": [
                {"type": "RiskLevel", "value": "HIGH"},
                {"type": "unit", "value": "rpm"},
            ]
        }
        assert extract_qualifier_value(element, "RiskLevel") == "HIGH"
        assert extract_qualifier_value(element, "unit") == "rpm"

    def test_extract_missing_qualifier(self):
        """Test extracting a missing qualifier."""
        element = {"qualifiers": []}
        assert extract_qualifier_value(element, "RiskLevel") is None
        assert extract_qualifier_value(element, "RiskLevel", "LOW") == "LOW"

    def test_extract_no_qualifiers(self):
        """Test extracting from element without qualifiers."""
        element = {}
        assert extract_qualifier_value(element, "RiskLevel") is None


class TestPropertySchema:
    """Test property schema generation."""

    def test_double_property(self):
        """Test schema for double property."""
        prop = {
            "modelType": "Property",
            "idShort": "Speed",
            "valueType": "xs:double",
            "qualifiers": [
                {"type": "Min", "value": "0"},
                {"type": "Max", "value": "3600"},
                {"type": "unit", "value": "rpm"},
            ],
        }

        schema = build_property_schema(prop)

        assert schema["type"] == "number"
        assert schema["minimum"] == 0.0
        assert schema["maximum"] == 3600.0
        assert "rpm" in schema.get("description", "")

    def test_integer_property(self):
        """Test schema for integer property."""
        prop = {
            "modelType": "Property",
            "idShort": "Count",
            "valueType": "xs:integer",
            "qualifiers": [
                {"type": "Min", "value": "1"},
                {"type": "Max", "value": "100"},
            ],
        }

        schema = build_property_schema(prop)

        assert schema["type"] == "integer"
        assert schema["minimum"] == 1
        assert schema["maximum"] == 100

    def test_string_property(self):
        """Test schema for string property."""
        prop = {
            "modelType": "Property",
            "idShort": "Name",
            "valueType": "xs:string",
            "description": [{"language": "en", "text": "Equipment name"}],
        }

        schema = build_property_schema(prop)

        assert schema["type"] == "string"
        assert "Equipment name" in schema.get("description", "")

    def test_boolean_property(self):
        """Test schema for boolean property."""
        prop = {
            "modelType": "Property",
            "idShort": "Active",
            "valueType": "xs:boolean",
        }

        schema = build_property_schema(prop)
        assert schema["type"] == "boolean"


class TestOperationSchema:
    """Test operation schema generation."""

    def test_generate_tool_schema(self, sample_submodel):
        """Test generating tool schema from operation."""
        operation = sample_submodel["submodelElements"][1]  # SetSpeed

        tool = generate_tool_schema(
            operation,
            submodel_id="urn:test:submodel:control",
            operation_path="SetSpeed",
        )

        assert tool.name == "SetSpeed"
        assert tool.risk_level == "HIGH"
        assert tool.delegation_url == "http://opservice:8087/operations/SetSpeed"
        assert "RPM" in tool.input_schema["properties"]
        assert "simulate" in tool.input_schema["properties"]
        assert "safety_reasoning" in tool.input_schema["properties"]

    def test_input_schema_required_fields(self, sample_submodel):
        """Test that safety fields are required."""
        operation = sample_submodel["submodelElements"][1]

        schema = build_input_schema(operation)

        assert "simulate" in schema["required"]
        assert "safety_reasoning" in schema["required"]
        assert "RPM" in schema["required"]

    def test_tool_spec_to_llm_format(self):
        """Test converting ToolSpec to LLM format."""
        tool = ToolSpec(
            name="TestOp",
            description="Test operation",
            input_schema={"type": "object", "properties": {}},
            submodel_id="urn:test",
            operation_path="TestOp",
        )

        llm_format = tool_spec_to_llm_format(tool)

        assert llm_format["name"] == "TestOp"
        assert llm_format["description"] == "Test operation"
        assert "input_schema" in llm_format
        assert "parameters" in llm_format  # OpenAI compatibility
