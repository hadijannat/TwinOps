"""Pytest configuration and fixtures."""

import asyncio
import json
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

from twinops.common.settings import Settings


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings() -> Settings:
    """Create test settings."""
    return Settings(
        twin_base_url="http://localhost:8081",
        mqtt_broker_host="localhost",
        mqtt_broker_port=1883,
        repo_id="test-repo",
        aas_id="urn:test:aas:001",
        llm_provider="rules",
        policy_verification_required=False,
    )


@pytest.fixture
def sample_aas() -> dict[str, Any]:
    """Sample AAS structure."""
    return {
        "modelType": "AssetAdministrationShell",
        "id": "urn:test:aas:001",
        "idShort": "TestAAS",
        "assetInformation": {
            "assetKind": "Instance",
            "globalAssetId": "urn:test:asset:001",
        },
        "submodels": [
            {
                "type": "ModelReference",
                "keys": [{"type": "Submodel", "value": "urn:test:submodel:control"}],
            }
        ],
    }


@pytest.fixture
def sample_submodel() -> dict[str, Any]:
    """Sample submodel with operations."""
    return {
        "modelType": "Submodel",
        "id": "urn:test:submodel:control",
        "idShort": "Control",
        "submodelElements": [
            {
                "modelType": "Property",
                "idShort": "CurrentSpeed",
                "valueType": "xs:double",
                "value": 1000.0,
            },
            {
                "modelType": "Operation",
                "idShort": "SetSpeed",
                "qualifiers": [
                    {"type": "RiskLevel", "valueType": "xs:string", "value": "HIGH"},
                    {
                        "type": "invocationDelegation",
                        "valueType": "xs:string",
                        "value": "http://opservice:8087/operations/SetSpeed",
                    },
                ],
                "inputVariables": [
                    {
                        "value": {
                            "modelType": "Property",
                            "idShort": "RPM",
                            "valueType": "xs:double",
                            "qualifiers": [
                                {"type": "Min", "valueType": "xs:double", "value": "0"},
                                {"type": "Max", "valueType": "xs:double", "value": "3600"},
                            ],
                        }
                    }
                ],
            },
            {
                "modelType": "Operation",
                "idShort": "GetStatus",
                "qualifiers": [
                    {"type": "RiskLevel", "valueType": "xs:string", "value": "LOW"},
                ],
                "inputVariables": [],
            },
        ],
    }


@pytest.fixture
def sample_policy() -> dict[str, Any]:
    """Sample policy configuration."""
    return {
        "require_simulation_for_risk": "HIGH",
        "require_approval_for_risk": "CRITICAL",
        "approval_roles": ["admin", "maintenance", "supervisor"],
        "role_bindings": {
            "operator": {"allow": ["SetSpeed", "GetStatus"]},
            "viewer": {"allow": ["GetStatus"]},
            "admin": {"allow": ["*"]},
        },
        "interlocks": [
            {
                "id": "temp-high",
                "deny_when": {
                    "submodel": "urn:test:submodel:control",
                    "path": "Temperature",
                    "op": ">",
                    "value": 95,
                },
                "message": "Temperature too high",
            }
        ],
        "task_submodel_id": "urn:test:submodel:control",
        "tasks_property_path": "TasksJson",
    }


@pytest.fixture
def mock_twin_client() -> AsyncMock:
    """Mock twin client."""
    client = AsyncMock()
    client.get_aas = AsyncMock(return_value={})
    client.get_submodel = AsyncMock(return_value={})
    client.get_full_twin = AsyncMock(return_value={"aas": {}, "submodels": {}})
    client.get_property_value = AsyncMock(return_value=None)
    client.set_property_value = AsyncMock()
    client.invoke_delegated_operation = AsyncMock(
        return_value={"executionState": "Completed", "result": {}}
    )
    return client


@pytest.fixture
def mock_mqtt_client() -> MagicMock:
    """Mock MQTT client."""
    client = MagicMock()
    client.set_subscriptions = MagicMock()
    client.add_handler = MagicMock()
    return client
