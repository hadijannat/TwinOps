"""Tests for safety kernel functionality."""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from twinops.agent.safety import (
    AuditLogger,
    PolicyConfig,
    RiskLevel,
    SafetyKernel,
    TaskStatus,
)


class TestPolicyConfig:
    """Test policy configuration parsing."""

    def test_from_dict(self, sample_policy):
        """Test creating PolicyConfig from dict."""
        config = PolicyConfig.from_dict(sample_policy)

        assert config.require_simulation_for_risk == RiskLevel.HIGH
        assert config.require_approval_for_risk == RiskLevel.CRITICAL
        assert "operator" in config.role_bindings
        assert len(config.interlocks) == 1

    def test_default_values(self):
        """Test default policy values."""
        config = PolicyConfig()

        assert config.require_simulation_for_risk == RiskLevel.HIGH
        assert config.require_approval_for_risk == RiskLevel.CRITICAL
        assert config.role_bindings == {}
        assert config.interlocks == []


class TestAuditLogger:
    """Test audit logging functionality."""

    @pytest.fixture
    def temp_log_file(self):
        """Create temporary log file."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_log_entry(self, temp_log_file):
        """Test writing a log entry."""
        logger = AuditLogger(temp_log_file)
        logger.log(event="test", tool="TestOp", risk="LOW")

        with open(temp_log_file) as f:
            entries = [json.loads(line) for line in f]

        assert len(entries) == 1
        assert entries[0]["event"] == "test"
        assert entries[0]["tool"] == "TestOp"
        assert "hash" in entries[0]
        assert "ts" in entries[0]

    def test_hash_chain(self, temp_log_file):
        """Test hash chain integrity."""
        logger = AuditLogger(temp_log_file)

        logger.log(event="first")
        logger.log(event="second")
        logger.log(event="third")

        with open(temp_log_file) as f:
            entries = [json.loads(line) for line in f]

        # First entry has empty prev_hash
        assert entries[0]["prev_hash"] == ""

        # Subsequent entries chain to previous
        assert entries[1]["prev_hash"] == entries[0]["hash"]
        assert entries[2]["prev_hash"] == entries[1]["hash"]

    def test_verify_chain_valid(self, temp_log_file):
        """Test chain verification on valid log."""
        logger = AuditLogger(temp_log_file)

        logger.log(event="first")
        logger.log(event="second")
        logger.log(event="third")

        is_valid, broken = logger.verify_chain()

        assert is_valid is True
        assert broken == []

    def test_verify_chain_tampered(self, temp_log_file):
        """Test chain verification detects tampering."""
        logger = AuditLogger(temp_log_file)

        logger.log(event="first")
        logger.log(event="second")

        # Tamper with the log
        with open(temp_log_file, "r") as f:
            lines = f.readlines()

        # Modify the first entry
        entry = json.loads(lines[0])
        entry["event"] = "tampered"
        lines[0] = json.dumps(entry) + "\n"

        with open(temp_log_file, "w") as f:
            f.writelines(lines)

        # Verify should fail
        is_valid, broken = logger.verify_chain()

        assert is_valid is False
        assert 1 in broken  # First line is corrupted


class TestSafetyKernel:
    """Test safety kernel functionality."""

    @pytest.fixture
    def safety_kernel(self, mock_twin_client, sample_policy):
        """Create safety kernel with mocks."""
        shadow = AsyncMock()
        shadow.get_submodel = AsyncMock(
            return_value={
                "submodelElements": [
                    {
                        "idShort": "PolicyJson",
                        "value": json.dumps(sample_policy),
                    }
                ]
            }
        )
        shadow.get_property_value = AsyncMock(return_value=50.0)  # Below threshold

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            audit_path = f.name

        kernel = SafetyKernel(
            shadow=shadow,
            twin_client=mock_twin_client,
            audit_logger=AuditLogger(audit_path),
            policy_submodel_id="urn:test:submodel:policy",
            require_policy_verification=False,
        )

        yield kernel

        if os.path.exists(audit_path):
            os.unlink(audit_path)

    @pytest.mark.asyncio
    async def test_rbac_allowed(self, safety_kernel):
        """Test RBAC allows authorized operations."""
        decision = await safety_kernel.evaluate(
            tool_name="SetSpeed",
            tool_risk="HIGH",
            roles=("operator",),
            params={"RPM": 1200},
        )

        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_rbac_denied(self, safety_kernel):
        """Test RBAC denies unauthorized operations."""
        decision = await safety_kernel.evaluate(
            tool_name="SetSpeed",
            tool_risk="HIGH",
            roles=("viewer",),  # Viewer can't SetSpeed
            params={"RPM": 1200},
        )

        assert decision.allowed is False
        assert "not authorized" in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_simulation_forced_for_high_risk(self, safety_kernel):
        """Test simulation is forced for high-risk operations."""
        decision = await safety_kernel.evaluate(
            tool_name="SetSpeed",
            tool_risk="HIGH",
            roles=("operator",),
            params={"RPM": 1200, "simulate": False},
        )

        assert decision.allowed is True
        assert decision.force_simulation is True

    @pytest.mark.asyncio
    async def test_simulation_not_forced_when_requested(self, safety_kernel):
        """Test simulation not forced when already requested."""
        decision = await safety_kernel.evaluate(
            tool_name="SetSpeed",
            tool_risk="HIGH",
            roles=("operator",),
            params={"RPM": 1200, "simulate": True},
        )

        assert decision.allowed is True
        assert decision.force_simulation is False

    @pytest.mark.asyncio
    async def test_approval_required_for_critical(self, safety_kernel):
        """Test approval required for critical operations."""
        decision = await safety_kernel.evaluate(
            tool_name="EmergencyStop",
            tool_risk="CRITICAL",
            roles=("admin",),  # Admin can do anything
            params={},
        )

        assert decision.allowed is True
        assert decision.require_approval is True

    @pytest.mark.asyncio
    async def test_approval_roles_authorization(self, safety_kernel):
        """Test approval authorization is policy-driven."""
        assert await safety_kernel.is_approval_authorized(("admin",)) is True
        assert await safety_kernel.is_approval_authorized(("viewer",)) is False

    @pytest.mark.asyncio
    async def test_interlock_violation(self, safety_kernel):
        """Test interlock violation blocks operation."""
        # Set temperature above threshold
        safety_kernel._shadow.get_property_value = AsyncMock(return_value=100.0)

        decision = await safety_kernel.evaluate(
            tool_name="SetSpeed",
            tool_risk="HIGH",
            roles=("operator",),
            params={"RPM": 1200},
        )

        assert decision.allowed is False
        assert "temperature" in decision.reason.lower()
