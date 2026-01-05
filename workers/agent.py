"""Worker Agent for executing tasks within a repository.

Uses the Claude Agent SDK (ClaudeSDKClient) with full repository access tools
to intelligently navigate and modify code in the repository.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
    tool,
    create_sdk_mcp_server,
)

from shared.config import get_settings
from shared.models import JobSpec, WorkerJobStatus, WorkerResult

logger = logging.getLogger(__name__)


WORKER_SYSTEM_PROMPT = """You are a Worker Agent executing tasks in a code repository.

Your goal: {goal}

You have full access to the repository and can:
- Read and explore files
- Make code changes
- Run tests and builds
- Create commits and branches

Work carefully and methodically:
1. First understand the codebase structure
2. Find the relevant files
3. Make the necessary changes
4. Test your changes if possible
5. Commit your work

When you have completed the task, use the complete_task tool with a summary of what you did.

Be thorough but efficient. Focus on completing the specified goal."""


def create_worker_mcp_server(repo_dir: Path, job_spec: JobSpec):
    """Create an MCP server with worker tools.

    Args:
        repo_dir: Path to the repository
        job_spec: Job specification

    Returns:
        MCP server with worker tools
    """

    # Track completion state
    completion_state = {"completed": False, "result": None}

    @tool(
        "read_file",
        "Read the contents of a file from the repository",
        {"path": str},
    )
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        """Read a file from the repository."""
        file_path = repo_dir / args["path"]
        if not file_path.exists():
            return {"content": [{"type": "text", "text": f"File not found: {args['path']}"}]}
        if not file_path.is_file():
            return {"content": [{"type": "text", "text": f"Not a file: {args['path']}"}]}
        try:
            content = file_path.read_text()
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            return {"content": [{"type": "text", "text": content}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error reading file: {e}"}]}

    @tool(
        "write_file",
        "Write content to a file (creates or overwrites)",
        {"path": str, "content": str},
    )
    async def write_file(args: dict[str, Any]) -> dict[str, Any]:
        """Write a file to the repository."""
        file_path = repo_dir / args["path"]
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(args["content"])
            return {"content": [{"type": "text", "text": f"File written: {args['path']}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error writing file: {e}"}]}

    @tool(
        "list_directory",
        "List contents of a directory",
        {"path": str},
    )
    async def list_directory(args: dict[str, Any]) -> dict[str, Any]:
        """List directory contents."""
        path = args.get("path", "")
        dir_path = repo_dir / path if path else repo_dir
        if not dir_path.exists():
            return {"content": [{"type": "text", "text": f"Directory not found: {path}"}]}
        if not dir_path.is_dir():
            return {"content": [{"type": "text", "text": f"Not a directory: {path}"}]}

        items = []
        for item in sorted(dir_path.iterdir()):
            if item.name.startswith(".") and item.name not in [".gitignore", ".env.example"]:
                continue
            item_type = "dir" if item.is_dir() else "file"
            items.append(f"{item_type}: {item.name}")

        text = "\n".join(items[:100]) or "Directory is empty"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "search_files",
        "Search for files matching a glob pattern",
        {"pattern": str},
    )
    async def search_files(args: dict[str, Any]) -> dict[str, Any]:
        """Search for files matching a pattern."""
        import glob as glob_module

        matches = glob_module.glob(str(repo_dir / args["pattern"]), recursive=True)
        relative = [str(Path(m).relative_to(repo_dir)) for m in matches[:50]]
        text = "\n".join(relative) or "No matches found"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "search_content",
        "Search for content in files using grep",
        {"pattern": str, "path": str},
    )
    async def search_content(args: dict[str, Any]) -> dict[str, Any]:
        """Search for content using grep."""
        path = args.get("path", "")
        search_path = repo_dir / path if path else repo_dir
        result = subprocess.run(
            ["grep", "-rn", "--include=*", args["pattern"], str(search_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout[:10000] if result.stdout else "No matches found"
        return {"content": [{"type": "text", "text": output}]}

    @tool(
        "run_command",
        "Run a shell command in the repository",
        {"command": str},
    )
    async def run_command(args: dict[str, Any]) -> dict[str, Any]:
        """Run a shell command."""
        command = args["command"]

        # Safety: block some dangerous commands
        blocked = ["rm -rf /", "sudo", "curl", "wget", "nc", "netcat"]
        for b in blocked:
            if b in command.lower():
                return {"content": [{"type": "text", "text": f"Blocked command: {b}"}]}

        result = subprocess.run(
            command,
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        output = ""
        if result.stdout:
            output += result.stdout[:5000]
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr[:2000]}"
        if result.returncode != 0:
            output += f"\nExit code: {result.returncode}"

        return {"content": [{"type": "text", "text": output or "Command completed with no output"}]}

    @tool(
        "git_status",
        "Show git status",
        {},
    )
    async def git_status(args: dict[str, Any]) -> dict[str, Any]:
        """Get git status."""
        result = subprocess.run(
            ["git", "status"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        return {"content": [{"type": "text", "text": result.stdout or result.stderr}]}

    @tool(
        "git_diff",
        "Show git diff of changes",
        {},
    )
    async def git_diff(args: dict[str, Any]) -> dict[str, Any]:
        """Get git diff."""
        result = subprocess.run(
            ["git", "diff"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        diff = result.stdout[:20000] if result.stdout else "No changes"
        return {"content": [{"type": "text", "text": diff}]}

    @tool(
        "git_commit",
        "Create a git commit with the current changes",
        {"message": str},
    )
    async def git_commit(args: dict[str, Any]) -> dict[str, Any]:
        """Create a git commit."""
        # Add all changes
        subprocess.run(["git", "add", "-A"], cwd=repo_dir)

        # Commit
        result = subprocess.run(
            ["git", "commit", "-m", args["message"]],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            # Get commit hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            return {
                "content": [{
                    "type": "text",
                    "text": f"Committed: {hash_result.stdout.strip()}\n{result.stdout}",
                }]
            }
        else:
            return {"content": [{"type": "text", "text": f"Commit failed: {result.stderr}"}]}

    @tool(
        "create_branch",
        "Create a new git branch",
        {"name": str},
    )
    async def create_branch(args: dict[str, Any]) -> dict[str, Any]:
        """Create and checkout a new branch."""
        # Sanitize branch name
        safe_name = args["name"].replace(" ", "-").replace("/", "-")

        result = subprocess.run(
            ["git", "checkout", "-b", safe_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Created and switched to branch: {safe_name}",
                }]
            }
        else:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Failed to create branch: {result.stderr}",
                }]
            }

    @tool(
        "complete_task",
        "Mark the task as complete with a summary of what was accomplished",
        {"summary": str, "branch_name": str},
    )
    async def complete_task(args: dict[str, Any]) -> dict[str, Any]:
        """Mark the task as complete."""
        # Get the current commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        commit_hash = result.stdout.strip() if result.returncode == 0 else None

        # Get current branch if not specified
        branch_name = args.get("branch_name")
        if not branch_name:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            current_branch = result.stdout.strip()
            # Only set branch_name if it's not the original ref
            if current_branch and current_branch != job_spec.repo.ref:
                branch_name = current_branch

        completion_state["completed"] = True
        completion_state["result"] = WorkerResult(
            job_id=job_spec.job_id,
            status=WorkerJobStatus.SUCCEEDED,
            commit_hash=commit_hash,
            branch_name=branch_name,
            summary=args["summary"],
        )

        return {"content": [{"type": "text", "text": "Task marked as complete"}]}

    # Create MCP server with all tools
    server = create_sdk_mcp_server(
        name="worker-tools",
        version="1.0.0",
        tools=[
            read_file,
            write_file,
            list_directory,
            search_files,
            search_content,
            run_command,
            git_status,
            git_diff,
            git_commit,
            create_branch,
            complete_task,
        ],
    )

    # Attach completion state accessor
    server.get_completion_state = lambda: completion_state  # type: ignore

    return server


# Tool names for the worker
WORKER_TOOL_NAMES = [
    "mcp__worker-tools__read_file",
    "mcp__worker-tools__write_file",
    "mcp__worker-tools__list_directory",
    "mcp__worker-tools__search_files",
    "mcp__worker-tools__search_content",
    "mcp__worker-tools__run_command",
    "mcp__worker-tools__git_status",
    "mcp__worker-tools__git_diff",
    "mcp__worker-tools__git_commit",
    "mcp__worker-tools__create_branch",
    "mcp__worker-tools__complete_task",
]


class WorkerAgent:
    """Agent for executing tasks within a repository.

    Uses the Claude Agent SDK with MCP tools to navigate and modify code.
    """

    def __init__(
        self,
        job_spec: JobSpec,
        repo_dir: Path,
        github_token: str | None = None,
    ):
        """Initialize the worker agent.

        Args:
            job_spec: The job specification
            repo_dir: Path to the cloned repository
            github_token: GitHub token for auth
        """
        self.job_spec = job_spec
        self.repo_dir = repo_dir
        self.github_token = github_token

        settings = get_settings()
        self.model = settings.worker_model

        # Create MCP server with worker tools
        self.mcp_server = create_worker_mcp_server(repo_dir, job_spec)

    def _get_agent_options(self) -> ClaudeAgentOptions:
        """Get ClaudeAgentOptions for the worker agent.

        Returns:
            Configured ClaudeAgentOptions
        """
        system_prompt = WORKER_SYSTEM_PROMPT.format(goal=self.job_spec.goal)

        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            permission_mode="acceptEdits",
            max_turns=50,
            cwd=str(self.repo_dir),
            mcp_servers={"worker": self.mcp_server},
            allowed_tools=WORKER_TOOL_NAMES,
        )

    async def execute(self) -> WorkerResult:
        """Execute the job using the Agent SDK.

        Returns:
            The result of the execution
        """
        logger.info(f"Worker agent starting: {self.job_spec.goal}")

        prompt = f"""Please complete the following task:

