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
        description="Repository ID for MQTT topic scoping",
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
    job_timeout: float = Field(
        default=300.0,
        description="Maximum time to wait for job completion",
    )
    approval_timeout: float = Field(
        default=3600.0,
        description="Maximum time to wait for human approval",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
