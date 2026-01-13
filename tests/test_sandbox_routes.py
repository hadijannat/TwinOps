"""Tests for sandbox submodel element routes."""

import base64

from starlette.testclient import TestClient

from twinops.sandbox.main import create_app


def _b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("utf-8").rstrip("=")


def test_submodel_value_route_takes_precedence() -> None:
    app = create_app()
    with TestClient(app) as client:
        submodel_id = _b64url("urn:example:submodel:control")

        value_resp = client.get(
            f"/submodels/{submodel_id}/submodel-elements/TasksJson/$value"
        )
        assert value_resp.status_code == 200
        payload = value_resp.json()
        assert isinstance(payload, str)
        assert "\"tasks\"" in payload
        encoded_resp = client.get(
            f"/submodels/{submodel_id}/submodel-elements/TasksJson/%24value"
        )
        assert encoded_resp.status_code == 200
        encoded_payload = encoded_resp.json()
        assert isinstance(encoded_payload, str)
        assert "\"tasks\"" in encoded_payload

        element_resp = client.get(
            f"/submodels/{submodel_id}/submodel-elements/TasksJson"
        )
        assert element_resp.status_code == 200
        element = element_resp.json()
        assert element.get("idShort") == "TasksJson"
        assert element.get("modelType") == "Property"
