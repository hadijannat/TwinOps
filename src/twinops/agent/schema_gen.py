"""AAS Operation to LLM Tool Schema conversion."""

from dataclasses import dataclass, field
from typing import Any

from twinops.common.logging import get_logger

logger = get_logger(__name__)


# XSD type to JSON Schema type mapping
XSD_TO_JSON_TYPE: dict[str, str] = {
    "xs:string": "string",
    "xs:boolean": "boolean",
    "xs:integer": "integer",
    "xs:int": "integer",
    "xs:long": "integer",
    "xs:short": "integer",
    "xs:byte": "integer",
    "xs:unsignedInt": "integer",
    "xs:unsignedLong": "integer",
    "xs:unsignedShort": "integer",
    "xs:unsignedByte": "integer",
    "xs:decimal": "number",
    "xs:float": "number",
    "xs:double": "number",
    "xs:date": "string",
    "xs:dateTime": "string",
    "xs:time": "string",
    "xs:duration": "string",
    "xs:anyURI": "string",
    "xs:base64Binary": "string",
    "xs:hexBinary": "string",
}


@dataclass
class ToolSpec:
    """Specification for an LLM-callable tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    submodel_id: str
    operation_path: str
    risk_level: str = "LOW"
    delegation_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def extract_qualifier_value(
    element: dict[str, Any],
    qualifier_type: str,
    default: str | None = None,
) -> str | None:
    """
    Extract a qualifier value from an element.

    Args:
        element: AAS element
        qualifier_type: Qualifier type to find
        default: Default value if not found

    Returns:
        Qualifier value or default
    """
    qualifiers = element.get("qualifiers", [])
    for q in qualifiers:
        if q.get("type") == qualifier_type:
            value = q.get("value", default)
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return str(value)
    return default


def extract_constraint_value(
    element: dict[str, Any],
    constraint_type: str,
) -> Any | None:
    """
    Extract a constraint value (min/max) from qualifiers.

    Handles both "Min"/"Max" and "minimum"/"maximum" naming.
    """
    qualifiers = element.get("qualifiers", [])
    for q in qualifiers:
        q_type = q.get("type", "").lower()
        if q_type in (constraint_type.lower(), constraint_type):
            return q.get("value")
    return None


def value_type_to_json_type(value_type: str | None) -> str:
    """Convert AAS valueType to JSON Schema type."""
    if not value_type:
        return "string"
    return XSD_TO_JSON_TYPE.get(value_type, "string")


def build_property_schema(prop: dict[str, Any]) -> dict[str, Any]:
    """
    Build JSON Schema for a Property element.

    Args:
        prop: AAS Property element

    Returns:
        JSON Schema object
    """
    value_type = prop.get("valueType", "xs:string")
    json_type = value_type_to_json_type(value_type)

    schema: dict[str, Any] = {"type": json_type}

    # Add description
    descriptions = prop.get("description", [])
    if descriptions:
        # Find English description or use first
        desc_text = None
        for d in descriptions:
            if d.get("language") == "en":
                desc_text = d.get("text")
                break
        if not desc_text and descriptions:
            desc_text = descriptions[0].get("text")
        if desc_text:
            schema["description"] = desc_text

    # Add constraints
    min_val = extract_constraint_value(prop, "Min")
    max_val = extract_constraint_value(prop, "Max")

    if json_type in ("integer", "number"):
        if min_val is not None:
            schema["minimum"] = float(min_val) if json_type == "number" else int(min_val)
        if max_val is not None:
            schema["maximum"] = float(max_val) if json_type == "number" else int(max_val)
    elif json_type == "string":
        if min_val is not None:
            schema["minLength"] = int(min_val)
        if max_val is not None:
            schema["maxLength"] = int(max_val)

    # Add unit to description if present
    unit = extract_qualifier_value(prop, "unit")
    if unit:
        current_desc = schema.get("description", "")
        schema["description"] = f"{current_desc} (Unit: {unit})".strip()

    return schema


def build_collection_schema(collection: dict[str, Any]) -> dict[str, Any]:
    """
    Build JSON Schema for a SubmodelElementCollection.

    Recursively processes nested elements.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }
    required = []

    elements = collection.get("value", [])
    for elem in elements:
        id_short = elem.get("idShort")
        if not id_short:
            continue

        model_type = elem.get("modelType", "")

        if model_type == "Property":
            schema["properties"][id_short] = build_property_schema(elem)
        elif model_type == "SubmodelElementCollection":
            schema["properties"][id_short] = build_collection_schema(elem)
        elif model_type == "SubmodelElementList":
            schema["properties"][id_short] = build_list_schema(elem)

        # Check if required
        required_flag = extract_qualifier_value(elem, "required", "false") or "false"
        if required_flag.lower() == "true":
            required.append(id_short)

    if required:
        schema["required"] = required

    return schema


