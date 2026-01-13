"""Safety Kernel - Multi-layer defense for AI agent operations."""

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from twinops.agent.policy_signing import (
    PolicyVerificationError,
    extract_signed_policy_from_submodel,
    verify_and_load_policy,
)
from twinops.agent.shadow import ShadowTwinManager
from twinops.agent.twin_client import TwinClient
from twinops.common.logging import get_logger
from twinops.common.http import get_request_id, get_subject

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

logger = get_logger(__name__)


class RiskLevel(str, Enum):
    """Operation risk levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TaskStatus(str, Enum):
    """Human-in-the-loop task status."""

    PENDING_APPROVAL = "PendingApproval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    EXPIRED = "Expired"


@dataclass
class PolicyConfig:
    """Parsed policy configuration."""

    require_simulation_for_risk: RiskLevel = RiskLevel.HIGH
    require_approval_for_risk: RiskLevel = RiskLevel.CRITICAL
    role_bindings: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    interlocks: list[dict[str, Any]] = field(default_factory=list)
    task_submodel_id: str = ""
    tasks_property_path: str = "TasksJson"
    job_status_submodel_id: str = ""
    job_status_property_path: str = "JobStatusJson"
    is_verified: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyConfig":
        """Create PolicyConfig from dictionary."""
        sim_risk = data.get("require_simulation_for_risk", "HIGH")
        approval_risk = data.get("require_approval_for_risk", "CRITICAL")

        return cls(
            require_simulation_for_risk=RiskLevel(sim_risk),
            require_approval_for_risk=RiskLevel(approval_risk),
            role_bindings=data.get("role_bindings", {}),
            interlocks=data.get("interlocks", []),
            task_submodel_id=data.get("task_submodel_id", ""),
            tasks_property_path=data.get("tasks_property_path", "TasksJson"),
            job_status_submodel_id=data.get("job_status_submodel_id", ""),
            job_status_property_path=data.get("job_status_property_path", "JobStatusJson"),
        )


@dataclass
class SafetyDecision:
    """Result of safety evaluation."""

    allowed: bool
    reason: str | None = None
    force_simulation: bool = False
    require_approval: bool = False
    task_id: str | None = None


class AuditLogger:
    """Hash-chained audit log for tamper evidence."""

    def __init__(self, log_path: str):
        """
        Initialize audit logger.

        Args:
            log_path: Path to JSONL audit log file
        """
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._prev_hash = ""
        self._lock_supported = fcntl is not None

        if self._log_path.exists():
            try:
                with open(self._log_path, "rb") as f:
                    self._prev_hash = self._read_last_hash_locked(f)
            except OSError:
                self._prev_hash = ""

    def _compute_hash(self, data: dict[str, Any]) -> str:
        """Compute SHA-256 hash of entry data."""
        content = json.dumps(data, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _acquire_lock(self, file_obj) -> None:
        if self._lock_supported:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)

    def _release_lock(self, file_obj) -> None:
        if self._lock_supported:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)

    def _read_last_hash_locked(self, file_obj) -> str:
        file_obj.seek(0)
        last_line = b""
        for line in file_obj:
            if line.strip():
                last_line = line
        if not last_line:
            return ""
        try:
            entry = json.loads(last_line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""
        return entry.get("hash", "")

    def log(
        self,
        event: str,
        tool: str | None = None,
        risk: str | None = None,
        roles: tuple[str, ...] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> None:
        """
        Write a hash-chained audit entry.

        Args:
            event: Event type (intent, simulated, executed, denied, etc.)
            tool: Tool/operation name
            risk: Risk level
            roles: User roles
            result: Operation result
            error: Error message if failed
            **extra: Additional fields
        """
        entry = {
            "ts": time.time(),
            "event": event,
            "prev_hash": self._prev_hash,
        }

        if tool:
            entry["tool"] = tool
        if risk:
            entry["risk"] = risk
        if roles:
            entry["roles"] = list(roles)
        if result:
            entry["result"] = result
        if error:
            entry["error"] = error

        entry.update(extra)

        request_id = get_request_id()
        subject = get_subject()
        if request_id and "request_id" not in entry:
            entry["request_id"] = request_id
        if subject and "subject" not in entry:
            entry["subject"] = subject

        with open(self._log_path, "a+b") as f:
            self._acquire_lock(f)
            try:
                prev_hash = self._read_last_hash_locked(f) or self._prev_hash
                entry["prev_hash"] = prev_hash
                entry["hash"] = self._compute_hash(entry)

                f.seek(0, os.SEEK_END)
                f.write((json.dumps(entry) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            finally:
                self._release_lock(f)

        self._prev_hash = entry["hash"]
        logger.debug("Audit entry logged", audit_event=event, tool=tool)

    def verify_chain(self) -> tuple[bool, list[int]]:
        """
        Verify the hash chain integrity.

        Returns:
            Tuple of (is_valid, list_of_broken_line_numbers)
        """
        broken = []
        prev_hash = ""

        if not self._log_path.exists():
            return True, []

        with open(self._log_path) as f:
            for i, line in enumerate(f, 1):
                try:
                    entry = json.loads(line)
                    if entry.get("prev_hash") != prev_hash:
                        broken.append(i)

                    # Verify entry hash
                    stored_hash = entry.pop("hash", "")
                    computed_hash = self._compute_hash(entry)
                    entry["hash"] = stored_hash

                    if stored_hash != computed_hash:
                        broken.append(i)

                    prev_hash = stored_hash
                except json.JSONDecodeError:
                    broken.append(i)

        return len(broken) == 0, broken


class SafetyKernel:
    """
    Multi-layer defense model for AI agent operations.

    Implements:
    1. Role-based access control (RBAC)
    2. Interlock predicate evaluation
    3. Simulation forcing for high-risk operations
    4. Human-in-the-loop approval gates
    5. Tamper-evident audit logging
    """

    # Risk level ordering for comparison
    RISK_ORDER = {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
        RiskLevel.CRITICAL: 3,
    }

    def __init__(
        self,
        shadow: ShadowTwinManager,
        twin_client: TwinClient,
        audit_logger: AuditLogger,
        policy_submodel_id: str,
        require_policy_verification: bool = True,
        interlock_fail_safe: bool = True,
        policy_cache_ttl_seconds: int = 300,
        policy_max_age_seconds: float | None = None,
    ):
        """
        Initialize safety kernel.

        Args:
            shadow: Shadow twin for state access
            twin_client: HTTP client for task management
            audit_logger: Audit log writer
            policy_submodel_id: ID of PolicyTwin submodel
            require_policy_verification: Whether to require signed policies
            interlock_fail_safe: If True, deny when interlock property missing (fail-safe)
        """
        self._shadow = shadow
        self._twin_client = twin_client
        self._audit = audit_logger
        self._policy_submodel_id = policy_submodel_id
        self._require_verification = require_policy_verification
        self._interlock_fail_safe = interlock_fail_safe
        self._cached_policy: PolicyConfig | None = None
        self._policy_load_time: float = 0
        self._policy_loaded_at: float = 0
        self._policy_hash: str | None = None
        self._policy_cache_ttl_seconds = policy_cache_ttl_seconds
        self._policy_max_age_seconds = policy_max_age_seconds

    def _hash_policy(self, policy_json: str) -> str:
        return hashlib.sha256(policy_json.encode("utf-8")).hexdigest()

    async def load_policy(self, force_reload: bool = False) -> PolicyConfig:
        """
        Load and verify policy from twin.

        Args:
            force_reload: Force reload even if cached

        Returns:
            PolicyConfig object
        """
        # Use cache if fresh (5 minute TTL)
        if not force_reload and self._cached_policy:
            policy_age = time.time() - self._policy_loaded_at
            if self._policy_max_age_seconds and policy_age > self._policy_max_age_seconds:
                logger.warning(
                    "Policy stale, forcing reload",
                    age=round(policy_age, 1),
                    max_age=self._policy_max_age_seconds,
                )
                force_reload = True
            elif time.time() - self._policy_load_time < self._policy_cache_ttl_seconds:
                return self._cached_policy

        submodel = await self._shadow.get_submodel(self._policy_submodel_id)
        if not submodel:
            logger.warning("Policy submodel not found, using defaults")
            self._cached_policy = PolicyConfig()
            self._policy_load_time = time.time()
            return self._cached_policy

        # Try to extract signed policy
        signed = await extract_signed_policy_from_submodel(submodel)

        if signed:
            try:
                policy_dict = verify_and_load_policy(
                    signed,
                    require_verification=self._require_verification,
                )
                config = PolicyConfig.from_dict(policy_dict)
                config.is_verified = signed.is_verified
                self._cached_policy = config
                self._policy_load_time = time.time()
                self._policy_loaded_at = time.time()
                self._policy_hash = self._hash_policy(signed.policy_json)

                self._audit.log(
                    event="policy_loaded",
                    policy_hash=self._policy_hash,
                    verified=config.is_verified,
                    source="signed",
                )

                logger.info(
                    "Policy loaded",
                    verified=config.is_verified,
                    interlocks=len(config.interlocks),
                )
                return config

            except PolicyVerificationError as e:
                logger.error("Policy verification failed", error=str(e))
                if self._require_verification:
                    raise
        else:
            # Try loading from PolicyJson property directly (unsigned)
            for elem in submodel.get("submodelElements", []):
                if elem.get("idShort") == "PolicyJson":
                    value = elem.get("value", "{}")
                    policy_dict = json.loads(value) if isinstance(value, str) else value
                    config = PolicyConfig.from_dict(policy_dict)
                    config.is_verified = False
                    self._cached_policy = config
                    self._policy_load_time = time.time()
                    self._policy_loaded_at = time.time()
                    if isinstance(value, str):
                        self._policy_hash = self._hash_policy(value)
                    else:
                        self._policy_hash = self._hash_policy(json.dumps(value, sort_keys=True))

                    self._audit.log(
                        event="policy_loaded",
                        policy_hash=self._policy_hash,
                        verified=False,
                        source="unsigned",
                    )

                    if self._require_verification:
                        logger.error("Unsigned policy rejected")
                        raise PolicyVerificationError("Unsigned policy rejected")

                    return config

        # Fallback to defaults
        if self._require_verification:
            raise PolicyVerificationError("Signed policy not found")
        self._cached_policy = PolicyConfig()
        self._policy_load_time = time.time()
        self._policy_loaded_at = self._policy_load_time
        self._policy_hash = None
        self._audit.log(
            event="policy_default",
            verified=False,
            source="default",
        )
        return self._cached_policy

    async def evaluate(
        self,
        tool_name: str,
        tool_risk: str,
        roles: tuple[str, ...],
        params: dict[str, Any],
        action_id: str | None = None,
        shadow_freshness: float | None = None,
    ) -> SafetyDecision:
        """
        Evaluate whether a tool call should be allowed.

        Implements the multi-layer defense model:
        1. RBAC check
        2. Interlock evaluation
        3. Simulation forcing
        4. Approval requirement

        Args:
            tool_name: Name of the tool/operation
            tool_risk: Risk level of the operation
            roles: User's roles
            params: Tool parameters
            action_id: Idempotency key for duplicate detection
            shadow_freshness: Age of shadow twin state in seconds

        Returns:
            SafetyDecision with allow/deny and conditions
        """
        try:
            config = await self.load_policy()
        except PolicyVerificationError as e:
            logger.error("Policy verification failed", error=str(e))
            try:
                from twinops.common.metrics import record_safety_decision

                record_safety_decision("denied", "policy_verification")
            except Exception:
                pass
            return SafetyDecision(
                allowed=False,
                reason="Policy verification failed",
            )

        # Log intent with action context
        self._audit.log(
            event="intent",
            tool=tool_name,
            risk=tool_risk,
            roles=roles,
            params=params,
            action_id=action_id,
            shadow_freshness=shadow_freshness,
        )

        # Layer 1: RBAC
        if not self._check_rbac(tool_name, roles, config):
            self._audit.log(
                event="denied",
                tool=tool_name,
                reason="rbac",
                roles=roles,
            )
            try:
                from twinops.common.metrics import record_safety_decision

                record_safety_decision("denied", "rbac")
            except Exception:
                pass
            return SafetyDecision(
                allowed=False,
                reason=f"Role(s) {roles} not authorized for {tool_name}",
            )

        # Layer 2: Interlocks
        interlock_msg = await self._evaluate_interlocks(config)
        if interlock_msg:
            self._audit.log(
                event="denied",
                tool=tool_name,
                reason="interlock",
                message=interlock_msg,
            )
            try:
                from twinops.common.metrics import record_safety_decision

                record_safety_decision("denied", "interlock")
            except Exception:
                pass
            return SafetyDecision(
                allowed=False,
                reason=interlock_msg,
            )

        # Layer 3: Simulation forcing
        risk = RiskLevel(tool_risk)
        force_sim = self._should_force_simulation(risk, params, config)

        # Layer 4: Approval requirement
        require_approval = self._should_require_approval(risk, config)

        decision = SafetyDecision(
            allowed=True,
            force_simulation=force_sim,
            require_approval=require_approval,
        )
        try:
            from twinops.common.metrics import record_safety_decision

            decision_reason = "approval_required" if require_approval else "allowed"
            record_safety_decision("allowed", decision_reason)
        except Exception:
            pass

        return decision

    def _check_rbac(
        self,
        tool_name: str,
        roles: tuple[str, ...],
        config: PolicyConfig,
    ) -> bool:
        """Check if any role is allowed to use the tool."""
        if not config.role_bindings:
            # No RBAC configured = allow all
            return True

        for role in roles:
            binding = config.role_bindings.get(role, {})
            allowed = binding.get("allow", [])

            if "*" in allowed or tool_name in allowed:
                return True

        return False

    async def _evaluate_interlocks(self, config: PolicyConfig) -> str | None:
        """
        Evaluate all interlock predicates.

        Returns:
            Error message if any interlock is violated, None otherwise
        """
        for rule in config.interlocks:
            deny_when = rule.get("deny_when", {})
            submodel_id = deny_when.get("submodel")
            path = deny_when.get("path")
            op = deny_when.get("op")
            threshold = deny_when.get("value")
            interlock_id = rule.get("id", "unknown")

            if not all([submodel_id, path, op]):
                logger.warning(
                    "Interlock rule has missing configuration",
                    interlock_id=interlock_id,
                    submodel_id=submodel_id,
                    path=path,
                    op=op,
                )
                continue

            current = await self._shadow.get_property_value(submodel_id, path)
            if current is None:
                # SAFETY: Missing interlock property is a critical condition
                logger.warning(
                    "Interlock property not found in shadow state",
                    interlock_id=interlock_id,
                    submodel_id=submodel_id,
                    path=path,
                    fail_safe=self._interlock_fail_safe,
                )
                if self._interlock_fail_safe:
                    # Fail-safe: Deny operation when interlock state is unknown
                    return (
                        f"Safety interlock {interlock_id} cannot be evaluated: "
                        f"property {path} not found in submodel {submodel_id}. "
                        f"Operation denied for safety (fail-safe mode)."
                    )
                # Fail-open: Log warning but continue (not recommended for production)
                continue

            if self._violates(current, op, threshold):
                return rule.get("message", f"Interlock {interlock_id} violated")

        return None

    def _violates(self, current: Any, op: str, threshold: Any) -> bool:
        """Check if current value violates the condition."""
        try:
            if op == ">":
                return float(current) > float(threshold)
            elif op == "<":
                return float(current) < float(threshold)
            elif op == ">=":
                return float(current) >= float(threshold)
            elif op == "<=":
                return float(current) <= float(threshold)
            elif op == "==":
                return str(current) == str(threshold)
            elif op == "!=":
                return str(current) != str(threshold)
        except (ValueError, TypeError):
            pass
        return False

    def _should_force_simulation(
        self,
        risk: RiskLevel,
        params: dict[str, Any],
        config: PolicyConfig,
    ) -> bool:
        """Check if simulation should be forced."""
        # Already requesting simulation
        if params.get("simulate", False):
            return False

        # Compare risk levels
        return (
            self.RISK_ORDER[risk]
            >= self.RISK_ORDER[config.require_simulation_for_risk]
        )

    def _should_require_approval(
        self,
        risk: RiskLevel,
        config: PolicyConfig,
    ) -> bool:
        """Check if human approval is required."""
        return (
            self.RISK_ORDER[risk]
            >= self.RISK_ORDER[config.require_approval_for_risk]
        )

    async def create_approval_task(
        self,
        tool_name: str,
        tool_risk: str,
        roles: tuple[str, ...],
        params: dict[str, Any],
        simulation_result: dict[str, Any] | None = None,
        action_id: str | None = None,
    ) -> str:
        """
        Create a human-in-the-loop approval task.

        Args:
            tool_name: Tool name
            tool_risk: Risk level
            roles: Requesting roles
            params: Tool parameters
            simulation_result: Result from simulation run
            action_id: Idempotency key for duplicate detection

        Returns:
            Task ID
        """
        config = await self.load_policy()

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = {
            "task_id": task_id,
            "tool": tool_name,
            "risk": tool_risk,
            "requested_by_roles": list(roles),
            "args": {k: v for k, v in params.items() if k not in ("simulate", "safety_reasoning")},
            "safety_reasoning": params.get("safety_reasoning", ""),
            "status": TaskStatus.PENDING_APPROVAL.value,
            "created_at": time.time(),
        }

        if simulation_result:
            task["simulate_result"] = simulation_result

        # Include action_id for idempotency tracking
        if action_id:
            task["action_id"] = action_id

        await self._twin_client.add_task(
            config.task_submodel_id,
            config.tasks_property_path,
            task,
        )

        self._audit.log(
            event="approval_requested",
            tool=tool_name,
            task_id=task_id,
            roles=roles,
            action_id=action_id,
        )

        logger.info("Approval task created", task_id=task_id, tool=tool_name)
        return task_id

    async def check_task_status(self, task_id: str) -> TaskStatus:
        """
        Check the status of an approval task.

        Args:
            task_id: Task identifier

        Returns:
            Current task status
        """
        config = await self.load_policy()
        tasks = await self._twin_client.get_tasks(
            config.task_submodel_id,
            config.tasks_property_path,
        )

        for task in tasks:
            if task.get("task_id") == task_id:
                return TaskStatus(task.get("status", TaskStatus.PENDING_APPROVAL.value))

        return TaskStatus.EXPIRED

    async def get_pending_tasks(self) -> list[dict[str, Any]]:
        """
        Get all pending approval tasks.

        Returns:
            List of pending tasks
        """
        config = await self.load_policy()
        tasks = await self._twin_client.get_tasks(
            config.task_submodel_id,
            config.tasks_property_path,
        )
        return [
            task for task in tasks
            if task.get("status") == TaskStatus.PENDING_APPROVAL.value
        ]

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """
        Get a specific task by ID.

        Args:
            task_id: Task identifier

        Returns:
            Task details or None if not found
        """
        config = await self.load_policy()
        tasks = await self._twin_client.get_tasks(
            config.task_submodel_id,
            config.tasks_property_path,
        )

        for task in tasks:
            if task.get("task_id") == task_id:
                return task

        return None

    async def get_all_tasks(self) -> list[dict[str, Any]]:
        """
        Get all tasks (pending, approved, rejected).

        Returns:
            List of all tasks
        """
        config = await self.load_policy()
        return await self._twin_client.get_tasks(
            config.task_submodel_id,
            config.tasks_property_path,
        )

    async def approve_task(self, task_id: str, approver: str = "unknown") -> bool:
        """
        Approve a pending task.

        Args:
            task_id: Task identifier
            approver: Identity of the approver

        Returns:
            True if task was approved, False if not found or not pending
        """
        config = await self.load_policy()
        tasks = await self._twin_client.get_tasks(
            config.task_submodel_id,
            config.tasks_property_path,
        )

        for task in tasks:
            if task.get("task_id") == task_id:
                if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
                    logger.warning(
                        "Cannot approve task - not in pending state",
                        task_id=task_id,
                        current_status=task.get("status"),
                    )
                    return False

                task["status"] = TaskStatus.APPROVED.value
                task["approved_by"] = approver
                task["approved_at"] = time.time()

                await self._twin_client.update_tasks(
                    config.task_submodel_id,
                    config.tasks_property_path,
                    tasks,
                )

                self._audit.log(
                    event="approved",
                    task_id=task_id,
                    approved_by=approver,
                )
                logger.info("Task approved", task_id=task_id, approved_by=approver)
                return True

        logger.warning("Task not found for approval", task_id=task_id)
        return False

    async def reject_task(
        self, task_id: str, rejector: str = "unknown", reason: str = ""
    ) -> bool:
        """
        Reject a pending task.

        Args:
            task_id: Task identifier
            rejector: Identity of the rejector
            reason: Reason for rejection

        Returns:
            True if task was rejected, False if not found or not pending
        """
        config = await self.load_policy()
        tasks = await self._twin_client.get_tasks(
            config.task_submodel_id,
            config.tasks_property_path,
        )

        for task in tasks:
            if task.get("task_id") == task_id:
                if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
                    logger.warning(
                        "Cannot reject task - not in pending state",
                        task_id=task_id,
                        current_status=task.get("status"),
                    )
                    return False

                task["status"] = TaskStatus.REJECTED.value
                task["rejected_by"] = rejector
                task["rejected_at"] = time.time()
                task["rejection_reason"] = reason

                await self._twin_client.update_tasks(
                    config.task_submodel_id,
                    config.tasks_property_path,
                    tasks,
                )

                self._audit.log(
                    event="rejected",
                    task_id=task_id,
                    rejected_by=rejector,
                    reason=reason,
                )
                logger.info(
                    "Task rejected",
                    task_id=task_id,
                    rejected_by=rejector,
                    reason=reason,
                )
                return True

        logger.warning("Task not found for rejection", task_id=task_id)
        return False

    async def wait_for_approval(
        self,
        task_id: str,
        timeout: float = 3600.0,
        poll_interval: float = 2.0,
    ) -> tuple[bool, str]:
        """
        Wait for task approval or rejection.

        Args:
            task_id: Task identifier
            timeout: Maximum wait time in seconds
            poll_interval: Time between status checks

        Returns:
            Tuple of (approved, reason)
        """
        import asyncio

        start = time.time()

        while time.time() - start < timeout:
            status = await self.check_task_status(task_id)

            if status == TaskStatus.APPROVED:
                self._audit.log(event="approved", task_id=task_id)
                return True, "Task approved"

            if status == TaskStatus.REJECTED:
                self._audit.log(event="rejected", task_id=task_id)
                return False, "Task rejected by human operator"

            if status == TaskStatus.EXPIRED:
                return False, "Task not found or expired"

            await asyncio.sleep(poll_interval)

        # Timeout
        self._audit.log(event="timeout", task_id=task_id)
        return False, "Approval timeout"

    def log_execution(
        self,
        tool_name: str,
        risk: str,
        roles: tuple[str, ...],
        result: dict[str, Any],
        simulated: bool = False,
        action_id: str | None = None,
    ) -> None:
        """Log a successful execution."""
        self._audit.log(
            event="simulated" if simulated else "executed",
            tool=tool_name,
            risk=risk,
            roles=roles,
            result=result,
            action_id=action_id,
        )

    def log_error(
        self,
        tool_name: str,
        roles: tuple[str, ...],
        error: str,
        action_id: str | None = None,
    ) -> None:
        """Log an execution error."""
        self._audit.log(
            event="error",
            tool=tool_name,
            roles=roles,
            error=error,
            action_id=action_id,
        )
