"""MCP Server with tools for the Home Orchestrator Agent.

Defines custom tools using the Agent SDK's @tool decorator and creates
an MCP server that can be used with ClaudeSDKClient.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool, create_sdk_mcp_server

if TYPE_CHECKING:
    from agent_home.persistence import EventStore, StateManager

logger = logging.getLogger(__name__)


def create_orchestrator_mcp_server(
    memory_path: Path,
    state_manager: "StateManager",
    event_store: "EventStore",
    pending_workers_callback: Any = None,
):
    """Create an MCP server with orchestrator tools.

    Args:
        memory_path: Path to the memory directory
        state_manager: State manager for persistence
        event_store: Event store for logging
        pending_workers_callback: Callback to register pending workers

    Returns:
        MCP server instance with registered tools
    """

    # Store for pending workers (used during planning)
    pending_workers: list[dict[str, Any]] = []

    @tool(
        "spawn_worker",
        "Spawn a worker sandbox to perform work on a repository. "
        "Workers can clone repos, run tests, make changes, and create PRs. "
        "Use this when you need to execute code or make changes to a codebase.",
        {
            "repo_url": str,
            "ref": str,
            "goal": str,
            "kind": str,
            "produce_pr": bool,
            "bootstrap_commands": list,
        },
    )
    async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
        """Spawn a worker job for code execution."""
        from shared.models import (
            JobConstraints,
            JobSpec,
            RepoRef,
            RuntimeProfile,
            WorkerJobKind,
        )

        job_id = f"job_{uuid.uuid4().hex[:12]}"

        spec = JobSpec(
            job_id=job_id,
            repo=RepoRef(
                url=args["repo_url"],
                ref=args.get("ref", "main"),
            ),
            goal=args["goal"],
            kind=WorkerJobKind(args.get("kind", "implement")),
            constraints=JobConstraints(
                produce_pr=args.get("produce_pr", False),
            ),
            runtime_profile=RuntimeProfile(
                bootstrap=args.get("bootstrap_commands", []),
            ),
        )

        # Record the pending worker
        pending_workers.append({
            "job_id": job_id,
            "spec": spec,
        })

        if pending_workers_callback:
            pending_workers_callback(pending_workers[-1])

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "status": "queued",
                    "job_id": job_id,
                    "message": f"Worker job {job_id} queued for: {spec.goal}",
                }),
            }]
        }

    @tool(
        "read_memory",
        "Read content from the agent's persistent memory. "
        "Memory contains facts, preferences, project notes, and skills.",
        {"path": str},
    )
    async def read_memory(args: dict[str, Any]) -> dict[str, Any]:
        """Read a file from memory."""
        path = memory_path / args["path"]

        if not path.exists():
            return {
                "content": [{
                    "type": "text",
                    "text": f"File not found: {args['path']}",
                }]
            }

        if not path.is_file():
            return {
                "content": [{
                    "type": "text",
                    "text": f"Not a file: {args['path']}",
                }]
            }

        # Security check
        try:
            path.resolve().relative_to(memory_path.resolve())
        except ValueError:
            return {
                "content": [{
                    "type": "text",
                    "text": "Access denied: path outside memory directory",
                }]
            }

        try:
            content = path.read_text()
            return {
                "content": [{
                    "type": "text",
                    "text": content,
                }]
            }
        except Exception as e:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error reading file: {e}",
                }]
            }

    @tool(
        "write_memory",
        "Write content to the agent's persistent memory. "
        "Use this to store important facts, preferences, or project information.",
        {"path": str, "content": str, "append": bool},
    )
    async def write_memory(args: dict[str, Any]) -> dict[str, Any]:
        """Write a file to memory."""
        path = memory_path / args["path"]

        # Security check
        try:
            path.resolve().relative_to(memory_path.resolve())
        except ValueError:
            return {
                "content": [{
                    "type": "text",
                    "text": "Access denied: path outside memory directory",
                }]
            }

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            if args.get("append", False) and path.exists():
                with open(path, "a") as f:
                    f.write(args["content"])
            else:
                path.write_text(args["content"])

            return {
                "content": [{
                    "type": "text",
                    "text": f"Successfully wrote to {args['path']}",
                }]
            }
        except Exception as e:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error writing file: {e}",
                }]
            }

    @tool(
        "list_memory",
        "List files and directories in the agent's memory.",
        {"path": str},
    )
    async def list_memory(args: dict[str, Any]) -> dict[str, Any]:
        """List directory contents in memory."""
        path = memory_path / args.get("path", "")

        if not path.exists():
            return {
                "content": [{
                    "type": "text",
                    "text": f"Path not found: {args.get('path', '')}",
                }]
            }

        if not path.is_dir():
            return {
                "content": [{
                    "type": "text",
                    "text": f"Not a directory: {args.get('path', '')}",
                }]
            }

        # Security check
        try:
            path.resolve().relative_to(memory_path.resolve())
        except ValueError:
            return {
                "content": [{
                    "type": "text",
                    "text": "Access denied: path outside memory directory",
                }]
            }

        try:
            items = []
            for item in sorted(path.iterdir()):
                item_type = "dir" if item.is_dir() else "file"
                items.append(f"{item_type}: {item.name}")

            if not items:
                return {
                    "content": [{
                        "type": "text",
                        "text": "Directory is empty",
                    }]
                }

            return {
                "content": [{
                    "type": "text",
                    "text": "\n".join(items),
                }]
            }
        except Exception as e:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error listing directory: {e}",
                }]
            }

    @tool(
        "create_self_prompt",
        "Create a self-prompt for later processing. "
        "Use this to remind yourself to review something, follow up on a task, "
        "or note something for future attention.",
        {"topic": str, "content": str, "priority": str},
    )
    async def create_self_prompt(args: dict[str, Any]) -> dict[str, Any]:
        """Create a self-prompt file."""
        inbox_path = memory_path / "inbox"
        inbox_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        topic = args["topic"].replace(" ", "_").lower()[:30]
        filename = f"{timestamp}__{topic}.md"

        file_path = inbox_path / filename

        content = f"""# Self-Prompt: {args['topic']}

