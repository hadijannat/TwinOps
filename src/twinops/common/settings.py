"""Configuration management using pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="TWINOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Twin connection
    twin_base_url: str = Field(
        default="http://localhost:8081",
        description="Base URL for the AAS repository",
    )
    submodel_base_url: str | None = Field(
        default=None,
        description="Base URL for the Submodel repository (if separate from AAS)",
    )
    repo_id: str = Field(
        default="default",
        description="Repository ID for MQTT topic scoping (legacy, prefer aas_repo_id/submodel_repo_id)",
    )
    aas_repo_id: str | None = Field(
        default=None,
        description="Repository ID for AAS repository MQTT topics (defaults to repo_id if not set)",
    )
    submodel_repo_id: str | None = Field(
        default=None,
        description="Repository ID for Submodel repository MQTT topics (defaults to repo_id if not set)",
    )
    aas_id: str = Field(
        default="urn:example:aas:pump-001",
        description="ID of the AAS to manage",
    )

    # MQTT
    mqtt_broker_host: str = Field(
        default="localhost",
        description="MQTT broker hostname",
    )
    mqtt_broker_port: int = Field(
        default=1883,
        description="MQTT broker port",
    )
    mqtt_client_id: str = Field(
        default="twinops-agent",
        description="MQTT client identifier",
    )
    mqtt_username: str | None = Field(
        default=None,
        description="MQTT username (optional)",
    )
    mqtt_password: str | None = Field(
        default=None,
        description="MQTT password (optional)",
    )
    mqtt_tls_enabled: bool = Field(
        default=False,
        description="Enable TLS for MQTT connections",
    )
    mqtt_tls_ca_cert: str | None = Field(
        default=None,
        description="Path to CA certificate for MQTT TLS",
    )
    mqtt_tls_client_cert: str | None = Field(
        default=None,
        description="Path to client certificate for MQTT TLS",
    )
    mqtt_tls_client_key: str | None = Field(
        default=None,
        description="Path to client key for MQTT TLS",
    )

    # HTTP TLS for AAS/Submodel repositories
    twin_tls_enabled: bool = Field(
        default=False,
        description="Enable TLS for TwinClient HTTP connections",
    )
    twin_tls_ca_cert: str | None = Field(
        default=None,
        description="Path to CA certificate for TwinClient TLS",
    )
    twin_tls_client_cert: str | None = Field(
        default=None,
        description="Path to client certificate for TwinClient TLS",
    )
    twin_tls_client_key: str | None = Field(
        default=None,
        description="Path to client key for TwinClient TLS",
    )
    twin_tls_insecure: bool = Field(
        default=False,
        description="Disable TLS verification for TwinClient (not recommended)",
    )

    # LLM
    llm_provider: Literal["anthropic", "openai", "rules"] = Field(
        default="rules",
        description="LLM provider to use",
    )
    anthropic_api_key: str | None = Field(
        default=None,
        description="Anthropic API key",
    )
    openai_api_key: str | None = Field(
        default=None,
        description="OpenAI API key",
    )
    llm_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model identifier for LLM",
    )
    llm_max_tokens: int = Field(
        default=4096,
        description="Maximum tokens for LLM response",
    )
    llm_request_timeout: float = Field(
        default=30.0,
        description="Timeout for LLM API requests in seconds",
    )
    llm_circuit_failure_threshold: int = Field(
        default=3,
        description="Number of consecutive failures before circuit breaker opens",
    )
    llm_circuit_recovery_timeout: float = Field(
        default=60.0,
        description="Seconds to wait before testing if LLM service recovered",
    )
    llm_fallback_enabled: bool = Field(
        default=True,
        description="Enable fallback to rules-based client when LLM circuit opens",
    )

    # Agent
    agent_port: int = Field(
        default=8080,
        description="Port for the agent HTTP server",
    )
    agent_host: str = Field(
        default="0.0.0.0",
        description="Host for the agent HTTP server",
    )
    capability_top_k: int = Field(
        default=12,
        description="Number of top tools to retrieve per query",
    )
    rate_limit_rpm: float = Field(
        default=60.0,
        description="Rate limit in requests per minute",
    )
    agent_workers: int = Field(
        default=1,
        description="Number of Uvicorn worker processes for the agent API",
    )
    metrics_multiprocess_dir: str | None = Field(
        default=None,
        description="Directory for Prometheus multiprocess metrics (required for >1 worker)",
    )

    # Auth
    auth_mode: Literal["none", "mtls"] = Field(
        default="none",
        description="Authentication mode for the agent API",
    )
    auth_exempt_paths: tuple[str, ...] = Field(
        default=("/health", "/ready"),
        description="Paths exempt from authentication",
    )
    opservice_auth_mode: Literal["none", "hmac"] = Field(
        default="none",
        description="Authentication mode for opservice endpoints",
    )
    opservice_auth_exempt_paths: tuple[str, ...] = Field(
        default=("/health", "/metrics"),
        description="Paths exempt from opservice auth",
    )
    opservice_hmac_secret: str | None = Field(
        default=None,
        description="Shared secret for opservice HMAC auth",
    )
    opservice_hmac_header: str = Field(
        default="X-TwinOps-Signature",
        description="Header carrying HMAC signature",
    )
    opservice_hmac_timestamp_header: str = Field(
        default="X-TwinOps-Timestamp",
        description="Header carrying HMAC timestamp",
    )
    opservice_hmac_ttl_seconds: int = Field(
        default=300,
        description="Max age (seconds) for opservice HMAC signatures",
    )
    mtls_role_map: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Mapping of certificate subject to roles (JSON)",
    )
    mtls_allow_unmapped: bool = Field(
        default=False,
        description="Allow unmapped client subjects with default roles",
    )
    mtls_trust_proxy_headers: bool = Field(
        default=False,
        description="Trust mTLS headers from a reverse proxy",
    )
    mtls_subject_header: str = Field(
        default="X-SSL-Client-DN",
        description="Header carrying client subject DN when proxying mTLS",
    )
    mtls_forwarded_cert_header: str = Field(
        default="X-Forwarded-Client-Cert",
        description="Header carrying client certificate details when proxying mTLS",
    )

    # Safety
    default_roles: tuple[str, ...] = Field(
        default=("viewer",),
        description="Default roles when none specified",
    )
    audit_log_path: str = Field(
        default="audit_logs/audit.jsonl",
        description="Path to the audit log file",
    )
    policy_verification_required: bool = Field(
        default=True,
        description="Whether to require policy signature verification",
    )
    policy_cache_ttl_seconds: int = Field(
        default=300,
        description="Seconds to cache a loaded policy before reloading",
    )
    policy_max_age_seconds: float | None = Field(
        default=None,
        description="Maximum policy age in seconds before forcing reload/deny",
    )
    policy_submodel_id: str = Field(
        default="urn:example:submodel:policy",
        description="ID of the PolicyTwin submodel containing safety policies",
    )
    interlock_fail_safe: bool = Field(
        default=True,
        description="If True, deny operations when interlock property is missing (fail-safe). If False, allow with warning (fail-open).",
    )

    # Sandbox/OpService
    sandbox_port: int = Field(
        default=8081,
        description="Port for the sandbox AAS server",
    )
    opservice_port: int = Field(
        default=8087,
        description="Port for the operation service",
    )

    # Timeouts
    http_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds",
    )
    job_poll_interval: float = Field(
        default=1.0,
        description="Interval for polling job status",
    )
    job_poll_max_interval: float = Field(
        default=5.0,
        description="Maximum backoff interval for job polling",
    )
    job_poll_jitter: float = Field(
        default=0.1,
        description="Jitter ratio (0-1) applied to job polling interval",
    )
    job_timeout: float = Field(
        default=300.0,
        description="Maximum time to wait for job completion",
    )
    job_http_fallback_polls: int = Field(
        default=5,
        description="Number of shadow polls without update before falling back to HTTP",
    )
    approval_timeout: float = Field(
        default=3600.0,
        description="Maximum time to wait for human approval",
    )

    # Resilience / concurrency
    twin_client_failure_threshold: int = Field(
        default=5,
        description="Circuit breaker failures before opening",
    )
    twin_client_recovery_timeout: float = Field(
        default=30.0,
        description="Seconds before circuit breaker half-open",
    )
    twin_client_half_open_max_calls: int = Field(
        default=3,
        description="Successful calls to close half-open circuit",
    )
    tool_concurrency_limit: int | None = Field(
        default=None,
        description="Max concurrent tool executions (None = unlimited)",
    )
    llm_concurrency_limit: int | None = Field(
        default=None,
        description="Max concurrent LLM requests (None = unlimited)",
    )
    twin_client_max_concurrency: int | None = Field(
        default=None,
        description="Max concurrent TwinClient HTTP requests (None = unlimited)",
    )
    tool_execution_timeout: float | None = Field(
        default=None,
        description="Max seconds to wait for a tool execution before timing out",
    )
    tool_retry_max_attempts: int = Field(
        default=1,
        description="Max retry attempts for tool execution on transient errors",
    )
    tool_retry_base_delay: float = Field(
        default=0.5,
        description="Base delay for tool retry backoff",
    )
    tool_retry_max_delay: float = Field(
        default=5.0,
        description="Max delay for tool retry backoff",
    )
    tool_retry_jitter: float = Field(
        default=0.2,
        description="Jitter ratio for tool retry backoff",
    )
    tool_idempotency_ttl_seconds: float = Field(
        default=300.0,
        description="TTL for tool idempotency cache entries",
    )
    tool_idempotency_max_entries: int = Field(
        default=1000,
        description="Max entries for tool idempotency cache",
    )
    tool_idempotency_storage: Literal["memory", "sqlite"] = Field(
        default="memory",
        description="Storage backend for idempotency cache",
    )
    tool_idempotency_sqlite_path: str = Field(
        default="data/idempotency.sqlite",
        description="SQLite path for idempotency store",
    )

    # Tracing
    tracing_enabled: bool = Field(
        default=False,
        description="Enable OpenTelemetry tracing",
    )
    tracing_otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP collector endpoint (e.g. http://localhost:4317)",
    )
    tracing_console: bool = Field(
        default=False,
        description="Emit traces to console (debug only)",
    )
    tracing_service_name: str | None = Field(
        default=None,
        description="Service name for tracing (defaults to service-specific)",
    )

    # Startup validation
    startup_timeout: float = Field(
        default=120.0,
        description="Maximum time to wait for dependencies during startup",
    )
    startup_retry_interval: float = Field(
        default=5.0,
        description="Interval between dependency check retries during startup",
    )
    startup_validate_aas: bool = Field(
        default=True,
        description="Validate that the configured AAS exists during startup",
    )

    @property
    def effective_aas_repo_id(self) -> str:
        """Get the effective AAS repository ID (aas_repo_id or fallback to repo_id)."""
        return self.aas_repo_id if self.aas_repo_id is not None else self.repo_id

    @property
    def effective_submodel_repo_id(self) -> str:
        """Get the effective Submodel repository ID (submodel_repo_id or fallback to repo_id)."""
        return self.submodel_repo_id if self.submodel_repo_id is not None else self.repo_id


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