{self.job_spec.goal}

Start by exploring the repository structure to understand the codebase.
When you are done, use the complete_task tool with a summary of what you accomplished."""

        try:
            options = self._get_agent_options()

            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                logger.debug(f"Agent: {block.text[:200]}...")
                            elif isinstance(block, ToolUseBlock):
                                logger.debug(f"Tool: {block.name}")

                    elif isinstance(message, ResultMessage):
                        logger.info(
                            f"Worker completed: {message.num_turns} turns, "
                            f"{message.duration_ms}ms"
                        )

            # Check completion state
            completion_state = self.mcp_server.get_completion_state()  # type: ignore
            if completion_state["completed"] and completion_state["result"]:
                return completion_state["result"]

            # If not explicitly completed, check if there were changes
            return self._create_implicit_result()

        except Exception as e:
            logger.error(f"Worker agent failed: {e}")
            return WorkerResult(
                job_id=self.job_spec.job_id,
                status=WorkerJobStatus.FAILED,
                error=str(e),
            )

    def _create_implicit_result(self) -> WorkerResult:
        """Create a result when agent didn't explicitly complete.

        Returns:
            WorkerResult based on current state
        """
        # Check for changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            # There are uncommitted changes
            return WorkerResult(
                job_id=self.job_spec.job_id,
                status=WorkerJobStatus.FAILED,
                error="Agent made changes but did not commit or complete",
            )

        # Check for commits beyond the original ref
        result = subprocess.run(
            ["git", "log", f"{self.job_spec.repo.ref}..HEAD", "--oneline"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            # There are new commits
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )

            return WorkerResult(
                job_id=self.job_spec.job_id,
                status=WorkerJobStatus.SUCCEEDED,
                commit_hash=hash_result.stdout.strip(),
                branch_name=branch_result.stdout.strip() or None,
                summary="Task completed (agent did not provide explicit summary)",
            )

        # No changes at all
        return WorkerResult(
            job_id=self.job_spec.job_id,
            status=WorkerJobStatus.FAILED,
            error="Agent did not complete the task or make any changes",
        )
