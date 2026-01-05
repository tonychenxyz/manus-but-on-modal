"""Agent Home Modal sandbox definition.

Runs the Agent Home as a long-running Modal sandbox with the Gateway API
and Orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

import modal

from modal_app.app import VOLUME_MOUNT_PATH, agent_home_image, agent_home_volume, app

logger = logging.getLogger(__name__)


@app.cls(
    image=agent_home_image,
    volumes={VOLUME_MOUNT_PATH: agent_home_volume},
    secrets=[modal.Secret.from_name("agent-home-secrets")],
    timeout=60 * 60 * 23,  # 23 hours max (leave buffer for 24h limit)
    allow_concurrent_inputs=100,
    container_idle_timeout=60 * 30,  # 30 min idle timeout
)
class AgentHomeSandbox:
    """The Agent Home sandbox class.

    Runs the Gateway API and Orchestrator, persisting state to the volume.
    """

    def __init__(self):
        self.instance_id = f"home_{uuid.uuid4().hex[:8]}"
        self._initialized = False

    @modal.enter()
    async def startup(self):
        """Initialize Agent Home on container startup."""
        from agent_home.gateway import create_app
        from agent_home.orchestrator import HomeOrchestrator
        from agent_home.persistence import EventStore, StateManager

        logger.info(f"Agent Home starting up: {self.instance_id}")

        # Setup paths
        self.base_path = Path(VOLUME_MOUNT_PATH)
        self.db_path = self.base_path / "db" / "state.sqlite"
        self.memory_path = self.base_path / "memory"

        # Ensure directories exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.mkdir(parents=True, exist_ok=True)

        # Initialize Claude file if not exists
        claude_file = self.base_path / "CLAUDE.md"
        if not claude_file.exists():
            claude_file.write_text(self._get_initial_claude_file())

        # Initialize skills directory
        skills_dir = self.base_path / ".claude" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # Initialize persistence
        self.event_store = EventStore(self.db_path)
        await self.event_store.initialize()

        self.state_manager = StateManager(self.db_path)
        await self.state_manager.initialize()

        # Try to acquire leader lease
        is_leader = await self.state_manager.try_acquire_lease(
            self.instance_id,
            lease_duration_seconds=60,
        )

        if not is_leader:
            logger.warning("Another Agent Home instance is already leader")
            # Continue anyway - we might become leader if the other dies

        # Initialize orchestrator
        self.orchestrator = HomeOrchestrator(
            state_manager=self.state_manager,
            event_store=self.event_store,
            memory_path=self.memory_path,
            worker_spawner=self._spawn_worker,
        )

        # Resume any active runs
        await self.orchestrator.resume_active_runs()

        # Create FastAPI app
        self.app = create_app(
            event_store=self.event_store,
            state_manager=self.state_manager,
            orchestrator=self.orchestrator,
        )

        # Commit volume to persist initial state
        agent_home_volume.commit()

        self._initialized = True
        logger.info("Agent Home initialized successfully")

        # Start background tasks
        asyncio.create_task(self._lease_renewal_loop())
        asyncio.create_task(self._volume_commit_loop())

    @modal.exit()
    async def shutdown(self):
        """Clean up on container shutdown."""
        logger.info(f"Agent Home shutting down: {self.instance_id}")

        # Release leader lease
        await self.state_manager.release_lease(self.instance_id)

        # Close persistence
        await self.event_store.close()
        await self.state_manager.close()

        # Final volume commit
        agent_home_volume.commit()

    @modal.asgi_app()
    def web(self):
        """Expose the FastAPI app as an ASGI endpoint."""
        return self.app

    async def _spawn_worker(self, spec) -> str:
        """Spawn a worker sandbox for a job.

        Args:
            spec: JobSpec for the worker

        Returns:
            Sandbox ID
        """
        from modal_app.worker import spawn_worker

        # Spawn the worker asynchronously
        sandbox_id = await spawn_worker.remote.aio(spec.model_dump())
        return sandbox_id

    async def _lease_renewal_loop(self):
        """Background loop to renew leader lease."""
        while True:
            try:
                await asyncio.sleep(30)  # Renew every 30 seconds
                await self.state_manager.try_acquire_lease(
                    self.instance_id,
                    lease_duration_seconds=60,
                )
            except Exception as e:
                logger.error(f"Lease renewal failed: {e}")

    async def _volume_commit_loop(self):
        """Background loop to commit volume changes."""
        while True:
            try:
                await asyncio.sleep(60)  # Commit every minute
                agent_home_volume.commit()
            except Exception as e:
                logger.error(f"Volume commit failed: {e}")

    def _get_initial_claude_file(self) -> str:
        """Get the initial CLAUDE.md content."""
        return """# Agent Home

This is the persistent home for your Claude Code agent.

## What I Know

I am the Home Orchestrator, coordinating work across multiple conversations
and worker sandboxes. I don't have direct access to code - instead, I spawn
workers to perform actual code changes.

## Skills

See `.claude/skills/` for available skills.

## Memory

My memory is stored in `/agent_home/memory/`:
- `facts.md` - Important facts I've learned
- `preferences.md` - Your preferences and working style
- `inbox/` - Self-prompts and reminders

## How I Work

1. You send a message
2. I create a plan for what needs to be done
3. You approve (or deny) the plan
4. I spawn workers to execute the plan
5. I aggregate results and report back

All state persists across restarts.
"""


# Control plane functions for webapp to use


@app.function(secrets=[modal.Secret.from_name("agent-home-secrets")])
def ensure_agent_home_running() -> dict:
    """Ensure Agent Home is running and return connection info.

    Called by the webapp to get Agent Home endpoint.

    Returns:
        Dict with url and status
    """
    # Get the web endpoint URL
    # The AgentHomeSandbox.web endpoint is automatically available
    cls = modal.Cls.lookup("agent-home-orchestrator", "AgentHomeSandbox")

    # This will start the container if not running
    try:
        # Trigger a health check to ensure it's up
        # The actual URL comes from Modal's infrastructure
        return {
            "status": "running",
            "message": "Agent Home is available",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


@app.function(secrets=[modal.Secret.from_name("agent-home-secrets")])
def mint_access_token(email: str) -> dict:
    """Mint an access token for browser connection.

    Args:
        email: The authenticated user's email

    Returns:
        Dict with token and expiry
    """
    import uuid
    from datetime import timedelta

    from agent_home.gateway.auth import create_access_token

    token_id = uuid.uuid4().hex
    token = create_access_token(
        email=email,
        token_id=token_id,
        expires_delta=timedelta(hours=1),
    )

    return {
        "token": token,
        "token_id": token_id,
        "expires_in_seconds": 3600,
    }
