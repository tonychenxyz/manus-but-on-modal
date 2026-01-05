"""Worker sandbox spawning and execution.

Handles spawning ephemeral Modal sandboxes for worker jobs.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

import modal

from modal_app.app import VOLUME_MOUNT_PATH, agent_home_volume, app, worker_image

logger = logging.getLogger(__name__)


@app.function(
    image=worker_image,
    volumes={VOLUME_MOUNT_PATH: agent_home_volume},
    secrets=[modal.Secret.from_name("agent-home-secrets")],
    timeout=60 * 60,  # 1 hour default timeout
    cpu=2.0,
    memory=4096,
)
async def spawn_worker(job_spec_dict: dict[str, Any]) -> str:
    """Spawn a worker to execute a job.

    Args:
        job_spec_dict: JobSpec as a dictionary

    Returns:
        Sandbox ID for tracking
    """
    from shared.models import JobSpec, WorkerJobStatus
    from workers.runner import WorkerRunner

    # Parse job spec
    spec = JobSpec.model_validate(job_spec_dict)
    sandbox_id = f"sandbox_{uuid.uuid4().hex[:12]}"

    logger.info(f"Worker {sandbox_id} starting job {spec.job_id}")

    # Get GitHub token from environment
    github_token = os.environ.get("GITHUB_TOKEN", "")

    # Create work directory
    work_dir = Path(f"/tmp/worker_{spec.job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)

    # Run the worker
    runner = WorkerRunner(
        job_spec=spec,
        work_dir=work_dir,
        github_token=github_token,
    )

    result = await runner.run()

    # Write result back to Agent Home via the volume
    result_path = Path(VOLUME_MOUNT_PATH) / "runs" / spec.job_id[:12]
    result_path.mkdir(parents=True, exist_ok=True)

    # Save result
    result_file = result_path / "result.json"
    result_file.write_text(result.model_dump_json(indent=2))

    # Save logs
    logs_file = result_path / "logs.txt"
    logs_file.write_text(runner.get_logs())

    # Commit volume to persist results
    agent_home_volume.commit()

    # Update the worker job in the database
    # This is done via a separate function call to Agent Home
    await _notify_job_complete(spec.job_id, result.model_dump())

    logger.info(f"Worker {sandbox_id} completed job {spec.job_id}: {result.status}")

    return sandbox_id


async def _notify_job_complete(job_id: str, result_dict: dict[str, Any]) -> None:
    """Notify Agent Home that a job has completed.

    Args:
        job_id: The job ID
        result_dict: WorkerResult as dictionary
    """
    from datetime import datetime

    from aiosqlite import connect

    from shared.models import WorkerJobStatus, WorkerResult

    # Parse result
    result = WorkerResult.model_validate(result_dict)

    # Update the database directly (we have volume access)
    db_path = Path(VOLUME_MOUNT_PATH) / "db" / "state.sqlite"

    if not db_path.exists():
        logger.warning("Database not found, cannot update job status")
        return

    try:
        async with connect(str(db_path)) as db:
            await db.execute(
                """
                UPDATE worker_jobs
                SET status = ?, result = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (
                    result.status.value,
                    result.model_dump_json(),
                    datetime.utcnow().isoformat(),
                    job_id,
                ),
            )
            await db.commit()

        logger.info(f"Updated job {job_id} status to {result.status}")
    except Exception as e:
        logger.error(f"Failed to update job {job_id}: {e}")


# Helper function to spawn worker with different resource profiles
@app.function(
    image=worker_image,
    volumes={VOLUME_MOUNT_PATH: agent_home_volume},
    secrets=[modal.Secret.from_name("agent-home-secrets")],
    timeout=60 * 60 * 4,  # 4 hour timeout for heavy jobs
    cpu=4.0,
    memory=8192,
    gpu="any",  # Optional GPU for ML workloads
)
async def spawn_heavy_worker(job_spec_dict: dict[str, Any]) -> str:
    """Spawn a heavy worker with more resources.

    Used for intensive tasks like:
    - Large builds
    - Test suites
    - ML training

    Args:
        job_spec_dict: JobSpec as a dictionary

    Returns:
        Sandbox ID for tracking
    """
    # Same implementation as spawn_worker but with more resources
    return await spawn_worker.local(job_spec_dict)
