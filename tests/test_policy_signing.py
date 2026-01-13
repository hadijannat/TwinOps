"""Tests for CovenantTwin policy signing and verification."""

import json

import pytest

from twinops.agent.policy_signing import (
    PolicyVerificationError,
    generate_keypair,
    sign_policy,
    verify_policy_signature,
)


class TestKeypairGeneration:
    """Test Ed25519 keypair generation."""

    def test_generate_keypair(self):
        """Test generating a key pair."""
        private_pem, public_pem = generate_keypair()

        assert "-----BEGIN PRIVATE KEY-----" in private_pem
        assert "-----END PRIVATE KEY-----" in private_pem
        assert "-----BEGIN PUBLIC KEY-----" in public_pem
        assert "-----END PUBLIC KEY-----" in public_pem

    def test_keypairs_are_unique(self):
        """Test that each generation produces unique keys."""
        pair1 = generate_keypair()
        pair2 = generate_keypair()

        assert pair1[0] != pair2[0]
        assert pair1[1] != pair2[1]


class TestPolicySigning:
    """Test policy signing functionality."""

    @pytest.fixture
    def keypair(self):
        """Generate a keypair for tests."""
        return generate_keypair()

    def test_sign_policy(self, keypair):
        """Test signing a policy."""
        private_pem, _ = keypair
        policy_json = '{"test": "policy"}'

        signature = sign_policy(policy_json, private_pem)

        assert signature is not None
        assert len(signature) > 0

    def test_verify_valid_signature(self, keypair):
        """Test verifying a valid signature."""
        private_pem, public_pem = keypair
        policy_json = json.dumps({"require_simulation_for_risk": "HIGH"})

        signature = sign_policy(policy_json, private_pem)
        is_valid = verify_policy_signature(policy_json, public_pem, signature)

        assert is_valid is True

    def test_verify_invalid_signature(self, keypair):
        """Test detecting invalid signature."""
        _, public_pem = keypair
        policy_json = '{"test": "policy"}'
        fake_signature = "aW52YWxpZHNpZ25hdHVyZQ=="  # "invalidsignature" in base64

        is_valid = verify_policy_signature(policy_json, public_pem, fake_signature)

        assert is_valid is False

    def test_verify_tampered_policy(self, keypair):
        """Test detecting tampered policy."""
        private_pem, public_pem = keypair
        original_policy = '{"test": "policy"}'
        tampered_policy = '{"test": "tampered"}'

        signature = sign_policy(original_policy, private_pem)
        is_valid = verify_policy_signature(tampered_policy, public_pem, signature)

        assert is_valid is False

    def test_verify_wrong_key(self):
        """Test that verification fails with wrong key."""
        keypair1 = generate_keypair()
        keypair2 = generate_keypair()

        policy_json = '{"test": "policy"}'
        signature = sign_policy(policy_json, keypair1[0])

        # Verify with different public key
        is_valid = verify_policy_signature(policy_json, keypair2[1], signature)

        assert is_valid is False

    def test_sign_complex_policy(self, keypair, sample_policy):
        """Test signing a complex policy structure."""
        private_pem, public_pem = keypair
        policy_json = json.dumps(sample_policy)

        signature = sign_policy(policy_json, private_pem)
        is_valid = verify_policy_signature(policy_json, public_pem, signature)

        assert is_valid is True

    def test_preserves_exact_bytes(self, keypair):
        """Test that signing preserves exact JSON bytes."""
        private_pem, public_pem = keypair

        # These are semantically equivalent but different bytes
        policy1 = '{"a": 1, "b": 2}'
        policy2 = '{"b": 2, "a": 1}'

        signature = sign_policy(policy1, private_pem)

        # Should verify with exact same bytes
        assert verify_policy_signature(policy1, public_pem, signature) is True
        # Should NOT verify with reordered JSON
        assert verify_policy_signature(policy2, public_pem, signature) is False
