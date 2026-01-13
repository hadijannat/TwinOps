"""Tests for BaSyx topic encoding and parsing."""

import pytest

from twinops.common.basyx_topics import (
    EventType,
    ParsedTopic,
    RepositoryType,
    b64url_decode_nopad,
    b64url_encode_nopad,
    build_all_subscriptions,
    parse_topic,
)


class TestBase64Encoding:
    """Test Base64 URL-safe encoding/decoding."""

    def test_encode_simple(self):
        """Test encoding simple strings."""
        assert b64url_encode_nopad("test") == "dGVzdA"

    def test_encode_urn(self):
        """Test encoding URN-style IDs."""
        urn = "urn:example:aas:pump-001"
        encoded = b64url_encode_nopad(urn)
        assert "=" not in encoded  # No padding
        assert b64url_decode_nopad(encoded) == urn

    def test_decode_roundtrip(self):
        """Test encode/decode roundtrip."""
        original = "urn:test:submodel:control"
        encoded = b64url_encode_nopad(original)
        decoded = b64url_decode_nopad(encoded)
        assert decoded == original

    def test_encode_special_chars(self):
        """Test encoding strings with special characters."""
        text = "hello/world+test"
        encoded = b64url_encode_nopad(text)
        assert "/" not in encoded
        assert "+" not in encoded
        assert b64url_decode_nopad(encoded) == text


class TestTopicParsing:
    """Test MQTT topic parsing."""

    def test_parse_aas_created(self):
        """Test parsing AAS created event."""
        topic = "aas-repository/default/shells/created"
        parsed = parse_topic(topic)

        assert parsed is not None
        assert parsed.repository_type == RepositoryType.AAS
        assert parsed.repo_id == "default"
        assert parsed.event_type == EventType.CREATED
        assert parsed.entity_id is None

    def test_parse_aas_updated(self):
        """Test parsing AAS updated event with entity ID."""
        aas_id = "urn:test:aas:001"
        encoded_id = b64url_encode_nopad(aas_id)
        topic = f"aas-repository/default/shells/{encoded_id}/updated"

        parsed = parse_topic(topic)

        assert parsed is not None
        assert parsed.repository_type == RepositoryType.AAS
        assert parsed.event_type == EventType.UPDATED
        assert parsed.entity_id == aas_id

    def test_parse_submodel_element_updated(self):
        """Test parsing submodel element update."""
        sm_id = "urn:test:submodel:001"
        encoded_id = b64url_encode_nopad(sm_id)
        topic = f"submodel-repository/default/submodels/{encoded_id}/submodelElements/Property1/updated"

        parsed = parse_topic(topic)

        assert parsed is not None
        assert parsed.repository_type == RepositoryType.SUBMODEL
        assert parsed.event_type == EventType.UPDATED
        assert parsed.entity_id == sm_id
        assert parsed.element_path == "Property1"

    def test_parse_nested_element(self):
        """Test parsing nested submodel element path."""
        sm_id = "urn:test:submodel:001"
        encoded_id = b64url_encode_nopad(sm_id)
        topic = f"submodel-repository/default/submodels/{encoded_id}/submodelElements/Collection/Nested/Property/updated"

        parsed = parse_topic(topic)

        assert parsed is not None
        assert parsed.element_path == "Collection/Nested/Property"

    def test_parse_invalid_topic(self):
        """Test parsing invalid topics."""
        assert parse_topic("invalid") is None
        assert parse_topic("unknown-repo/default/shells/created") is None
        assert parse_topic("aas-repository/default/shells/invalid-event") is None


class TestSubscriptions:
    """Test subscription building."""

    def test_build_all_subscriptions(self):
        """Test building all subscriptions."""
        subs = build_all_subscriptions("my-repo")

        assert len(subs) == 2
        topics = [s.topic for s in subs]
        assert "aas-repository/my-repo/shells/#" in topics
        assert "submodel-repository/my-repo/submodels/#" in topics
