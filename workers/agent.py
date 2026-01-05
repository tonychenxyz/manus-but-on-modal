"""Worker Agent for executing tasks within a repository.

Uses the Anthropic API with tool use to intelligently navigate and modify
code in the repository.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

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

Be thorough but efficient. Focus on completing the specified goal."""


# Tool definitions for the worker agent
WORKER_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file relative to repo root",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates or overwrites)",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file relative to repo root",
                },
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List contents of a directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to directory relative to repo root (empty for root)",
                    "default": "",
                }
            },
        },
    },
    {
        "name": "search_files",
        "description": "Search for files matching a pattern",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match (e.g., '*.py', 'src/**/*.ts')",
                }
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "search_content",
        "description": "Search for content in files using grep",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: repo root)",
                    "default": "",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the repository",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "git_status",
        "description": "Show git status",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": "Show git diff of changes",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_commit",
        "description": "Create a git commit with the current changes",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"}
            },
            "required": ["message"],
        },
    },
    {
        "name": "create_branch",
        "description": "Create a new git branch",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Branch name"}
            },
            "required": ["name"],
        },
    },
    {
        "name": "complete",
        "description": "Mark the task as complete with a summary",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Summary of what was accomplished",
                },
                "branch_name": {
                    "type": "string",
                    "description": "Name of branch with changes (if any)",
                },
            },
            "required": ["summary"],
        },
    },
]


class WorkerAgent:
    """Agent for executing tasks within a repository.

    Uses Claude with tool use to navigate and modify code.
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
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.worker_model

        self._completed = False
        self._result: WorkerResult | None = None

    async def execute(self) -> WorkerResult:
        """Execute the job.

        Returns:
            The result of the execution
        """
        logger.info(f"Worker agent starting: {self.job_spec.goal}")

        system_prompt = WORKER_SYSTEM_PROMPT.format(goal=self.job_spec.goal)

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"""Please complete the following task:

{self.job_spec.goal}

