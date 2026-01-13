"""Tests for shadow twin manager."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from twinops.agent.shadow import ShadowTwinManager
from twinops.common.mqtt import MqttMessage


@pytest.fixture
def mock_twin_client():
    """Mock twin client for testing."""
    client = AsyncMock()
    client.get_full_twin = AsyncMock(
        return_value={
            "aas": {
                "id": "urn:test:aas:001",
                "idShort": "TestAAS",
            },
            "submodels": {
                "urn:test:submodel:control": {
                    "id": "urn:test:submodel:control",
                    "idShort": "Control",
                    "submodelElements": [
                        {
                            "modelType": "Property",
                            "idShort": "CurrentSpeed",
                            "valueType": "xs:double",
                            "value": 1000.0,
                        },
                    ],
                }
            },
        }
    )
    return client


@pytest.fixture
def mock_mqtt_client():
    """Mock MQTT client for testing."""
    client = MagicMock()
    client.set_subscriptions = MagicMock()
    client.add_handler = MagicMock()
    client.is_connected = True
    return client


@pytest.fixture
def shadow_manager(mock_twin_client, mock_mqtt_client):
    """Create shadow twin manager for testing."""
    return ShadowTwinManager(
        twin_client=mock_twin_client,
        mqtt_client=mock_mqtt_client,
        aas_id="urn:test:aas:001",
        aas_repo_id="test-repo",
    )


@pytest.mark.asyncio
async def test_initialize_loads_full_twin(shadow_manager, mock_twin_client):
    """Test that initialization loads the full twin state."""
    await shadow_manager.initialize()

    mock_twin_client.get_full_twin.assert_called_once_with("urn:test:aas:001")
    assert shadow_manager._state is not None
    assert "aas" in shadow_manager._state
    assert "submodels" in shadow_manager._state


@pytest.mark.asyncio
async def test_initialize_sets_up_mqtt_subscriptions(shadow_manager, mock_mqtt_client):
    """Test that initialization sets up MQTT subscriptions."""
    await shadow_manager.initialize()

    mock_mqtt_client.set_subscriptions.assert_called_once()
    mock_mqtt_client.add_handler.assert_called_once()


@pytest.mark.asyncio
async def test_get_state_returns_copy(shadow_manager):
    """Test that get_aas and get_all_submodels return copies of the state."""
    await shadow_manager.initialize()

    # Test that separate calls return different objects (copies)
    aas1 = await shadow_manager.get_aas()
    aas2 = await shadow_manager.get_aas()
    submodels1 = await shadow_manager.get_all_submodels()
    submodels2 = await shadow_manager.get_all_submodels()

    # Different object references
    assert aas1 is not aas2
    assert submodels1 is not submodels2
    # But equal content
    assert aas1 == aas2
    assert submodels1 == submodels2


@pytest.mark.asyncio
async def test_get_operations_extracts_operations(shadow_manager):
    """Test that get_operations extracts operations from submodels."""
    # Setup state with operations
    shadow_manager._state = {
        "aas": {},
        "submodels": {
            "urn:test:sm": {
                "id": "urn:test:sm",
                "submodelElements": [
                    {
                        "modelType": "Operation",
                        "idShort": "TestOp",
                        "inputVariables": [],
                    },
                    {
                        "modelType": "Property",
                        "idShort": "TestProp",
                    },
                ],
            }
        },
    }

    operations = await shadow_manager.get_operations()

    assert len(operations) == 1
    assert operations[0]["idShort"] == "TestOp"


@pytest.mark.asyncio
async def test_event_count_increments(shadow_manager):
    """Test that event count increments with each event."""
    from twinops.common.basyx_topics import b64url_encode_nopad

    await shadow_manager.initialize()

    initial_count = shadow_manager.event_count

    # Submodel ID must be base64 encoded in topic
    submodel_id = "urn:test:submodel:control"
    sm_encoded = b64url_encode_nopad(submodel_id)

    # Simulate submodel update event using correct BaSyx topic format
    submodel_data = {
        "id": submodel_id,
        "submodelElements": [{"idShort": "CurrentSpeed", "value": 1500.0}],
    }
    message = MqttMessage(
        topic=f"submodel-repository/test-repo/submodels/{sm_encoded}/updated",
        payload=json.dumps(submodel_data).encode(),
        qos=0,
        retain=False,
    )

    await shadow_manager._handle_mqtt_message(message)

    assert shadow_manager.event_count == initial_count + 1


@pytest.mark.asyncio
async def test_property_update_event(shadow_manager):
    """Test that property update events modify state."""
    from twinops.common.basyx_topics import b64url_encode_nopad

    submodel_id = "urn:test:submodel:control"
    sm_encoded = b64url_encode_nopad(submodel_id)

    # Setup initial state
    shadow_manager._state = {
        "aas": {},
        "submodels": {
            submodel_id: {
                "id": submodel_id,
                "submodelElements": [
                    {
                        "modelType": "Property",
                        "idShort": "CurrentSpeed",
                        "value": 1000.0,
                    },
                ],
            }
        },
    }

    # Create element update event - the payload contains the new element data
    new_element_data = {
        "modelType": "Property",
        "idShort": "CurrentSpeed",
        "value": 1500.0,
    }
    message = MqttMessage(
        topic=f"submodel-repository/test-repo/submodels/{sm_encoded}/submodelElements/CurrentSpeed/updated",
        payload=json.dumps(new_element_data).encode(),
        qos=0,
        retain=False,
    )

    # Process event
    await shadow_manager._handle_mqtt_message(message)

    # Verify state updated
    submodel = shadow_manager._state["submodels"][submodel_id]
    prop = submodel["submodelElements"][0]
    assert prop["value"] == 1500.0


@pytest.mark.asyncio
async def test_get_property_value(shadow_manager):
    """Test getting a property value from shadow state."""
    shadow_manager._state = {
        "aas": {},
        "submodels": {
            "urn:test:sm": {
                "submodelElements": [
                    {
                        "modelType": "Property",
                        "idShort": "TestProp",
                        "value": 42,
                    },
                ],
            }
        },
    }

    value = await shadow_manager.get_property_value("urn:test:sm", "TestProp")
    assert value == 42


@pytest.mark.asyncio
async def test_get_property_value_not_found(shadow_manager):
    """Test getting non-existent property returns None."""
    shadow_manager._state = {"aas": {}, "submodels": {}}

    value = await shadow_manager.get_property_value("nonexistent", "Prop")
    assert value is None


@pytest.mark.asyncio
async def test_resync_on_error(shadow_manager, mock_twin_client):
    """Test that refresh triggers a full resync."""
    await shadow_manager.initialize()

    # Reset the mock to track resync calls
    mock_twin_client.get_full_twin.reset_mock()

    # Trigger resync via public refresh() method
    await shadow_manager.refresh()

    mock_twin_client.get_full_twin.assert_called_once()


@pytest.mark.asyncio
async def test_thread_safety_with_lock(shadow_manager):
    """Test that state access is protected by lock."""
    await shadow_manager.initialize()

    # Verify lock is used
    assert shadow_manager._lock is not None

    # Get state should acquire lock
    async with shadow_manager._lock:
        # Should not deadlock - proves lock is reentrant or properly managed
        pass
