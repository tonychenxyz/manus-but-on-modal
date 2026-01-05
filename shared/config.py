"""Configuration settings for Agent Home Orchestrator."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_HOME_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic API
    anthropic_api_key: str = ""

    # Modal configuration
    modal_app_name: str = "agent-home-orchestrator"
    modal_volume_name: str = "agent-home-volume"
    agent_home_timeout_hours: int = 23  # Max sandbox timeout, leave buffer for 24h limit
    worker_timeout_minutes: int = 60  # Default worker timeout

    # Agent Home paths (relative to volume mount)
    volume_mount_path: str = "/agent_home"
    db_path: str = "db/state.sqlite"
    conversations_path: str = "conversations"
    runs_path: str = "runs"
    memory_path: str = "memory"
    skills_path: str = ".claude/skills"

    # Gateway settings
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8080
    max_events_per_request: int = 200
    event_stream_buffer_size: int = 1000

    # Concurrency limits
    max_concurrent_runs: int = 5
    max_concurrent_workers: int = 10
    max_queued_runs_per_conversation: int = 5

    # Auth settings
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # GitHub settings
    github_token: str = ""
    github_app_id: str = ""
    github_app_private_key: str = ""

    # Curator settings
    curator_interval_seconds: int = 600  # 10 minutes
    curator_batch_size: int = 100

    # Model settings
    orchestrator_model: str = "claude-sonnet-4-20250514"
    worker_model: str = "claude-sonnet-4-20250514"


def get_settings() -> Settings:
    """Get application settings (cached)."""
    return Settings()
