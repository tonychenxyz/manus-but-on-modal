"""Worker runner for executing jobs in Modal sandboxes.

The runner handles:
- Cloning repositories
- Running bootstrap commands
- Executing the worker agent or direct commands
- Creating PRs and reporting results
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.models import JobSpec, WorkerJobStatus, WorkerResult

logger = logging.getLogger(__name__)


class WorkerRunner:
    """Executes worker jobs in a sandboxed environment.

    Responsible for:
    - Setting up the repository
    - Running bootstrap commands
    - Executing the work (via agent or commands)
    - Producing results
    """

    def __init__(
        self,
        job_spec: JobSpec,
        work_dir: Path | None = None,
        github_token: str | None = None,
    ):
        """Initialize the worker runner.

        Args:
            job_spec: The job specification
            work_dir: Working directory (created if not provided)
            github_token: GitHub token for auth
        """
        self.job_spec = job_spec
        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix="worker_"))
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
        self.repo_dir = self.work_dir / "repo"

        self._start_time = datetime.utcnow()
        self._logs: list[str] = []

    def log(self, message: str) -> None:
        """Add a log message."""
        timestamp = datetime.utcnow().isoformat()
        log_line = f"[{timestamp}] {message}"
        self._logs.append(log_line)
        logger.info(log_line)

    async def run(self) -> WorkerResult:
        """Execute the worker job.

        Returns:
            The result of the job execution
        """
        self.log(f"Starting worker job {self.job_spec.job_id}")
        self.log(f"Goal: {self.job_spec.goal}")
        self.log(f"Repo: {self.job_spec.repo.url}@{self.job_spec.repo.ref}")

        try:
            # Clone repository
            await self._clone_repo()

            # Run bootstrap commands
            await self._run_bootstrap()

            # Execute the work
            result = await self._execute_work()

            # Create PR if requested
            if self.job_spec.constraints.produce_pr:
                result = await self._create_pr(result)

            duration = (datetime.utcnow() - self._start_time).total_seconds()
            result.duration_seconds = duration

            self.log(f"Job completed with status: {result.status}")
            return result

        except Exception as e:
            logger.error(f"Worker job failed: {e}")
            duration = (datetime.utcnow() - self._start_time).total_seconds()
            return WorkerResult(
                job_id=self.job_spec.job_id,
                status=WorkerJobStatus.FAILED,
                error=str(e),
                summary=f"Job failed: {e}",
                duration_seconds=duration,
            )

    async def _clone_repo(self) -> None:
        """Clone the repository."""
        self.log("Cloning repository...")

        # Build clone URL with auth
        clone_url = self.job_spec.repo.url
        if self.github_token and "github.com" in clone_url:
            # Insert token for auth
            clone_url = clone_url.replace(
                "https://github.com",
                f"https://x-access-token:{self.github_token}@github.com",
            )

        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                self.job_spec.repo.ref,
                clone_url,
                str(self.repo_dir),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Git clone failed: {result.stderr}")

        self.log(f"Repository cloned to {self.repo_dir}")

    async def _run_bootstrap(self) -> None:
        """Run bootstrap commands."""
        if not self.job_spec.runtime_profile.bootstrap:
            self.log("No bootstrap commands configured")
            return

        self.log("Running bootstrap commands...")

        for cmd in self.job_spec.runtime_profile.bootstrap:
            self.log(f"$ {cmd}")

            result = subprocess.run(
                cmd,
                shell=True,
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=self.job_spec.constraints.timeout_seconds,
                env={
                    **os.environ,
                    **self.job_spec.runtime_profile.env,
                },
            )

            if result.stdout:
                for line in result.stdout.split("\n")[:20]:  # Limit output
                    self.log(f"  {line}")

            if result.returncode != 0:
                self.log(f"Command failed with code {result.returncode}")
                if result.stderr:
                    for line in result.stderr.split("\n")[:10]:
                        self.log(f"  ERROR: {line}")
                raise RuntimeError(f"Bootstrap command failed: {cmd}")

        self.log("Bootstrap complete")

    async def _execute_work(self) -> WorkerResult:
        """Execute the main work.

        Uses the WorkerAgent if available, otherwise runs as a command executor.
        """
        from workers.agent import WorkerAgent

        self.log("Executing work...")

        # Use the worker agent for intelligent task execution
        agent = WorkerAgent(
            job_spec=self.job_spec,
            repo_dir=self.repo_dir,
            github_token=self.github_token,
        )

        result = await agent.execute()

        return result

    async def _create_pr(self, result: WorkerResult) -> WorkerResult:
        """Create a pull request with the changes.

        Args:
            result: Current result (may have branch info)

        Returns:
            Updated result with PR URL
        """
        if not result.branch_name:
            self.log("No branch created, skipping PR creation")
            return result

        self.log(f"Creating PR from branch {result.branch_name}...")

        try:
            # Push the branch
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", result.branch_name],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if push_result.returncode != 0:
                self.log(f"Push failed: {push_result.stderr}")
                return result

            # Create PR using gh CLI if available
            pr_result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    f"[Agent] {self.job_spec.goal[:60]}",
                    "--body",
                    f"## Summary\n\n{result.summary}\n\n---\n*Created by Agent Home worker*",
                    "--head",
                    result.branch_name,
                ],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=60,
                env={
                    **os.environ,
                    "GH_TOKEN": self.github_token,
                },
            )

            if pr_result.returncode == 0:
                pr_url = pr_result.stdout.strip()
                self.log(f"PR created: {pr_url}")
                result.pr_url = pr_url
            else:
                self.log(f"PR creation failed: {pr_result.stderr}")

        except Exception as e:
            self.log(f"Error creating PR: {e}")

        return result

    def get_logs(self) -> str:
        """Get all logs as a string."""
        return "\n".join(self._logs)
