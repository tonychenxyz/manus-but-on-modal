"""Modal app definition and image configuration.

Defines the Modal app, images, and volumes used by Agent Home.
"""

from __future__ import annotations

import modal

# Create the Modal app
app = modal.App("agent-home-orchestrator")

# Create the persistent volume for Agent Home data
agent_home_volume = modal.Volume.from_name(
    "agent-home-volume",
    create_if_missing=True,
)

# Define the Agent Home image with all required dependencies
agent_home_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        # Claude Agent SDK (Claude Code as a library)
        "claude-agent-sdk>=0.1.0",
        "anthropic>=0.42.0",
        # Web framework
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.32.0",
        "websockets>=13.0",
        # Database
        "aiosqlite>=0.20.0",
        # Data validation
        "pydantic>=2.10.0",
        "pydantic-settings>=2.6.0",
        # Auth
        "python-jose[cryptography]>=3.3.0",
        # HTTP client
        "httpx>=0.28.0",
    )
    .add_local_python_source("modal_app")
    .add_local_python_source("shared")
    .add_local_python_source("agent_home")
)

# Define the Worker image with code execution tools
worker_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "curl", "gh")
    .pip_install(
        # Claude Agent SDK (Claude Code as a library)
        "claude-agent-sdk>=0.1.0",
        "anthropic>=0.42.0",
        # Data validation
        "pydantic>=2.10.0",
        "pydantic-settings>=2.6.0",
        # Common language toolchains
        "uv",  # Python package manager
    )
    .add_local_python_source("modal_app")
    .add_local_python_source("shared")
    .add_local_python_source("workers")
)


# Volume mount path
VOLUME_MOUNT_PATH = "/agent_home"