def build_list_schema(list_elem: dict[str, Any]) -> dict[str, Any]:
    """Build JSON Schema for a SubmodelElementList."""
    item_type = list_elem.get("typeValueListElement", "")

    items_schema: dict[str, Any]

    if item_type == "Property":
        # List of simple values
        value_type = list_elem.get("valueTypeListElement", "xs:string")
        items_schema = {"type": value_type_to_json_type(value_type)}
    elif item_type == "SubmodelElementCollection":
        # List of objects - infer from first value if available
        values = list_elem.get("value", [])
        items_schema = build_collection_schema(values[0]) if values else {"type": "object"}
    else:
        items_schema = {}

    return {
        "type": "array",
        "items": items_schema,
    }


def build_input_schema(operation: dict[str, Any]) -> dict[str, Any]:
    """
    Build complete input schema for an Operation.

    Args:
        operation: AAS Operation element

    Returns:
        JSON Schema for the operation's input
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    input_vars = operation.get("inputVariables", [])

    for var in input_vars:
        # Each inputVariable wraps a SubmodelElement
        elem = var.get("value", {})
        id_short = elem.get("idShort")
        if not id_short:
            continue

        model_type = elem.get("modelType", "")

        if model_type == "Property":
            schema["properties"][id_short] = build_property_schema(elem)
        elif model_type == "SubmodelElementCollection":
            schema["properties"][id_short] = build_collection_schema(elem)
        elif model_type == "SubmodelElementList":
            schema["properties"][id_short] = build_list_schema(elem)
        else:
            # Default to string
            schema["properties"][id_short] = {"type": "string"}

        # All input variables are typically required
        schema["required"].append(id_short)

    # Add mandatory safety fields for all tools
    schema["properties"]["simulate"] = {
        "type": "boolean",
        "description": "If true, run in simulation mode without affecting real equipment",
    }
    schema["properties"]["safety_reasoning"] = {
        "type": "string",
        "minLength": 8,
        "description": "Brief justification for why this action is safe and appropriate",
    }
    schema["required"].extend(["simulate", "safety_reasoning"])

    return schema


def build_description(operation: dict[str, Any], risk_level: str) -> str:
    """
    Build tool description with safety metadata.

    Args:
        operation: AAS Operation element
        risk_level: Operation risk level

    Returns:
        Enhanced description string
    """
    # Extract base description
    descriptions = operation.get("description", [])
    base_desc = ""
    for d in descriptions:
        if d.get("language") == "en":
            base_desc = d.get("text", "")
            break
    if not base_desc and descriptions:
        base_desc = descriptions[0].get("text", "")

    if not base_desc:
        base_desc = f"Execute {operation.get('idShort', 'operation')}"

    # Add risk context
    risk_context = {
        "LOW": "This operation is safe for routine use.",
        "MEDIUM": "This operation may affect process state.",
        "HIGH": "This operation actuates equipment. Simulation recommended.",
        "CRITICAL": "This operation is safety-critical. Requires approval.",
    }

    risk_note = risk_context.get(risk_level, "")
    return f"{base_desc} (Risk: {risk_level}). {risk_note}".strip()


def generate_tool_schema(
    operation: dict[str, Any],
    submodel_id: str,
    operation_path: str,
) -> ToolSpec:
    """
    Generate an LLM tool specification from an AAS Operation.

    Args:
        operation: AAS Operation element
        submodel_id: ID of the containing submodel
        operation_path: idShort path to the operation

    Returns:
        ToolSpec ready for LLM consumption
    """
    name = operation.get("idShort", "unknown_operation")
    risk_level = extract_qualifier_value(operation, "RiskLevel", "LOW") or "LOW"
    delegation_url = extract_qualifier_value(operation, "invocationDelegation")

    description = build_description(operation, risk_level)
    input_schema = build_input_schema(operation)

    return ToolSpec(
        name=name,
        description=description,
        input_schema=input_schema,
        submodel_id=submodel_id,
        operation_path=operation_path,
        risk_level=risk_level,
        delegation_url=delegation_url,
        metadata={
            "semantic_id": operation.get("semanticId"),
            "qualifiers": operation.get("qualifiers", []),
        },
    )


def generate_all_tool_schemas(
    operations: list[dict[str, Any]],
) -> list[ToolSpec]:
    """
    Generate tool schemas for all operations.

    Args:
        operations: List of operation elements with _submodel_id and _path

    Returns:
        List of ToolSpec objects
    """
    tools = []

    for op in operations:
        submodel_id = op.get("_submodel_id", "")
        operation_path = op.get("_path", op.get("idShort", ""))

        try:
            tool = generate_tool_schema(op, submodel_id, operation_path)
            tools.append(tool)
        except Exception as e:
            logger.warning(
                "Failed to generate schema for operation",
                operation=op.get("idShort"),
                error=str(e),
            )

    return tools


def tool_spec_to_llm_format(tool: ToolSpec) -> dict[str, Any]:
    """
    Convert ToolSpec to LLM-compatible format.

    Works with both Anthropic and OpenAI function calling.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        # OpenAI format compatibility
        "parameters": tool.input_schema,
    }
