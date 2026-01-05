"""Tools available to the Home Orchestrator Agent.

These are the only tools the orchestrator can use - no repo access, just
coordination and memory management.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agent_home.persistence import EventStore, StateManager

logger = logging.getLogger(__name__)


class SpawnWorkerInput(BaseModel):
    """Input for spawning a worker job."""

    repo_url: str = Field(..., description="Git repository URL")
    ref: str = Field(default="main", description="Git ref (branch, tag, or commit)")
    goal: str = Field(..., description="What the worker should accomplish")
    kind: str = Field(
        default="implement",
        description="Kind of work: implement, test, investigate, refactor, build",
    )
    produce_pr: bool = Field(
        default=False, description="Whether to create a pull request"
    )
    bootstrap_commands: list[str] = Field(
        default_factory=list, description="Commands to run after cloning"
    )


class ReadMemoryInput(BaseModel):
    """Input for reading from memory."""

    path: str = Field(..., description="Path relative to memory directory")


class WriteMemoryInput(BaseModel):
    """Input for writing to memory."""

    path: str = Field(..., description="Path relative to memory directory")
    content: str = Field(..., description="Content to write")
    append: bool = Field(default=False, description="Append instead of overwrite")


class ListMemoryInput(BaseModel):
    """Input for listing memory contents."""

    path: str = Field(default="", description="Path relative to memory directory")


class CreateSelfPromptInput(BaseModel):
    """Input for creating a self-prompt."""

    topic: str = Field(..., description="Topic/subject of the self-prompt")
    content: str = Field(
        ..., description="The prompt/reminder content for future processing"
    )
    priority: str = Field(default="normal", description="Priority: low, normal, high")


# Tool definitions for the Anthropic API
ORCHESTRATOR_TOOLS = [
    {
        "name": "spawn_worker",
        "description": (
            "Spawn a worker sandbox to perform work on a repository. "
            "Workers can clone repos, run tests, make changes, and create PRs. "
            "Use this when you need to execute code or make changes to a codebase."
        ),
        "input_schema": SpawnWorkerInput.model_json_schema(),
    },
    {
        "name": "read_memory",
        "description": (
            "Read content from the agent's persistent memory. "
            "Memory contains facts, preferences, project notes, and skills."
        ),
        "input_schema": ReadMemoryInput.model_json_schema(),
    },
    {
        "name": "write_memory",
        "description": (
            "Write content to the agent's persistent memory. "
            "Use this to store important facts, preferences, or project information."
        ),
        "input_schema": WriteMemoryInput.model_json_schema(),
    },
    {
        "name": "list_memory",
        "description": "List files and directories in the agent's memory.",
        "input_schema": ListMemoryInput.model_json_schema(),
    },
    {
        "name": "create_self_prompt",
        "description": (
            "Create a self-prompt for later processing. "
            "Use this to remind yourself to review something, follow up on a task, "
            "or note something for future attention."
        ),
        "input_schema": CreateSelfPromptInput.model_json_schema(),
    },
]


class OrchestratorToolHandler:
    """Handles tool execution for the orchestrator agent."""

    def __init__(
        self,
        memory_path: Path,
        state_manager: "StateManager",
        event_store: "EventStore",
        worker_spawner: Any = None,  # Will be set by orchestrator
    ):
        """Initialize the tool handler.

        Args:
            memory_path: Path to the memory directory
            state_manager: State manager for persistence
            event_store: Event store for logging
            worker_spawner: Callback to spawn worker jobs
        """
        self.memory_path = memory_path
        self.state_manager = state_manager
        self.event_store = event_store
        self.worker_spawner = worker_spawner
        self._pending_workers: list[dict[str, Any]] = []

    async def handle_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        run_id: str,
    ) -> str:
        """Handle a tool call from the agent.

        Args:
            tool_name: Name of the tool
            tool_input: Tool input parameters
            run_id: Current run ID

        Returns:
            Tool result as a string
        """
        try:
            if tool_name == "spawn_worker":
                return await self._spawn_worker(tool_input, run_id)
            elif tool_name == "read_memory":
                return await self._read_memory(tool_input)
            elif tool_name == "write_memory":
                return await self._write_memory(tool_input)
            elif tool_name == "list_memory":
                return await self._list_memory(tool_input)
            elif tool_name == "create_self_prompt":
                return await self._create_self_prompt(tool_input)
            else:
                return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return f"Error: {e}"

    async def _spawn_worker(
        self, input_data: dict[str, Any], run_id: str
    ) -> str:
        """Handle spawn_worker tool call.

        In planning mode, this just records the intent. In execution mode,
        it actually spawns the worker.
        """
        from shared.models import (
            JobConstraints,
            JobSpec,
            RepoRef,
            RuntimeProfile,
            WorkerJobKind,
        )
        import uuid

        spec = JobSpec(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            repo=RepoRef(
                url=input_data["repo_url"],
                ref=input_data.get("ref", "main"),
            ),
            goal=input_data["goal"],
            kind=WorkerJobKind(input_data.get("kind", "implement")),
            constraints=JobConstraints(
                produce_pr=input_data.get("produce_pr", False),
            ),
            runtime_profile=RuntimeProfile(
                bootstrap=input_data.get("bootstrap_commands", []),
            ),
        )

        # Record the pending worker
        self._pending_workers.append({
            "job_id": spec.job_id,
            "spec": spec,
            "run_id": run_id,
        })

        return json.dumps({
            "status": "queued",
            "job_id": spec.job_id,
            "message": f"Worker job {spec.job_id} queued for: {spec.goal}",
        })

    def get_pending_workers(self) -> list[dict[str, Any]]:
        """Get list of pending worker specs and clear the list."""
        workers = self._pending_workers.copy()
        self._pending_workers = []
        return workers

    async def _read_memory(self, input_data: dict[str, Any]) -> str:
        """Handle read_memory tool call."""
        path = self.memory_path / input_data["path"]

        if not path.exists():
            return f"File not found: {input_data['path']}"

        if not path.is_file():
            return f"Not a file: {input_data['path']}"

        # Security check - ensure path is within memory directory
        try:
            path.resolve().relative_to(self.memory_path.resolve())
        except ValueError:
            return "Access denied: path outside memory directory"

        try:
            content = path.read_text()
            return content
        except Exception as e:
            return f"Error reading file: {e}"

    async def _write_memory(self, input_data: dict[str, Any]) -> str:
        """Handle write_memory tool call."""
        path = self.memory_path / input_data["path"]

        # Security check
        try:
            path.resolve().relative_to(self.memory_path.resolve())
        except ValueError:
            return "Access denied: path outside memory directory"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            if input_data.get("append", False) and path.exists():
                with open(path, "a") as f:
                    f.write(input_data["content"])
            else:
                path.write_text(input_data["content"])

            return f"Successfully wrote to {input_data['path']}"
        except Exception as e:
            return f"Error writing file: {e}"

    async def _list_memory(self, input_data: dict[str, Any]) -> str:
        """Handle list_memory tool call."""
        path = self.memory_path / input_data.get("path", "")

        if not path.exists():
            return f"Path not found: {input_data.get('path', '')}"

        if not path.is_dir():
            return f"Not a directory: {input_data.get('path', '')}"

        # Security check
        try:
            path.resolve().relative_to(self.memory_path.resolve())
        except ValueError:
            return "Access denied: path outside memory directory"

        try:
            items = []
            for item in sorted(path.iterdir()):
                item_type = "dir" if item.is_dir() else "file"
                items.append(f"{item_type}: {item.name}")

            if not items:
                return "Directory is empty"

            return "\n".join(items)
        except Exception as e:
            return f"Error listing directory: {e}"

    async def _create_self_prompt(self, input_data: dict[str, Any]) -> str:
        """Handle create_self_prompt tool call."""
        inbox_path = self.memory_path / "inbox"
        inbox_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        topic = input_data["topic"].replace(" ", "_").lower()[:30]
        filename = f"{timestamp}__{topic}.md"

        file_path = inbox_path / filename

        content = f"""# Self-Prompt: {input_data['topic']}

**Created:** {datetime.utcnow().isoformat()}
**Priority:** {input_data.get('priority', 'normal')}

## Content

{input_data['content']}
"""

        try:
            file_path.write_text(content)
            return f"Created self-prompt: {filename}"
        except Exception as e:
            return f"Error creating self-prompt: {e}"