**Created:** {datetime.utcnow().isoformat()}
**Priority:** {args.get('priority', 'normal')}

## Content

{args['content']}
"""

        try:
            file_path.write_text(content)
            return {
                "content": [{
                    "type": "text",
                    "text": f"Created self-prompt: {filename}",
                }]
            }
        except Exception as e:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error creating self-prompt: {e}",
                }]
            }

    @tool(
        "get_conversation_history",
        "Get recent messages from the current conversation for context.",
        {"limit": int},
    )
    async def get_conversation_history(args: dict[str, Any]) -> dict[str, Any]:
        """Get conversation history (placeholder - needs conversation_id)."""
        return {
            "content": [{
                "type": "text",
                "text": "Conversation history is available in the session context.",
            }]
        }

    # Helper to get and clear pending workers
    def get_pending_workers() -> list[dict[str, Any]]:
        """Get and clear pending workers."""
        workers = pending_workers.copy()
        pending_workers.clear()
        return workers

    # Create MCP server with all tools
    server = create_sdk_mcp_server(
        name="orchestrator-tools",
        version="1.0.0",
        tools=[
            spawn_worker,
            read_memory,
            write_memory,
            list_memory,
            create_self_prompt,
            get_conversation_history,
        ],
    )

    # Attach helper method
    server.get_pending_workers = get_pending_workers  # type: ignore

    return server


# Tool names for reference (with MCP prefix)
ORCHESTRATOR_TOOL_NAMES = [
    "mcp__orchestrator-tools__spawn_worker",
    "mcp__orchestrator-tools__read_memory",
    "mcp__orchestrator-tools__write_memory",
    "mcp__orchestrator-tools__list_memory",
    "mcp__orchestrator-tools__create_self_prompt",
    "mcp__orchestrator-tools__get_conversation_history",
]