Start by exploring the repository structure to understand the codebase.""",
            }
        ]

        max_iterations = 50
        iteration = 0

        while not self._completed and iteration < max_iterations:
            iteration += 1

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=WORKER_TOOLS,  # type: ignore
                    messages=messages,  # type: ignore
                )
            except Exception as e:
                logger.error(f"API call failed: {e}")
                return WorkerResult(
                    job_id=self.job_spec.job_id,
                    status=WorkerJobStatus.FAILED,
                    error=f"API error: {e}",
                )

            # Process response
            assistant_content: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                    logger.debug(f"Agent: {block.text[:200]}...")

                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

                    # Execute tool
                    result = await self._handle_tool(block.name, block.input)  # type: ignore

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                    if self._completed:
                        break

            messages.append({"role": "assistant", "content": assistant_content})

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            elif response.stop_reason == "end_turn":
                break

            if self._completed:
                break

        if self._result:
            return self._result

        # If we got here without completing, mark as failed
        return WorkerResult(
            job_id=self.job_spec.job_id,
            status=WorkerJobStatus.FAILED,
            error="Agent did not complete the task",
        )

    async def _handle_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Handle a tool call.

        Args:
            tool_name: Name of the tool
            tool_input: Tool input parameters

        Returns:
            Tool result as a string
        """
        try:
            if tool_name == "read_file":
                return self._read_file(tool_input["path"])
            elif tool_name == "write_file":
                return self._write_file(tool_input["path"], tool_input["content"])
            elif tool_name == "list_directory":
                return self._list_directory(tool_input.get("path", ""))
            elif tool_name == "search_files":
                return self._search_files(tool_input["pattern"])
            elif tool_name == "search_content":
                return self._search_content(
                    tool_input["pattern"], tool_input.get("path", "")
                )
            elif tool_name == "run_command":
                return self._run_command(tool_input["command"])
            elif tool_name == "git_status":
                return self._git_status()
            elif tool_name == "git_diff":
                return self._git_diff()
            elif tool_name == "git_commit":
                return self._git_commit(tool_input["message"])
            elif tool_name == "create_branch":
                return self._create_branch(tool_input["name"])
            elif tool_name == "complete":
                return self._complete(
                    tool_input["summary"], tool_input.get("branch_name")
                )
            else:
                return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return f"Error: {e}"

    def _read_file(self, path: str) -> str:
        """Read a file from the repository."""
        file_path = self.repo_dir / path
        if not file_path.exists():
            return f"File not found: {path}"
        if not file_path.is_file():
            return f"Not a file: {path}"
        try:
            content = file_path.read_text()
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            return content
        except Exception as e:
            return f"Error reading file: {e}"

    def _write_file(self, path: str, content: str) -> str:
        """Write a file to the repository."""
        file_path = self.repo_dir / path
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
            return f"File written: {path}"
        except Exception as e:
            return f"Error writing file: {e}"

    def _list_directory(self, path: str) -> str:
        """List directory contents."""
        dir_path = self.repo_dir / path if path else self.repo_dir
        if not dir_path.exists():
            return f"Directory not found: {path}"
        if not dir_path.is_dir():
            return f"Not a directory: {path}"

        items = []
        for item in sorted(dir_path.iterdir()):
            if item.name.startswith("."):
                continue
            item_type = "dir" if item.is_dir() else "file"
            items.append(f"{item_type}: {item.name}")

        return "\n".join(items[:100]) or "Directory is empty"

    def _search_files(self, pattern: str) -> str:
        """Search for files matching a pattern."""
        import glob

        matches = glob.glob(str(self.repo_dir / pattern), recursive=True)
        relative = [str(Path(m).relative_to(self.repo_dir)) for m in matches[:50]]
        return "\n".join(relative) or "No matches found"

    def _search_content(self, pattern: str, path: str) -> str:
        """Search for content using grep."""
        search_path = self.repo_dir / path if path else self.repo_dir
        result = subprocess.run(
            ["grep", "-rn", "--include=*", pattern, str(search_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout[:10000] if result.stdout else "No matches found"
        return output

    def _run_command(self, command: str) -> str:
        """Run a shell command."""
        # Safety: block some dangerous commands
        blocked = ["rm -rf /", "sudo", "curl", "wget", "nc", "netcat"]
        for b in blocked:
            if b in command.lower():
                return f"Blocked command: {b}"

        result = subprocess.run(
            command,
            shell=True,
            cwd=self.repo_dir,
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

        return output or "Command completed with no output"

    def _git_status(self) -> str:
        """Get git status."""
        result = subprocess.run(
            ["git", "status"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        return result.stdout or result.stderr

    def _git_diff(self) -> str:
        """Get git diff."""
        result = subprocess.run(
            ["git", "diff"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        diff = result.stdout[:20000] if result.stdout else "No changes"
        return diff

    def _git_commit(self, message: str) -> str:
        """Create a git commit."""
        # Add all changes
        subprocess.run(["git", "add", "-A"], cwd=self.repo_dir)

        # Commit
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            # Get commit hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )
            return f"Committed: {hash_result.stdout.strip()}\n{result.stdout}"
        else:
            return f"Commit failed: {result.stderr}"

    def _create_branch(self, name: str) -> str:
        """Create and checkout a new branch."""
        # Sanitize branch name
        safe_name = name.replace(" ", "-").replace("/", "-")

        result = subprocess.run(
            ["git", "checkout", "-b", safe_name],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            return f"Created and switched to branch: {safe_name}"
        else:
            return f"Failed to create branch: {result.stderr}"

    def _complete(self, summary: str, branch_name: str | None) -> str:
        """Mark the task as complete."""
        # Get the current commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        commit_hash = result.stdout.strip() if result.returncode == 0 else None

        # Get current branch if not specified
        if not branch_name:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )
            current_branch = result.stdout.strip()
            # Only set branch_name if it's not the original ref
            if current_branch and current_branch != self.job_spec.repo.ref:
                branch_name = current_branch

        self._completed = True
        self._result = WorkerResult(
            job_id=self.job_spec.job_id,
            status=WorkerJobStatus.SUCCEEDED,
            commit_hash=commit_hash,
            branch_name=branch_name,
            summary=summary,
        )

        return "Task marked as complete"
